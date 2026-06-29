"""Tests for tools/hooks/lint_evidence.py — the host-authored, leaf-non-writable lint
evidence certificate (pipeline-root) the post_generate validator certifies against."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.hooks.lint_evidence import (
    lint_evidence_path,
    read_lint_evidence,
    write_lint_evidence,
)


class LintEvidenceTest(unittest.TestCase):
    def test_path_under_lint_evidence_dir(self) -> None:
        p = lint_evidence_path(pipeline_root=Path("/pipe"), source_id="src_x")
        self.assertEqual(p, Path("/pipe/lint_evidence/src_x.json"))

    def test_traversal_guard_rejects_unsafe_source_id(self) -> None:
        for bad in ("", ".", "..", "a/b", "a\\b", "a\x00b"):
            with self.assertRaises(ValueError):
                lint_evidence_path(pipeline_root=Path("/pipe"), source_id=bad)

    def test_write_then_read_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_linter = [{"preset": "fortitude", "command_id": "c1",
                           "command_log_ref": "workspace/p/src/command_log.jsonl"}]
            write_lint_evidence(pipeline_root=root, source_id="src_x",
                                preset="fortitude", ok=True, run_linter=run_linter)
            doc = read_lint_evidence(pipeline_root=root, source_id="src_x")
            assert doc is not None
            self.assertTrue(doc["ok"])
            self.assertEqual(doc["preset"], "fortitude")
            self.assertEqual(doc["source_id"], "src_x")
            self.assertEqual(doc["run_linter"], run_linter)
            self.assertIn("checked_at", doc)

    def test_read_absent_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                read_lint_evidence(pipeline_root=Path(tmp), source_id="missing"))

    def test_read_malformed_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = lint_evidence_path(pipeline_root=root, source_id="src_x")
            path.parent.mkdir(parents=True, exist_ok=True)
            # ok must be bool; here it is a string -> ValueError (fail-closed).
            path.write_text(
                '{"checked_at":"t","source_id":"src_x","preset":"fortitude",'
                '"ok":"true","run_linter":[{"preset":"fortitude","command_id":"c",'
                '"command_log_ref":"r"}]}',
                encoding="utf-8")
            with self.assertRaises(ValueError):
                read_lint_evidence(pipeline_root=root, source_id="src_x")

    def test_read_empty_run_linter_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_lint_evidence(pipeline_root=root, source_id="src_x",
                                preset="fortitude", ok=True, run_linter=[])
            with self.assertRaises(ValueError):
                read_lint_evidence(pipeline_root=root, source_id="src_x")


if __name__ == "__main__":
    unittest.main()
