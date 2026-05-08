// Type declarations for osm-district-lookup.

export interface ManifestLocale {
  lang: string;
  file: string;
  entries: number;
  size: number;
}

export interface IndexMeta {
  version: number;
  pageSize: number;
  numFeatures: number;
  numNodes: number;
  rootOffset: number;
  featuresOffset: number;
  featuresSize: number;
}

export interface OverlayMeta {
  version: number;
  pageSize: number;
  numFeatures: number;
  numPages: number;
  rootOffset: number;
  blobsOffset: number;
  blobsSize: number;
}

export interface Match {
  /** OSM id of the source feature. */
  osm_id: bigint;
  /** 0 = node, 1 = way, 2 = relation. */
  osm_type: number;
  /** Local-language name (from the .drt). */
  name: string;
  /** Set when an overlay is loaded and a translation exists for this id. */
  translatedName?: string;
  /** [minLng, minLat, maxLng, maxLat] in WGS84 degrees. */
  bbox: [number, number, number, number];
  /** Selected OSM tags (place, admin_level, leisure, boundary, name:* ...). */
  tags: Record<string, string>;
  /** Distance from query to the feature's bbox, in metres. 0 if inside. */
  minDist: number;
  /** Set by `lookup({ refine: true })`. true if the point is inside the
   *  stored polygon; null if no polygon is stored (e.g. place-node). */
  insidePolygon?: boolean | null;
  /** q24-delta-varint geometry buffer; null for point features. Use the
   *  `decodeGeometry` / `pointInQdv` helpers. */
  geometry: DataView | null;
}

export interface LookupOptions {
  /** Cap on results when no feature contains the point (default 5). */
  k?: number;
  /** Run point-in-polygon refinement; sets `insidePolygon` on each match. */
  refine?: boolean;
  /** Override the active locale for this call only. */
  locale?: string | null;
}

export interface LookupClientConfig {
  /** URL of the .drt file. */
  indexUrl: string;
  /** Base URL for .dn overlays (no trailing slash). */
  namesBaseUrl?: string;
  /** URL of planet-names.manifest.json. */
  manifestUrl?: string;
}

export class LookupClient {
  constructor(cfg: LookupClientConfig);
  index: DistrictIndex | null;
  overlay: NamesOverlay | null;
  locale: string | null;
  locales: ManifestLocale[];
  open(): Promise<this>;
  setLocale(lang: string | null): Promise<void>;
  lookup(lat: number, lng: number, opts?: LookupOptions): Promise<Match[]>;
  readonly stats: {
    indexRequests: number;
    indexBytes: number;
    overlayRequests: number;
    overlayBytes: number;
  };
}

export class DistrictIndex {
  static open(url: string): Promise<DistrictIndex>;
  meta: IndexMeta;
  backend: { requests: number; bytesRead: number; readRange(offset: number, length: number): Promise<DataView> };
  queryPoint(lon: number, lat: number, opts?: { k?: number }): Promise<Match[]>;
}

export class NamesOverlay {
  static open(url: string): Promise<NamesOverlay>;
  meta: OverlayMeta;
  backend: { requests: number; bytesRead: number; readRange(offset: number, length: number): Promise<DataView> };
  /** Returns Map<osm_id_string, translated_name>. */
  lookupMany(osmIds: Array<bigint | number | string>): Promise<Map<string, string>>;
}

/** q24-delta-varint geometry helpers. */
export const QDV_BITS: 24;
export const QDV_SCALE: number;
export function decodeGeometry(view: DataView | null): Array<Array<Array<[number, number]>>>;
export function pointInQdv(view: DataView | null, lon: number, lat: number): boolean;
