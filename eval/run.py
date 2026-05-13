#!/usr/bin/env python3
"""
For every test point in places.py, query both Mapbox reverse-geocode and the
browser-district .drt over HTTP Range and save the raw results to results.json.
Use compare.py afterwards to render the report.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from query import DistrictIndex, HttpBackend  # noqa: E402
from overlay import NamesOverlay  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from places import PLACES  # noqa: E402

MAPBOX_TOKEN = os.environ.get(
    "MAPBOX_TOKEN",
    # Public token from rtirl-obs/public/neighborhood.html (same one this is
    # meant to replace). Override with MAPBOX_TOKEN env var to use your own.
    "pk.eyJ1Ijoia2V2bW8zMTQiLCJhIjoiY2w0MW1qaTh3MG80dzNjcXRndmJ0a2JieiJ9."
    "Y_xABmAqvD-qZeed8MabxQ",
)
R2_BASE = os.environ.get(
    "R2_BASE", "https://pub-ba286604ef7044678dbc982b6ccb7fa4.r2.dev")
DRT_URL = os.environ.get("DRT_URL", f"{R2_BASE}/planet-districts.drt")
LANG = os.environ.get("LANG_CODE", "en")
OVERLAY_URL = os.environ.get(
    "OVERLAY_URL", f"{R2_BASE}/planet-names-{LANG}.dn")


def query_mapbox(lat: float, lng: float, lang: str = "en") -> dict:
    """Hit the same endpoint rtirl-obs/neighborhood.html uses."""
    qs = urllib.parse.urlencode({
        "access_token": MAPBOX_TOKEN,
        "language": lang,
    })
    url = (f"https://api.mapbox.com/geocoding/v5/mapbox.places/"
           f"{lng},{lat}.json?{qs}")
    req = urllib.request.Request(url, headers={"User-Agent": "browser-district-eval/0.1"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.load(resp)
    dt = time.time() - t0
    # Pull out the same fields the existing page uses.
    by_type: dict[str, dict] = {}
    for feat in body.get("features", []):
        for t in feat.get("place_type", []):
            by_type.setdefault(t, feat)
    extract = {}
    for k in ("country", "region", "postcode", "district", "place",
              "locality", "neighborhood", "address", "poi"):
        f = by_type.get(k)
        if f:
            extract[k] = {"text": f.get("text"), "place_name": f.get("place_name")}
    return {
        "extract": extract,
        "elapsed_ms": int(dt * 1000),
        "feature_count": len(body.get("features", [])),
    }


def _retry_429(fn):
    """Wrap an HTTP-backed call so 429s back off exponentially."""
    last_err = None
    for attempt in range(5):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if "429" not in str(e):
                raise
            time.sleep(0.5 * (2 ** attempt))
    raise last_err


def lookup_district(idx: DistrictIndex, ov: NamesOverlay | None,
                    lat: float, lng: float) -> dict:
    t0 = time.time()
    reqs0 = idx.backend.requests + (ov.backend.requests if ov else 0)
    bytes0 = idx.backend.bytes_read + (ov.backend.bytes_read if ov else 0)
    matches = _retry_429(lambda: list(idx.query_point(lng, lat)))
    translations = {}
    if ov and matches:
        translations = _retry_429(
            lambda: ov.lookup_many([m.osm_id for m in matches]))
    dt = time.time() - t0
    # Best "neighborhood" = smallest bbox among matches that aren't huge admins.
    # Best "place" / city context = a matching admin_level=8 if present.
    matches.sort(key=lambda m: (m.bbox[2] - m.bbox[0]) * (m.bbox[3] - m.bbox[1]))
    out = []
    for m in matches:
        out.append({
            "name": m.name,
            "translated_name": translations.get(m.osm_id),
            "osm_id": m.osm_id,
            "tags": dict(m.tags),
            "bbox": list(m.bbox),
        })
    reqs1 = idx.backend.requests + (ov.backend.requests if ov else 0)
    bytes1 = idx.backend.bytes_read + (ov.backend.bytes_read if ov else 0)
    return {
        "matches": out,
        "elapsed_ms": int(dt * 1000),
        "requests": reqs1 - reqs0,
        "bytes": bytes1 - bytes0,
    }


# Each worker keeps its own DistrictIndex (and its own HttpBackend), so the
# per-call request/byte deltas in lookup_district() are accurate. The header
# fetch is one extra request per worker — negligible.
_tls = threading.local()
def thread_local_index() -> DistrictIndex:
    idx = getattr(_tls, "idx", None)
    if idx is None:
        idx = DistrictIndex(HttpBackend(DRT_URL))
        _tls.idx = idx
    return idx


def thread_local_overlay() -> NamesOverlay:
    ov = getattr(_tls, "ov", None)
    if ov is None:
        ov = NamesOverlay.open(HttpBackend(OVERLAY_URL))
        _tls.ov = ov
    return ov


def run_one(place: tuple) -> dict:
    lat, lng, expected_n, city, country, category = place
    base = {
        "lat": lat, "lng": lng,
        "expected_neighborhood": expected_n,
        "city": city, "country": country, "category": category,
    }
    try:
        mapbox = query_mapbox(lat, lng)
    except Exception as e:
        mapbox = {"error": f"{type(e).__name__}: {e}"}
    try:
        district = lookup_district(
            thread_local_index(), thread_local_overlay(), lat, lng)
    except Exception as e:
        district = {"error": f"{type(e).__name__}: {e}"}
    return {**base, "mapbox": mapbox, "district": district}


def main():
    out_path = Path(__file__).parent / "results.json"

    print(f"querying {len(PLACES)} points "
          f"(.drt: {DRT_URL}, overlay: {OVERLAY_URL})", file=sys.stderr)
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(run_one, p): p for p in PLACES}
        for i, fut in enumerate(as_completed(futs), 1):
            results.append(fut.result())
            if i % 25 == 0 or i == len(PLACES):
                print(f"  {i}/{len(PLACES)}", file=sys.stderr)

    # Stable sort so the report is reproducible.
    results.sort(key=lambda r: (r["country"], r["city"], r["lat"], r["lng"]))
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"wrote {out_path}  ({len(results)} rows)", file=sys.stderr)


if __name__ == "__main__":
    main()
