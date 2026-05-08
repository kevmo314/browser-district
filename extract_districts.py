#!/usr/bin/env python3
"""
Extract district-level features from an OSM PBF.

Two-pass:
  1. osmium tags-filter (subprocess) -> small filtered.pbf with only candidate
     relations + their dependencies. Much faster than scanning the full PBF in
     Python.
  2. pyosmium with area assembly -> walks filtered.pbf, builds multipolygons,
     emits a length-prefixed stream of District protobufs.

District definition (override with --tag-spec):
  boundary=administrative AND admin_level >= MIN_ADMIN_LEVEL (default 8)
  OR place IN {neighbourhood, suburb, borough, quarter, city_district, district}
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import osmium
import shapely.geometry
import shapely.wkb
from google.protobuf.internal.encoder import _VarintBytes

sys.path.insert(0, str(Path(__file__).parent))
from proto import district_pb2  # noqa: E402
from qdv import encode_geometry  # noqa: E402

PLACE_DISTRICT_VALUES = {
    "neighbourhood",
    "suburb",
    "borough",
    "quarter",
    "city_district",
    "district",
}
PARK_LEISURE_VALUES = {
    "park",
    "nature_reserve",
}
PROTECTED_BOUNDARY_VALUES = {
    "protected_area",
    "national_park",
}
MIN_ADMIN_LEVEL = 8

# Areas (relations / closed ways) carry polygon features. Place nodes carry
# many neighborhoods that have no polygon in OSM (e.g. Williamsburg).
OSMIUM_AREA_FILTERS = [
    "a/boundary=administrative",
    f"a/boundary={','.join(sorted(PROTECTED_BOUNDARY_VALUES))}",
    f"a/leisure={','.join(sorted(PARK_LEISURE_VALUES))}",
    f"a/place={','.join(sorted(PLACE_DISTRICT_VALUES))}",
]
OSMIUM_NODE_FILTERS = [
    f"n/place={','.join(sorted(PLACE_DISTRICT_VALUES))}",
]


def is_district(tags) -> bool:
    # Quality bar: only keep features that have a name. Tons of unnamed park
    # polygons exist in OSM (random green patches in cities) — we don't want
    # those cluttering the index.
    if not tags.get("name"):
        return False
    if tags.get("boundary") == "administrative":
        try:
            return int(tags.get("admin_level", "0")) >= MIN_ADMIN_LEVEL
        except ValueError:
            return False
    if tags.get("boundary") in PROTECTED_BOUNDARY_VALUES:
        return True
    if tags.get("leisure") in PARK_LEISURE_VALUES:
        return True
    if tags.get("place") in PLACE_DISTRICT_VALUES:
        return True
    return False


def tag_value_or_empty(tags, key: str) -> str:
    try:
        return tags[key]
    except KeyError:
        return ""


def split_antimeridian_bboxes(geom):
    """Return one or two (minx, miny, maxx, maxy) bboxes for `geom`.

    Polygons that cross the antimeridian get a degenerate naive bbox spanning
    [-180, 180] in longitude. For example, the Papahānaumokuākea Marine
    National Monument (Hawaii) has vertices on both sides of the dateline; its
    raw bounds are roughly [-180, 19, 180, 32], which falsely matches points
    in Asia/Africa/Europe. We detect this case (naive width > 180°) and
    re-compute in shifted [0, 360] longitude space; if the polygon really
    crosses the dateline we emit two bboxes — one east half, one west half —
    sharing the same osm_id. Truly globe-spanning features (Antarctica) keep
    their naive bbox.
    """
    minx, miny, maxx, maxy = geom.bounds
    if maxx - minx <= 180:
        return [(minx, miny, maxx, maxy)]
    if geom.geom_type == "MultiPolygon":
        rings = [p.exterior.coords for p in geom.geoms]
    elif geom.geom_type == "Polygon":
        rings = [geom.exterior.coords]
    else:
        return [(minx, miny, maxx, maxy)]
    xs_shifted = [
        (x + 360 if x < 0 else x)
        for ring in rings for (x, _) in ring
    ]
    sh_min, sh_max = min(xs_shifted), max(xs_shifted)
    if sh_max - sh_min > 180:
        return [(minx, miny, maxx, maxy)]  # really globe-spanning
    if sh_max <= 180:
        return [(sh_min, miny, sh_max, maxy)]
    if sh_min >= 180:
        return [(sh_min - 360, miny, sh_max - 360, maxy)]
    # Genuine dateline crossing — split into two halves.
    return [
        (sh_min, miny, 180.0, maxy),         # east half
        (-180.0, miny, sh_max - 360, maxy),  # west half
    ]


class DistrictHandler(osmium.SimpleHandler):
    # Fixed tags we always carry. `name:*` tags are captured dynamically below
    # so we get every locale OSM has for the feature, not just one.
    INTERESTING_TAGS = (
        "name",
        "boundary",
        "admin_level",
        "place",
        "leisure",
        "protect_class",
        "wikidata",
        "wikipedia",
        "ref",
        "border_type",
        "type",
    )

    def __init__(self, out_stream, store_geometry: bool,
                 node_buffer_deg: float = 0.0):
        super().__init__()
        self.wkbfab = osmium.geom.WKBFactory()
        self.out = out_stream
        self.store_geometry = store_geometry
        self.node_buffer_deg = node_buffer_deg
        self.count = 0
        self.node_count = 0
        self.skipped_invalid = 0

    def _emit(self, msg):
        payload = msg.SerializeToString()
        self.out.write(_VarintBytes(len(payload)))
        self.out.write(payload)
        self.count += 1
        if self.count % 1000 == 0:
            print(f"  emitted {self.count} districts", file=sys.stderr)

    def _copy_tags(self, src_tags, dst_tags):
        # Fixed tags
        for k in self.INTERESTING_TAGS:
            v = tag_value_or_empty(src_tags, k)
            if v:
                dst_tags[k] = v
        # Every name:<lang> tag (en, ja, zh-Hant, …) so name overlays
        # for any locale can be built from this dpb without re-extracting.
        for tag in src_tags:
            if tag.k.startswith("name:") and tag.v:
                dst_tags[tag.k] = tag.v

    def node(self, n):
        if n.tags.get("place") not in PLACE_DISTRICT_VALUES:
            return
        if not n.tags.get("name"):
            return
        if not n.location.valid():
            return
        lon, lat = n.location.lon, n.location.lat
        buf = self.node_buffer_deg
        msg = district_pb2.District()
        msg.osm_id = n.id
        msg.osm_type = 0
        msg.name = tag_value_or_empty(n.tags, "name")
        msg.bbox.extend([lon - buf, lat - buf, lon + buf, lat + buf])
        self._copy_tags(n.tags, msg.tags)
        # Place nodes are points — no polygon to encode. Skip geometry.
        self.node_count += 1
        self._emit(msg)

    def area(self, a):
        if not is_district(a.tags):
            return
        try:
            wkb_hex = self.wkbfab.create_multipolygon(a)
        except Exception:
            self.skipped_invalid += 1
            return
        try:
            geom = shapely.wkb.loads(bytes.fromhex(wkb_hex))
        except Exception:
            self.skipped_invalid += 1
            return
        if geom.is_empty:
            self.skipped_invalid += 1
            return
        bboxes = split_antimeridian_bboxes(geom)

        # Encode geometry once (~10× smaller than WKB) and reuse across the
        # antimeridian-split records.
        qdv_bytes = encode_geometry(geom) if self.store_geometry else b""
        for minx, miny, maxx, maxy in bboxes:
            msg = district_pb2.District()
            # orig_id() returns the source relation/way id (areas have a
            # synthetic id).
            msg.osm_id = a.orig_id()
            # from_way() True => synthesized from a closed way; False => from
            # a relation.
            msg.osm_type = 1 if a.from_way() else 2
            msg.name = tag_value_or_empty(a.tags, "name")
            msg.bbox.extend([minx, miny, maxx, maxy])
            self._copy_tags(a.tags, msg.tags)
            if qdv_bytes:
                msg.geometry_qdv = qdv_bytes
            self._emit(msg)


def run_tags_filter(input_pbf: Path, filtered_pbf: Path) -> None:
    if not shutil.which("osmium"):
        raise SystemExit("osmium-tool (CLI) not found on PATH")
    cmd = [
        "osmium",
        "tags-filter",
        "--overwrite",
        "--progress",
        "-o",
        str(filtered_pbf),
        str(input_pbf),
        *OSMIUM_AREA_FILTERS,
        *OSMIUM_NODE_FILTERS,
    ]
    print(f"[1/2] tags-filter: {' '.join(cmd)}", file=sys.stderr)
    t0 = time.time()
    subprocess.run(cmd, check=True)
    size_mb = filtered_pbf.stat().st_size / (1024 * 1024)
    print(f"  filtered.pbf = {size_mb:.1f} MB in {time.time() - t0:.1f}s",
          file=sys.stderr)


def assemble_areas(filtered_pbf: Path, out_path: Path,
                   store_geometry: bool,
                   node_buffer_deg: float) -> tuple[int, int, int]:
    print(f"[2/2] assembling areas from {filtered_pbf}", file=sys.stderr)
    t0 = time.time()
    with open(out_path, "wb") as f:
        handler = DistrictHandler(f, store_geometry=store_geometry,
                                  node_buffer_deg=node_buffer_deg)
        # locations=True triggers the location index + area assembly two-pass.
        # `sparse_file_array` keeps node locations on disk; fine for filtered
        # extracts up to a few GB.
        idx = "sparse_file_array,nodes-cache.tmp"
        try:
            handler.apply_file(str(filtered_pbf), locations=True, idx=idx)
        finally:
            for leftover in Path(".").glob("nodes-cache.tmp*"):
                leftover.unlink(missing_ok=True)
    area_count = handler.count - handler.node_count
    print(f"  emitted {handler.count} districts "
          f"({area_count} polygons + {handler.node_count} place-nodes, "
          f"skipped {handler.skipped_invalid} invalid) in "
          f"{time.time() - t0:.1f}s", file=sys.stderr)
    return handler.count, handler.node_count, handler.skipped_invalid


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input_pbf", type=Path)
    ap.add_argument("output", type=Path,
                    help="Length-prefixed District protobuf stream (.dpb)")
    ap.add_argument("--keep-filtered", type=Path, default=None,
                    help="Keep the intermediate filtered.pbf at this path")
    ap.add_argument("--skip-filter", action="store_true",
                    help="Treat input_pbf as an already-filtered file (skip "
                         "the slow tags-filter pass and run only assembly)")
    ap.add_argument("--no-geometry", action="store_true",
                    help="Skip storing polygon WKB (smaller index, no PIP)")
    ap.add_argument("--node-buffer-deg", type=float, default=0.005,
                    help="Half-extent (degrees) used as a synthetic bbox "
                         "around place=neighbourhood/quarter/... nodes that "
                         "have no polygon in OSM. ~0.005 ≈ 500m. Use 0 to "
                         "store as zero-area points.")
    args = ap.parse_args()

    if not args.input_pbf.exists():
        raise SystemExit(f"input not found: {args.input_pbf}")

    if args.skip_filter:
        assemble_areas(args.input_pbf, args.output,
                       store_geometry=not args.no_geometry,
                       node_buffer_deg=args.node_buffer_deg)
    elif args.keep_filtered:
        filtered_path = args.keep_filtered
        filtered_path.parent.mkdir(parents=True, exist_ok=True)
        run_tags_filter(args.input_pbf, filtered_path)
        assemble_areas(filtered_path, args.output,
                       store_geometry=not args.no_geometry,
                       node_buffer_deg=args.node_buffer_deg)
    else:
        with tempfile.TemporaryDirectory(prefix="district-") as td:
            filtered_path = Path(td) / "filtered.osm.pbf"
            run_tags_filter(args.input_pbf, filtered_path)
            assemble_areas(filtered_path, args.output,
                           store_geometry=not args.no_geometry,
                           node_buffer_deg=args.node_buffer_deg)

    out_size = args.output.stat().st_size
    print(f"wrote {args.output} ({out_size / (1024 * 1024):.1f} MB)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
