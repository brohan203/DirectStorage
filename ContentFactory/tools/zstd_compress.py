#!/usr/bin/env python3
"""Generate deterministic multi-frame Zstd variants for one Content Factory set."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_BLOCK_SIZES_KB = (16,)
DEFAULT_CHUNK_SIZES_KB = (256,)
SHADER_BLOCK_SIZES_KB = (4, 8, 16)
SHADER_CHUNK_SIZES_KB = (64, 128, 256)


def load_driver():
    path = Path(__file__).with_name("process-set.py")
    spec = importlib.util.spec_from_file_location("process_set", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def run_zstd(arguments: list[str], *, data: bytes | None = None, timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(arguments, input=data, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"zstd timed out after {timeout} seconds") from error
    except OSError as error:
        raise RuntimeError(f"could not execute zstd: {error}") from error


def zstd_version(zstd_exe: Path) -> str:
    result = run_zstd([str(zstd_exe), "--version"], timeout=30)
    text = (result.stdout or result.stderr).decode("utf-8", "replace").strip()
    if result.returncode != 0:
        raise RuntimeError(f"zstd --version failed: {text}")
    match = re.search(r"\bv?(\d+\.\d+(?:\.\d+)?)\b", text)
    if not match:
        raise RuntimeError("zstd --version returned no recognizable version")
    return match.group(1)


def validate_sizes(values: list[int] | tuple[int, ...], name: str) -> tuple[int, ...]:
    if not values or any(type(value) is not int or value <= 0 for value in values):
        raise ValueError(f"{name} must contain positive integer KiB values")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} cannot contain duplicate values")
    return tuple(sorted(values))


def validate_matrix(block_sizes_kb: tuple[int, ...], chunk_sizes_kb: tuple[int, ...]) -> None:
    for block_kb in block_sizes_kb:
        for chunk_kb in chunk_sizes_kb:
            if block_kb > chunk_kb:
                raise ValueError(f"block size {block_kb} KiB cannot exceed chunk size {chunk_kb} KiB")


def output_relative_path(source: str, set_name: str, block_kb: int, chunk_kb: int) -> str:
    source_path = Path(source)
    filename = f"{source_path.name}-blk{block_kb}K-chunk{chunk_kb}K.zst"
    parent = source_path.parent.as_posix()
    return f"zstd/{set_name}/{parent + '/' if parent != '.' else ''}{filename}"


def compress_chunk(zstd_exe: Path, chunk: bytes, block_bytes: int) -> bytes:
    result = run_zstd([
        str(zstd_exe),
        f"--target-compressed-block-size={block_bytes}",
        f"--stream-size={len(chunk)}",
        "-q",
        "-c",
    ], data=chunk)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"zstd compression failed: {message}")
    if not result.stdout:
        raise RuntimeError("zstd compression produced an empty frame")
    return result.stdout


def decompress_stream(zstd_exe: Path, compressed: bytes) -> bytes:
    result = run_zstd([str(zstd_exe), "-q", "-d", "-c"], data=compressed)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"zstd verification failed: {message}")
    return result.stdout


def build_variants(
    root: Path,
    document: dict[str, Any],
    output_root: Path,
    zstd_exe: Path,
    block_sizes_kb: tuple[int, ...],
    chunk_sizes_kb: tuple[int, ...],
) -> list[dict[str, Any]]:
    set_name = document["set_name"]
    records: list[dict[str, Any]] = []
    for source in document["sources"]:
        source_path = root / "originals" / set_name / Path(source["path"])
        source_data = source_path.read_bytes()
        if not source_data:
            raise ValueError(f"Zstd source cannot be empty: {source['path']}")
        for chunk_kb in chunk_sizes_kb:
            chunk_bytes = chunk_kb * 1024
            chunks = [source_data[offset:offset + chunk_bytes] for offset in range(0, len(source_data), chunk_bytes)]
            for block_kb in block_sizes_kb:
                relative = output_relative_path(source["path"], set_name, block_kb, chunk_kb)
                output = output_root / Path(relative).relative_to(Path("zstd") / set_name)
                encoded = b"".join(compress_chunk(zstd_exe, chunk, block_kb * 1024) for chunk in chunks)
                if decompress_stream(zstd_exe, encoded) != source_data:
                    raise RuntimeError(f"Zstd round trip mismatch: {relative}")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(encoded)
                records.append({
                    "path": relative,
                    "size": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "source": source["path"],
                    "format": "zstd",
                    "parameters": {"block_size_kb": block_kb, "chunk_size_kb": chunk_kb},
                    "metadata": {"frame_count": len(chunks), "uncompressed_size": len(source_data)},
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
    lock = root / "manifests" / f".{set_name}.zstd.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise RuntimeError(f"another Zstd operation is active for set '{set_name}'") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def process(
    root: Path,
    set_name: str,
    zstd_exe: Path,
    block_sizes_kb: tuple[int, ...],
    chunk_sizes_kb: tuple[int, ...],
    overwrite: bool,
    allow_version_change: bool,
) -> Path:
    driver = load_driver()
    root = root.resolve()
    set_name = driver.validate_set_name(set_name)
    if zstd_exe.is_symlink():
        raise ValueError("zstd executable cannot be a symbolic link")
    zstd_exe = zstd_exe.resolve()
    if not zstd_exe.is_file():
        raise FileNotFoundError(f"zstd executable does not exist: {zstd_exe}")
    validate_matrix(block_sizes_kb, chunk_sizes_kb)
    current_version = zstd_version(zstd_exe)

    manifest = driver.manifest_path(root, set_name)
    with set_lock(root, set_name):
        document = driver.load_manifest(manifest)
        driver.verify_files(root, set_name, document)
        existing_zstd = [item for item in document["derivatives"] if item["format"] == "zstd"]
        recorded_version = document["tools"].get("zstd", {}).get("version")
        if existing_zstd and recorded_version != current_version and not allow_version_change:
            raise ValueError(
                f"Zstd version changed from {recorded_version!r} to {current_version!r}; use --allow-version-change"
            )

        final_root = root / "zstd" / set_name
        if final_root.is_symlink():
            raise ValueError(f"Zstd output root cannot be a symbolic link: {final_root}")
        if (existing_zstd or not directory_is_empty(final_root)) and not overwrite:
            raise FileExistsError("Zstd outputs already exist; use --overwrite")

        parent = root / "zstd"
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{set_name}.zstd.stage.", dir=parent))
        backup = Path(tempfile.mkdtemp(prefix=f".{set_name}.zstd.backup.", dir=parent))
        backup.rmdir()
        old_manifest = manifest.read_bytes()
        old_tree_moved = False
        new_tree_installed = False
        try:
            records = build_variants(root, document, stage, zstd_exe, block_sizes_kb, chunk_sizes_kb)
            updated = dict(document)
            updated["derivatives"] = sorted(
                [item for item in document["derivatives"] if item["format"] != "zstd"] + records,
                key=lambda item: item["path"],
            )
            updated["tools"] = dict(document["tools"])
            updated["tools"]["zstd"] = {"version": current_version}
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
                        f"Zstd commit failed ({original_error}); rollback also failed: {'; '.join(rollback_errors)}"
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
    parser.add_argument("--zstd-exe", type=Path, required=True)
    parser.add_argument(
        "--zstd-shader-matrix", action="store_true",
        help="explicitly generate all 4/8/16 KiB block by 64/128/256 KiB chunk variants",
    )
    parser.add_argument("--block-sizes-kb", nargs="+", type=int)
    parser.add_argument("--chunk-sizes-kb", nargs="+", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--allow-version-change", action="store_true")
    args = parser.parse_args()
    try:
        if args.zstd_shader_matrix and (args.block_sizes_kb is not None or args.chunk_sizes_kb is not None):
            raise ValueError("--zstd-shader-matrix cannot be combined with explicit Zstd sizes")
        block_sizes = (SHADER_BLOCK_SIZES_KB if args.zstd_shader_matrix
                       else args.block_sizes_kb or DEFAULT_BLOCK_SIZES_KB)
        chunk_sizes = (SHADER_CHUNK_SIZES_KB if args.zstd_shader_matrix
                       else args.chunk_sizes_kb or DEFAULT_CHUNK_SIZES_KB)
        output = process(
            args.root,
            args.set_name,
            args.zstd_exe,
            validate_sizes(block_sizes, "block sizes"),
            validate_sizes(chunk_sizes, "chunk sizes"),
            args.overwrite,
            args.allow_version_change,
        )
        print(f"Updated {output}")
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
