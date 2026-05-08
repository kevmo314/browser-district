// district_index.js
// HTTP-Range R-tree traversal + minimal protobuf parser for District messages.
// File format is documented in ../rtree_format.py.

const HEADER_SIZE = 64;
const PAGE_HEADER_SIZE = 8;
const ENTRY_SIZE = 32;
const MAGIC = "DISTRTRE";

// ---------- HTTP backend with Range support ----------

class HttpBackend {
  constructor(url) {
    this.url = url;
    this.requests = 0;
    this.bytesRead = 0;
  }

  async readRange(offset, length) {
    this.requests++;
    this.bytesRead += length;
    const resp = await fetch(this.url, {
      headers: { "Range": `bytes=${offset}-${offset + length - 1}` },
      cache: "no-store",
    });
    if (resp.status !== 206) {
      // 200 means the server ignored our Range and shipped the whole file.
      // python3 -m http.server does this. Fail loudly.
      throw new Error(
        `HTTP ${resp.status} (expected 206). The server isn't honoring Range ` +
        `requests. Use any range-aware server, e.g. \`npx http-server -p 8765 .\` ` +
        `or \`caddy file-server --listen :8765\`.`
      );
    }
    const buf = await resp.arrayBuffer();
    return new DataView(buf);
  }
}

// ---------- Index ----------

export class DistrictIndex {
  constructor(backend, meta) {
    this.backend = backend;
    this.meta = meta;
  }

  static async open(url) {
    const backend = new HttpBackend(url);
    const view = await backend.readRange(0, HEADER_SIZE);
    let magic = "";
    for (let i = 0; i < 8; i++) magic += String.fromCharCode(view.getUint8(i));
    if (magic !== MAGIC) throw new Error(`bad magic: ${magic}`);
    const meta = {
      version: view.getUint32(8, true),
      pageSize: view.getUint32(12, true),
      numFeatures: Number(view.getBigUint64(16, true)),
      numNodes: Number(view.getBigUint64(24, true)),
      rootOffset: Number(view.getBigUint64(32, true)),
      featuresOffset: Number(view.getBigUint64(40, true)),
      featuresSize: Number(view.getBigUint64(48, true)),
    };
    return new DistrictIndex(backend, meta);
  }

  // Try strict bbox-containment first (fast, parallel). If nothing contains
  // the point — common in parks, water, industrial areas — fall back to
  // a k-nearest-neighbor search on the same tree.
  async queryPoint(lon, lat, opts = {}) {
    const containing = await this._queryContaining(lon, lat);
    if (containing.length > 0) {
      for (const d of containing) d.minDist = 0;
      return containing;
    }
    return this._queryNearest(lon, lat, opts.k ?? 5);
  }

  // Breadth-first containment traversal: at each level we issue all matching
  // page fetches in parallel, so a depth-3 tree is ~3 round trips regardless
  // of how many pages match per level.
  async _queryContaining(lon, lat) {
    let frontier = [this.meta.rootOffset];
    const leafBlobs = [];
    while (frontier.length > 0) {
      const pages = await Promise.all(
        frontier.map((off) => this.backend.readRange(off, this.meta.pageSize))
      );
      const next = [];
      for (const page of pages) {
        const isLeaf = page.getUint8(0) === 1;
        const entryCount = page.getUint16(2, true);
        for (let i = 0; i < entryCount; i++) {
          const base = PAGE_HEADER_SIZE + i * ENTRY_SIZE;
          const minLon = page.getFloat32(base, true);
          const minLat = page.getFloat32(base + 4, true);
          const maxLon = page.getFloat32(base + 8, true);
          const maxLat = page.getFloat32(base + 12, true);
          if (lon < minLon || lon > maxLon || lat < minLat || lat > maxLat) continue;
          const childOffset = Number(page.getBigUint64(base + 16, true));
          const childSize = page.getUint32(base + 24, true);
          if (isLeaf) leafBlobs.push({ offset: childOffset, size: childSize });
          else next.push(childOffset);
        }
      }
      frontier = next;
    }
    if (leafBlobs.length === 0) return [];
    const blobs = await Promise.all(
      leafBlobs.map((b) => this.backend.readRange(b.offset, b.size))
    );
    return blobs.map(parseDistrictBlob);
  }

  // Best-first kNN. Min-priority queue keyed by min-dist(point -> bbox) in
  // metres (lat-cosine corrected). Pop the closest item; if it's a page, fetch
  // it and push its entries; if it's a feature, fetch the blob and add it to
  // the result list. Stops once we have k features.
  async _queryNearest(lon, lat, k) {
    const cosLat = Math.cos(lat * Math.PI / 180);
    const R = 6371000;
    const deg = Math.PI / 180;
    const minDistM = (minLon, minLat, maxLon, maxLat) => {
      const dx = Math.max(0, minLon - lon, lon - maxLon) * cosLat;
      const dy = Math.max(0, minLat - lat, lat - maxLat);
      return Math.sqrt(dx * dx + dy * dy) * deg * R;
    };
    const pq = new MinHeap();
    pq.push({ minDist: 0, kind: "page", offset: this.meta.rootOffset });
    const out = [];
    while (pq.size() > 0 && out.length < k) {
      const item = pq.pop();
      if (item.kind === "feature") {
        const blob = await this.backend.readRange(item.offset, item.size);
        const d = parseDistrictBlob(blob);
        d.minDist = item.minDist;
        out.push(d);
        continue;
      }
      const page = await this.backend.readRange(item.offset, this.meta.pageSize);
      const isLeaf = page.getUint8(0) === 1;
      const entryCount = page.getUint16(2, true);
      for (let i = 0; i < entryCount; i++) {
        const base = PAGE_HEADER_SIZE + i * ENTRY_SIZE;
        const minLon = page.getFloat32(base, true);
        const minLat = page.getFloat32(base + 4, true);
        const maxLon = page.getFloat32(base + 8, true);
        const maxLat = page.getFloat32(base + 12, true);
        const childOffset = Number(page.getBigUint64(base + 16, true));
        const childSize = page.getUint32(base + 24, true);
        pq.push({
          minDist: minDistM(minLon, minLat, maxLon, maxLat),
          kind: isLeaf ? "feature" : "page",
          offset: childOffset,
          size: childSize,
        });
      }
    }
    return out;
  }
}

