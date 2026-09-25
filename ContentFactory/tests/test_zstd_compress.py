import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


FACTORY_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = FACTORY_ROOT.parent
DRIVER = FACTORY_ROOT / "tools" / "process-set.py"
PROCESSOR = FACTORY_ROOT / "tools" / "zstd_compress.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class ZstdCompressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source_root = self.root / "originals" / "sample_set"
        (source_root / "nested").mkdir(parents=True)
        (source_root / "small.bin").write_bytes(bytes(range(251)) * 3)
        (source_root / "nested" / "large.bin").write_bytes(bytes(range(256)) * 400)
        self.fake_zstd = self.root / "fake-zstd.cmd"
        self.fake_zstd.write_text(
            f'@echo off\n"{sys.executable}" "{Path(__file__).with_name("fake_zstd.py")}" %*\n',
            encoding="utf-8", newline="\r\n")
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
             "--zstd-exe", str(self.fake_zstd), *map(str, args)],
            capture_output=True, text=True, check=False,
        )

    def test_default_produces_one_configuration_and_records_verified_metadata(self):
        result = self.run_processor()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.root / "manifests" / "sample_set.json").read_text(encoding="utf-8"))
        records = manifest["derivatives"]
        self.assertEqual(len(records), 2)
        self.assertEqual([record["path"] for record in records], sorted(record["path"] for record in records))
        self.assertEqual(manifest["tools"]["zstd"]["version"], "1.0")
        self.assertEqual(
            {(r["parameters"]["block_size_kb"], r["parameters"]["chunk_size_kb"]) for r in records},
            {(16, 256)},
        )
        for record in records:
            output = self.root / Path(record["path"])
            self.assertTrue(output.is_file())
            self.assertEqual(record["size"], output.stat().st_size)
            self.assertEqual(record["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(record["validation"], "pass")
        verify = subprocess.run(
            [sys.executable, str(DRIVER), "sample_set", "--root", str(self.root), "--verify"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)

    def test_full_matrix_requires_explicit_shader_flag(self):
        result = self.run_processor("--zstd-shader-matrix")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.root / "manifests" / "sample_set.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["derivatives"]), 18)
        self.assertEqual(
            {(r["parameters"]["block_size_kb"], r["parameters"]["chunk_size_kb"])
             for r in manifest["derivatives"]},
            {(block, chunk) for block in (4, 8, 16) for chunk in (64, 128, 256)},
        )

    def test_shader_matrix_rejects_explicit_sizes(self):
        result = self.run_processor("--zstd-shader-matrix", "--block-sizes-kb", 4)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be combined", result.stderr)

    def test_chunking_records_frame_count(self):
        result = self.run_processor("--block-sizes-kb", 4, "--chunk-sizes-kb", 64)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.root / "manifests" / "sample_set.json").read_text(encoding="utf-8"))
        by_source = {record["source"]: record for record in manifest["derivatives"]}
        self.assertEqual(by_source["small.bin"]["metadata"]["frame_count"], 1)
        self.assertEqual(by_source["nested/large.bin"]["metadata"]["frame_count"], 2)

    def test_refuses_existing_outputs_without_overwrite_and_is_deterministic(self):
        self.assertEqual(self.run_processor().returncode, 0)
        manifest_path = self.root / "manifests" / "sample_set.json"
        first_manifest = manifest_path.read_bytes()
        first_hashes = {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.root / "zstd" / "sample_set").rglob("*.zst")
        }
        result = self.run_processor()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("use --overwrite", result.stderr)
        self.assertEqual(self.run_processor("--overwrite").returncode, 0)
        self.assertEqual(first_manifest, manifest_path.read_bytes())
        second_hashes = {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.root / "zstd" / "sample_set").rglob("*.zst")
        }
        self.assertEqual(first_hashes, second_hashes)

    def test_failed_compression_preserves_existing_outputs_and_manifest(self):
        self.assertEqual(self.run_processor().returncode, 0)
        manifest_path = self.root / "manifests" / "sample_set.json"
        before_manifest = manifest_path.read_bytes()
        output = next((self.root / "zstd" / "sample_set").rglob("*.zst"))
        before_output = output.read_bytes()
        bad = self.root / "bad-zstd.cmd"
        bad.write_text("@echo off\r\nexit /b 9\r\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(PROCESSOR), "sample_set", "--root", str(self.root),
             "--zstd-exe", str(bad), "--overwrite"],
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before_manifest, manifest_path.read_bytes())
        self.assertEqual(before_output, output.read_bytes())

    def test_rejects_empty_sources_and_invalid_size_lists(self):
        module = load_module(PROCESSOR, "zstd_compress")
        with self.assertRaises(ValueError):
            module.validate_sizes([4, 4], "block sizes")
        with self.assertRaises(ValueError):
            module.validate_sizes([0], "chunk sizes")
        with self.assertRaises(ValueError):
            module.validate_matrix((128,), (64,))
        empty = self.root / "originals" / "sample_set" / "empty.bin"
        empty.write_bytes(b"")
        subprocess.run(
            [sys.executable, str(DRIVER), "sample_set", "--root", str(self.root), "--overwrite"],
            capture_output=True, text=True, check=False,
        )
        result = self.run_processor("--block-sizes-kb", 4, "--chunk-sizes-kb", 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be empty", result.stderr)


if __name__ == "__main__":
    unittest.main()
