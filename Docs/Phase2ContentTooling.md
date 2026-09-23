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

The internal DirectStorage source defines the Phase 2 HLK product contract:

- `hlk/makehlkcontent/main.cpp` writes three parallel files from one ordered source manifest: `dstoragetest.uncompressed`, `dstoragetest.gdeflate`, and `dstoragetest.zstd`.
- `hlk/makehlkcontent/makehlkcontent.h` defines the 8-byte header, 12-byte entry records, and the `ReadContents` reader used by HLK tests.
- `hlk/dstoragetest/main.cpp` loads the three files by matching index and content type. Zstd metacommand tests create the command with `ZSTD_META_COMMAND_FLAG_SINGLE_FRAME_MODE`, so every `.zstd` archive entry must be one standalone frame.
- `dstoragecore/lib/DStorageZstdCompressionCodec.cpp` uses level 3 and a 256 KiB window. `Tools/hlk_content_set.py` mirrors those settings with `--zstd=wlog=18`.

Implemented tools:

- `Tools/create_hlk_archive.py` creates deterministic archives from explicit entry manifests.
- `Tools/validate_hlk_archive.py` independently parses, validates, reports, and extracts archive entries.
- `Tools/hlk_content_set.py` builds the required lockstep uncompressed/GDeflate/Zstd triplet from the same sorted source list. It uses GDeflate BestRatio (level 12), strips the standalone tool header before packaging, and validates one-frame Zstd and raw GDeflate payloads against each source.
- `Tools/archive_set.py` remains an optional generic derivative packager. Its mixed derivative archives are not the HLK test-content triplet.

Compatibility is now verified directly against the internal reader: a C++ checker compiled with `hlk/makehlkcontent/makehlkcontent.h` called the actual `ReadContents` implementation and accepted all three generated files with matching lockstep entry types/counts. Real Zstd entries were decoded independently and real raw GDeflate entries were rewrapped and verified by `GDeflateContentTool`.

The BC7-inclusive representative set produced seven lockstep HLK entries. Archive sizes were 222,357 bytes uncompressed, 219,016 bytes GDeflate, and 219,071 bytes Zstd. The complete workflow contained 164 source, derivative, sidecar, archive, and manifest files; an overwrite rebuild reproduced all 164 byte-for-byte.

`makehlkcontent` intentionally reserves entry-table space before appending payloads, then rewrites the populated table at offset 8 after compression sizes and offsets are known. However, its placeholder write starts at `&codecEntries[c]` instead of `&codecEntries[0]`; for codec index 1 this reads past the vector while writing the full table. The Content Factory writer does not reproduce that undefined behavior—it writes a valid complete table directly.

The archive format remains a compact test format rather than a general-purpose self-describing package. Logical names, codec identifiers, uncompressed sizes, hashes, GACL transform metadata, and the archive-group relationship remain in the Content Factory manifest.

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
