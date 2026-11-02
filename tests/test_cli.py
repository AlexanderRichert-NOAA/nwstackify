import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nwstackify.cli import (
    DEFAULT_SPACK_REF,
    DEFAULT_SPACK_URL,
    SYSTEM_HOSTNAME_PATTERNS,
    complete_path,
    determine_system,
    find_manifest,
    read_spack_source,
)


class CliTests(unittest.TestCase):
    def test_detects_ursa_from_fqdn(self):
        self.assertEqual(determine_system("ufe-login.example.org"), "ursa")

    def test_rejects_unconfigured_fqdn(self):
        with self.assertRaisesRegex(ValueError, "unsupported host"):
            determine_system("other.example.org")

    def test_selects_system_from_extensible_hostname_mapping(self):
        with patch.dict(
            SYSTEM_HOSTNAME_PATTERNS,
            {"new-system": (re.compile(r"^newhost.*"),)},
        ):
            self.assertEqual(determine_system("newhost.example.org"), "new-system")

    def test_prefers_manifest_in_current_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            direct_manifest = root / "spack.yaml"
            direct_manifest.touch()
            (root / "spack").mkdir()
            (root / "spack" / "spack.yaml").touch()
            self.assertEqual(find_manifest(root), direct_manifest)

    def test_falls_back_to_spack_subdirectory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            nested_manifest = root / "spack" / "spack.yaml"
            nested_manifest.parent.mkdir()
            nested_manifest.touch()
            self.assertEqual(find_manifest(root), nested_manifest)

    def test_reads_source_comments_and_defaults(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = Path(temporary_directory) / "spack.yaml"
            manifest.write_text(
                "# metadata spack-url: https://example.org/spack\n"
                "# metadata spack-ref: abc123\n"
            )
            self.assertEqual(
                read_spack_source(manifest),
                ("https://example.org/spack", "abc123"),
            )
            manifest.write_text("spack:\n  specs: []\n")
            self.assertEqual(read_spack_source(manifest), (DEFAULT_SPACK_URL, DEFAULT_SPACK_REF))

    def test_path_completion(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "install-dir").mkdir()
            candidate = complete_path(str(root / "install"), 0)
            self.assertEqual(candidate, str(root / "install-dir") + "/")
            self.assertIsNone(complete_path(str(root / "missing"), 0))


if __name__ == "__main__":
    unittest.main()
