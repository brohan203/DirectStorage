#!/usr/bin/env python3
"""Generate deterministic GDeflate variants for one Content Factory set."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_LEVELS = tuple(range(1, 13))
HEADER = struct.Struct("<IHHIIQQ")
MAGIC = 0x31464447
FILE_VERSION = 1


def load_driver():
    path = Path(__file__).with_name("process-set.py")
    spec = importlib.util.spec_from_file_location("process_set", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load Content Factory driver: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_tool(arguments: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"GDeflateContentTool timed out after {timeout} seconds") from error
    except OSError as error:
        raise RuntimeError(f"could not execute GDeflateContentTool: {error}") from error


def tool_version(executable: Path) -> str:
    result = run_tool([str(executable), "--version"], timeout=30)
    text = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(f"GDeflateContentTool --version failed: {text}")
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", text)
    if not match:
        raise RuntimeError("GDeflateContentTool --version returned no recognizable version")
    return match.group(1)


def validate_levels(values: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    if not values or any(type(value) is not int or value < 1 or value > 12 for value in values):
        raise ValueError("GDeflate levels must be unique integers from 1 through 12")
    if len(set(values)) != len(values):
        raise ValueError("GDeflate levels cannot contain duplicates")
    return tuple(sorted(values))


def output_relative_path(source: str, set_name: str, level: int) -> str:
    source_path = Path(source)
    filename = f"{source_path.name}-level{level}.gdeflate"
    parent = source_path.parent.as_posix()
    return f"gdeflate/{set_name}/{parent + '/' if parent != '.' else ''}{filename}"


def read_header(path: Path) -> dict[str, int]:
    data = path.read_bytes()
    if len(data) < HEADER.size:
        raise RuntimeError(f"GDeflate header is truncated: {path}")
    magic, version, header_size, level, reserved, uncompressed_size, compressed_size = HEADER.unpack_from(data)
    if magic != MAGIC or version != FILE_VERSION or header_size != HEADER.size or reserved != 0:
        raise RuntimeError(f"invalid GDeflate header: {path}")
    if level < 1 or level > 12:
        raise RuntimeError(f"invalid GDeflate level in header: {path}")
    if len(data) != header_size + compressed_size:
        raise RuntimeError(f"GDeflate file size does not match header: {path}")
    return {
        "level": level,
        "header_size": header_size,
        "uncompressed_size": uncompressed_size,
        "compressed_size": compressed_size,
    }


def build_variants(
    root: Path,
    document: dict[str, Any],
    output_root: Path,
    executable: Path,
    levels: tuple[int, ...],
) -> list[dict[str, Any]]:
    set_name = document["set_name"]
    records: list[dict[str, Any]] = []
    for source in document["sources"]:
        source_path = root / "originals" / set_name / Path(source["path"])
        if source["size"] == 0:
            raise ValueError(f"GDeflate source cannot be empty: {source['path']}")
        for level in levels:
            relative = output_relative_path(source["path"], set_name, level)
            output = output_root / Path(relative).relative_to(Path("gdeflate") / set_name)
            output.parent.mkdir(parents=True, exist_ok=True)
            result = run_tool([
                str(executable), "--input", str(source_path), "--output", str(output),
                "--level", str(level),
            ])
            if result.returncode != 0:
                raise RuntimeError(f"GDeflate generation failed: {(result.stderr or result.stdout).strip()}")
            verify = run_tool([str(executable), "--verify", str(output)])
            if verify.returncode != 0:
                raise RuntimeError(f"GDeflate verification failed: {(verify.stderr or verify.stdout).strip()}")
            header = read_header(output)
            if header["level"] != level or header["uncompressed_size"] != source["size"]:
                raise RuntimeError(f"GDeflate metadata mismatch: {relative}")
            data = output.read_bytes()
            records.append({
                "path": relative,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "source": source["path"],
                "format": "gdeflate",
                "parameters": {"compression_level": level},
                "metadata": {
                    "header_size": header["header_size"],
                    "compressed_size": header["compressed_size"],
                    "uncompressed_size": header["uncompressed_size"],
                },
                "validation": "pass",
            })
    return sorted(records, key=lambda item: item["path"])


def directory_is_empty(path: Path) -> bool:
    return not path.exists() or (path.is_dir() and not any(path.iterdir()))


def write_bytes_atomic(path: Path, data: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def set_lock(root: Path, set_name: str) -> Iterator[None]:
    lock = root / "manifests" / f".{set_name}.gdeflate.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise RuntimeError(f"another GDeflate operation is active for set '{set_name}'") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def process(root: Path, set_name: str, executable: Path, levels: tuple[int, ...], overwrite: bool) -> Path:
    driver = load_driver()
    root = root.resolve()
    set_name = driver.validate_set_name(set_name)
    if executable.is_symlink():
        raise ValueError("GDeflate executable cannot be a symbolic link")
    executable = executable.resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"GDeflate executable does not exist: {executable}")
    version = tool_version(executable)
    manifest = driver.manifest_path(root, set_name)

    with set_lock(root, set_name):
        document = driver.load_manifest(manifest)
        driver.verify_files(root, set_name, document)
        existing = [item for item in document["derivatives"] if item["format"] == "gdeflate"]
        final_root = root / "gdeflate" / set_name
        if final_root.is_symlink():
            raise ValueError(f"GDeflate output root cannot be a symbolic link: {final_root}")
        if (existing or not directory_is_empty(final_root)) and not overwrite:
            raise FileExistsError("GDeflate outputs already exist; use --overwrite")

        parent = root / "gdeflate"
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{set_name}.gdeflate.stage.", dir=parent))
        backup = Path(tempfile.mkdtemp(prefix=f".{set_name}.gdeflate.backup.", dir=parent))
        backup.rmdir()
        old_manifest = manifest.read_bytes()
        old_tree_moved = False
        new_tree_installed = False
        try:
            records = build_variants(root, document, stage, executable, levels)
            updated = dict(document)
            updated["derivatives"] = sorted(
                [item for item in document["derivatives"] if item["format"] != "gdeflate"] + records,
                key=lambda item: item["path"],
            )
            updated["tools"] = dict(document["tools"])
            updated["tools"]["GDeflateContentTool"] = {"version": version}
            driver.validate_manifest(updated)
            if final_root.exists():
                os.replace(final_root, backup)
                old_tree_moved = True
            os.replace(stage, final_root)
            new_tree_installed = True
            try:
                driver.write_json_atomic(manifest, updated)
                driver.verify_files(root, set_name, updated)
            except Exception as original_error:
                rollback_errors: list[str] = []
                try:
                    if new_tree_installed and final_root.exists():
                        shutil.rmtree(final_root)
                    if old_tree_moved and backup.exists():
                        os.replace(backup, final_root)
                    write_bytes_atomic(manifest, old_manifest)
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
                if rollback_errors:
                    raise RuntimeError(
                        f"GDeflate commit failed ({original_error}); rollback also failed: {'; '.join(rollback_errors)}"
                    ) from original_error
                raise
            shutil.rmtree(backup, ignore_errors=True)
            return manifest
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            if backup.exists() and not final_root.exists():
                os.replace(backup, final_root)
            elif backup.exists():
                shutil.rmtree(backup, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("set_name")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--gdeflate-exe", type=Path, required=True)
    parser.add_argument("--levels", nargs="+", type=int, default=list(DEFAULT_LEVELS))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        output = process(args.root, args.set_name, args.gdeflate_exe, validate_levels(args.levels), args.overwrite)
        print(f"Updated {output}")
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
