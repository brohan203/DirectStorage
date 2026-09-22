import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CREATOR = ROOT / "Tools" / "create_hlk_archive.py"
VALIDATOR = ROOT / "Tools" / "validate_hlk_archive.py"


class HlkArchiveCreatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        (self.sources / "texture.bin").write_bytes(b"texture-data")
        (self.sources / "geometry.bin").write_bytes(b"geometry-data")

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, script: Path, *args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True, check=False)

    def write_manifest(self, entries: list[dict[str, object]]) -> Path:
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        return manifest

    def test_create_validate_and_extract(self):
        manifest = self.write_manifest([
            {"path": "texture.bin", "content_type": "texture"},
            {"path": "geometry.bin", "content_type": "geometry"},
        ])
        archive = self.root / "content.bin"
        first = self.run_script(CREATOR, manifest, archive, "--source-root", self.sources, "--alignment", 16)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_bytes = archive.read_bytes()

        extracted = self.root / "extracted"
        report = self.root / "report.json"
        validation = self.run_script(VALIDATOR, archive, "--extract", extracted, "--report", report)
        self.assertEqual(validation.returncode, 0, validation.stderr)
        self.assertEqual((extracted / "entry-0000.bin").read_bytes(), b"texture-data")
        self.assertEqual((extracted / "entry-0001.bin").read_bytes(), b"geometry-data")
        parsed = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(parsed["entries"][0]["offset"] % 16, 0)
        self.assertEqual(parsed["entries"][1]["offset"] % 16, 0)

        second = self.run_script(CREATOR, manifest, archive, "--source-root", self.sources, "--alignment", 16)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first_bytes, archive.read_bytes())

    def test_rejects_path_traversal(self):
        outside = self.root / "outside.bin"
        outside.write_bytes(b"outside")
        manifest = self.write_manifest([{"path": "../outside.bin", "content_type": 1}])
        result = self.run_script(CREATOR, manifest, self.root / "content.bin", "--source-root", self.sources)
        self.assertNotEqual(result.returncode, 0)

    def test_rejects_duplicate_sources_and_invalid_types(self):
        duplicate = self.write_manifest([
            {"path": "texture.bin", "content_type": 1},
            {"path": "texture.bin", "content_type": 1},
        ])
        self.assertNotEqual(self.run_script(CREATOR, duplicate, self.root / "duplicate.bin", "--source-root", self.sources).returncode, 0)

        invalid = self.write_manifest([{"path": "texture.bin", "content_type": 99}])
        self.assertNotEqual(self.run_script(CREATOR, invalid, self.root / "invalid.bin", "--source-root", self.sources).returncode, 0)

    def test_rejects_non_power_of_two_alignment(self):
        manifest = self.write_manifest([{"path": "texture.bin", "content_type": 1}])
        result = self.run_script(CREATOR, manifest, self.root / "content.bin", "--source-root", self.sources, "--alignment", 3)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
