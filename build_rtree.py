#!/usr/bin/env python3
"""
Build a packed Hilbert R-tree from a length-prefixed District protobuf stream
(produced by extract_districts.py) into a single .drt file.

Strategy:
  1. Stream the input once. For each record:
       - record (file_pos, length) of where its blob will live in the output.
       - capture its bbox + Hilbert key from the centroid.
  2. Sort all features by Hilbert key (good 2D spatial locality on a linear axis).
  3. Stream a SECOND pass over the input, writing the feature blobs to the
     output in *Hilbert order*. Track the new (offset, size) of each blob.
  4. Pack leaves bottom-up: chunk sorted features into groups of FANOUT,
     compute parent MBRs, repeat until one root.
  5. Write tree pages: leaves first, then level 1, ..., root last. Parent
     entries reference already-written child pages by absolute offset.
  6. Rewrite header in place with final root_offset / counts.
"""
from __future__ import annotations

import argparse
import math
import sys
import tempfile
import time
from pathlib import Path

from google.protobuf.internal.encoder import _VarintBytes

sys.path.insert(0, str(Path(__file__).parent))
from proto import district_pb2  # noqa: E402
from rtree_format import (  # noqa: E402
    DEFAULT_PAGE_SIZE, ENTRY_STRUCT, PAGE_HEADER_STRUCT,
    entries_per_page, pack_header,
)

HILBERT_ORDER = 16  # 2^16 cells per axis -> good resolution for global coords
COPY_BUF_SIZE = 1 << 20  # 1 MiB chunks when copying feature blobs


# --- Hilbert curve --------------------------------------------------------
# Standard xy2d from https://en.wikipedia.org/wiki/Hilbert_curve

