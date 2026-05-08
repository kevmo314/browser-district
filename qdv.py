"""
Quantized-Delta-Varint geometry codec.

Replaces WKB for polygon storage:
  - Coordinates quantized to 24-bit integers along each axis (resolution
    360°/2^24 ≈ 2.4 m at the equator, ≈ 1 m at 65° latitude).
  - Within each ring, vertices are stored as deltas from the previous vertex,
    ZigZag-varint encoded. The first vertex's delta is from (0, 0).
  - Same idea Mapbox vector tiles use; ~10× smaller than WKB, fully invertible
    (modulo quantization).

Schema (all varints; deltas are ZigZag):
  varint num_polygons
  per polygon:
    varint num_rings
    per ring:
      varint num_points
      per point: zigzag_varint dx, zigzag_varint dy
"""
from __future__ import annotations

QDV_BITS = 28
QDV_SCALE = (1 << QDV_BITS) / 360.0   # 28-bit ≈ 745654 / degree → 15cm grid
QDV_X_OFFSET = 180.0
QDV_Y_OFFSET = 90.0


_QDV_MAX = 1 << QDV_BITS


def _q(deg: float, offset: float) -> int:
    v = int((deg + offset) * QDV_SCALE)
    if v < 0:
        return 0
    if v >= _QDV_MAX:
        return _QDV_MAX - 1
    return v


def _unq(q: int, offset: float) -> float:
    return q / QDV_SCALE - offset


def _emit_varint(out: bytearray, n: int) -> None:
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)


def _emit_zigzag(out: bytearray, n: int) -> None:
    _emit_varint(out, (n << 1) ^ (n >> 63))


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    n = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return n, pos
        shift += 7


def _read_zigzag(buf: bytes, pos: int) -> tuple[int, int]:
    v, pos = _read_varint(buf, pos)
    return (v >> 1) ^ -(v & 1), pos


def _ring_coords(coords) -> list[tuple[float, float]]:
    # shapely 2 ring coords return a CoordinateSequence with (x, y) tuples.
    return [(x, y) for x, y in coords]


def encode_geometry(geom) -> bytes:
    """Encode a shapely Polygon or MultiPolygon. Returns empty bytes for any
    non-polygon geometry (Point, LineString, …)."""
    out = bytearray()
    if geom.geom_type == "MultiPolygon":
        polys = list(geom.geoms)
    elif geom.geom_type == "Polygon":
        polys = [geom]
    else:
        return b""
    _emit_varint(out, len(polys))
    for p in polys:
        rings = [_ring_coords(p.exterior.coords)]
        for h in p.interiors:
            rings.append(_ring_coords(h.coords))
        _emit_varint(out, len(rings))
        for ring in rings:
            _emit_varint(out, len(ring))
            px = py = 0
            for x, y in ring:
                qx = _q(x, QDV_X_OFFSET)
                qy = _q(y, QDV_Y_OFFSET)
                _emit_zigzag(out, qx - px)
                _emit_zigzag(out, qy - py)
                px, py = qx, qy
    return bytes(out)


def decode_geometry(buf: bytes) -> list[list[list[tuple[float, float]]]]:
    """Decode to nested rings: [polygon[ring[(lon, lat), ...]]]."""
    if not buf:
        return []
    pos = 0
    out = []
    np_, pos = _read_varint(buf, pos)
    for _ in range(np_):
        rings = []
        nr, pos = _read_varint(buf, pos)
        for _ in range(nr):
            n, pos = _read_varint(buf, pos)
            ring = []
            px = py = 0
            for _ in range(n):
                dx, pos = _read_zigzag(buf, pos)
                dy, pos = _read_zigzag(buf, pos)
                px += dx
                py += dy
                ring.append((_unq(px, QDV_X_OFFSET), _unq(py, QDV_Y_OFFSET)))
            rings.append(ring)
        out.append(rings)
    return out


def point_in_qdv(buf: bytes, lon: float, lat: float) -> bool:
    """Even-odd ray-cast point-in-polygon directly on the q24-encoded geometry,
    without materializing a full coordinate list. A point is inside the
    geometry iff it's inside an odd number of (outer ∪ inner) rings."""
    if not buf:
        return False
    qx_target = _q(lon, QDV_X_OFFSET)
    qy_target = _q(lat, QDV_Y_OFFSET)
    pos = 0
    inside = False
    np_, pos = _read_varint(buf, pos)
    for _ in range(np_):
        nr, pos = _read_varint(buf, pos)
        for _ in range(nr):
            n, pos = _read_varint(buf, pos)
            # Walk the ring, ray-cast horizontally to the right.
            first_x = first_y = None
            px = py = 0
            prev_x = prev_y = None
            for i in range(n):
                dx, pos = _read_zigzag(buf, pos)
                dy, pos = _read_zigzag(buf, pos)
                px += dx
                py += dy
                if first_x is None:
                    first_x, first_y = px, py
                else:
                    if ((prev_y > qy_target) != (py > qy_target)) and \
                       (qx_target < (px - prev_x) * (qy_target - prev_y) /
                                    (py - prev_y) + prev_x):
                        inside = not inside
                prev_x, prev_y = px, py
            # Close the ring (last edge from prev → first).
            if first_x is not None and prev_x is not None and \
               (first_x != prev_x or first_y != prev_y):
                if ((prev_y > qy_target) != (first_y > qy_target)) and \
                   (qx_target < (first_x - prev_x) * (qy_target - prev_y) /
                                (first_y - prev_y) + prev_x):
                    inside = not inside
    return inside
