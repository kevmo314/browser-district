#!/usr/bin/env python3
"""
Query a packed district R-tree by lat/lng.

Two backends:
  --file PATH     mmap-style local file (single-process)
  --url URL       HTTP(S) with Range requests; only fetches the pages it needs
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from typing import Iterable, Iterator

from google.protobuf.internal.decoder import _DecodeVarint

sys.path.insert(0, str(Path(__file__).parent))
from proto import district_pb2  # noqa: E402
from rtree_format import (  # noqa: E402
    ENTRY_STRUCT, HEADER_STRUCT, PAGE_HEADER_STRUCT, unpack_header,
)


# --- Backends -------------------------------------------------------------

class FileBackend:
    def __init__(self, path: Path):
        self.f = open(path, "rb")
        self.requests = 0
        self.bytes_read = 0

    def read_range(self, offset: int, length: int) -> bytes:
        self.requests += 1
        self.bytes_read += length
        self.f.seek(offset)
        return self.f.read(length)


class HttpBackend:
    def __init__(self, url: str):
        import urllib.request  # stdlib only
        self.url = url
        self._urlopen = urllib.request.urlopen
        self._Request = urllib.request.Request
        self.requests = 0
        self.bytes_read = 0

    def read_range(self, offset: int, length: int) -> bytes:
        self.requests += 1
        self.bytes_read += length
        req = self._Request(self.url, headers={
            "Range": f"bytes={offset}-{offset + length - 1}",
        })
        with self._urlopen(req) as resp:
            if resp.status not in (200, 206):
                raise RuntimeError(f"unexpected HTTP {resp.status}")
            return resp.read()


# --- Index reader ---------------------------------------------------------

class DistrictIndex:
    def __init__(self, backend):
        self.backend = backend
        # Need page_size to know how big the header page is. The header
        # is fixed-size (64 bytes) but is stored in the first page; one
        # range request grabs both the header and the tree page header
        # info we need. We read 64 bytes first.
        head = backend.read_range(0, HEADER_STRUCT.size)
        self.meta = unpack_header(head)
        self.page_size = self.meta["page_size"]

    def query_point(self, lon: float, lat: float) -> Iterator[district_pb2.District]:
        # Single-point query: find every leaf entry whose bbox contains (lon, lat),
        # then load that feature blob and (optionally) refine via WKB polygon.
        for blob_offset, blob_size in self._traverse(lon, lat):
            blob = self.backend.read_range(blob_offset, blob_size)
            length, hdr_end = _DecodeVarint(blob, 0)
            msg = district_pb2.District()
            msg.ParseFromString(blob[hdr_end:hdr_end + length])
            yield msg

    def _traverse(self, lon: float, lat: float) -> Iterator[tuple[int, int]]:
        stack = [self.meta["root_offset"]]
        while stack:
            page_off = stack.pop()
            page = self.backend.read_range(page_off, self.page_size)
            is_leaf, _, entry_count, _ = PAGE_HEADER_STRUCT.unpack_from(page, 0)
            base = PAGE_HEADER_STRUCT.size
            for i in range(entry_count):
                (minx, miny, maxx, maxy, child_off, child_size, _) = \
                    ENTRY_STRUCT.unpack_from(page, base + i * ENTRY_STRUCT.size)
                if lon < minx or lon > maxx or lat < miny or lat > maxy:
                    continue
                if is_leaf:
                    yield child_off, child_size
                else:
                    stack.append(child_off)


# --- CLI ------------------------------------------------------------------

def refine_with_wkb(msg: district_pb2.District, lon: float, lat: float) -> bool:
    """Return True if (lon, lat) is inside the stored polygon. Accepts when
    no geometry is present (place-nodes / index-only builds) — in those cases
    the bbox match is the only hit signal we have."""
    if msg.geometry_qdv:
        from qdv import point_in_qdv
        return point_in_qdv(msg.geometry_qdv, lon, lat)
    if msg.geometry_wkb:
        try:
            import shapely.wkb
            from shapely.geometry import Point
            geom = shapely.wkb.loads(msg.geometry_wkb)
            if geom.geom_type == "Point":
                return True
            return geom.intersects(Point(lon, lat))
        except Exception:
            return True
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="local .drt file")
    src.add_argument("--url", help="HTTP(S) URL serving the .drt with Range support")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--no-refine", action="store_true",
                    help="Skip point-in-polygon refinement (return raw bbox hits)")
    args = ap.parse_args()

    if args.file:
        backend = FileBackend(args.file)
    else:
        backend = HttpBackend(args.url)

    idx = DistrictIndex(backend)
    print(f"# index meta: {idx.meta}", file=sys.stderr)

    matches = []
    for d in idx.query_point(args.lon, args.lat):
        if args.no_refine or refine_with_wkb(d, args.lon, args.lat):
            matches.append(d)

    matches.sort(key=lambda m: (
        # Smaller bbox area first => more specific district near the top.
        (m.bbox[2] - m.bbox[0]) * (m.bbox[3] - m.bbox[1]),
    ))
    for m in matches:
        if m.tags.get("place"):
            kind = f"place={m.tags['place']}"
        elif m.tags.get("admin_level"):
            kind = f"admin_level={m.tags['admin_level']}"
        elif m.tags.get("leisure"):
            kind = f"leisure={m.tags['leisure']}"
        elif m.tags.get("boundary"):
            kind = f"boundary={m.tags['boundary']}"
        else:
            kind = ""
        bbox = [round(x, 4) for x in m.bbox]
        print(f"{m.name!r:<40} {kind:<24} bbox={bbox} osm_id={m.osm_id}")
    print(f"# {len(matches)} match(es); "
          f"{backend.requests} requests, {backend.bytes_read:,} bytes",
          file=sys.stderr)


if __name__ == "__main__":
    main()
