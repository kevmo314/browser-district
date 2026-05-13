// browser-district
// HTTP-Range client for the OSM district R-tree + per-locale name overlays.

export { LookupClient, DEFAULT_BASE_URL } from "./lookup_client.js";
export { DistrictIndex } from "./district_index.js";
export { NamesOverlay } from "./names_overlay.js";
export { decodeGeometry, pointInQdv, QDV_BITS, QDV_SCALE } from "./qdv.js";
