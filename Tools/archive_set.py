#!/usr/bin/env python3
"""Create and manifest a verified deterministic DirectStorage archive for one content set."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

CONTENT_TYPES = {"unknown": 0, "texture": 1, "geometry": 2, "text": 3}


def load_module(name: str, filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_content_types(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("content type mapping must be a JSON object")
    result: dict[str, str] = {}
    for derivative, content_type in document.items():
        if not isinstance(derivative, str) or not derivative:
            raise ValueError("content type mapping keys must be derivative paths")
        if content_type not in CONTENT_TYPES:
            raise ValueError(f"invalid content type for {derivative}: {content_type!r}")
        result[derivative] = content_type
    return result


def default_content_type(record: dict[str, Any]) -> str:
    return "texture" if record["format"] == "gacl" else "unknown"


def write_bytes_atomic(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
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
    lock = root / "manifests" / f".{set_name}.archive.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise RuntimeError(f"another archive operation is active for set '{set_name}'") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def verify_payloads(archive: Path, records: list[dict[str, Any]], archive_module: Any) -> None:
    entries, _ = archive_module.parse_archive(archive)
    if len(entries) != len(records):
        raise ValueError("archive entry count does not match selected derivatives")
    with archive.open("rb") as stream:
        for entry, record in zip(entries, records):
            stream.seek(entry.offset)
            payload = stream.read(entry.size)
            if len(payload) != record["size"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
                raise ValueError(f"archive payload does not match derivative: {record['path']}")


def process(
    root: Path,
    set_name: str,
    archive_name: str,
    formats: list[str],
    alignment: int,
    content_types_path: Path | None,
    overwrite: bool,
) -> Path:
    if not archive_name or Path(archive_name).name != archive_name or archive_name in {".", ".."}:
        raise ValueError("archive name must be a single safe file name")
    if not archive_name.endswith(".bin"):
        raise ValueError("archive name must end with .bin")
    if alignment <= 0 or alignment & (alignment - 1):
        raise ValueError("alignment must be a positive power of two")
    allowed_formats = {"zstd", "gdeflate", "gacl"}
    if not formats or set(formats) - allowed_formats:
        raise ValueError("formats must contain zstd, gdeflate, and/or gacl")

    driver = load_module("process_set_for_archive", "process-set.py")
    creator = load_module("create_hlk_archive_for_set", "create_hlk_archive.py")
    validator = load_module("validate_hlk_archive_for_set", "validate_hlk_archive.py")
    root = root.resolve()
    set_name = driver.validate_set_name(set_name)
    manifest_path = driver.manifest_path(root, set_name)
    overrides = load_content_types(content_types_path.resolve() if content_types_path else None)

    with set_lock(root, set_name):
        document = driver.load_manifest(manifest_path)
        driver.verify_files(root, set_name, document)
        records = [item for item in document["derivatives"] if item["format"] in set(formats)]
        if not records:
            raise ValueError("no derivatives match the requested archive formats")
        known = {item["path"] for item in records}
        unknown_overrides = sorted(set(overrides) - known)
        if unknown_overrides:
            raise ValueError(f"content type mapping references unselected derivatives: {', '.join(unknown_overrides)}")
        records.sort(key=lambda item: item["path"])
        entry_types = [overrides.get(item["path"], default_content_type(item)) for item in records]
        final_relative = f"dstorage/{set_name}/{archive_name}"
        final_path = root / final_relative
        old_record = next((item for item in document["archives"] if item["path"] == final_relative), None)
        if (old_record is not None or final_path.exists()) and not overwrite:
            raise FileExistsError("archive already exists; use --overwrite")
        if final_path.is_symlink():
            raise ValueError("archive output cannot be a symbolic link")

        archive_root = root / "dstorage"
        archive_root.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix=f".{set_name}.archive.stage.", dir=archive_root))
        staged_archive = stage_dir / archive_name
        input_manifest = stage_dir / "entries.json"
        input_manifest.write_text(json.dumps({"entries": [
            {"path": item["path"], "content_type": content_type}
            for item, content_type in zip(records, entry_types)
        ]}, indent=2) + "\n", encoding="utf-8")
        backup = final_path.with_name(f".{final_path.name}.backup")
        old_manifest = manifest_path.read_bytes()
        moved_old = False
        installed_new = False
        try:
            creator.create_archive(input_manifest, staged_archive, root, alignment)
            verify_payloads(staged_archive, records, validator)
            archive_data = staged_archive.read_bytes()
            archive_record = {
                "path": final_relative,
                "size": len(archive_data),
                "sha256": hashlib.sha256(archive_data).hexdigest(),
                "format": "dstorage",
                "entries": [
                    {"index": index, "derivative": item["path"], "content_type": content_type}
                    for index, (item, content_type) in enumerate(zip(records, entry_types))
                ],
                "validation": "pass",
            }
            updated = dict(document)
            updated["archives"] = sorted(
                [item for item in document["archives"] if item["path"] != final_relative] + [archive_record],
                key=lambda item: item["path"],
            )
            updated["tools"] = dict(document["tools"])
            updated["tools"]["create_hlk_archive"] = {"version": "1"}
            updated["tools"]["validate_hlk_archive"] = {"version": "1"}
            driver.validate_manifest(updated)

            final_path.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                backup.unlink()
            if final_path.exists():
                os.replace(final_path, backup)
                moved_old = True
            os.replace(staged_archive, final_path)
            installed_new = True
            try:
                driver.write_json_atomic(manifest_path, updated)
                driver.verify_files(root, set_name, updated)
            except Exception as original_error:
                rollback_errors: list[str] = []
                for action in (
                    lambda: final_path.unlink() if installed_new and final_path.exists() else None,
                    lambda: os.replace(backup, final_path) if moved_old and backup.exists() else None,
                    lambda: write_bytes_atomic(manifest_path, old_manifest),
                ):
                    try:
                        action()
                    except Exception as rollback_error:
                        rollback_errors.append(str(rollback_error))
                if rollback_errors:
                    raise RuntimeError(
                        f"archive commit failed ({original_error}); rollback also failed: {'; '.join(rollback_errors)}"
                    ) from original_error
                raise RuntimeError(f"archive commit failed and was rolled back: {original_error}") from original_error
            backup.unlink(missing_ok=True)
            return manifest_path
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)
            if backup.exists() and not final_path.exists():
                os.replace(backup, final_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("set_name")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--archive-name", default="content.bin")
    parser.add_argument("--formats", nargs="+", default=["zstd", "gdeflate", "gacl"])
    parser.add_argument("--alignment", type=int, default=16)
    parser.add_argument("--content-types", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        output = process(
            args.root, args.set_name, args.archive_name, args.formats,
            args.alignment, args.content_types, args.overwrite,
        )
        print(f"Updated {output}")
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
