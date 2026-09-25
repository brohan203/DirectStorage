#!/usr/bin/env python3
"""Build the lockstep uncompressed, GDeflate, and Zstd archives consumed by DirectStorage HLK tests."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

CONTENT_TYPES = {"unknown": 0, "texture": 1, "geometry": 2, "text": 3}
TYPE_BY_EXTENSION = {
    ".dds": "texture",
    ".ply": "geometry",
    ".txt": "text",
}
GDEFLATE_HEADER = struct.Struct("<IHHIIQQ")
GDEFLATE_MAGIC = 0x31464447


def load_module(name: str, filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_tool(arguments: list[str], *, input_data: bytes | None = None, timeout: int = 300) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(arguments, input=input_data, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"tool timed out after {timeout} seconds: {arguments[0]}") from error
    except OSError as error:
        raise RuntimeError(f"could not execute tool {arguments[0]}: {error}") from error


def verify_tool(executable: Path, version_argument: str = "--version") -> str:
    if executable.is_symlink():
        raise ValueError(f"tool cannot be a symbolic link: {executable}")
    executable = executable.resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    result = run_tool([str(executable), version_argument], timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"tool version query failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode(errors="replace").strip()


def content_type(path: str) -> str:
    return TYPE_BY_EXTENSION.get(Path(path).suffix.lower(), "unknown")


def compress_zstd(executable: Path, source: bytes) -> bytes:
    result = run_tool([
        str(executable), "-3", "--zstd=wlog=18", f"--stream-size={len(source)}", "-q", "-c",
    ], input_data=source)
    if result.returncode != 0:
        raise RuntimeError(f"Zstd compression failed: {result.stderr.decode(errors='replace').strip()}")
    restored = run_tool([str(executable), "-q", "-d", "-c"], input_data=result.stdout)
    if restored.returncode != 0 or restored.stdout != source:
        raise RuntimeError("Zstd entry failed standalone-frame round-trip validation")
    return result.stdout


def compress_gdeflate(executable: Path, source_path: Path, temporary_root: Path) -> bytes:
    output = temporary_root / f"{hashlib.sha256(source_path.as_posix().encode()).hexdigest()}.gdeflate"
    result = run_tool([
        str(executable), "--input", str(source_path), "--output", str(output),
        "--level", "12", "--overwrite",
    ])
    if result.returncode != 0:
        raise RuntimeError(f"GDeflate compression failed: {result.stderr.decode(errors='replace').strip()}")
    verify = run_tool([str(executable), "--verify", str(output)])
    if verify.returncode != 0:
        raise RuntimeError(f"GDeflate verification failed: {verify.stderr.decode(errors='replace').strip()}")
    data = output.read_bytes()
    if len(data) < GDEFLATE_HEADER.size:
        raise RuntimeError("GDeflate output header is truncated")
    magic, version, header_size, level, reserved, original_size, compressed_size = GDEFLATE_HEADER.unpack_from(data)
    if (magic, version, header_size, level, reserved) != (GDEFLATE_MAGIC, 1, GDEFLATE_HEADER.size, 12, 0):
        raise RuntimeError("GDeflate output header does not match the HLK payload contract")
    payload = data[header_size:]
    if original_size != source_path.stat().st_size or compressed_size != len(payload):
        raise RuntimeError("GDeflate output sizes do not match the source or payload")
    return payload


def write_entry_manifest(path: Path, entries: list[dict[str, str]]) -> None:
    path.write_text(json.dumps({"entries": entries}, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def verify_archive_payloads(
    archive: Path,
    expected: list[bytes],
    archive_module: Any,
) -> None:
    entries, _ = archive_module.parse_archive(archive)
    if len(entries) != len(expected):
        raise RuntimeError("HLK archive entry count mismatch")
    with archive.open("rb") as stream:
        for index, (entry, payload) in enumerate(zip(entries, expected)):
            stream.seek(entry.offset)
            if stream.read(entry.size) != payload:
                raise RuntimeError(f"HLK archive payload mismatch at entry {index}")


@contextmanager
def set_lock(root: Path, set_name: str) -> Iterator[None]:
    lock = root / "manifests" / f".{set_name}.hlk.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise RuntimeError(f"another HLK operation is active for set '{set_name}'") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def process(
    root: Path,
    set_name: str,
    archive_prefix: str,
    zstd_executable: Path,
    gdeflate_executable: Path,
    alignment: int,
    overwrite: bool,
) -> Path:
    if not archive_prefix or Path(archive_prefix).name != archive_prefix or archive_prefix in {".", ".."}:
        raise ValueError("archive prefix must be a safe file name")
    if alignment <= 0 or alignment & (alignment - 1):
        raise ValueError("alignment must be a positive power of two")
    driver = load_module("process_set_for_hlk", "process-set.py")
    creator = load_module("create_hlk_archive_for_hlk", "create_hlk_archive.py")
    reader = load_module("validate_hlk_archive_for_hlk", "validate_hlk_archive.py")
    root = root.resolve()
    set_name = driver.validate_set_name(set_name)
    verify_tool(zstd_executable)
    verify_tool(gdeflate_executable)
    zstd_executable = zstd_executable.resolve()
    gdeflate_executable = gdeflate_executable.resolve()
    manifest_path = driver.manifest_path(root, set_name)

    with set_lock(root, set_name):
        document = driver.load_manifest(manifest_path)
        driver.verify_files(root, set_name, document)
        sources = sorted(document["sources"], key=lambda item: item["path"])
        output_root = root / "dstorage" / set_name
        output_root.mkdir(parents=True, exist_ok=True)
        filenames = {
            "uncompressed": f"{archive_prefix}.uncompressed",
            "gdeflate": f"{archive_prefix}.gdeflate",
            "zstd": f"{archive_prefix}.zstd",
        }
        final_paths = {codec: output_root / name for codec, name in filenames.items()}
        relative_paths = {codec: path.relative_to(root).as_posix() for codec, path in final_paths.items()}
        existing_records = {item["path"]: item for item in document["archives"]}
        if any(path.exists() or relative_paths[codec] in existing_records for codec, path in final_paths.items()) and not overwrite:
            raise FileExistsError("HLK content archives already exist; use --overwrite")

        stage = Path(tempfile.mkdtemp(prefix=f".{set_name}.hlk.stage.", dir=output_root))
        backups: dict[str, Path] = {}
        old_manifest = manifest_path.read_bytes()
        installed: list[Path] = []
        try:
            temporary_codec = stage / "codec"
            temporary_codec.mkdir()
            original_payloads: list[bytes] = []
            gdeflate_payloads: list[bytes] = []
            zstd_payloads: list[bytes] = []
            entry_specs: list[dict[str, str]] = []
            for source in sources:
                source_path = root / "originals" / set_name / source["path"]
                original = source_path.read_bytes()
                original_payloads.append(original)
                gdeflate_payloads.append(compress_gdeflate(gdeflate_executable, source_path, temporary_codec))
                zstd_payloads.append(compress_zstd(zstd_executable, original))
                entry_specs.append({"path": source["path"], "content_type": content_type(source["path"])})

            staged_paths: dict[str, Path] = {}
            payload_sets = {
                "uncompressed": original_payloads,
                "gdeflate": gdeflate_payloads,
                "zstd": zstd_payloads,
            }
            for codec, payloads in payload_sets.items():
                payload_root = stage / codec
                payload_root.mkdir()
                entries: list[dict[str, str]] = []
                for index, (source, payload, spec) in enumerate(zip(sources, payloads, entry_specs)):
                    payload_path = payload_root / f"{index:08d}.bin"
                    payload_path.write_bytes(payload)
                    entries.append({"path": payload_path.relative_to(stage).as_posix(), "content_type": spec["content_type"]})
                entry_manifest = stage / f"{codec}.json"
                write_entry_manifest(entry_manifest, entries)
                archive_path = stage / filenames[codec]
                creator.create_archive(entry_manifest, archive_path, stage, alignment)
                verify_archive_payloads(archive_path, payloads, reader)
                staged_paths[codec] = archive_path

            records = []
            for codec, archive_path in staged_paths.items():
                data = archive_path.read_bytes()
                records.append({
                    "path": relative_paths[codec],
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "format": "dstorage",
                    "archive_group": archive_prefix,
                    "payload_codec": codec,
                    "entries": [
                        {"index": index, "source": source["path"], "content_type": spec["content_type"]}
                        for index, (source, spec) in enumerate(zip(sources, entry_specs))
                    ],
                    "validation": "pass",
                })
            updated = dict(document)
            replaced = set(relative_paths.values())
            updated["archives"] = sorted(
                [item for item in document["archives"] if item["path"] not in replaced] + records,
                key=lambda item: item["path"],
            )
            updated["tools"] = dict(document["tools"])
            updated["tools"]["makehlkcontent-compatible"] = {"version": "1"}
            driver.validate_manifest(updated)

            try:
                for codec, final_path in final_paths.items():
                    if final_path.exists():
                        backup = stage / f"{codec}.backup"
                        os.replace(final_path, backup)
                        backups[codec] = backup
                    os.replace(staged_paths[codec], final_path)
                    installed.append(final_path)
                driver.write_json_atomic(manifest_path, updated)
                driver.verify_files(root, set_name, updated)
            except Exception as original_error:
                rollback_errors = []
                for path in installed:
                    try:
                        path.unlink(missing_ok=True)
                    except Exception as error:
                        rollback_errors.append(str(error))
                for codec, backup in backups.items():
                    try:
                        os.replace(backup, final_paths[codec])
                    except Exception as error:
                        rollback_errors.append(str(error))
                try:
                    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{manifest_path.name}.", dir=manifest_path.parent)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(old_manifest); stream.flush(); os.fsync(stream.fileno())
                    os.replace(temporary_name, manifest_path)
                except Exception as error:
                    rollback_errors.append(str(error))
                if rollback_errors:
                    raise RuntimeError(
                        f"HLK commit failed ({original_error}); rollback also failed: {'; '.join(rollback_errors)}"
                    ) from original_error
                raise RuntimeError(f"HLK commit failed and was rolled back: {original_error}") from original_error
            return manifest_path
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("set_name")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--archive-prefix", default="dstoragetest")
    parser.add_argument("--zstd-exe", type=Path, required=True)
    parser.add_argument("--gdeflate-exe", type=Path, required=True)
    parser.add_argument("--alignment", type=int, default=1)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        output = process(
            args.root, args.set_name, args.archive_prefix, args.zstd_exe,
            args.gdeflate_exe, args.alignment, args.overwrite,
        )
        print(f"Updated {output}")
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
