"""Verify license preservation and safe extraction from container archives."""

import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("build_evidence", Path(__file__).parents[1] / "build_evidence.py")
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)


class NoticeExtractionTests(unittest.TestCase):
    def archive(self, root, files, links=()):
        path = root / "image.tar"
        with tarfile.open(path, "w") as bundle:
            for name, content in files:
                item = tarfile.TarInfo(name)
                item.size = len(content)
                bundle.addfile(item, io.BytesIO(content))
            for name, target in links:
                item = tarfile.TarInfo(name)
                item.type = tarfile.SYMTYPE
                item.linkname = target
                bundle.addfile(item)
        return path

    def test_preserves_notice_bytes_and_resolves_relative_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = self.archive(root, [("usr/share/common-licenses/MIT", b"copyright\r\nlicense\n"), ("app/private.json", b"exclude")],
                                   [("usr/share/doc/example/copyright", "../../common-licenses/MIT")])
            rows = evidence.extract_notices(archive, root / "out")
            self.assertEqual(2, len(rows))
            self.assertEqual(b"copyright\r\nlicense\n", (root / "out/usr/share/doc/example/copyright").read_bytes())
            self.assertFalse((root / "out/app/private.json").exists())

    def test_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = self.archive(root, [("../../LICENSE", b"bad")])
            with self.assertRaises(ValueError):
                evidence.extract_notices(archive, root / "out")

    def test_missing_link_target_fails_instead_of_silently_omitting_notice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = self.archive(root, [], [("licenses/LICENSE", "missing")])
            with self.assertRaises(ValueError):
                evidence.extract_notices(archive, root / "out")

    def test_rejects_link_cycle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = self.archive(root, [], [("licenses/LICENSE", "NOTICE"), ("licenses/NOTICE", "LICENSE")])
            with self.assertRaises(ValueError):
                evidence.extract_notices(archive, root / "out")


if __name__ == "__main__":
    unittest.main()
