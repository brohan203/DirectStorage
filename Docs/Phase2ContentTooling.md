# DirectStorage infrastructure Phase 2 content tooling

This document records the implementation boundaries for the GDeflate processor, DirectStorage archive tooling, and GACL conditioning work. It cross-checks those tracks against the recovered infrastructure specification and current source trees.

## GDeflate processor

The recovered specification requires a `gdeflate/` subtree beside `originals/`, `zstd/`, `gacl/`, and `dstorage/`. The first implementation is `GDeflate/GDeflateContentTool`.

Key choices:

- Use the repository's GDeflate library directly.
- Support every GDeflate library level from 1 through 12. Levels 1, 9, and 12 correspond to the DirectStorage fastest, default, and best-ratio presets shown by `GDeflateDemo`.
- CPU-decompress and byte-compare every generated file.
- Use a fixed-width, versioned standalone header instead of the demo header's platform-dependent `size_t` field.
- Generate deterministic content and a machine-readable `variants.json` inventory.

The standalone format is an intermediate Content Factory representation. DirectStorage archive creation should consume the payload and metadata rather than treating the standalone header as an archive entry contract.

## DirectStorage archive tooling

The recovered specification requires DirectStorage archive creation through the HLK `makehlkcontent` path. Investigation of the internal DirectStorage source found:

- `hlk/makehlkcontent/main.cpp` implements the writer.
- `hlk/makehlkcontent/makehlkcontent.h` defines the on-disk header and entry records.
- `hlk/content/dstoragetest.manifest` defines the existing content matrix.

The current format is a compact test format rather than a general-purpose archive schema. Its header and entry records use fixed `uint32_t` fields, but it is order-sensitive and omits logical names, hashes, uncompressed sizes, codec identifiers, and transform metadata. The ABI-dependent `size_t` concern applies to the standalone GDeflate demo header, not the HLK records. The writer also contains a suspicious initial entry-table write before its final table rewrite.

Implemented compatibility tools:

- `Tools/create_hlk_archive.py` creates deterministic archives from explicit JSON manifests.
- `Tools/validate_hlk_archive.py` independently validates and optionally extracts archive entries.
- Tests cover valid extraction, deterministic rebuilds, traversal, duplicates, invalid types/version/alignment, truncation, overlap, and out-of-bounds payloads.

`Tools/archive_set.py` now integrates the compatibility writer with consolidated manifests. It selects validated Zstd, GDeflate, and GACL derivatives; maps GACL to the HLK `texture` type by default; supports explicit content-type overrides; parses the completed archive; and byte-checks each payload against the derivative hash before atomically installing the archive and manifest record.

The BC7-inclusive deterministic representative set produced 152 archive entries and a 4,606,128-byte archive. A complete overwrite rebuild reproduced all 162 source, derivative, sidecar, archive, and manifest files byte-for-byte.

Remaining archive compatibility checkpoint:

1. Run the internal HLK reader against an archive created by the compatibility writer. The internal reader source/binary is not present in the accessible DirectStorage, Samples, or pinned GACL checkouts.
2. Reproduce and fix or safely wrap the suspicious initial table write in `makehlkcontent` when that internal source is available.
3. Decide separately whether a versioned production archive format is warranted.

## GACL conditioning

The recovered Phase 2 specification defines `.gacl` as a shuffled payload followed by Zstd compression. Phase 3 separately needs shuffled but uncompressed inputs because DirectStorage applies the inverse transform after decompression. These are two stages of one pipeline, not competing definitions:

```text
DDS BC payload -> GACL shuffle -> shuffled intermediate -> Zstd -> .gacl
```

The full pinned build is blocked on these unavailable legacy packages:

- `directxtex_desktop_win10` 2025.7.10.1
- `Microsoft.googletest.v140.windesktop.msvcstl.static.rt-dyn` 1.8.1.7
- `Microsoft.ML.OnnxRuntime` 1.24.2

Core `gacllib` directly includes DirectXTex and ONNX Runtime headers.

`Tools/GACLShuffleSpike` now provides a deliberately reduced build target for the standard BC1, BC3, BC4, and BC5 shuffle sources. It builds those pinned GACL files and the pinned Zstd submodule without DirectXTex, ONNX Runtime, or GoogleTest. Its standalone harness performs byte-exact shuffle/reverse checks and verifies direct-Zstd and shuffle-plus-Zstd streams independently.

Validated locally:

- Reduced x64 Release target builds with MSVC/CMake.
- BC1, BC3, BC4, and BC5 synthetic block streams round-trip byte-for-byte.
- Both direct and shuffled streams round-trip through the pinned Zstd revision.

All 50 available BC1/3/4/5 first-mip DDS payloads from the pinned GACL InsectsDemo corpus round-trip byte-for-byte, including streams whose block count is not divisible by four. At Zstd level 12, aggregate measurements were:

| Format | Files | Direct Zstd | Shuffle + Zstd | Weighted delta |
|---|---:|---:|---:|---:|
| BC1 | 2 | 8,188,227 | 7,250,978 | -11.45% |
| BC3 | 41 | 75,495,468 | 65,369,325 | -13.41% |
| BC4 | 5 | 5,056,528 | 5,004,665 | -1.03% |
| BC5 | 2 | 25,479,421 | 24,102,238 | -5.41% |

Individual files do not all improve, especially for BC3 and BC4. Conditioning remains content-dependent and should be measured before selecting a transform.


Phase 2 includes BC7 through the pinned library's production-supported transform ID 7 (`GACL_SHUFFLE_TRANSFORM_ZSTD_ONLY`). BC7 output is therefore a constrained-Zstd stream with no experimental texture rearrangement. The pinned split/join transforms (IDs 5 and 6) remain explicitly experimental: the library removes them from `GROUP_ANY_SUPPORTED`, and mode-join metadata export is incomplete. They are deferred as a future compression optimization, not as a blocker for BC7 content support.

Phase 2 production support is implemented by `Tools/GACLContentTool` and `Tools/gacl_compress.py` for BC1, BC3, BC4, BC5, and BC7 array item 0 / mip 0. The stage records width, height, DDS format, mip/array metadata, selected transform identity/version, constrained-Zstd parameters, and compressed/uncompressed sizes in the consolidated manifest. Before installation, every raw `.gacl` stream is decompressed, reverse-transformed where applicable, and byte-compared with the original BC payload.

Current build blocker: the pinned Visual Studio solution references legacy NuGet dependencies (`directxtex_desktop_win10` 2025.7.10.1, GoogleTest 1.8.1.7, and ONNX Runtime 1.24.2) that are not available from the configured public NuGet feed. The core library project also directly includes DirectXTex and ONNX Runtime headers, so the solution cannot be treated as dependency-free without creating a deliberately reduced shuffle-only target.
