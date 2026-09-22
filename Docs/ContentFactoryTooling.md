# Content Factory tooling

The Phase 2 Content Factory prepares deterministic codec inputs and DirectStorage archives outside CI. CI consumes generated products; it does not regenerate them.

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
python Tools\process-set.py <set-name> --root C:\content-factory
```

The driver inventories every source file, computes SHA-256 values, creates the format directories, validates the document, and atomically writes `manifests/<set-name>.json`.

The driver establishes the source and manifest contract. Codec stages append verified `derivatives` records for Zstd, GDeflate, and GACL, plus ordered `archives` records for DirectStorage packages.

Generate the required Zstd 3×3 matrix with an explicit Zstd CLI:

```powershell
python Tools\zstd_compress.py <set-name> `
  --root C:\content-factory `
  --zstd-exe C:\tools\zstd.exe
```

The processor creates 4/8/16 KiB target-block variants crossed with 64/128/256 KiB frame chunks. Each chunk is one Zstd frame, the concatenated stream is decoded and byte-compared with its source before installation, and the output tree plus manifest are replaced transactionally. Use `--overwrite` for a deterministic rebuild. A changed Zstd version requires the additional explicit `--allow-version-change` flag.

Generate GDeflate variants with the built content tool:

```powershell
python Tools\gdeflate_compress.py <set-name> `
  --root C:\content-factory `
  --gdeflate-exe C:\build\GDeflateContentTool.exe
```

By default, the processor creates levels 1 through 12 for every source. Use `--levels` to select a smaller production matrix. Each output is verified by the C++ tool, its 32-byte header is independently checked, and its header sizes and compression level are recorded in the consolidated manifest. Output-tree and manifest replacement is transactional; `--overwrite` is required to replace an existing GDeflate matrix.

Generate BC1/BC3/BC4/BC5/BC7 GACL derivatives with the reduced production tool:

```powershell
python Tools\gacl_compress.py <set-name> `
  --root C:\content-factory `
  --gacl-exe C:\build\GACLContentTool.exe
```

Phase 2 conditions array item 0 / mip 0 and records the DDS format, dimensions, transform identity/version, and constrained-Zstd settings in the manifest. Every raw `.gacl` stream is decompressed, reverse-transformed where applicable, and byte-compared before installation. BC7 uses production-supported transform ID 7 (`GACL_SHUFFLE_TRANSFORM_ZSTD_ONLY`); the pinned experimental BC7 split/join transforms remain deferred as a future compression optimization.

Create a verified deterministic archive from manifest derivatives:

```powershell
python Tools\archive_set.py <set-name> `
  --root C:\content-factory `
  --formats zstd gdeflate gacl
```

GACL entries default to `texture`; other derivatives default to `unknown`. Use `--content-types <json>` to override individual derivative paths with `unknown`, `texture`, `geometry`, or `text`. Archive payloads are parsed back and byte-checked against derivative hashes before installation.

Run the complete workflow in dependency order:

```powershell
python Tools\run-content-factory.py <set-name> `
  --root C:\content-factory `
  --stages all `
  --zstd-exe C:\tools\zstd.exe `
  --gdeflate-exe C:\build\GDeflateContentTool.exe `
  --gacl-exe C:\build\GACLContentTool.exe
```

Generate the deterministic representative source set with `Tools\generate-phase2-sources.py`. It combines the repository's `Avocado.bin`, a chunking workload, and BC1/BC3/BC4/BC5/BC7 DDS fixtures.

Use `--overwrite` for intentional deterministic rebuilds. The manifest contains no timestamps and uses stable sorting and serialization, so unchanged sources produce byte-identical output.

## Verify a set

```powershell
python Tools\process-set.py <set-name> --root C:\content-factory --verify
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

`Tools/content-set-manifest.schema.json` is the machine-readable schema. Important sections are:

- `sources` — original relative path, byte size, and SHA-256
- `derivatives` — source relationship, output format, codec parameters, optional format metadata, and validation state
- `archives` — archive identity plus ordered derivative membership and content type
- `tools` — tool versions and optional pinned revisions

GACL derivative metadata is expected to carry DXGI format, dimensions, array item, mip, transform ID/version/options, and Zstd settings. HLK-compatible archives do not store that metadata internally, so the manifest remains authoritative.

## Related tools

- `GDeflate/GDeflateContentTool` generates and verifies standalone GDeflate content.
- `Tools/GACLContentTool` generates and verifies production BC1/3/4/5/BC7 GACL payloads.
- `Tools/GACLShuffleSpike` retains the exploratory compression comparison harness.
- `Tools/create_hlk_archive.py` creates deterministic HLK-compatible archives.
- `Tools/validate_hlk_archive.py` validates and extracts archive payloads.

## Just ask any agent

Ask an agent to create or verify a Content Factory set, inspect its manifest, run codec validation, or explain why a recorded file no longer matches its source.
