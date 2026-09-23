# HLK-compatible archive tools

The internal DirectStorage HLK tests consume three lockstep archives generated from the same ordered source list:

- `dstoragetest.uncompressed`
- `dstoragetest.gdeflate`
- `dstoragetest.zstd`

Use the set-level generator:

```powershell
python Tools\hlk_content_set.py <set-name> `
  --root C:\content-factory `
  --zstd-exe C:\tools\zstd.exe `
  --gdeflate-exe C:\build\GDeflateContentTool.exe
```

The generator matches the internal `makehlkcontent` contract:

- identical entry count, order, and content type across all three files;
- source extensions map `.dds` to texture, `.ply` to geometry/model, `.txt` to text, and other files to unknown;
- GDeflate entries use BestRatio (level 12) and contain the raw DirectStorage stream without the standalone tool's 32-byte wrapper;
- Zstd entries are standalone level-3 frames with a 256 KiB window, matching the Zstd metacommand's single-frame mode;
- the resulting archives and their lockstep relationship are recorded in the consolidated manifest.

## Low-level writer and reader

`create_hlk_archive.py` writes one archive from an explicit JSON entry list. Entry order is significant because the format stores no logical names:

```json
{
  "entries": [
    { "path": "texture.bin", "content_type": "texture" },
    { "path": "geometry.bin", "content_type": "geometry" },
    { "path": "text.bin", "content_type": "text" }
  ]
}
```

```powershell
python Tools\create_hlk_archive.py manifest.json content.bin `
  --source-root path\to\payloads `
  --alignment 1
python Tools\validate_hlk_archive.py content.bin `
  --extract extracted `
  --report report.json
```

Content types may be `unknown`, `texture`, `geometry`, or `text`; numeric values 0 through 3 are also accepted. Validation checks version, entry type, table bounds, ordered non-overlapping payloads, and archive bounds.

## Generic derivative packaging

`archive_set.py` can package selected derivatives for diagnostics or transport. Its mixed derivative archive is not the HLK `dstoragetest` triplet.

## Compatibility evidence

The generated triplet was opened by the actual internal `ReadContents` implementation from `hlk/makehlkcontent/makehlkcontent.h`. Entry counts/types matched across all three files, and real Zstd/GDeflate payloads were independently decoded and compared with the uncompressed entries.

The format consists of an 8-byte header followed by 12-byte entries and raw payloads. It stores no file names, codec, uncompressed size, or transform metadata; those values remain external in the Content Factory manifest and test scenario.