// Tiny binary min-heap keyed by item.minDist.
class MinHeap {
  constructor() { this.data = []; }
  size() { return this.data.length; }
  push(x) { this.data.push(x); this._up(this.data.length - 1); }
  pop() {
    const top = this.data[0];
    const last = this.data.pop();
    if (this.data.length > 0) { this.data[0] = last; this._down(0); }
    return top;
  }
  _up(i) {
    const a = this.data;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (a[p].minDist <= a[i].minDist) break;
      [a[p], a[i]] = [a[i], a[p]]; i = p;
    }
  }
  _down(i) {
    const a = this.data, n = a.length;
    while (true) {
      const l = 2 * i + 1, r = l + 1;
      let s = i;
      if (l < n && a[l].minDist < a[s].minDist) s = l;
      if (r < n && a[r].minDist < a[s].minDist) s = r;
      if (s === i) break;
      [a[s], a[i]] = [a[i], a[s]]; i = s;
    }
  }
}

// ---------- Minimal protobuf reader (only what District needs) ----------
//
// District {
//   uint64 osm_id            = 1;  // wire type 0 (varint)
//   uint32 osm_type          = 2;  // wire type 0 (varint)
//   string name              = 3;  // wire type 2 (length-delimited)
//   repeated double bbox     = 4;  // wire type 2 (packed)
//   map<string,string> tags  = 5;  // wire type 2 (sub-message per entry)
//   bytes geometry_wkb       = 6;  // wire type 2 (deprecated)
//   bytes geometry_qdv       = 7;  // wire type 2 (q24-delta-varint, see qdv.js)
// }

const decoder = new TextDecoder("utf-8");

function readVarint(view, pos) {
  let value = 0n;
  let shift = 0n;
  while (true) {
    const b = view.getUint8(pos++);
    value |= BigInt(b & 0x7f) << shift;
    if ((b & 0x80) === 0) break;
    shift += 7n;
  }
  return { value, next: pos };
}

function readVarintNum(view, pos) {
  const r = readVarint(view, pos);
  return { value: Number(r.value), next: r.next };
}

function decodeUtf8(view, start, end) {
  return decoder.decode(new Uint8Array(view.buffer, view.byteOffset + start, end - start));
}

function skipField(view, pos, wireType) {
  if (wireType === 0) return readVarint(view, pos).next;
  if (wireType === 2) {
    const len = readVarintNum(view, pos);
    return len.next + len.value;
  }
  if (wireType === 1) return pos + 8;
  if (wireType === 5) return pos + 4;
  throw new Error(`unsupported wire type ${wireType}`);
}

function parseMapEntry(view, start, end) {
  let k = "";
  let v = "";
  let pos = start;
  while (pos < end) {
    const t = readVarintNum(view, pos);
    pos = t.next;
    const fieldNum = t.value >>> 3;
    const wireType = t.value & 0x7;
    if (wireType !== 2) {
      pos = skipField(view, pos, wireType);
      continue;
    }
    const len = readVarintNum(view, pos);
    pos = len.next;
    const s = decodeUtf8(view, pos, pos + len.value);
    pos += len.value;
    if (fieldNum === 1) k = s;
    else if (fieldNum === 2) v = s;
  }
  return [k, v];
}

function parseDistrict(view, start, end) {
  const out = {
    osm_id: 0n,
    osm_type: 0,
    name: "",
    bbox: [],
    tags: {},
    /** DataView slice over the q24-delta-varint geometry, or null. */
    geometry: null,
  };
  let pos = start;
  while (pos < end) {
    const t = readVarintNum(view, pos);
    pos = t.next;
    const fieldNum = t.value >>> 3;
    const wireType = t.value & 0x7;
    if (wireType === 0) {
      const v = readVarint(view, pos);
      pos = v.next;
      if (fieldNum === 1) out.osm_id = v.value;
      else if (fieldNum === 2) out.osm_type = Number(v.value);
    } else if (wireType === 2) {
      const len = readVarintNum(view, pos);
      pos = len.next;
      const valEnd = pos + len.value;
      if (fieldNum === 3) {
        out.name = decodeUtf8(view, pos, valEnd);
      } else if (fieldNum === 4) {
        // packed repeated double — 8 bytes each
        for (let i = pos; i < valEnd; i += 8) {
          out.bbox.push(view.getFloat64(i, true));
        }
      } else if (fieldNum === 5) {
        const [k, v] = parseMapEntry(view, pos, valEnd);
        out.tags[k] = v;
      } else if (fieldNum === 7) {
        // q24-delta-varint geometry — keep as a DataView slice for the caller
        // (use the qdv.js helpers to decode or PIP-test).
        out.geometry = new DataView(view.buffer,
                                    view.byteOffset + pos,
                                    valEnd - pos);
      }
      pos = valEnd;
    } else {
      pos = skipField(view, pos, wireType);
    }
  }
  return out;
}

// Each blob in the file is a varint length prefix followed by the District message.
function parseDistrictBlob(view) {
  const len = readVarintNum(view, 0);
  const end = len.next + len.value;
  return parseDistrict(view, len.next, end);
}
