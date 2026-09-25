from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
import sys
from pathlib import Path

import pytest

FACTORY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = FACTORY_ROOT.parent
TOOLS = FACTORY_ROOT / "tools"


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hlk = load("hlk_content_set", "hlk_content_set.py")
process_set = load("process_set_for_hlk_tests", "process-set.py")
reader = load("validate_hlk_archive_for_hlk_tests", "validate_hlk_archive.py")


def wrapper(root: Path, name: str, helper: str) -> Path:
    path = root / name
    path.write_text(f'@echo off\r\n"{sys.executable}" "{Path(__file__).with_name(helper)}" %*\r\n', encoding="utf-8")
    return path


def initialize(root: Path) -> dict[str, bytes]:
    source_root = root / "originals" / "sample"
    source_root.mkdir(parents=True)
    files = {
        "a.dds": b"texture",
        "b.ply": b"model-data",
        "c.txt": b"text-data",
        "d.bin": bytes(range(251)) * 3,
    }
    for name, data in files.items():
        (source_root / name).write_bytes(data)
    document = process_set.create_manifest(root, "sample")
    process_set.write_json_atomic(process_set.manifest_path(root, "sample"), document)
    return files


def archive_payloads(path: Path) -> tuple[list[int], list[bytes]]:
    entries, _ = reader.parse_archive(path)
    payloads = []
    with path.open("rb") as stream:
        for entry in entries:
            stream.seek(entry.offset)
            payloads.append(stream.read(entry.size))
    return [entry.content_type for entry in entries], payloads


def fake_zstd_decode(data: bytes) -> bytes:
    assert data[:4] == b"FZST"
    size = struct.unpack_from("<I", data, 4)[0]
    assert len(data) == 8 + size
    return data[8:]


def test_builds_lockstep_hlk_triplet_and_manifest(tmp_path: Path) -> None:
    originals = initialize(tmp_path)
    zstd = wrapper(tmp_path, "zstd.cmd", "fake_zstd.py")
    gdeflate = wrapper(tmp_path, "gdeflate.cmd", "fake_gdeflate.py")
    manifest_path = hlk.process(tmp_path, "sample", "dstoragetest", zstd, gdeflate, 1, False)
    document = process_set.load_manifest(manifest_path)
    process_set.verify_files(tmp_path, "sample", document)
    records = {item["payload_codec"]: item for item in document["archives"]}
    assert set(records) == {"uncompressed", "gdeflate", "zstd"}
    assert {item["archive_group"] for item in records.values()} == {"dstoragetest"}
    expected_sources = sorted(originals)
    expected_types = [1, 2, 3, 0]
    expected_payloads = [originals[name] for name in expected_sources]

    uncompressed_types, uncompressed = archive_payloads(tmp_path / records["uncompressed"]["path"])
    gdeflate_types, gdeflate_payloads = archive_payloads(tmp_path / records["gdeflate"]["path"])
    zstd_types, zstd_payloads = archive_payloads(tmp_path / records["zstd"]["path"])
    assert uncompressed_types == gdeflate_types == zstd_types == expected_types
    assert uncompressed == expected_payloads
    assert gdeflate_payloads == expected_payloads
    assert [fake_zstd_decode(payload) for payload in zstd_payloads] == expected_payloads
    for record in records.values():
        assert [entry["source"] for entry in record["entries"]] == expected_sources


def test_hlk_triplet_is_deterministic_and_requires_overwrite(tmp_path: Path) -> None:
    initialize(tmp_path)
    zstd = wrapper(tmp_path, "zstd.cmd", "fake_zstd.py")
    gdeflate = wrapper(tmp_path, "gdeflate.cmd", "fake_gdeflate.py")
    hlk.process(tmp_path, "sample", "dstoragetest", zstd, gdeflate, 1, False)
    root = tmp_path / "dstorage" / "sample"
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.glob("dstoragetest.*")}
    with pytest.raises(FileExistsError, match="--overwrite"):
        hlk.process(tmp_path, "sample", "dstoragetest", zstd, gdeflate, 1, False)
    hlk.process(tmp_path, "sample", "dstoragetest", zstd, gdeflate, 1, True)
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.glob("dstoragetest.*")}
    assert after == before


def test_content_type_mapping_matches_internal_hlk_contract() -> None:
    assert hlk.content_type("texture.DDS") == "texture"
    assert hlk.content_type("model.ply") == "geometry"
    assert hlk.content_type("readme.txt") == "text"
    assert hlk.content_type("blob.bin") == "unknown"
    assert hlk.content_type("readme.txt") == "text"
    assert hlk.content_type("blob.bin") == "unknown"


def test_manifest_rejects_incomplete_or_non_lockstep_hlk_groups(tmp_path: Path) -> None:
    initialize(tmp_path)
    zstd = wrapper(tmp_path, "zstd.cmd", "fake_zstd.py")
    gdeflate = wrapper(tmp_path, "gdeflate.cmd", "fake_gdeflate.py")
    manifest_path = hlk.process(tmp_path, "sample", "dstoragetest", zstd, gdeflate, 1, False)
    document = process_set.load_manifest(manifest_path)

    incomplete = json.loads(json.dumps(document))
    incomplete["archives"] = incomplete["archives"][:-1]
    with pytest.raises(ValueError, match="all three"):
        process_set.validate_manifest(incomplete)

    mismatch = json.loads(json.dumps(document))
    target = next(item for item in mismatch["archives"] if item["payload_codec"] == "zstd")
    target["entries"][0]["content_type"] = "unknown" if target["entries"][0]["content_type"] != "unknown" else "text"
    with pytest.raises(ValueError, match="not lockstep"):
        process_set.validate_manifest(mismatch)
