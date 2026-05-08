# osm-district-lookup

Resolve a `(lat, lng)` to the OSM administrative areas, neighborhoods, parks,
and protected areas at that point — by sending a handful of HTTP `Range`
requests against a static `.drt` file and (optionally) a `.dn` per-locale name
overlay. No server needed beyond plain static hosting (S3, CDN, nginx, …) that
honors `Range:`.

## Install

```bash
npm install osm-district-lookup
```

## Files you need to host

The package is the *client*; the data files are produced by the build pipeline
in this repo:

| File | What | Typical size |
|---|---|---|
| `planet-districts.drt` | Packed R-tree + per-feature payloads (bbox, name, tags, q24 polygon) | ~900 MB (full planet) |
| `planet-names-<lang>.dn` | Per-locale name overlay; used to translate names away from the local script | a few KB to ~10 MB per locale |
| `planet-names.manifest.json` | Index of available locales | <100 KB |

Ship them to any HTTP server that supports `Range` requests (basically
everything except `python3 -m http.server`).

## Usage

```js
import { LookupClient } from "osm-district-lookup";

const client = new LookupClient({
  indexUrl:     "https://cdn.example.com/planet-districts.drt",
  namesBaseUrl: "https://cdn.example.com/names",
  manifestUrl:  "https://cdn.example.com/names/planet-names.manifest.json",
});

await client.open();
await client.setLocale("en");   // optional

const matches = await client.lookup(40.7150, -73.9550);
// → [{
//     osm_id: 158863515n,
//     name: "Williamsburg",
//     translatedName: "Williamsburg",
//     bbox: [-73.9584, 40.7096, -73.9485, 40.7196],
//     tags: { place: "quarter", "name:en": "Williamsburg", ... },
//     minDist: 0,
//     geometry: null  // (place-node, no polygon stored)
//   }, ...]

console.log(client.stats); // { indexRequests, indexBytes, overlayRequests, overlayBytes }
```

### Point-in-polygon refinement

Bbox-only matching can produce false positives near the bbox edges of an
L-shaped or coastline-following district. Pass `refine: true` to get an
exact polygon test using the stored geometry:

```js
const matches = await client.lookup(40.7740, -73.9710, { refine: true });
// each match gains an `insidePolygon: boolean | null` field
// (null when the feature has no polygon, e.g. place-nodes)
```

### k-nearest fallback

If no polygon contains the point (parks, water, etc.), the client returns the
`k` nearest features instead, each with `minDist > 0`. Default `k = 5`; tune via
`lookup(lat, lng, { k: 10 })`.

### Available locales

```js
console.log(client.locales);
// [
//   { lang: "en",      file: "planet-names-en.dn",      entries: 280447, size: 10747904 },
//   { lang: "ja-Hira", file: "planet-names-ja-Hira.dn", entries: 132626, size: 6504448 },
//   ...
// ]
```

When `manifestUrl` is set, the client populates `client.locales` automatically
on `open()`. Switch language at any time with `setLocale()`; the next `lookup()`
will use the new overlay.

## Lower-level pieces

If you want to bypass the high-level client:

```js
import { DistrictIndex, NamesOverlay, decodeGeometry, pointInQdv } from "osm-district-lookup";
```

- **`DistrictIndex.open(url)`** — direct R-tree access. `index.queryPoint(lng, lat)` returns matches; uses BFS containment first, falls back to k-NN if nothing contains the point.
- **`NamesOverlay.open(url)`** — direct overlay access. `overlay.lookupMany([id1, id2, …])` does a batched B+tree lookup and returns a `Map<id_string, name>`.
- **`decodeGeometry(view)`** — explode a `q24` geometry to nested coordinate arrays `[polygon[ring[[lng, lat], …]]]`.
- **`pointInQdv(view, lon, lat)`** — even-odd ray-cast PIP that decodes the geometry on the fly (no allocation).

## File formats

The two binary formats (`.drt` and `.dn`) are documented in
`rtree_format.py` and `names_format.py` in the source repository. Both share
the same shape:

- 4 KB header page (magic + offsets).
- Variable-length blob section (length-prefixed protobufs for `.drt`,
  packed UTF-8 strings for `.dn`).
- Page-aligned tree pages, leaves first, root last.
- Every node fits in one page so traversal is one HTTP `Range` per page.

A typical lookup costs **5–15 range requests** against the planet `.drt`,
totaling 20–60 KB transferred — independent of the file's overall size.

## Browser & Node compatibility

Pure ES modules, no dependencies. Works in:

- Modern browsers (Chrome 67+, Safari 15+, Firefox 68+) — needs `BigInt`,
  `DataView.getBigUint64`, `fetch`.
- Node 18+ (built-in `fetch`).

## License

MIT.
