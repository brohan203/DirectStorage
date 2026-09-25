import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


FACTORY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = FACTORY_ROOT.parent
VALIDATOR = FACTORY_ROOT / "tools" / "validate_hlk_archive.py"
HEADER = struct.Struct("<II")
ENTRY = struct.Struct("<III")


class HlkArchiveValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_validator(self, archive: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(VALIDATOR), str(archive), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def make_archive(self, entries: list[tuple[int, bytes]]) -> Path:
        archive = self.root / "content.bin"
        offset = HEADER.size + len(entries) * ENTRY.size
        table = []
        payload = bytearray()
        for content_type, data in entries:
            table.append(ENTRY.pack(content_type, offset, len(data)))
            payload.extend(data)
            offset += len(data)
        archive.write_bytes(HEADER.pack(1, len(entries)) + b"".join(table) + payload)
        return archive

    def test_valid_archive_extracts_and_reports(self):
        archive = self.make_archive([(1, b"texture"), (2, b"geometry"), (3, b"text")])
        extracted = self.root / "extracted"
        report = self.root / "report.json"

        result = self.run_validator(archive, "--extract", str(extracted), "--report", str(report))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((extracted / "entry-0000.bin").read_bytes(), b"texture")
        self.assertEqual((extracted / "entry-0001.bin").read_bytes(), b"geometry")
        self.assertEqual((extracted / "entry-0002.bin").read_bytes(), b"text")
        parsed = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(parsed["entry_count"], 3)
        self.assertEqual(parsed["entries"][0]["offset"], HEADER.size + 3 * ENTRY.size)
        self.assertEqual(parsed["entries"][0]["size"], len(b"texture"))
        self.assertEqual(len(parsed["entries"][0]["sha256"]), 64)

    def test_rejects_truncated_table(self):
        archive = self.root / "truncated-table.bin"
        archive.write_bytes(HEADER.pack(1, 2) + ENTRY.pack(1, 32, 1))
        result = self.run_validator(archive)
        self.assertNotEqual(result.returncode, 0)

    def test_rejects_out_of_bounds_payload(self):
        archive = self.root / "bad-bounds.bin"
        archive.write_bytes(HEADER.pack(1, 1) + ENTRY.pack(1, 20, 100) + b"x")
        result = self.run_validator(archive)
        self.assertNotEqual(result.returncode, 0)

    def test_rejects_overlapping_entries(self):
        archive = self.root / "overlap.bin"
        table_end = HEADER.size + 2 * ENTRY.size
        archive.write_bytes(
            HEADER.pack(1, 2)
            + ENTRY.pack(1, table_end, 4)
            + ENTRY.pack(2, table_end + 2, 4)
            + b"abcdef"
        )
        result = self.run_validator(archive)
        self.assertNotEqual(result.returncode, 0)

    def test_rejects_invalid_version_and_type(self):
        invalid_version = self.root / "bad-version.bin"
        invalid_version.write_bytes(HEADER.pack(2, 0))
        self.assertNotEqual(self.run_validator(invalid_version).returncode, 0)

        invalid_type = self.root / "bad-type.bin"
        table_end = HEADER.size + ENTRY.size
        invalid_type.write_bytes(HEADER.pack(1, 1) + ENTRY.pack(99, table_end, 1) + b"x")
        self.assertNotEqual(self.run_validator(invalid_type).returncode, 0)


if __name__ == "__main__":
    unittest.main()
