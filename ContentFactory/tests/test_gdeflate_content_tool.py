import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class GDeflateContentToolTests(unittest.TestCase):
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

    def tearDown(self):
        self.temp.cleanup()

    def run_tool(self, *args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.tool), *map(str, args)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_batch_generation_is_deterministic_and_verifiable(self):
        source = self.root / "source"
        output = self.root / "output"
        (source / "nested").mkdir(parents=True)
        (source / "a.bin").write_bytes(b"abc123" * 10_000)
        (source / "nested" / "b.bin").write_bytes(bytes(range(256)) * 400)

        command = ("--input", source, "--output", output, "--recursive", "--level", 9)
        first = self.run_tool(*command)
        self.assertEqual(first.returncode, 0, first.stderr)

        manifest = json.loads((output / "variants.json").read_text(encoding="utf-8"))
        self.assertEqual({item["source"] for item in manifest["variants"]}, {"a.bin", "nested/b.bin"})
        for item in manifest["variants"]:
            source_path = source / item["source"]
            output_path = output / item["output"]
            self.assertEqual(item["source_sha256"], hashlib.sha256(source_path.read_bytes()).hexdigest())
            self.assertEqual(item["payload_sha256"], hashlib.sha256(output_path.read_bytes()[32:]).hexdigest())

        hashes_before = {
            item["output"]: hashlib.sha256((output / item["output"]).read_bytes()).hexdigest()
            for item in manifest["variants"]
        }
        verify = self.run_tool("--verify", output, "--recursive")
        self.assertEqual(verify.returncode, 0, verify.stderr)

        second = self.run_tool(*command, "--overwrite")
        self.assertEqual(second.returncode, 0, second.stderr)
        hashes_after = {
            item["output"]: hashlib.sha256((output / item["output"]).read_bytes()).hexdigest()
            for item in manifest["variants"]
        }
        self.assertEqual(hashes_before, hashes_after)

    def test_all_supported_levels_round_trip(self):
        source = self.root / "input.bin"
        source.write_bytes((b"directstorage-gdeflate-" * 10_000) + bytes(range(256)))
        for level in range(1, 13):
            output = self.root / f"level-{level}.gdeflate"
            generated = self.run_tool("--input", source, "--output", output, "--level", level)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            verified = self.run_tool("--verify", output)
            self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_truncated_file_is_rejected(self):
        source = self.root / "input.bin"
        output = self.root / "output.gdeflate"
        source.write_bytes(b"content" * 1_000)
        self.assertEqual(self.run_tool("--input", source, "--output", output).returncode, 0)
        output.write_bytes(output.read_bytes()[:-1])
        result = self.run_tool("--verify", output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("size does not match the header", result.stderr)

    def test_out_of_range_levels_are_rejected(self):
        source = self.root / "input.bin"
        source.write_bytes(b"content")
        for level in (0, 13):
            result = self.run_tool("--input", source, "--output", self.root / f"bad-{level}.gdeflate", "--level", level)
            self.assertNotEqual(result.returncode, 0)

    def test_empty_input_is_rejected(self):
        source = self.root / "empty.bin"
        source.write_bytes(b"")
        result = self.run_tool("--input", source, "--output", self.root / "empty.gdeflate")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