def _hilbert_xy2d(n: int, x: int, y: int) -> int:
    d = 0
    s = n // 2
    while s > 0:
        rx = 1 if (x & s) > 0 else 0
        ry = 1 if (y & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        s //= 2
    return d


def hilbert_key(lon: float, lat: float) -> int:
    n = 1 << HILBERT_ORDER
    x = max(0, min(n - 1, int((lon + 180.0) / 360.0 * n)))
    y = max(0, min(n - 1, int((lat + 90.0) / 180.0 * n)))
    return _hilbert_xy2d(n, x, y)


# --- Streaming varint --------------------------------------------------

def _read_varint(f) -> int | None:
    """Read one protobuf varint from a binary file. Returns None at EOF."""
    shift = 0
    result = 0
    while True:
        b = f.read(1)
        if not b:
            if shift == 0:
                return None
            raise EOFError("truncated varint")
        byte = b[0]
        result |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            return result
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _varint_size(n: int) -> int:
    s = 1
    while n >= 0x80:
        n >>= 7
        s += 1
    return s




# --- Pass 1: scan input -> per-feature (input_offset, length, bbox, hilbert)

def scan_input(in_path: Path):
    """Yield (input_offset, blob_length, bbox4f, hilbert_key) per record.

    Streams: never holds more than one record's payload in memory.
    """
    msg = district_pb2.District()
    with open(in_path, "rb") as f:
        while True:
            record_start = f.tell()
            length = _read_varint(f)
            if length is None:
                return
            payload = f.read(length)
            if len(payload) != length:
                raise EOFError("truncated record")
            msg.Clear()
            msg.ParseFromString(payload)
            bbox = (msg.bbox[0], msg.bbox[1], msg.bbox[2], msg.bbox[3])
            cx = (bbox[0] + bbox[2]) * 0.5
            cy = (bbox[1] + bbox[3]) * 0.5
            h = hilbert_key(cx, cy)
            blob_total = _varint_size(length) + length
            yield record_start, blob_total, bbox, h


def strip_name_langs_to_temp(src: Path, tmp_dir: Path) -> Path:
    """Stream src.dpb → tmp_dir/slim.dpb, dropping every `name:<lang>` tag from
    each record. Used to trim translations out of the .drt (they live in the
    per-locale .dn overlays). Returns the path of the slim dpb."""
    out_path = tmp_dir / "slim.dpb"
    msg = district_pb2.District()
    seen = stripped = 0
    with open(src, "rb") as f, open(out_path, "wb") as g:
        while True:
            length = _read_varint(f)
            if length is None:
                break
            payload = f.read(length)
            if len(payload) != length:
                raise EOFError("truncated record")
            msg.Clear()
            msg.ParseFromString(payload)
            seen += 1
            # Drop name:<lang> tags — overlays carry them.
            to_drop = [k for k in msg.tags if k.startswith("name:")]
            for k in to_drop:
                del msg.tags[k]
            stripped += len(to_drop)
            new_payload = msg.SerializeToString()
            g.write(_VarintBytes(len(new_payload)))
            g.write(new_payload)
    src_size = src.stat().st_size
    out_size = out_path.stat().st_size
    print(f"  stripped {stripped:,} name:* tags across {seen:,} records "
          f"({src_size/1e6:.0f} MB → {out_size/1e6:.0f} MB)", file=sys.stderr)
    return out_path


# --- Pass 2: write features in Hilbert order ------------------------------

def write_features_in_order(in_path: Path, out_f,
                            sorted_records, features_offset: int):
    """Copy the input blobs to out_f in `sorted_records` order, streaming.

    Reads via seek+read on the input — random access into the on-disk file
    rather than slurping it. Returns (out_offset, blob_length, bbox) per record
    in the same order, written directly to out_f as we go.
    """
    out_records = []
    cursor = features_offset
    with open(in_path, "rb") as in_f:
        for in_off, blob_len, bbox, _ in sorted_records:
            in_f.seek(in_off)
            remaining = blob_len
            while remaining:
                chunk = in_f.read(min(COPY_BUF_SIZE, remaining))
                if not chunk:
                    raise EOFError(f"unexpected EOF at {in_off}")
                out_f.write(chunk)
                remaining -= len(chunk)
            out_records.append((cursor, blob_len, bbox))
            cursor += blob_len
    return out_records


# --- Pack pages bottom-up -------------------------------------------------

def _bbox_union(entries):
    minx = min(e[2][0] for e in entries)
    miny = min(e[2][1] for e in entries)
    maxx = max(e[2][2] for e in entries)
    maxy = max(e[2][3] for e in entries)
    return (minx, miny, maxx, maxy)


def _serialize_page(is_leaf: bool, entries, page_size: int) -> bytes:
    """entries: list of (child_offset, child_size, bbox) of length <= fanout."""
    buf = bytearray(page_size)
    PAGE_HEADER_STRUCT.pack_into(buf, 0, 1 if is_leaf else 0, 0,
                                 len(entries), 0)
    pos = PAGE_HEADER_STRUCT.size
    for child_offset, child_size, bbox in entries:
        ENTRY_STRUCT.pack_into(buf, pos,
                               bbox[0], bbox[1], bbox[2], bbox[3],
                               child_offset, child_size, 0)
        pos += ENTRY_STRUCT.size
    return bytes(buf)


def build_and_write_tree(out_f, leaf_records, page_size: int, tree_offset: int):
    """leaf_records: list of (out_offset, blob_length, bbox) sorted by Hilbert.

    Returns (root_offset, num_nodes).
    """
    fanout = entries_per_page(page_size)
    if fanout < 2:
        raise ValueError(f"page_size {page_size} too small")

    # Build leaf level: chunks of `fanout` records become one leaf page each.
    current_level = []  # list of (child_offset, child_size, bbox)
    cursor = tree_offset
    pages_written = 0

    # Stream leaves out as we build them.
    pending_leaves = leaf_records
    leaf_pages = []  # (page_offset, bbox)
    for i in range(0, len(pending_leaves), fanout):
        chunk = pending_leaves[i:i + fanout]
        # Each leaf entry points to a feature blob.
        entries = [(off, size, bbox) for (off, size, bbox) in chunk]
        page = _serialize_page(is_leaf=True, entries=entries, page_size=page_size)
        out_f.write(page)
        leaf_pages.append((cursor, _bbox_union(entries)))
        cursor += page_size
        pages_written += 1

    current_level = [(off, page_size, bbox) for (off, bbox) in leaf_pages]

    # Build internal levels.
    while len(current_level) > 1:
        next_level = []
        for i in range(0, len(current_level), fanout):
            chunk = current_level[i:i + fanout]
            page = _serialize_page(is_leaf=False, entries=chunk,
                                   page_size=page_size)
            out_f.write(page)
            next_level.append((cursor, page_size, _bbox_union(chunk)))
            cursor += page_size
            pages_written += 1
        current_level = next_level

    root_offset = current_level[0][0]
    return root_offset, pages_written


# --- Driver ---------------------------------------------------------------

def build(in_path: Path, out_path: Path, page_size: int = DEFAULT_PAGE_SIZE,
          strip_name_langs: bool = True):
    """If strip_name_langs is True (default), every `name:<lang>` tag is
    removed from each record before it lands in the .drt — translations live
    in the per-locale .dn overlays anyway, and stripping them shaves
    significant size."""
    workdir = tempfile.TemporaryDirectory(prefix="drt-")
    if strip_name_langs:
        print(f"[0/3] stripping name:* tags from {in_path}", file=sys.stderr)
        t0s = time.time()
        in_path = strip_name_langs_to_temp(in_path, Path(workdir.name))
        print(f"  done in {time.time() - t0s:.1f}s", file=sys.stderr)

    t0 = time.time()
    print(f"[1/3] scanning {in_path}", file=sys.stderr)
    records = list(scan_input(in_path))
    print(f"  {len(records)} records in {time.time() - t0:.1f}s", file=sys.stderr)

    print("[2/3] sorting by Hilbert key", file=sys.stderr)
    t1 = time.time()
    records.sort(key=lambda r: r[3])
    print(f"  sorted in {time.time() - t1:.1f}s", file=sys.stderr)

    print(f"[3/3] writing {out_path}", file=sys.stderr)
    t2 = time.time()
    with open(out_path, "wb") as out_f:
        # 1 page reserved for the header.
        out_f.write(b"\x00" * page_size)
        features_offset = page_size

        # Stream features in Hilbert order; capture their final (offset, size, bbox).
        leaf_records = write_features_in_order(in_path, out_f, records,
                                               features_offset)
        features_size = out_f.tell() - features_offset

        # Pad to next page boundary so tree pages are aligned.
        pad = (-out_f.tell()) % page_size
        if pad:
            out_f.write(b"\x00" * pad)
        tree_offset = out_f.tell()

        root_offset, num_nodes = build_and_write_tree(
            out_f, leaf_records, page_size, tree_offset)

        # Patch header.
        header = pack_header(
            page_size=page_size,
            num_features=len(records),
            num_nodes=num_nodes,
            root_offset=root_offset,
            features_offset=features_offset,
            features_size=features_size,
        )
        out_f.seek(0)
        out_f.write(header)

    final_size = out_path.stat().st_size
    fanout = entries_per_page(page_size)
    depth = max(1, math.ceil(math.log(max(len(records), 1), fanout)))
    print(f"  wrote {final_size:,} bytes in {time.time() - t2:.1f}s", file=sys.stderr)
    print(f"  features={len(records)} pages={num_nodes} fanout={fanout} "
          f"~depth={depth}", file=sys.stderr)
    print(f"  root_offset={root_offset:,} features=[{features_offset:,}, "
          f"{features_offset + features_size:,})", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path,
                    help="District protobuf stream (.dpb)")
    ap.add_argument("output", type=Path, help="Packed R-tree (.drt)")
    ap.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE,
                    help="Page size in bytes (default 4096)")
    ap.add_argument("--keep-name-langs", action="store_true",
                    help="Keep name:<lang> tags in the .drt (default: strip; "
                         "translations live in the per-locale .dn overlays)")
    args = ap.parse_args()
    build(args.input, args.output, args.page_size,
          strip_name_langs=not args.keep_name_langs)


if __name__ == "__main__":
    main()
