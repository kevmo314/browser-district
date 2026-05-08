// lookup_client.js
// High-level client that ties the spatial R-tree (.drt), the per-locale name
// overlays (.dn), and an optional point-in-polygon refinement step into one
// call: lookup(lat, lng) -> array of matched districts.

import { DistrictIndex } from "./district_index.js";
import { NamesOverlay } from "./names_overlay.js";
import { pointInQdv } from "./qdv.js";

/**
 * @typedef {Object} Match
 * @property {bigint} osm_id            OSM id of the source feature.
 * @property {number} osm_type          0 = node, 1 = way, 2 = relation.
 * @property {string} name              Local-language name (from `.drt`).
 * @property {string} [translatedName]  Set when an overlay is loaded and a
 *                                       translation exists for this id.
 * @property {[number, number, number, number]} bbox  [minLng, minLat, maxLng, maxLat].
 * @property {Object<string, string>} tags             Selected tags from OSM.
 * @property {number} minDist           Distance from the query point to the
 *                                       feature's bbox, in metres. 0 if the
 *                                       point is inside the bbox.
 * @property {boolean} [insidePolygon]  Set by `lookup()` when refinement is
 *                                       requested: true if the point is
 *                                       actually inside the stored polygon.
 */

/**
 * @typedef {Object} LookupOptions
 * @property {number} [k=5] Cap on results when no feature contains the point
 *                          (kNN fallback in the spatial index).
 * @property {boolean} [refine=false] Run point-in-polygon on each match using
 *                                    the stored q24 geometry; sets
 *                                    `insidePolygon` on each result.
 * @property {string|null} [locale]  Override the client's current locale for
 *                                   this call only. Use `null` for none.
 */

export class LookupClient {
  /**
   * @param {Object} cfg
   * @param {string} cfg.indexUrl       URL of the .drt file.
   * @param {string} [cfg.namesBaseUrl] Base URL for .dn overlay files (no
   *                                    trailing slash). If omitted, name
   *                                    translations are disabled.
   * @param {string} [cfg.manifestUrl]  URL of planet-names.manifest.json. If
   *                                    omitted, locales are loaded by guessing
   *                                    the filename `planet-names-<lang>.dn`
   *                                    under namesBaseUrl.
   */
  constructor({ indexUrl, namesBaseUrl, manifestUrl }) {
    this.indexUrl = indexUrl;
    this.namesBaseUrl = namesBaseUrl ?? null;
    this.manifestUrl = manifestUrl ?? null;
    /** @type {DistrictIndex|null} */
    this.index = null;
    /** @type {NamesOverlay|null} */
    this.overlay = null;
    /** @type {string|null} */
    this.locale = null;
    /** @type {Array<{lang:string, file:string, entries:number, size:number}>} */
    this.locales = [];
  }

  /** Open the spatial index and (if `manifestUrl` is set) load the manifest. */
  async open() {
    this.index = await DistrictIndex.open(this.indexUrl);
    if (this.manifestUrl) {
      try {
        const r = await fetch(this.manifestUrl, { cache: "no-store" });
        if (r.ok) {
          const m = await r.json();
          this.locales = Array.isArray(m.locales) ? m.locales : [];
        }
      } catch {
        // Manifest is optional — silently ignore.
      }
    }
    return this;
  }

  /** Switch the active locale. Pass `null` or `""` to disable translation. */
  async setLocale(lang) {
    if (!lang) { this.overlay = null; this.locale = null; return; }
    if (!this.namesBaseUrl) {
      throw new Error("namesBaseUrl was not configured");
    }
    let file = `planet-names-${lang}.dn`;
    if (this.locales.length > 0) {
      const entry = this.locales.find((e) => e.lang === lang);
      if (!entry) throw new Error(`locale ${lang} not in manifest`);
      file = entry.file;
    }
    this.overlay = await NamesOverlay.open(`${this.namesBaseUrl}/${file}`);
    this.locale = lang;
  }

  /**
   * Resolve (lat, lng) to matching districts.
   * @param {number} lat
   * @param {number} lng
   * @param {LookupOptions} [opts]
   * @returns {Promise<Match[]>}
   */
  async lookup(lat, lng, opts = {}) {
    if (!this.index) throw new Error("call open() first");
    // Wrap longitude across the dateline (Leaflet sometimes returns
    // values outside [-180, 180] when the user has panned).
    const wrapped = ((lng + 180) % 360 + 360) % 360 - 180;

    const raw = await this.index.queryPoint(wrapped, lat, { k: opts.k ?? 5 });

    // Antimeridian-crossing features are stored as two records sharing one
    // osm_id — keep the closer copy.
    const dedup = new Map();
    for (const m of raw) {
      const key = m.osm_id.toString();
      const prev = dedup.get(key);
      if (!prev || m.minDist < prev.minDist) dedup.set(key, m);
    }
    const matches = Array.from(dedup.values());

    // Optional polygon refinement (only for features with stored geometry).
    if (opts.refine) {
      for (const m of matches) {
        m.insidePolygon = m.geometry
          ? pointInQdv(m.geometry, wrapped, lat)
          : null; // unknown — point feature or geometry not stored
      }
    }

    // Translation overlay (per-call locale override > client-level locale).
    const overrideLocale = opts.locale === undefined ? this.locale : opts.locale;
    if (overrideLocale && this.overlay && overrideLocale === this.locale) {
      const translated = await this.overlay.lookupMany(
        matches.map((m) => m.osm_id)
      );
      for (const m of matches) {
        const t = translated.get(m.osm_id.toString());
        if (t) m.translatedName = t;
      }
    }

    // Default sort: containing matches first (minDist=0), ties by distance
    // from click to the bbox centroid.
    const cosLat = Math.cos(lat * Math.PI / 180);
    const R = 6371000;
    const deg = Math.PI / 180;
    for (const m of matches) {
      const cx = (m.bbox[0] + m.bbox[2]) * 0.5;
      const cy = (m.bbox[1] + m.bbox[3]) * 0.5;
      const dx = (cx - wrapped) * cosLat;
      const dy = (cy - lat);
      m._centroidDist = Math.sqrt(dx * dx + dy * dy) * deg * R;
    }
    matches.sort(
      (a, b) => (a.minDist - b.minDist) || (a._centroidDist - b._centroidDist)
    );
    return matches;
  }

  /** Aggregate cost since the client was opened (across both files). */
  get stats() {
    return {
      indexRequests: this.index?.backend.requests ?? 0,
      indexBytes: this.index?.backend.bytesRead ?? 0,
      overlayRequests: this.overlay?.backend.requests ?? 0,
      overlayBytes: this.overlay?.backend.bytesRead ?? 0,
    };
  }
}
