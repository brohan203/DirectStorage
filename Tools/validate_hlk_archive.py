#!/usr/bin/env python3
"""Validate and optionally extract a DirectStorage HLK content archive.

The format is defined by the internal DirectStorage HLK makehlkcontent tool:
8-byte header (<version, entry count>) followed by 12-byte entries
(<content type, payload offset, payload size>) and payload bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path

HEADER = struct.Struct("<II")
ENTRY = struct.Struct("<III")
VERSION = 1
VALID_TYPES = {0, 1, 2, 3}


@dataclass(frozen=True)
class ArchiveEntry:
    index: int
    content_type: int
    offset: int
    size: int


def parse_archive(path: Path) -> tuple[list[ArchiveEntry], int]:
    file_size = path.stat().st_size
    with path.open("rb") as stream:
        raw_header = stream.read(HEADER.size)
        if len(raw_header) != HEADER.size:
            raise ValueError("archive header is truncated")
        version, count = HEADER.unpack(raw_header)
        if version != VERSION:
            raise ValueError(f"unsupported archive version {version}")

        table_end = HEADER.size + count * ENTRY.size
        if table_end > file_size:
            raise ValueError("entry table extends beyond the archive")

        entries: list[ArchiveEntry] = []
        previous_end = table_end
        for index in range(count):
            raw_entry = stream.read(ENTRY.size)
            if len(raw_entry) != ENTRY.size:
                raise ValueError(f"entry {index} is truncated")
            content_type, offset, size = ENTRY.unpack(raw_entry)
            if content_type not in VALID_TYPES:
                raise ValueError(f"entry {index} has invalid content type {content_type}")
            if offset < previous_end:
                raise ValueError(f"entry {index} overlaps the archive header, table, or previous entry")
            if offset + size > file_size:
                raise ValueError(f"entry {index} extends beyond the archive")
            entries.append(ArchiveEntry(index, content_type, offset, size))
            previous_end = offset + size

    return entries, file_size


def extract_entries(archive: Path, entries: list[ArchiveEntry], output: Path) -> dict[int, tuple[str, str]]:
    output.mkdir(parents=True, exist_ok=True)
    extracted: dict[int, tuple[str, str]] = {}
    with archive.open("rb") as stream:
        for entry in entries:
            stream.seek(entry.offset)
            payload = stream.read(entry.size)
            if len(payload) != entry.size:
                raise ValueError(f"entry {entry.index} payload is truncated")
            output_path = output / f"entry-{entry.index:04d}.bin"
            output_path.write_bytes(payload)
            extracted[entry.index] = (hashlib.sha256(payload).hexdigest(), output_path.name)
    return extracted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--extract", type=Path, help="directory receiving raw entry payloads")
    parser.add_argument("--report", type=Path, help="optional JSON validation report")
    args = parser.parse_args()

    entries, file_size = parse_archive(args.archive)
    extracted = extract_entries(args.archive, entries, args.extract) if args.extract else {}
    report: dict[str, object] = {
        "schema_version": 1,
        "archive": str(args.archive),
        "archive_size": file_size,
        "entry_count": len(entries),
        "entries": [
            {
                **entry.__dict__,
                "sha256": extracted.get(entry.index, (None, None))[0],
                "output": extracted.get(entry.index, (None, None))[1],
            }
            for entry in entries
        ],
    }

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"Validated {len(entries)} entries in {args.archive} ({file_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
