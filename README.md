# district

Resolve a `(lat, lng)` to the OSM administrative areas, neighborhoods,
parks, and protected areas at that point — by sending a handful of HTTP
`Range` requests at a static `.drt` file and (optionally) a per-locale
`.dn` name overlay. No server. ~5–15 range requests per query, ~20–60 KB
transferred, against a 1.3 GB planet index.

**Live demo:** https://kevmo314.github.io/district/web/
**npm package:** [`osm-district-lookup`](./package)

## What's in here

| Path | What |
|---|---|
| `extract_districts.py` | OSM PBF → length-prefixed District protobufs (the `.dpb`). Two-pass: `osmium tags-filter` then pyosmium area assembly. Captures admin boundaries, place neighborhoods, parks, and protected areas; encodes geometry as q28 delta-varint. |
| `build_rtree.py` | `.dpb` → `.drt`. Hilbert-sorts features, packs leaves bottom-up. Strips `name:<lang>` tags (they live in the overlays). |
| `build_names.py` | `.dpb` → per-locale `.dn` overlays. `--all` builds every language in a single scan. |
| `query.py` | Local CLI lookup (file or HTTP URL). Optional point-in-polygon refine. |
| `qdv.py` | q28 delta-varint geometry codec — encode + decode + streaming PIP. |
| `rtree_format.py` / `names_format.py` | Binary file formats (documented inline). |
| `proto/district.proto` | Per-feature payload schema. |
| `package/` | npm package (`osm-district-lookup`) — JS client for the browser/Node. |
| `web/` | Demo site (Leaflet map; click → lookup). Deployed to Pages. |
| `.github/workflows/build.yml` | Manual trigger: download planet, build everything, push to R2. |
| `.github/workflows/pages.yml` | On-push to `main`: deploy demo to GitHub Pages. |

## File formats

Both `.drt` and `.dn` share the same shape:

- 4 KB header page (magic + offsets).
- Variable-length blob section (length-prefixed protobufs for `.drt`,
  packed UTF-8 strings for `.dn`).
- Page-aligned tree pages, leaves first, root last.
- Every node fits in one page so traversal is one HTTP `Range` per page.

`.drt` is a packed Hilbert R-tree on bbox; `.dn` is a packed B+tree on `osm_id`.

### Geometry encoding (q28 delta-varint)

Polygons live in field 7 (`geometry_qdv`) as a custom encoding:

1. **Quantize** each lng/lat to a 28-bit integer (≈15 cm grid at the equator).
2. **Delta-encode** consecutive vertices within each ring (the first vertex's
   delta is from `(0, 0)`).
3. **ZigZag-varint** every delta — small signed deltas land in 1–2 bytes.

Result: ~10× smaller than WKB, fully invertible, decodable in ~30 lines of JS.
Generic compression (gzip/zstd) on raw WKB only saves ~20% because f64 binary
is already high-entropy.

## Pipeline (manual)

```bash
# 1. Filter the planet (~42 min over a fast network connection)
osmium tags-filter --overwrite \
  -o planet-filtered.osm.pbf planet-latest.osm.pbf \
  a/boundary=administrative \
  a/boundary=national_park,protected_area \
  a/leisure=nature_reserve,park \
  a/place=borough,city_district,district,neighbourhood,quarter,suburb \
  n/place=borough,city_district,district,neighbourhood,quarter,suburb

# 2. Assembly + q28 geometry encoding (~25 min)
python extract_districts.py planet-filtered.osm.pbf planet-districts.dpb --skip-filter

# 3. Pack the R-tree (~1 min)
python build_rtree.py planet-districts.dpb planet-districts.drt

# 4. Build all 631 per-locale name overlays in a single scan (~3 min)
python build_names.py planet-districts.dpb names --all
```

Outputs:
- `planet-districts.drt` — ~1.3 GB
- `names/planet-names-*.dn` — 631 files, ~44 MB total
- `names/planet-names.manifest.json` — locale index

## Pipeline (CI)

The `Build & publish district index` workflow runs the entire pipeline on a
GitHub-hosted runner and uploads the artifacts to the R2 bucket `district`.
It stream-downloads the 86 GB planet PBF directly into `osmium tags-filter`
(stdin) so nothing is materialized that wouldn't fit on the runner's 14 GB
SSD. Trigger it from the Actions tab — runs in ~90 min.

Required secrets (set via `gh secret set` or the repo settings UI):
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `CF_ACCOUNT_ID` (used to construct `https://<id>.r2.cloudflarestorage.com`)

## Hosting

Files live in the public R2 bucket `district`. The public URL is
`https://pub-ba286604ef7044678dbc982b6ccb7fa4.r2.dev/`. CORS is configured
to allow GET/HEAD with `Range` from any origin.

GitHub Pages can't host the data files directly — the `.drt` exceeds the
100 MB per-file limit — but it serves the demo site; the demo's JS fetches
the data cross-origin from R2 via Range.

## License

MIT. OSM data © OpenStreetMap contributors, used under ODbL.
