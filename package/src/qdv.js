// qdv.js
// Decoder + point-in-polygon for the q24 delta-varint geometry encoding used
// by the District protobuf's `geometry_qdv` field.
//
// Format (all varints; deltas are ZigZag):
//   varint num_polygons
//   per polygon:
//     varint num_rings           (1 outer + N inner)
//     per ring:
//       varint num_points
//       per point:
//         zigzag-varint dx, zigzag-varint dy
//
// Coordinates are quantized to 24-bit integers along each axis. Resolution at
// the equator is 360°/2²⁴ ≈ 2.4 m. Within each ring, vertices are stored as
// deltas from the previous vertex; the first vertex's delta is from (0, 0).

export const QDV_BITS = 28;
// 28-bit grid ≈ 15cm at the equator. Use 2 ** QDV_BITS — `1 << 28` would be
// fine for 28 but `1 << 32` would wrap to a negative int in JS, so use the
// float form to keep the formula valid if QDV_BITS ever becomes 30 or 32.
export const QDV_SCALE = Math.pow(2, QDV_BITS) / 360;
const QDV_X_OFFSET = 180;
const QDV_Y_OFFSET = 90;

const QDV_MAX = Math.pow(2, QDV_BITS);

function quantize(deg, offset) {
  const v = Math.floor((deg + offset) * QDV_SCALE);
  if (v < 0) return 0;
  if (v >= QDV_MAX) return QDV_MAX - 1;
  return v;
}

function dequantize(q, offset) {
  return q / QDV_SCALE - offset;
}

// --- Varint readers ---------------------------------------------------

// Note: avoid `<<` for the cumulative varint — it wraps at 32 bits, which
// would break q30+. Multiplying by 2**shift keeps us in the safe 53-bit
// float range and works for any QDV_BITS up to ~50.
function readVarint(view, pos) {
  let n = 0;
  let mult = 1;
  while (true) {
    const b = view.getUint8(pos++);
    n += (b & 0x7f) * mult;
    if ((b & 0x80) === 0) return [n, pos];
    mult *= 128;
  }
}

function readZigZag(view, pos) {
  const [v, p] = readVarint(view, pos);
  // Same here: don't use `>>> 1`, do it in float space.
  return [(v % 2 === 0) ? v / 2 : -(v + 1) / 2, p];
}

// --- Decode to nested arrays ------------------------------------------

/**
 * Decode a q24 geometry buffer into nested coordinate arrays.
 * Returns: [polygon[ring[[lng, lat], ...]]]
 */
export function decodeGeometry(view) {
  if (!view || view.byteLength === 0) return [];
  let pos = 0;
  const out = [];
  let np;
  [np, pos] = readVarint(view, pos);
  for (let p = 0; p < np; p++) {
    const polygon = [];
    let nr;
    [nr, pos] = readVarint(view, pos);
    for (let r = 0; r < nr; r++) {
      let n;
      [n, pos] = readVarint(view, pos);
      const ring = [];
      let px = 0, py = 0;
      for (let i = 0; i < n; i++) {
        let dx, dy;
        [dx, pos] = readZigZag(view, pos);
        [dy, pos] = readZigZag(view, pos);
        px += dx; py += dy;
        ring.push([dequantize(px, QDV_X_OFFSET), dequantize(py, QDV_Y_OFFSET)]);
      }
      polygon.push(ring);
    }
    out.push(polygon);
  }
  return out;
}

// --- Streaming point-in-polygon ---------------------------------------

/**
 * Even-odd ray-cast point-in-polygon, decoded on the fly.
 * The point is "inside" the geometry iff it's contained by an odd number
 * of (outer ∪ inner) rings.
 */
export function pointInQdv(view, lon, lat) {
  if (!view || view.byteLength === 0) return false;
  const qx = quantize(lon, QDV_X_OFFSET);
  const qy = quantize(lat, QDV_Y_OFFSET);
  let pos = 0;
  let inside = false;
  let np;
  [np, pos] = readVarint(view, pos);
  for (let p = 0; p < np; p++) {
    let nr;
    [nr, pos] = readVarint(view, pos);
    for (let r = 0; r < nr; r++) {
      let n;
      [n, pos] = readVarint(view, pos);
      let firstX = null, firstY = null;
      let prevX = 0, prevY = 0;
      let px = 0, py = 0;
      for (let i = 0; i < n; i++) {
        let dx, dy;
        [dx, pos] = readZigZag(view, pos);
        [dy, pos] = readZigZag(view, pos);
        px += dx; py += dy;
        if (i === 0) {
          firstX = px; firstY = py;
        } else {
          if (((prevY > qy) !== (py > qy)) &&
              (qx < ((px - prevX) * (qy - prevY)) / (py - prevY) + prevX)) {
            inside = !inside;
          }
        }
        prevX = px; prevY = py;
      }
      // Close the ring (last edge from prev → first).
      if (firstX !== null && (firstX !== prevX || firstY !== prevY)) {
        if (((prevY > qy) !== (firstY > qy)) &&
            (qx < ((firstX - prevX) * (qy - prevY)) / (firstY - prevY) + prevX)) {
          inside = !inside;
        }
      }
    }
  }
  return inside;
}
