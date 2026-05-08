// names_overlay.js
// Per-locale name overlay reader. Sister of district_index.js — same shape
// but the tree is 1-D (B+tree on osm_id) instead of 2-D R-tree on bbox.
// File format: ../names_format.py.

const HEADER_SIZE = 64;
const PAGE_HEADER_SIZE = 8;
const INTERNAL_ENTRY_SIZE = 16;
const LEAF_ENTRY_SIZE = 24;
const MAGIC = "DISTNAME";

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
      throw new Error(
        `HTTP ${resp.status} (expected 206). The server isn't honoring Range ` +
        `requests. Use a range-aware server, e.g. \`npx http-server -p 8765 .\``
      );
    }
    return new DataView(await resp.arrayBuffer());
  }
}

const decoder = new TextDecoder("utf-8");

export class NamesOverlay {
  constructor(backend, meta) {
    this.backend = backend;
    this.meta = meta;
    // Cache the root page — every lookup needs it.
    this._rootPagePromise = null;
  }

  static async open(url) {
    const backend = new HttpBackend(url);
    const view = await backend.readRange(0, HEADER_SIZE);
    let magic = "";
    for (let i = 0; i < 8; i++) magic += String.fromCharCode(view.getUint8(i));
    if (magic !== MAGIC) throw new Error(`bad names magic: ${magic}`);
    const meta = {
      version: view.getUint32(8, true),
      pageSize: view.getUint32(12, true),
      numFeatures: Number(view.getBigUint64(16, true)),
      numPages: Number(view.getBigUint64(24, true)),
      rootOffset: Number(view.getBigUint64(32, true)),
      blobsOffset: Number(view.getBigUint64(40, true)),
      blobsSize: Number(view.getBigUint64(48, true)),
    };
    return new NamesOverlay(backend, meta);
  }

  _root() {
    if (!this._rootPagePromise) {
      this._rootPagePromise = this.backend.readRange(
        this.meta.rootOffset, this.meta.pageSize);
    }
    return this._rootPagePromise;
  }

  // Look up multiple osm_ids in one call. Returns Map<osm_id_string, name>.
  // Same-level page fetches are issued in parallel, so for k osm_ids and a
  // depth-d tree we do ~d round trips total (root is cached).
  async lookupMany(osmIds) {
    const result = new Map();
    if (osmIds.length === 0) return result;
    // BigInt copies so we can compare in 64-bit space.
    const targets = osmIds.map((x) => typeof x === "bigint" ? x : BigInt(x));

    // Each pending lookup carries a list of pending {target, originalId}.
    // Group by current page offset so we batch all lookups hitting the same page.
    let frontier = new Map(); // pageOffset -> [{target, originalId}, ...]
    const rootPage = await this._root();
    frontier.set(this.meta.rootOffset, targets.map((t, i) => ({
      target: t, originalId: osmIds[i],
    })));

    const leafBlobs = []; // {originalId, offset, size}

    while (frontier.size > 0) {
      // Fetch every distinct page in this level in parallel.
      const offsets = Array.from(frontier.keys());
      const pages = await Promise.all(offsets.map((off) =>
        off === this.meta.rootOffset
          ? rootPage
          : this.backend.readRange(off, this.meta.pageSize)
      ));
      const next = new Map();
      for (let pi = 0; pi < offsets.length; pi++) {
        const page = pages[pi];
        const lookups = frontier.get(offsets[pi]);
        const isLeaf = page.getUint8(0) === 1;
        const entryCount = page.getUint16(2, true);
        if (isLeaf) {
          // Leaves: linear scan for each target (entries sorted ascending).
          for (const { target, originalId } of lookups) {
            for (let i = 0; i < entryCount; i++) {
              const base = PAGE_HEADER_SIZE + i * LEAF_ENTRY_SIZE;
              const osmId = page.getBigUint64(base, true);
              if (osmId === target) {
                const off = Number(page.getBigUint64(base + 8, true));
                const sz = page.getUint32(base + 16, true);
                leafBlobs.push({ originalId, offset: off, size: sz });
                break;
              }
              if (osmId > target) break; // entries sorted, no later match
            }
          }
        } else {
          // Internal: find the child whose max_osm_id >= target.
          for (const { target, originalId } of lookups) {
            for (let i = 0; i < entryCount; i++) {
              const base = PAGE_HEADER_SIZE + i * INTERNAL_ENTRY_SIZE;
              const maxId = page.getBigUint64(base, true);
              if (maxId >= target) {
                const childOff = Number(page.getBigUint64(base + 8, true));
                if (!next.has(childOff)) next.set(childOff, []);
                next.get(childOff).push({ target, originalId });
                break;
              }
            }
            // If we never found maxId >= target, the id isn't in the overlay;
            // silently drop (caller falls back to local name).
          }
        }
      }
      frontier = next;
    }

    if (leafBlobs.length === 0) return result;

    // Fetch all matching name blobs in parallel.
    const blobs = await Promise.all(leafBlobs.map((b) =>
      this.backend.readRange(b.offset, b.size)
    ));
    for (let i = 0; i < leafBlobs.length; i++) {
      const view = blobs[i];
      const name = decoder.decode(new Uint8Array(
        view.buffer, view.byteOffset, view.byteLength));
      // Use string keys so callers can store BigInt or Number osm_ids alike.
      result.set(String(leafBlobs[i].originalId), name);
    }
    return result;
  }
}
