#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import importlib.util
import json
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


archive_set = load("archive_set", "archive_set.py")
process_set = load("process_set_for_archive_tests", "process-set.py")
validator = load("validate_hlk_archive_for_tests", "validate_hlk_archive.py")


def file_record(path: str, source: str, format_name: str, data: bytes) -> dict[str, object]:
    if format_name == "zstd":
        parameters = {"block_size_kb": 4, "chunk_size_kb": 64}
        metadata = {"frame_count": 1, "uncompressed_size": 4}
    elif format_name == "gdeflate":
        parameters = {"compression_level": 1}
        metadata = {"header_size": 32, "compressed_size": len(data) - 32, "uncompressed_size": 4}
    else:
        parameters = {
            "texture_format": "BC1",
            "zstd_level": 12,
            "target_block_size": 65536,
            "transform_id": 1,
            "transform_name": "GACL_SHUFFLE_TRANSFORM_ZSTD_BC1_224",
            "transform_version": 1,
        }
        metadata = {
            "dds_format": "DXT1", "width": 4, "height": 4, "mip_count": 1,
            "array_size": 1, "array_item": 0, "mip": 0,
            "uncompressed_size": 8, "compressed_size": len(data),
            "container": "raw_zstd_stream",
        }
    return {
        "path": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "source": source,
        "format": format_name,
        "parameters": parameters,
        "metadata": metadata,
        "validation": "pass",
    }


def initialize_set(root: Path) -> dict[str, object]:
    source_root = root / "originals" / "sample"
    source_root.mkdir(parents=True)
    (source_root / "source.bin").write_bytes(b"source")
    document = process_set.create_manifest(root, "sample")
    derivatives = [
        ("zstd/sample/z.bin.zst", "zstd", b"zstd-payload"),
        ("gdeflate/sample/g.bin.gdeflate", "gdeflate", b"H" * 32 + b"gdeflate-payload"),
        ("gacl/sample/t.dds.gacl", "gacl", b"gacl-payload"),
    ]
    for path, format_name, data in derivatives:
        output = root / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        document["derivatives"].append(file_record(path, "source.bin", format_name, data))
    document["derivatives"].sort(key=lambda item: item["path"])
    process_set.validate_manifest(document)
    process_set.write_json_atomic(process_set.manifest_path(root, "sample"), document)
    return document


def test_creates_verified_ordered_archive_and_manifest(tmp_path: Path) -> None:
    initialize_set(tmp_path)
    manifest_path = archive_set.process(
        tmp_path, "sample", "content.bin", ["zstd", "gdeflate", "gacl"], 16, None, False
    )
    document = process_set.load_manifest(manifest_path)
    process_set.verify_files(tmp_path, "sample", document)
    assert document["tools"]["create_hlk_archive"]["version"] == "1"
    record = document["archives"][0]
    assert record["path"] == "dstorage/sample/content.bin"
    assert [item["derivative"] for item in record["entries"]] == sorted(
        item["path"] for item in document["derivatives"]
    )
    assert [item["content_type"] for item in record["entries"]] == ["texture", "unknown", "unknown"]

    archive = tmp_path / record["path"]
    entries, _ = validator.parse_archive(archive)
    assert [item.content_type for item in entries] == [1, 0, 0]
    with archive.open("rb") as stream:
        for entry, manifest_entry in zip(entries, record["entries"]):
            derivative = next(item for item in document["derivatives"] if item["path"] == manifest_entry["derivative"])
            stream.seek(entry.offset)
            assert hashlib.sha256(stream.read(entry.size)).hexdigest() == derivative["sha256"]


def test_archive_is_deterministic_and_requires_overwrite(tmp_path: Path) -> None:
    initialize_set(tmp_path)
    archive_set.process(tmp_path, "sample", "content.bin", ["zstd", "gdeflate", "gacl"], 16, None, False)
    archive = tmp_path / "dstorage/sample/content.bin"
    first = archive.read_bytes()
    with pytest.raises(FileExistsError, match="--overwrite"):
        archive_set.process(tmp_path, "sample", "content.bin", ["zstd"], 16, None, False)
    archive_set.process(tmp_path, "sample", "content.bin", ["zstd", "gdeflate", "gacl"], 16, None, True)
    assert archive.read_bytes() == first


def test_content_type_overrides_and_format_filter(tmp_path: Path) -> None:
    initialize_set(tmp_path)
    mapping = tmp_path / "types.json"
    mapping.write_text(json.dumps({"zstd/sample/z.bin.zst": "text"}), encoding="utf-8")
    manifest_path = archive_set.process(tmp_path, "sample", "zstd.bin", ["zstd"], 8, mapping, False)
    record = process_set.load_manifest(manifest_path)["archives"][0]
    assert record["entries"] == [{
        "index": 0,
        "derivative": "zstd/sample/z.bin.zst",
        "content_type": "text",
    }]
    entries, _ = validator.parse_archive(tmp_path / record["path"])
    assert entries[0].content_type == 3


def test_unknown_override_is_rejected(tmp_path: Path) -> None:
    initialize_set(tmp_path)
    mapping = tmp_path / "types.json"
    mapping.write_text(json.dumps({"zstd/sample/missing.zst": "text"}), encoding="utf-8")
    with pytest.raises(ValueError, match="unselected derivatives"):
        archive_set.process(tmp_path, "sample", "bad.bin", ["zstd"], 16, mapping, False)


def test_no_matching_format_is_rejected(tmp_path: Path) -> None:
    initialize_set(tmp_path)
    document = process_set.load_manifest(process_set.manifest_path(tmp_path, "sample"))
    document["derivatives"] = [item for item in document["derivatives"] if item["format"] == "zstd"]
    process_set.write_json_atomic(process_set.manifest_path(tmp_path, "sample"), document)
    with pytest.raises(ValueError, match="no derivatives"):
        archive_set.process(tmp_path, "sample", "none.bin", ["gacl"], 16, None, False)


def test_commit_failure_reports_rollback_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    initialize_set(tmp_path)
    original_loader = archive_set.load_module

    def load_with_failure(name: str, filename: str):
        module = original_loader(name, filename)
        if filename == "process-set.py":
            original_write = module.write_json_atomic

            def fail_write(path: Path, document: dict[str, object]) -> None:
                if document.get("archives"):
                    raise OSError("manifest commit failure")
                original_write(path, document)

            module.write_json_atomic = fail_write
        return module

    monkeypatch.setattr(archive_set, "load_module", load_with_failure)
    monkeypatch.setattr(
        archive_set, "write_bytes_atomic",
        lambda *_: (_ for _ in ()).throw(OSError("manifest rollback failure")),
    )
    with pytest.raises(RuntimeError, match="rollback also failed"):
        archive_set.process(tmp_path, "sample", "content.bin", ["zstd"], 16, None, False)
