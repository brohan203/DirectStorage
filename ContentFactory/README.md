# Content Factory tooling

The Phase 2 Content Factory prepares deterministic codec inputs and DirectStorage archives. The Content Factory workflow independently generates a synthetic representative set inside CI; production content stays outside CI.

## Folder layout

Use a format-first root with one matching directory per set:

```text
content-factory/
├── originals/<set-name>/
├── zstd/<set-name>/
├── gdeflate/<set-name>/
├── gacl/<set-name>/
├── dstorage/<set-name>/
└── manifests/<set-name>.json
```

Set names may contain letters, digits, `.`, `_`, and `-`. Source trees may contain nested directories; paths in manifests always use `/` separators and are sorted ordinally.

## Create a set manifest

Put source files under `originals/<set-name>/`, then run:

```powershell
python ContentFactory\tools\process-set.py <set-name> --root C:\content-factory
```

The driver inventories every source file, computes SHA-256 values, creates the format directories, validates the document, and atomically writes `manifests/<set-name>.json`.

The driver establishes the source and manifest contract. Codec stages append verified `derivatives` records for Zstd, GDeflate, and GACL, plus ordered `archives` records for DirectStorage packages.

By default, the Zstd stage produces one 16 KiB target-block / 256 KiB frame-chunk variant per source:

```powershell
python ContentFactory\tools\zstd_compress.py <set-name> `
  --root C:\content-factory `
  --zstd-exe C:\tools\zstd.exe
```

For sets specifically targeting the Zstd shader and metacommand matrix, explicitly request all 4/8/16 KiB block × 64/128/256 KiB chunk combinations with `--zstd-shader-matrix`. Alternatively supply `--block-sizes-kb` and `--chunk-sizes-kb` for selected combinations; the matrix flag and explicit sizes cannot be combined. The factory orchestrator accepts the same options. Each chunk is one Zstd frame; the concatenated stream is decoded and byte-compared with its source before transactional installation. Use `--overwrite` for a deterministic rebuild. A changed Zstd version requires the additional explicit `--allow-version-change` flag.

Generate GDeflate variants with the built content tool:

```powershell
python ContentFactory\tools\gdeflate_compress.py <set-name> `
  --root C:\content-factory `
  --gdeflate-exe C:\build\GDeflateContentTool.exe
```

By default, the processor creates levels 1 through 12 for every source. Use `--levels` to select a smaller production matrix. Each output is verified by the C++ tool, its 32-byte header is independently checked, and its header sizes and compression level are recorded in the consolidated manifest. Output-tree and manifest replacement is transactional; `--overwrite` is required to replace an existing GDeflate matrix.

Generate BC1/BC3/BC4/BC5/BC7 GACL derivatives with the reduced production tool:

```powershell
python ContentFactory\tools\gacl_compress.py <set-name> `
  --root C:\content-factory `
  --gacl-exe C:\build\GACLContentTool.exe
```

Phase 2 conditions array item 0 / mip 0 and records the DDS format, dimensions, transform identity/version, and constrained-Zstd settings in the manifest. Every raw `.gacl` stream is decompressed, reverse-transformed where applicable, and byte-compared before installation. BC7 uses production-supported transform ID 7 (`GACL_SHUFFLE_TRANSFORM_ZSTD_ONLY`); the pinned experimental BC7 split/join transforms remain deferred as a future compression optimization.

Create the spec-compatible HLK content triplet from the ordered source set:

```powershell
python ContentFactory\tools\hlk_content_set.py <set-name> `
  --root C:\content-factory `
  --zstd-exe C:\tools\zstd.exe `
  --gdeflate-exe C:\build\GDeflateContentTool.exe
```

This produces lockstep `dstoragetest.uncompressed`, `dstoragetest.gdeflate`, and `dstoragetest.zstd` files. Every Zstd entry is a standalone level-3 frame with a 256 KiB window for the Zstd metacommand's single-frame mode. Content types derive from source extensions. `archive_set.py` remains available for optional generic derivative packaging; mixed derivative archives are not HLK test content.

Run the complete workflow in dependency order:

```powershell
python ContentFactory\tools\run-content-factory.py <set-name> `
  --root C:\content-factory `
  --stages all `
  --zstd-exe C:\tools\zstd.exe `
  --gdeflate-exe C:\build\GDeflateContentTool.exe `
  --gacl-exe C:\build\GACLContentTool.exe
```

The orchestrator normally generates one 16/256 KiB Zstd configuration per source; add `--zstd-shader-matrix` only for Zstd shader/metacommand coverage sets.

Generate the deterministic representative source set with `ContentFactory\tools\generate-phase2-sources.py`. It combines the repository's `Avocado.bin`, a chunking workload, and BC1/BC3/BC4/BC5/BC7 DDS fixtures. Run `ContentFactory\tools\process-set.py <set-name> --root <root>` afterward to create its manifest before running the orchestrator.

Use `--overwrite` for intentional deterministic rebuilds. The manifest contains no timestamps and uses stable sorting and serialization, so unchanged sources produce byte-identical output.

## Verify a set

```powershell
python ContentFactory\tools\process-set.py <set-name> --root C:\content-factory --verify
```

Verification checks:

- Manifest schema version and strict structure
- Safe normalized relative paths
- Sorted, unique source inventory
- Current source sizes and SHA-256 values
- Derivative and archive references
- Recorded derivative/archive size and SHA-256 values

Verification fails if sources were added, removed, renamed, or modified after manifest creation.

## Manifest contract

`ContentFactory/tools/content-set-manifest.schema.json` is the machine-readable schema. Important sections are:

- `sources` — original relative path, byte size, and SHA-256
- `derivatives` — source relationship, output format, codec parameters, optional format metadata, and validation state
- `archives` — generic derivative archives or source-backed HLK triplets, including archive group, payload codec, ordered membership, and content type
- `tools` — tool versions and optional pinned revisions

GACL derivative metadata is expected to carry DXGI format, dimensions, array item, mip, transform ID/version/options, and Zstd settings. HLK-compatible archives do not store that metadata internally, so the manifest remains authoritative.

## Related tools

- `GDeflate/GDeflateContentTool` generates and verifies standalone GDeflate content.
- `ContentFactory/GACLContentTool` generates and verifies production BC1/3/4/5/BC7 GACL payloads.
- `ContentFactory/experiments/GACLShuffleSpike` retains the exploratory compression comparison harness.
- `ContentFactory/tools/hlk_content_set.py` creates the spec-compatible lockstep HLK archive triplet.
- `ContentFactory/tools/create_hlk_archive.py` is the deterministic low-level archive writer.
- `ContentFactory/tools/validate_hlk_archive.py` validates and extracts archive payloads.

## Just ask any agent

Ask an agent to create or verify a Content Factory set, inspect its manifest, run codec validation, or explain why a recorded file no longer matches its source.
