#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
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


orchestrator = load("run_content_factory", "run-content-factory.py")
process_set = load("process_set_for_orchestration_tests", "process-set.py")


def fake_zstd(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    wrapper = root / "fake-zstd.cmd"
    wrapper.write_text(
        f'@echo off\r\n"{sys.executable}" "{Path(__file__).with_name("fake_zstd.py")}" %*\r\n',
        encoding="utf-8",
    )
    return wrapper


def fake_gdeflate(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    wrapper = root / "fake-gdeflate.cmd"
    wrapper.write_text(
        f'@echo off\r\n"{sys.executable}" "{Path(__file__).with_name("fake_gdeflate.py")}" %*\r\n',
        encoding="utf-8",
    )
    return wrapper


def arguments(root: Path, **overrides) -> Namespace:
    values = {
        "set_name": "sample",
        "root": root,
        "sources": None,
        "stages": ["zstd", "archive"],
        "overwrite": False,
        "zstd_exe": fake_zstd(root),
        "zstd_shader_matrix": False,
        "block_sizes_kb": [4],
        "chunk_sizes_kb": [64],
        "allow_version_change": False,
        "gdeflate_exe": None,
        "gdeflate_levels": [1],
        "gacl_exe": None,
        "gacl_zstd_level": 12,
        "gacl_target_block_size": 65536,
        "hlk_archive_prefix": "dstoragetest",
        "hlk_alignment": 1,
        "archive_name": "content.bin",
        "archive_formats": ["zstd"],
        "archive_alignment": 16,
        "content_types": None,
    }
    values.update(overrides)
    return Namespace(**values)


def initialize(root: Path) -> None:
    source = root / "originals" / "sample" / "data.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(bytes(range(251)) * 3)
    document = process_set.create_manifest(root, "sample")
    process_set.write_json_atomic(process_set.manifest_path(root, "sample"), document)


def test_runs_zstd_archive_and_final_verification(tmp_path: Path) -> None:
    initialize(tmp_path)
    manifest_path = orchestrator.run(arguments(tmp_path))
    document = process_set.load_manifest(manifest_path)
    process_set.verify_files(tmp_path, "sample", document)
    assert len(document["derivatives"]) == 1
    assert document["derivatives"][0]["format"] == "zstd"
    assert len(document["archives"]) == 1
    assert document["archives"][0]["entries"][0]["derivative"] == document["derivatives"][0]["path"]


def test_default_zstd_sizes_produce_one_variant_per_source(tmp_path: Path) -> None:
    initialize(tmp_path)
    manifest_path = orchestrator.run(arguments(
        tmp_path, stages=["zstd"], block_sizes_kb=None, chunk_sizes_kb=None))
    document = process_set.load_manifest(manifest_path)
    assert len(document["derivatives"]) == 1
    assert document["derivatives"][0]["parameters"]["block_size_kb"] == 16
    assert document["derivatives"][0]["parameters"]["chunk_size_kb"] == 256


def test_shader_matrix_requires_explicit_flag(tmp_path: Path) -> None:
    initialize(tmp_path)
    manifest_path = orchestrator.run(arguments(
        tmp_path, stages=["zstd"], block_sizes_kb=None, chunk_sizes_kb=None,
        zstd_shader_matrix=True))
    document = process_set.load_manifest(manifest_path)
    assert len(document["derivatives"]) == 9


def test_shader_matrix_rejects_custom_sizes(tmp_path: Path) -> None:
    initialize(tmp_path)
    with pytest.raises(ValueError, match="cannot be combined"):
        orchestrator.run(arguments(tmp_path, stages=["zstd"], zstd_shader_matrix=True))


def test_gacl_is_skipped_for_non_dds_set(tmp_path: Path) -> None:
    initialize(tmp_path)
    manifest_path = orchestrator.run(arguments(tmp_path, stages=["zstd", "gacl"], gacl_exe=None))
    document = process_set.load_manifest(manifest_path)
    assert {item["format"] for item in document["derivatives"]} == {"zstd"}


def test_all_requires_gdeflate_tool_after_zstd(tmp_path: Path) -> None:
    initialize(tmp_path)
    with pytest.raises(ValueError, match="--gdeflate-exe"):
        orchestrator.run(arguments(tmp_path, stages=["all"]))


def test_hlk_stage_creates_lockstep_archives(tmp_path: Path) -> None:
    initialize(tmp_path)
    manifest_path = orchestrator.run(arguments(
        tmp_path,
        stages=["hlk"],
        gdeflate_exe=fake_gdeflate(tmp_path),
    ))
    document = process_set.load_manifest(manifest_path)
    assert {record["payload_codec"] for record in document["archives"]} == {
        "uncompressed", "gdeflate", "zstd"
    }

def test_duplicate_and_mixed_all_stages_are_rejected(tmp_path: Path) -> None:
    initialize(tmp_path)
    with pytest.raises(ValueError, match="cannot be combined"):
        orchestrator.run(arguments(tmp_path, stages=["all", "zstd"]))
    with pytest.raises(ValueError, match="duplicates"):
        orchestrator.run(arguments(tmp_path, stages=["zstd", "zstd"]))


def test_missing_manifest_requires_sources(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="provide --sources"):
        orchestrator.run(arguments(tmp_path, sources=None, stages=["archive"]))


def test_sources_initialize_new_set_and_copy_files(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    first = external / "first.bin"
    second = external / "second.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    manifest_path = orchestrator.run(arguments(
        tmp_path,
        sources=[first, second],
        stages=["zstd"],
    ))
    document = process_set.load_manifest(manifest_path)
    assert [item["path"] for item in document["sources"]] == ["first.bin", "second.bin"]
    assert (tmp_path / "originals/sample/first.bin").read_bytes() == b"first"
    assert (tmp_path / "originals/sample/second.bin").read_bytes() == b"second"


def test_sources_reject_existing_set_and_duplicate_names(tmp_path: Path) -> None:
    initialize(tmp_path)
    external = tmp_path / "external.bin"
    external.write_bytes(b"external")
    with pytest.raises(FileExistsError, match="new content set"):
        orchestrator.run(arguments(tmp_path, sources=[external], stages=["zstd"]))

    other_root = tmp_path / "other"
    a = tmp_path / "a" / "same.bin"
    b = tmp_path / "b" / "same.bin"
    a.parent.mkdir(); b.parent.mkdir(); a.write_bytes(b"a"); b.write_bytes(b"b")
    with pytest.raises(ValueError, match="unique"):
        orchestrator.run(arguments(other_root, sources=[a, b], stages=["zstd"]))
