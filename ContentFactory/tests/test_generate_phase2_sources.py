#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

FACTORY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = FACTORY_ROOT.parent


def load_module():
    path = FACTORY_ROOT / "tools" / "generate-phase2-sources.py"
    spec = importlib.util.spec_from_file_location("generate_phase2_sources", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generator = load_module()


def hashes(path: Path) -> dict[str, str]:
    return {
        item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(path.rglob("*")) if item.is_file()
    }


def test_generates_deterministic_representative_sources(tmp_path: Path) -> None:
    first = generator.generate(tmp_path, "phase2", REPO_ROOT, False)
    first_hashes = hashes(first)
    assert set(first_hashes) == {
        "avocado.bin", "chunked.bin", "bc1.dds", "bc3.dds", "bc4.dds", "bc5.dds", "bc7.dds"
    }
    with pytest.raises(FileExistsError, match="--overwrite"):
        generator.generate(tmp_path, "phase2", REPO_ROOT, False)
    second = generator.generate(tmp_path, "phase2", REPO_ROOT, True)
    assert hashes(second) == first_hashes


def test_generated_dds_files_match_supported_formats(tmp_path: Path) -> None:
    output = generator.generate(tmp_path, "phase2", REPO_ROOT, False)
    gacl_path = FACTORY_ROOT / "tools" / "gacl_compress.py"
    spec = importlib.util.spec_from_file_location("gacl_for_generator_test", gacl_path)
    assert spec and spec.loader
    gacl = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gacl
    spec.loader.exec_module(gacl)
    assert {gacl.parse_dds_first_mip(path.read_bytes()).format for path in output.glob("*.dds")} == {
        "BC1", "BC3", "BC4", "BC5", "BC7"
    }
    for path in output.glob("*.dds"):
        assert any(path.read_bytes()[128:])
