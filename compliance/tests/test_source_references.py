"""Tests for generated source-reference evidence integrity."""

import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "source_references", Path(__file__).parents[1] / "source_references.py"
)
source_references = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source_references)


class ChecksumTests(unittest.TestCase):
    def test_adds_generated_references_to_existing_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence = Path(temp)
            references = evidence / "source-references.json"
            references.write_text("[]\n", encoding="utf-8")
            (evidence / "SHA256SUMS.json").write_text("{}\n", encoding="utf-8")

            source_references.add_to_checksums(evidence, references)

            sums = json.loads((evidence / "SHA256SUMS.json").read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(references.read_bytes()).hexdigest(), sums["source-references.json"])

    def test_allows_standalone_source_lookup_without_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence = Path(temp)
            references = evidence / "source-references.json"
            references.write_text("[]\n", encoding="utf-8")

            source_references.add_to_checksums(evidence, references)

            self.assertFalse((evidence / "SHA256SUMS.json").exists())


if __name__ == "__main__":
    unittest.main()
