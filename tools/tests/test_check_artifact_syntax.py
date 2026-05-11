#!/usr/bin/env python3

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.check_artifact_syntax import check_file, main


class CheckArtifactSyntaxTest(unittest.TestCase):
    def test_accepts_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "perf.json"
            path.write_text('{"walltime_sec": 0.1}\n', encoding="utf-8")
            check_file(path, fmt="json", expected_top="object")

    def test_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "perf.json"
            path.write_text('{"walltime_sec": .000002}\n', encoding="utf-8")
            with self.assertRaises(Exception):
                check_file(path, fmt="json", expected_top="object")

    def test_accepts_yaml_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.ir.yaml"
            path.write_text("case_id: sample\nsweep: []\n", encoding="utf-8")
            check_file(path, fmt="yaml", expected_top="object")

    def test_rejects_wrong_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.ir.yaml"
            path.write_text("- sample\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                check_file(path, fmt="yaml", expected_top="object")

    def test_main_returns_nonzero_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text('{"x": }\n', encoding="utf-8")
            rc = main(["--format", "json", "--expect-top", "object", str(path)])
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
