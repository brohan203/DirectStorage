#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "Tools"


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gacl = load("gacl_compress", "gacl_compress.py")
process_set = load("process_set_for_gacl", "process-set.py")


def make_dds(path: Path, *, fourcc: int = 0x31545844, dxgi: int | None = None) -> bytes:
    offset = 148 if dxgi is not None else 128
    bytes_per_block = 8
    if dxgi in (76, 77, 78, 82, 83, 84):
        bytes_per_block = 16
    data = bytearray(offset + bytes_per_block * 4)
    struct.pack_into("<I", data, 0, gacl.DDS_MAGIC)
    struct.pack_into("<I", data, 4, 124)
    struct.pack_into("<I", data, 12, 8)
    struct.pack_into("<I", data, 16, 8)
    struct.pack_into("<I", data, 28, 1)
    struct.pack_into("<I", data, 84, gacl.FOURCC_DX10 if dxgi is not None else fourcc)
    if dxgi is not None:
        struct.pack_into("<IIIII", data, 128, dxgi, 3, 0, 1, 0)
    for index in range(offset, len(data)):
        data[index] = (index * 13 + 7) & 0xFF
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return bytes(data)


def make_fake_tool(path: Path) -> None:
    helper = path.with_suffix(".py")
    helper.write_text(
        """import argparse
from pathlib import Path
p=argparse.ArgumentParser(add_help=False)
p.add_argument('--version', action='store_true')
p.add_argument('--verify', action='store_true')
p.add_argument('--input')
p.add_argument('--output')
p.add_argument('--original')
p.add_argument('--format')
p.add_argument('--zstd-level')
p.add_argument('--target-block-size')
a=p.parse_args()
if a.version:
    print('GACLContentTool 1.0.0')
elif a.verify:
    if Path(a.input).read_bytes() != b'GACL' + Path(a.original).read_bytes():
        raise SystemExit(1)
    print('GACL verification passed')
else:
    Path(a.output).write_bytes(b'GACL' + Path(a.input).read_bytes())
    print('transform_id=1')
""",
        encoding="utf-8",
    )
    path.write_text(f'@"{sys.executable}" "{helper}" %*\n', encoding="utf-8")


def initialize_set(root: Path, set_name: str = "textures") -> Path:
    source = root / "originals" / set_name / "image.dds"
    make_dds(source)
    manifest = process_set.create_manifest(root, set_name)
    process_set.write_json_atomic(process_set.manifest_path(root, set_name), manifest)
    return source


def test_parse_legacy_and_dx10_dds(tmp_path: Path) -> None:
    legacy = make_dds(tmp_path / "legacy.dds")
    info = gacl.parse_dds_first_mip(legacy)
    assert (info.format, info.format_name, info.width, info.height, info.data_size) == ("BC1", "DXT1", 8, 8, 32)

    dx10 = make_dds(tmp_path / "dx10.dds", dxgi=83)
    info = gacl.parse_dds_first_mip(dx10)
    assert (info.format, info.format_name, info.array_size, info.data_offset, info.data_size) == (
        "BC5", "DXGI_FORMAT_BC5_UNORM", 1, 148, 64
    )


def test_multi_mip_dds_extracts_only_first_mip(tmp_path: Path) -> None:
    path = tmp_path / "mips.dds"
    data = bytearray(make_dds(path))
    struct.pack_into("<I", data, 28, 4)
    data.extend(b"lower-mips")
    info = gacl.parse_dds_first_mip(bytes(data))
    assert info.mip_count == 4
    assert info.data_offset == 128
    assert info.data_size == 32


def test_dx10_array_records_array_size_but_extracts_item_zero(tmp_path: Path) -> None:
    path = tmp_path / "array.dds"
    data = bytearray(make_dds(path, dxgi=83))
    struct.pack_into("<I", data, 140, 3)
    info = gacl.parse_dds_first_mip(bytes(data))
    assert info.array_size == 3
    assert info.data_offset == 148
    assert info.data_size == 64


def test_bc7_is_explicitly_deferred(tmp_path: Path) -> None:
    data = make_dds(tmp_path / "bc7.dds", dxgi=98)
    with pytest.raises(ValueError, match="deferred to Phase 3"):
        gacl.parse_dds_first_mip(data)


def test_process_adds_verified_manifest_record(tmp_path: Path) -> None:
    initialize_set(tmp_path)
    tool = tmp_path / "fake_gacl.cmd"
    make_fake_tool(tool)
    manifest_path = gacl.process(tmp_path, "textures", tool, 12, 65536, False)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tools"]["GACLContentTool"]["version"] == "1.0.0"
    assert len(manifest["derivatives"]) == 1
    record = manifest["derivatives"][0]
    assert record["format"] == "gacl"
    assert record["parameters"] == {
        "texture_format": "BC1",
        "zstd_level": 12,
        "target_block_size": 65536,
        "transform_id": 1,
        "transform_name": "GACL_SHUFFLE_TRANSFORM_ZSTD_BC1_224",
        "transform_version": 1,
    }
    assert record["metadata"]["container"] == "raw_zstd_stream"
    assert record["metadata"]["array_item"] == 0
    assert record["metadata"]["mip"] == 0
    output = tmp_path / record["path"]
    assert output.read_bytes().startswith(b"GACL")
    process_set.verify_files(tmp_path, "textures", manifest)


def test_existing_outputs_require_overwrite(tmp_path: Path) -> None:
    initialize_set(tmp_path)
    tool = tmp_path / "fake_gacl.cmd"
    make_fake_tool(tool)
    gacl.process(tmp_path, "textures", tool, 12, 65536, False)
    with pytest.raises(FileExistsError, match="--overwrite"):
        gacl.process(tmp_path, "textures", tool, 12, 65536, False)
    gacl.process(tmp_path, "textures", tool, 13, 32768, True)
    manifest = process_set.load_manifest(process_set.manifest_path(tmp_path, "textures"))
    record = next(item for item in manifest["derivatives"] if item["format"] == "gacl")
    assert record["parameters"]["zstd_level"] == 13
    assert record["parameters"]["target_block_size"] == 32768


def test_unsupported_dds_leaves_manifest_and_tree_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "originals" / "textures" / "image.dds"
    make_dds(source, dxgi=98)
    manifest = process_set.create_manifest(tmp_path, "textures")
    process_set.write_json_atomic(process_set.manifest_path(tmp_path, "textures"), manifest)
    manifest_path = process_set.manifest_path(tmp_path, "textures")
    before = manifest_path.read_bytes()
    tool = tmp_path / "fake_gacl.cmd"
    make_fake_tool(tool)
    with pytest.raises(ValueError, match="deferred to Phase 3"):
        gacl.process(tmp_path, "textures", tool, 12, 65536, False)
    assert manifest_path.read_bytes() == before
    assert not (tmp_path / "gacl" / "textures").exists()


def test_non_dds_set_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "originals" / "textures" / "blob.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"data")
    manifest = process_set.create_manifest(tmp_path, "textures")
    process_set.write_json_atomic(process_set.manifest_path(tmp_path, "textures"), manifest)
    tool = tmp_path / "fake_gacl.cmd"
    make_fake_tool(tool)
    with pytest.raises(ValueError, match="no supported"):
        gacl.process(tmp_path, "textures", tool, 12, 65536, False)
