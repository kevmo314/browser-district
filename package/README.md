# browser-district

[![npm version](https://img.shields.io/npm/v/browser-district)](https://www.npmjs.com/package/browser-district)

Resolve a `(lat, lng)` to OSM administrative areas, neighborhoods, parks, and protected areas — directly in the browser, by sending a handful of HTTP `Range` requests against a static `.drt` file (no server). Check out the [demo](https://kevmo314.github.io/browser-district/).

## Usage

### Unpkg

```html
<script type="module">
  import { LookupClient } from "https://unpkg.com/browser-district@latest/src/index.js";

  const client = await new LookupClient({
    indexUrl:    "https://pub-ba286604ef7044678dbc982b6ccb7fa4.r2.dev/planet-districts.drt",
    namesBaseUrl: "https://pub-ba286604ef7044678dbc982b6ccb7fa4.r2.dev",
    manifestUrl:  "https://pub-ba286604ef7044678dbc982b6ccb7fa4.r2.dev/planet-names.manifest.json",
  }).open();
  await client.setLocale("en");

  console.log(await client.lookup(40.7150, -73.9550));
  // → [{ name: "Williamsburg", translatedName: "Williamsburg", bbox: [...], ... }]
</script>
```

### NPM

```bash
npm install browser-district
```

```javascript
import { LookupClient } from "browser-district";

const client = await new LookupClient({
  indexUrl:    "https://pub-ba286604ef7044678dbc982b6ccb7fa4.r2.dev/planet-districts.drt",
  namesBaseUrl: "https://pub-ba286604ef7044678dbc982b6ccb7fa4.r2.dev",
  manifestUrl:  "https://pub-ba286604ef7044678dbc982b6ccb7fa4.r2.dev/planet-names.manifest.json",
}).open();
await client.setLocale("en");

console.log(await client.lookup(40.7150, -73.9550, { refine: true }));
```
