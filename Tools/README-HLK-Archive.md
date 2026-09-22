# HLK-compatible archive tools

These Python tools create and inspect the compact archive format used by the internal DirectStorage HLK content generator.

## Create

Prepare a JSON manifest. Entry order is significant because the HLK archive stores no logical names:

```json
{
  "entries": [
    { "path": "texture.bin", "content_type": "texture" },
    { "path": "geometry.bin", "content_type": "geometry" },
    { "path": "text.bin", "content_type": "text" }
  ]
}
```

Then run:

```powershell
python Tools\create_hlk_archive.py manifest.json content.bin `
  --source-root path\to\payloads `
  --alignment 4096
```

Content types can be written as `unknown`, `texture`, `geometry`, or `text`; numeric values `0` through `3` are also accepted. The writer rejects unsafe paths, duplicate source paths, invalid types, invalid alignment, and archives that exceed the format's 32-bit offset/size limit.

```powershell
python Tools\validate_hlk_archive.py content.bin `
  --extract extracted `
  --report report.json
```

Validation checks version, entry type, table bounds, ordered non-overlapping payload ranges, and archive bounds. Extraction records a SHA-256 for each payload.

For a Content Factory set, use `Tools\archive_set.py`. It selects validated derivatives from the consolidated manifest, applies default or overridden content types, creates the archive, parses it back, and byte-checks every payload against the derivative hash before atomically updating the archive and manifest.

```powershell
python Tools\archive_set.py <set-name> --root C:\content-factory
```

## Compatibility scope

The HLK format consists of an 8-byte header followed by 12-byte entries and raw payloads. It stores no file names, compression format, uncompressed size, or transform metadata. Those values remain external—in the manifest and test scenario—so this tooling intentionally does not treat the format as a general-purpose package format.
