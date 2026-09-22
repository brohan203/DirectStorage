#!/usr/bin/env python3
"""Create a deterministic representative source set for Phase 2 Content Factory validation."""

from __future__ import annotations

import argparse
import shutil
import struct
from pathlib import Path

DDS_MAGIC = 0x20534444
FOURCC_DX10 = 0x30315844
FORMATS = {
    "bc1.dds": (0x31545844, 8),
    "bc3.dds": (0x35545844, 16),
    "bc4.dds": (0x31495441, 8),
    "bc5.dds": (0x32495441, 16),
}


def deterministic_bytes(size: int, seed: int) -> bytes:
    state = seed & 0xFFFFFFFF
    result = bytearray(size)
    for index in range(size):
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        result[index] = state >> 24
    return bytes(result)


def make_dds(path: Path, fourcc: int, bytes_per_block: int, seed: int) -> None:
    width, height, mip_count = 19, 13, 4
    first_mip_size = ((width + 3) // 4) * ((height + 3) // 4) * bytes_per_block
    data = bytearray(128 + first_mip_size)
    struct.pack_into("<I", data, 0, DDS_MAGIC)
    struct.pack_into("<I", data, 4, 124)
    struct.pack_into("<I", data, 12, height)
    struct.pack_into("<I", data, 16, width)
    struct.pack_into("<I", data, 28, mip_count)
    struct.pack_into("<I", data, 76, 32)
    struct.pack_into("<I", data, 80, 0x4)
    struct.pack_into("<I", data, 84, fourcc)
    data[128:] = deterministic_bytes(first_mip_size, seed)
    path.write_bytes(data)


def make_bc7_dds(path: Path, seed: int) -> None:
    width, height, mip_count = 19, 13, 4
    first_mip_size = ((width + 3) // 4) * ((height + 3) // 4) * 16
    data = bytearray(148 + first_mip_size)
    struct.pack_into("<I", data, 0, DDS_MAGIC)
    struct.pack_into("<I", data, 4, 124)
    struct.pack_into("<I", data, 12, height)
    struct.pack_into("<I", data, 16, width)
    struct.pack_into("<I", data, 28, mip_count)
    struct.pack_into("<I", data, 76, 32)
    struct.pack_into("<I", data, 80, 0x4)
    struct.pack_into("<I", data, 84, FOURCC_DX10)
    struct.pack_into("<IIIII", data, 128, 98, 3, 0, 1, 0)
    data[148:] = deterministic_bytes(first_mip_size, seed)
    path.write_bytes(data)


def generate(root: Path, set_name: str, repository_root: Path, overwrite: bool) -> Path:
    destination = root.resolve() / "originals" / set_name
    if destination.exists() and not overwrite:
        raise FileExistsError(f"source set already exists: {destination}; use --overwrite")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    real_asset = repository_root.resolve() / "Samples" / "BulkLoadDemo" / "BulkLoadDemo" / "SampleModel" / "Avocado.bin"
    if not real_asset.is_file():
        raise FileNotFoundError(f"representative repository asset not found: {real_asset}")
    shutil.copyfile(real_asset, destination / "avocado.bin")
    (destination / "chunked.bin").write_bytes(deterministic_bytes(192 * 1024 + 137, 0xC0FFEE))
    for index, (name, (fourcc, bytes_per_block)) in enumerate(FORMATS.items(), start=1):
        make_dds(destination / name, fourcc, bytes_per_block, 0xABC000 + index)
    make_bc7_dds(destination / "bc7.dds", 0xABC007)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("set_name", nargs="?", default="phase2-representative")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        output = generate(args.root, args.set_name, args.repository_root, args.overwrite)
        print(f"Generated {output}")
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
