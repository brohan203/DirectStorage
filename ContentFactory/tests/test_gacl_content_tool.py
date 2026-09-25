import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class GACLContentToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        value = os.environ.get("GACL_CONTENT_TOOL")
        if not value:
            raise unittest.SkipTest("Set GACL_CONTENT_TOOL to the built executable")
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

    def test_all_phase2_formats_are_deterministic_and_verifiable(self):
        cases = {"BC1": (8, 1), "BC3": (16, 2), "BC4": (8, 3), "BC5": (16, 4), "BC7": (16, 7)}
        for format_name, (bytes_per_block, transform_id) in cases.items():
            source = self.root / f"{format_name}.bin"
            first = self.root / f"{format_name}-first.gacl"
            second = self.root / f"{format_name}-second.gacl"
            source.write_bytes(bytes((index * 37 + 11) & 0xFF for index in range(bytes_per_block * 17)))
            arguments = (
                "--input", source, "--format", format_name,
                "--zstd-level", 12, "--target-block-size", 65536,
            )
            generated = self.run_tool(*arguments, "--output", first)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            self.assertIn(f"transform_id={transform_id}", generated.stdout)
            regenerated = self.run_tool(*arguments, "--output", second)
            self.assertEqual(regenerated.returncode, 0, regenerated.stderr)
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
            verified = self.run_tool("--verify", "--input", first, "--original", source, "--format", format_name)
            self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_corruption_is_rejected_for_bc7(self):
        source = self.root / "BC7.bin"
        output = self.root / "BC7.gacl"
        source.write_bytes(bytes(range(256)))
        generated = self.run_tool(
            "--input", source, "--output", output, "--format", "BC7",
            "--zstd-level", 12, "--target-block-size", 65536,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr)
        damaged = bytearray(output.read_bytes())
        damaged[-1] ^= 0xFF
        output.write_bytes(damaged)
        verified = self.run_tool("--verify", "--input", output, "--original", source, "--format", "BC7")
        self.assertNotEqual(verified.returncode, 0)


if __name__ == "__main__":
    unittest.main()
