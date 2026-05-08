"""
Binary file format for the per-locale name overlay.

Same general shape as rtree_format.py — fixed-size pages, header at offset 0,
blobs section, then tree pages (leaves first, root last). The only structural
difference: the tree is 1-D (B+tree on osm_id) instead of 2-D (R-tree on bbox).

Layout:
  [header page]                    page 0,        page_size bytes
  [name blobs]                     packed UTF-8 strings, no length prefix
                                   (size lives in the leaf entry)
  [pad to next page boundary]
  [tree pages]                     leaves first, then level-1, ..., root last
  EOF

Header (first page_size bytes; only the first 64 are meaningful):
  offset  size  field
  0       8     magic = "DISTNAME"
  8       4     version (u32)
  12      4     page_size (u32)
  16      8     num_features (u64)
  24      8     num_pages (u64)
  32      8     root_offset (u64)
  40      8     blobs_offset (u64)
  48      8     blobs_size (u64)
  56      8     reserved

Page (page_size bytes):
  offset  size  field
  0       1     is_leaf (1 = leaf, 0 = internal)
  1       1     reserved
  2       2     entry_count (u16)
  4       4     reserved
  8       ...   entries[entry_count]
  ...     ...   padding to page_size

Internal entry (16 bytes):
  offset  size  field
  0       8     max_osm_id (u64)   max osm_id in the pointed-to subtree
  8       8     child_offset (u64) byte offset of the child page

Leaf entry (24 bytes):
  offset  size  field
  0       8     osm_id (u64)
  8       8     name_offset (u64)  absolute byte offset into blobs section
  16      4     name_size (u32)    UTF-8 byte length of the name
  20      4     reserved

Lookup (one osm_id):
  - Read root page
  - If internal: find the first entry with max_osm_id >= target → recurse
  - If leaf: scan entries (sorted by osm_id) for exact match
  - If found: range-fetch (name_offset, name_size) and decode UTF-8
  - If target > max of any entry along the way: no entry exists for this osm_id
"""
from __future__ import annotations

import struct

MAGIC = b"DISTNAME"
VERSION = 1
DEFAULT_PAGE_SIZE = 4096
PAGE_HEADER_SIZE = 8
INTERNAL_ENTRY_SIZE = 16
LEAF_ENTRY_SIZE = 24

HEADER_STRUCT = struct.Struct("<8sIIQQQQQ8x")           # 64 bytes
PAGE_HEADER_STRUCT = struct.Struct("<BBHI")             # 8 bytes
INTERNAL_ENTRY_STRUCT = struct.Struct("<QQ")            # 16 bytes
LEAF_ENTRY_STRUCT = struct.Struct("<QQII")              # 24 bytes


def internal_entries_per_page(page_size: int = DEFAULT_PAGE_SIZE) -> int:
    return (page_size - PAGE_HEADER_SIZE) // INTERNAL_ENTRY_SIZE


def leaf_entries_per_page(page_size: int = DEFAULT_PAGE_SIZE) -> int:
    return (page_size - PAGE_HEADER_SIZE) // LEAF_ENTRY_SIZE


def pack_header(*, page_size: int, num_features: int, num_pages: int,
                root_offset: int, blobs_offset: int,
                blobs_size: int) -> bytes:
    return HEADER_STRUCT.pack(
        MAGIC, VERSION, page_size,
        num_features, num_pages,
        root_offset, blobs_offset, blobs_size,
    )


def unpack_header(buf: bytes):
    (magic, version, page_size, num_features, num_pages,
     root_offset, blobs_offset, blobs_size) = HEADER_STRUCT.unpack_from(buf, 0)
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r}")
    if version != VERSION:
        raise ValueError(f"unsupported version {version}")
    return {
        "page_size": page_size,
        "num_features": num_features,
        "num_pages": num_pages,
        "root_offset": root_offset,
        "blobs_offset": blobs_offset,
        "blobs_size": blobs_size,
    }
