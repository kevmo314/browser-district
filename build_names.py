#!/usr/bin/env python3
"""
Build a per-locale name overlay (.dn) from the District protobuf stream.

For each feature in the dpb, we look up tag `name:<lang>`. If it exists and
differs from the local `name`, we add (osm_id, translated_name) to the overlay.
The client falls back to the local `name` (already in the .drt) when no
overlay entry exists for an osm_id.

The overlay file is a packed B+tree on osm_id, addressable by HTTP Range —
same shape as the R-tree but 1-D.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from proto import district_pb2  # noqa: E402
from names_format import (  # noqa: E402
    DEFAULT_PAGE_SIZE, INTERNAL_ENTRY_STRUCT, LEAF_ENTRY_STRUCT,
    PAGE_HEADER_STRUCT, internal_entries_per_page, leaf_entries_per_page,
    pack_header,
)

# Skip non-language `name:*` tags (etymology, pronunciation, historical years).
# A real BCP-47 / OSM language tag matches this loose pattern: lowercase
# letters, optional script/region subtags. Reject anything containing other
# punctuation or digits, plus a small denylist of known non-language keys.
_LANG_TAG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_NON_LANG = {"etymology", "pronunciation", "see", "left", "right", "full",
             "short", "official", "alt", "old", "loc"}


def is_real_language(lang: str) -> bool:
    if lang in _NON_LANG:
        return False
    return bool(_LANG_TAG_RE.match(lang))


def _read_varint(f) -> int | None:
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


def collect_overlay(in_path: Path, lang: str):
    """Yield (osm_id, name_bytes) for each feature whose name:<lang> exists
    and differs from the local `name`."""
    if not is_real_language(lang):
        raise SystemExit(f"refusing suspicious language tag: {lang!r}")
    key = f"name:{lang}"
    msg = district_pb2.District()
    with open(in_path, "rb") as f:
        while True:
            length = _read_varint(f)
            if length is None:
                return
            payload = f.read(length)
            if len(payload) != length:
                raise EOFError("truncated record")
            msg.Clear()
            msg.ParseFromString(payload)
            translated = msg.tags.get(key)
            if not translated or translated == msg.name:
                continue
            yield msg.osm_id, translated.encode("utf-8")


def collect_all_overlays(in_path: Path) -> dict[str, list]:
    """Single-pass scan: returns {lang: [(osm_id, name_bytes), ...]} for every
    real language tag found in the dpb whose value differs from the local
    `name`."""
    per_locale: dict[str, list] = {}
    msg = district_pb2.District()
    seen_features = 0
    with open(in_path, "rb") as f:
        while True:
            length = _read_varint(f)
            if length is None:
                break
            payload = f.read(length)
            if len(payload) != length:
                raise EOFError("truncated record")
            msg.Clear()
            msg.ParseFromString(payload)
            seen_features += 1
            local = msg.name
            for k, v in msg.tags.items():
                if not k.startswith("name:") or not v:
                    continue
                lang = k[5:]
                if not is_real_language(lang):
                    continue
                if v == local:
                    continue
                per_locale.setdefault(lang, []).append(
                    (msg.osm_id, v.encode("utf-8"))
                )
            if seen_features % 100000 == 0:
                print(f"  scanned {seen_features:,} features, "
                      f"{len(per_locale)} locales so far",
                      file=sys.stderr)
    return per_locale


def _serialize_internal(entries, page_size: int) -> bytes:
    """entries: list of (max_osm_id, child_offset)"""
    buf = bytearray(page_size)
    PAGE_HEADER_STRUCT.pack_into(buf, 0, 0, 0, len(entries), 0)
    pos = PAGE_HEADER_STRUCT.size
    for max_id, child_off in entries:
        INTERNAL_ENTRY_STRUCT.pack_into(buf, pos, max_id, child_off)
        pos += INTERNAL_ENTRY_STRUCT.size
    return bytes(buf)


def _serialize_leaf(entries, page_size: int) -> bytes:
    """entries: list of (osm_id, name_offset, name_size)"""
    buf = bytearray(page_size)
    PAGE_HEADER_STRUCT.pack_into(buf, 0, 1, 0, len(entries), 0)
    pos = PAGE_HEADER_STRUCT.size
    for osm_id, off, size in entries:
        LEAF_ENTRY_STRUCT.pack_into(buf, pos, osm_id, off, size, 0)
        pos += LEAF_ENTRY_STRUCT.size
    return bytes(buf)


def write_overlay(out_path: Path, entries: list,
                  page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """Write one .dn from sorted (osm_id, name_bytes) entries. Returns stats."""
    leaf_fanout = leaf_entries_per_page(page_size)
    internal_fanout = internal_entries_per_page(page_size)
    with open(out_path, "wb") as out_f:
        out_f.write(b"\x00" * page_size)  # reserve header
        blobs_offset = page_size
        leaf_records = []  # (osm_id, blob_offset, blob_size)
        cursor = blobs_offset
        for osm_id, name_bytes in entries:
            out_f.write(name_bytes)
            leaf_records.append((osm_id, cursor, len(name_bytes)))
            cursor += len(name_bytes)
        blobs_size = out_f.tell() - blobs_offset
        pad = (-out_f.tell()) % page_size
        if pad:
            out_f.write(b"\x00" * pad)
        tree_offset = out_f.tell()

        # Leaf level.
        leaf_pages = []  # (max_osm_id, page_offset)
        for i in range(0, len(leaf_records), leaf_fanout):
            chunk = leaf_records[i:i + leaf_fanout]
            page = _serialize_leaf(chunk, page_size)
            out_f.write(page)
            leaf_pages.append((chunk[-1][0],
                               tree_offset + (i // leaf_fanout) * page_size))
        cursor = tree_offset + len(leaf_pages) * page_size
        current_level = leaf_pages
        pages_written = len(leaf_pages)
        # Internal levels.
        while len(current_level) > 1:
            next_level = []
            for i in range(0, len(current_level), internal_fanout):
                chunk = current_level[i:i + internal_fanout]
                page = _serialize_internal(chunk, page_size)
                out_f.write(page)
                next_level.append((chunk[-1][0], cursor))
                cursor += page_size
                pages_written += 1
            current_level = next_level
        root_offset = current_level[0][1]
        # Patch header.
        header = pack_header(
            page_size=page_size,
            num_features=len(entries),
            num_pages=pages_written,
            root_offset=root_offset,
            blobs_offset=blobs_offset,
            blobs_size=blobs_size,
        )
        out_f.seek(0)
        out_f.write(header)
    return {"entries": len(entries), "pages": pages_written,
            "size": out_path.stat().st_size}


def build(in_path: Path, out_path: Path, lang: str,
          page_size: int = DEFAULT_PAGE_SIZE):
    print(f"[1/3] collecting name:{lang} overlay from {in_path}", file=sys.stderr)
    t0 = time.time()
    entries = list(collect_overlay(in_path, lang))
    print(f"  {len(entries):,} translated entries in {time.time() - t0:.1f}s",
          file=sys.stderr)
    if not entries:
        raise SystemExit(f"no name:{lang} entries found in {in_path}")
    print("[2/3] sorting by osm_id", file=sys.stderr)
    t1 = time.time()
    entries.sort(key=lambda e: e[0])
    print(f"  sorted in {time.time() - t1:.1f}s", file=sys.stderr)
    print(f"[3/3] writing {out_path}", file=sys.stderr)
    t2 = time.time()
    stats = write_overlay(out_path, entries, page_size)
    print(f"  wrote {stats['size']:,} bytes in {time.time() - t2:.1f}s "
          f"(entries={stats['entries']} pages={stats['pages']})",
          file=sys.stderr)


def build_all(in_path: Path, out_dir: Path, min_entries: int,
              page_size: int = DEFAULT_PAGE_SIZE):
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[1/2] single-pass scan of {in_path}", file=sys.stderr)
    t0 = time.time()
    per_locale = collect_all_overlays(in_path)
    print(f"  {len(per_locale)} locales, "
          f"{sum(len(v) for v in per_locale.values()):,} total entries "
          f"in {time.time() - t0:.1f}s", file=sys.stderr)

    print(f"[2/2] sorting + writing per-locale overlays", file=sys.stderr)
    t1 = time.time()
    manifest = []
    skipped_small = 0
    for lang in sorted(per_locale):
        entries = per_locale[lang]
        if len(entries) < min_entries:
            skipped_small += 1
            continue
        entries.sort(key=lambda e: e[0])
        out_path = out_dir / f"planet-names-{lang}.dn"
        stats = write_overlay(out_path, entries, page_size)
        manifest.append({
            "lang": lang,
            "file": out_path.name,
            "entries": stats["entries"],
            "size": stats["size"],
        })
    print(f"  wrote {len(manifest)} overlays "
          f"(skipped {skipped_small} with <{min_entries} entries) "
          f"in {time.time() - t1:.1f}s", file=sys.stderr)

    # Manifest so the browser can populate the locale dropdown without doing
    # 658 HEAD requests.
    import json
    manifest_path = out_dir / "planet-names.manifest.json"
    manifest.sort(key=lambda m: -m["entries"])
    with open(manifest_path, "w") as f:
        json.dump({"locales": manifest}, f, indent=2, ensure_ascii=False)
    print(f"  wrote {manifest_path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path, help="District protobuf stream (.dpb)")
    ap.add_argument("output", type=Path,
                    help="Output .dn (single-locale mode) or output directory "
                         "(--all mode)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--lang",
                   help="Locale code (e.g. en, ja, es, zh-Hant)")
    g.add_argument("--all", action="store_true",
                   help="Build overlays for every name:<lang> tag in one scan")
    ap.add_argument("--min-entries", type=int, default=1,
                    help="--all only: skip locales with fewer entries (default 1)")
    ap.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    args = ap.parse_args()
    if args.all:
        build_all(args.input, args.output, args.min_entries, args.page_size)
    else:
        build(args.input, args.output, args.lang, args.page_size)


if __name__ == "__main__":
    main()
