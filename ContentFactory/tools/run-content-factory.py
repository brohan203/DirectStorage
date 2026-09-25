#!/usr/bin/env python3
"""Run Content Factory inventory, derivative, archive, and verification stages in order."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any


def load_module(name: str, filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(args: argparse.Namespace) -> Path:
    driver = load_module("process_set_for_orchestration", "process-set.py")
    root = args.root.resolve()
    set_name = driver.validate_set_name(args.set_name)
    stages = args.stages
    if stages == ["all"]:
        stages = ["zstd", "gdeflate", "gacl", "hlk"]
    if "all" in stages:
        raise ValueError("all cannot be combined with other stages")
    if len(stages) != len(set(stages)):
        raise ValueError("stages cannot contain duplicates")

    manifest_path = driver.manifest_path(root, set_name)
    if args.sources is not None:
        if not args.sources:
            raise ValueError("--sources requires at least one file")
        source_root = root / "originals" / set_name
        if manifest_path.exists() or source_root.exists():
            raise FileExistsError("--sources can initialize only a new content set")
        names: set[str] = set()
        resolved_sources: set[Path] = set()
        for source in args.sources:
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"source must be a regular non-symlink file: {source}")
            resolved = source.resolve()
            if resolved in resolved_sources:
                raise ValueError(f"source paths must reference unique files: {source}")
            if source.name in names:
                raise ValueError(f"source file names must be unique: {source.name}")
            resolved_sources.add(resolved)
            names.add(source.name)
        source_root.mkdir(parents=True)
        try:
            for source in args.sources:
                shutil.copyfile(source, source_root / source.name)
            document = driver.create_manifest(root, set_name)
            driver.validate_manifest(document)
            driver.write_json_atomic(manifest_path, document)
        except Exception:
            shutil.rmtree(source_root, ignore_errors=True)
            raise
    elif not manifest_path.exists():
        raise FileNotFoundError("manifest does not exist; provide --sources to initialize the set")

    document = driver.load_manifest(manifest_path)
    driver.verify_files(root, set_name, document)
    if "zstd" in stages:
        if args.zstd_exe is None:
            raise ValueError("--zstd-exe is required for the zstd stage")
        module = load_module("zstd_for_orchestration", "zstd_compress.py")
        if args.zstd_shader_matrix and (args.block_sizes_kb is not None or args.chunk_sizes_kb is not None):
            raise ValueError("--zstd-shader-matrix cannot be combined with explicit Zstd sizes")
        block_sizes = (module.SHADER_BLOCK_SIZES_KB if args.zstd_shader_matrix
                       else args.block_sizes_kb or module.DEFAULT_BLOCK_SIZES_KB)
        chunk_sizes = (module.SHADER_CHUNK_SIZES_KB if args.zstd_shader_matrix
                       else args.chunk_sizes_kb or module.DEFAULT_CHUNK_SIZES_KB)
        manifest_path = module.process(
            root, set_name, args.zstd_exe,
            module.validate_sizes(block_sizes, "block sizes"),
            module.validate_sizes(chunk_sizes, "chunk sizes"),
            args.overwrite, args.allow_version_change,
        )
    if "gdeflate" in stages:
        if args.gdeflate_exe is None:
            raise ValueError("--gdeflate-exe is required for the gdeflate stage")
        module = load_module("gdeflate_for_orchestration", "gdeflate_compress.py")
        manifest_path = module.process(
            root, set_name, args.gdeflate_exe,
            module.validate_levels(args.gdeflate_levels), args.overwrite,
        )
    if "gacl" in stages:
        document = driver.load_manifest(manifest_path)
        has_dds = any(Path(item["path"]).suffix.lower() == ".dds" for item in document["sources"])
        if has_dds:
            if args.gacl_exe is None:
                raise ValueError("--gacl-exe is required when the set contains DDS sources")
            module = load_module("gacl_for_orchestration", "gacl_compress.py")
            manifest_path = module.process(
                root, set_name, args.gacl_exe,
                args.gacl_zstd_level, args.gacl_target_block_size, args.overwrite,
            )
    if "hlk" in stages:
        if args.zstd_exe is None or args.gdeflate_exe is None:
            raise ValueError("--zstd-exe and --gdeflate-exe are required for the hlk stage")
        module = load_module("hlk_for_orchestration", "hlk_content_set.py")
        manifest_path = module.process(
            root, set_name, args.hlk_archive_prefix,
            args.zstd_exe, args.gdeflate_exe, args.hlk_alignment, args.overwrite,
        )
    if "archive" in stages:
        module = load_module("archive_for_orchestration", "archive_set.py")
        manifest_path = module.process(
            root, set_name, args.archive_name, args.archive_formats,
            args.archive_alignment, args.content_types, args.overwrite,
        )

    document = driver.load_manifest(manifest_path)
    driver.verify_files(root, set_name, document)
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("set_name")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--sources", nargs="+", type=Path, metavar="FILE")
    parser.add_argument(
        "--stages", nargs="+",
        choices=["all", "zstd", "gdeflate", "gacl", "hlk", "archive"],
        default=["all"],
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--zstd-exe", type=Path)
    parser.add_argument(
        "--zstd-shader-matrix", action="store_true",
        help="explicitly generate all 4/8/16 KiB block by 64/128/256 KiB chunk Zstd variants",
    )
    parser.add_argument("--block-sizes-kb", nargs="+", type=int)
    parser.add_argument("--chunk-sizes-kb", nargs="+", type=int)
    parser.add_argument("--allow-version-change", action="store_true")
    parser.add_argument("--gdeflate-exe", type=Path)
    parser.add_argument("--gdeflate-levels", nargs="+", type=int, default=list(range(1, 13)))
    parser.add_argument("--gacl-exe", type=Path)
    parser.add_argument("--gacl-zstd-level", type=int, default=12)
    parser.add_argument("--gacl-target-block-size", type=int, default=64 * 1024)
    parser.add_argument("--hlk-archive-prefix", default="dstoragetest")
    parser.add_argument("--hlk-alignment", type=int, default=1)
    parser.add_argument("--archive-name", default="content.bin")
    parser.add_argument("--archive-formats", nargs="+", default=["zstd", "gdeflate", "gacl"])
    parser.add_argument("--archive-alignment", type=int, default=16)
    parser.add_argument("--content-types", type=Path)
    args = parser.parse_args()
    try:
        output = run(args)
        print(f"Verified {output}")
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
