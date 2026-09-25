# GACL shuffle-only spike

This target proves that pinned Game Asset Conditioning Library transforms can build without the full project's DirectXTex, ONNX Runtime, or GoogleTest dependencies.

The spike covers the standard, size-preserving BC1, BC3, BC4, and BC5 version-1 transforms. For each format it:

1. Generates deterministic synthetic BC block data, including aligned and straggler-tail cases.
2. Calls the pinned GACL shuffle implementation.
3. Reverses the transform on the CPU and byte-compares it with the source.
4. Compresses and decompresses both source and shuffled streams with pinned Zstd.
5. Prints direct-Zstd and shuffle-plus-Zstd sizes.

Optional `<format> <file.dds>` pairs process the first mip of real BC1/3/4/5 DDS files with a minimal header reader, preserving independence from DirectXTex. Both DX10 headers and legacy DXT1/DXT5/BC4U/BC5U FourCC values are supported.

BC7 is intentionally separate. The pinned GACL source states that BC7 tries multiple transforms and retains the best compressed result, so it has no equivalent transform-only contract (`gacl/Lib/shuffle/bc7.h`). Experimental BC1/BC3 version-2 transforms are also outside this spike.

## Pinned dependencies

- GACL `ca03e5ece5951642a02fbcd20b28d1397735e17a`
- GACL Zstd submodule `f9938c217da17ec3e9dcd2a2d99c5cf39536aeb9`

Pass the pinned GACL checkout explicitly; the build never downloads a dependency. The target currently requires MSVC and the Windows SDK.

## Build

```powershell
$cmake = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'

& $cmake `
  -S ContentFactory/experiments/GACLShuffleSpike `
  -B out/gacl-shuffle-spike `
  -G 'Visual Studio 17 2022' `
  -A x64 `
  -DGACL_ROOT=C:\path\to\pinned\gacl

& $cmake --build out/gacl-shuffle-spike --config Release --target GACLShuffleSpike
.\out\gacl-shuffle-spike\Release\GACLShuffleSpike.exe

.\out\gacl-shuffle-spike\Release\GACLShuffleSpike.exe `
  BC1 C:\content\albedo.dds `
  BC3 C:\content\alpha.dds `
  BC4 C:\content\mask.dds `
  BC5 C:\content\normal.dds
```

A successful run ends with:

```text
GACL shuffle-only spike: PASS
```

Synthetic inputs are deliberately high entropy, so their ratios are only a correctness signal. Use representative DDS fixtures for compression measurements.
