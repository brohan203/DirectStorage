import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


FACTORY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = FACTORY_ROOT.parent
DRIVER = FACTORY_ROOT / "tools" / "process-set.py"
PROCESSOR = FACTORY_ROOT / "tools" / "gdeflate_compress.py"


class GDeflateCompressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        value = os.environ.get("GDEFLATE_CONTENT_TOOL")
        if not value:
            raise RuntimeError("Set GDEFLATE_CONTENT_TOOL to the built executable")
        cls.tool = Path(value).resolve()
        if not cls.tool.is_file():
            raise FileNotFoundError(cls.tool)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        sources = self.root / "originals" / "sample_set"
        (sources / "nested").mkdir(parents=True)
        (sources / "a.bin").write_bytes(b"gdeflate" * 1000)
        (sources / "nested" / "b.bin").write_bytes(bytes(range(256)) * 100)
        result = subprocess.run(
            [sys.executable, str(DRIVER), "sample_set", "--root", str(self.root)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self.temp.cleanup()

    def run_processor(self, *args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PROCESSOR), "sample_set", "--root", str(self.root),
             "--gdeflate-exe", str(self.tool), *map(str, args)],
            capture_output=True, text=True, check=False,
        )

    def test_generates_all_levels_and_updates_manifest(self):
        result = self.run_processor()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.root / "manifests" / "sample_set.json").read_text(encoding="utf-8"))
        records = [item for item in manifest["derivatives"] if item["format"] == "gdeflate"]
        self.assertEqual(len(records), 24)
        self.assertEqual({item["parameters"]["compression_level"] for item in records}, set(range(1, 13)))
        self.assertEqual(manifest["tools"]["GDeflateContentTool"]["version"], "1.0.0")
        for record in records:
            output = self.root / Path(record["path"])
            self.assertTrue(output.is_file())
            self.assertEqual(record["size"], output.stat().st_size)
            self.assertEqual(record["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            verify = subprocess.run([str(self.tool), "--verify", str(output)], capture_output=True, text=True)
            self.assertEqual(verify.returncode, 0, verify.stderr)
        verify = subprocess.run(
            [sys.executable, str(DRIVER), "sample_set", "--root", str(self.root), "--verify"],
            capture_output=True, text=True,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)

    def test_subset_is_deterministic_and_requires_overwrite(self):
        command = ("--levels", 1, 9, 12)
        self.assertEqual(self.run_processor(*command).returncode, 0)
        manifest_path = self.root / "manifests" / "sample_set.json"
        before_manifest = manifest_path.read_bytes()
        before = {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.root / "gdeflate" / "sample_set").rglob("*.gdeflate")
        }
        refused = self.run_processor(*command)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("use --overwrite", refused.stderr)
        self.assertEqual(self.run_processor(*command, "--overwrite").returncode, 0)
        self.assertEqual(before_manifest, manifest_path.read_bytes())
        after = {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.root / "gdeflate" / "sample_set").rglob("*.gdeflate")
        }
        self.assertEqual(before, after)

    def test_failed_generation_preserves_existing_state(self):
        self.assertEqual(self.run_processor("--levels", 1).returncode, 0)
        manifest_path = self.root / "manifests" / "sample_set.json"
        before_manifest = manifest_path.read_bytes()
        output = next((self.root / "gdeflate" / "sample_set").rglob("*.gdeflate"))
        before_output = output.read_bytes()
        bad = self.root / "bad-tool.cmd"
        bad.write_text("@echo off\r\nif \"%1\"==\"--version\" (echo BadTool 1.0.0 & exit /b 0)\r\nexit /b 7\r\n")
        result = subprocess.run(
            [sys.executable, str(PROCESSOR), "sample_set", "--root", str(self.root),
             "--gdeflate-exe", str(bad), "--levels", "1", "--overwrite"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before_manifest, manifest_path.read_bytes())
        self.assertEqual(before_output, output.read_bytes())

    def test_rejects_invalid_levels_and_empty_source(self):
        for levels in ((0,), (13,), (1, 1)):
            result = self.run_processor("--levels", *levels)
            self.assertNotEqual(result.returncode, 0)
        empty = self.root / "originals" / "sample_set" / "empty.bin"
        empty.write_bytes(b"")
        subprocess.run(
            [sys.executable, str(DRIVER), "sample_set", "--root", str(self.root), "--overwrite"],
            capture_output=True, text=True,
        )
        result = self.run_processor("--levels", 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be empty", result.stderr)


if __name__ == "__main__":
    unittest.main()
