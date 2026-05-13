"""
Python reader for the per-locale .dn name overlay (B+tree on osm_id).
Mirrors package/src/names_overlay.js so the Python query CLI / eval can
produce the same translated names the browser does.
"""
from __future__ import annotations

import struct
from typing import Dict, Iterable

from names_format import (
    HEADER_STRUCT, INTERNAL_ENTRY_STRUCT, LEAF_ENTRY_STRUCT,
    PAGE_HEADER_STRUCT, MAGIC, unpack_header,
)


class NamesOverlay:
    """HTTP-Range or local-file reader for a packed `.dn` overlay."""

    def __init__(self, backend, meta: dict):
        self.backend = backend
        self.meta = meta
        self._root_cache: bytes | None = None

    @classmethod
    def open(cls, backend) -> "NamesOverlay":
        head = backend.read_range(0, HEADER_STRUCT.size)
        meta = unpack_header(head)
        return cls(backend, meta)

    def _root(self) -> bytes:
        if self._root_cache is None:
            self._root_cache = self.backend.read_range(
                self.meta["root_offset"], self.meta["page_size"])
        return self._root_cache

    def lookup_many(self, osm_ids: Iterable[int]) -> Dict[int, str]:
        """BFS by tree level, batching all lookups that hit the same page —
        same shape as the JS NamesOverlay.lookupMany."""
        ids = list(osm_ids)
        if not ids:
            return {}
        page_size = self.meta["page_size"]
        # frontier: page_offset -> list of target osm_ids
        frontier = {self.meta["root_offset"]: list(ids)}
        leaf_blobs: list[tuple[int, int, int]] = []  # (osm_id, off, size)
        while frontier:
            new_frontier: dict[int, list[int]] = {}
            for off, targets in frontier.items():
                page = (self._root()
                        if off == self.meta["root_offset"]
                        else self.backend.read_range(off, page_size))
                is_leaf, _, n, _ = PAGE_HEADER_STRUCT.unpack_from(page, 0)
                base = PAGE_HEADER_STRUCT.size
                if is_leaf:
                    # Sorted-by-osm_id linear scan per target.
                    for tgt in targets:
                        for i in range(n):
                            entry_off = base + i * LEAF_ENTRY_STRUCT.size
                            osm_id, name_off, name_size, _ = (
                                LEAF_ENTRY_STRUCT.unpack_from(page, entry_off))
                            if osm_id == tgt:
                                leaf_blobs.append((tgt, name_off, name_size))
                                break
                            if osm_id > tgt:
                                break
                else:
                    for tgt in targets:
                        for i in range(n):
                            entry_off = base + i * INTERNAL_ENTRY_STRUCT.size
                            max_id, child_off = (
                                INTERNAL_ENTRY_STRUCT.unpack_from(page, entry_off))
                            if max_id >= tgt:
                                new_frontier.setdefault(child_off, []).append(tgt)
                                break
                        # else: target larger than any subtree — no entry.
            frontier = new_frontier

        if not leaf_blobs:
            return {}
        out = {}
        for osm_id, off, size in leaf_blobs:
            out[osm_id] = self.backend.read_range(off, size).decode("utf-8")
        return out
