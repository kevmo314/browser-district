"""
Binary file format for the HTTP-range-friendly district R-tree.

Layout:
  [header page]                        page 0,        page_size bytes
  [features blob]                      protobuf records, length-prefixed
                                       (varint length + District message)
  [pad to next page boundary]
  [r-tree pages]                       leaves first, then level-1, ..., root last
  EOF

Why this order:
  - Header is a fixed-size first page; one range request gets you everything
    needed to start traversal (root_offset + page_size).
  - Features come before r-tree pages so leaf entries can reference absolute
    byte offsets of feature blobs that were written earlier.
  - R-tree pages are page-aligned and a fixed size, so a traversal step is
    always a single `Range: bytes=N-(N+page_size-1)` request.

Header (first page_size bytes; only the first 64 are meaningful):
  offset  size  field
  0       8     magic = "DISTRTRE"
  8       4     version (u32, little-endian)
  12      4     page_size (u32)
  16      8     num_features (u64)
  24      8     num_nodes (u64)
  32      8     root_offset (u64, absolute byte offset of root page)
  40      8     features_offset (u64)
  48      8     features_size (u64)
  56      8     reserved

Page (page_size bytes):
  offset  size  field
  0       1     is_leaf (1 = leaf, 0 = internal)
  1       1     reserved
  2       2     entry_count (u16)
  4       4     reserved
  8       ...   entries[entry_count], each ENTRY_SIZE bytes
  ...     ...   padding to page_size

Entry (32 bytes):
  offset  size  field
  0       4     min_lon (f32)  WGS84 degrees
  4       4     min_lat (f32)
  8       4     max_lon (f32)
  12      4     max_lat (f32)
  16      8     child_offset (u64)
                  - internal: byte offset of child page
                  - leaf:     byte offset of feature blob
  24      4     child_size (u32)
                  - internal: page_size (constant, included for symmetry)
                  - leaf:     length of feature blob in bytes
  28      4     reserved
"""
from __future__ import annotations

import struct

MAGIC = b"DISTRTRE"
VERSION = 1
DEFAULT_PAGE_SIZE = 4096
ENTRY_SIZE = 32
PAGE_HEADER_SIZE = 8
HEADER_STRUCT = struct.Struct("<8sIIQQQQQ8x")  # 64 bytes
ENTRY_STRUCT = struct.Struct("<ffffQII")        # 32 bytes
PAGE_HEADER_STRUCT = struct.Struct("<BBHI")     # 8 bytes


def entries_per_page(page_size: int = DEFAULT_PAGE_SIZE) -> int:
    return (page_size - PAGE_HEADER_SIZE) // ENTRY_SIZE


def pack_header(*, page_size: int, num_features: int, num_nodes: int,
                root_offset: int, features_offset: int,
                features_size: int) -> bytes:
    return HEADER_STRUCT.pack(
        MAGIC, VERSION, page_size,
        num_features, num_nodes,
        root_offset, features_offset, features_size,
    )


def unpack_header(buf: bytes):
    (magic, version, page_size, num_features, num_nodes,
     root_offset, features_offset, features_size) = HEADER_STRUCT.unpack_from(buf, 0)
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r}")
    if version != VERSION:
        raise ValueError(f"unsupported version {version}")
    return {
        "page_size": page_size,
        "num_features": num_features,
        "num_nodes": num_nodes,
        "root_offset": root_offset,
        "features_offset": features_offset,
        "features_size": features_size,
    }
