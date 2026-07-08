"""Tests for tools/hooks/syntax_evidence.py — the host-authored, leaf-non-writable
syntax evidence certificate (pipeline-root) the post_generate validator certifies
the conductor-run Generate.syntax compiler gate against."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.hooks.syntax_evidence import (
    read_syntax_evidence,
    syntax_evidence_path,
    write_syntax_evidence,
)

_PASS_STAGE = {
    "compiler": "gfortran",
    "status": "pass",
    "compiler_version": "GNU Fortran (test) 13.0.0",
    "command_id": "c1",
    "command_log_ref": "workspace/p/src/command_log.jsonl",
}


class SyntaxEvidenceTest(unittest.TestCase):
    def test_path_under_syntax_evidence_dir(self) -> None:
        p = syntax_evidence_path(pipeline_root=Path("/pipe"), source_id="src_x")
        self.assertEqual(p, Path("/pipe/syntax_evidence/src_x.json"))

    def test_traversal_guard_rejects_unsafe_source_id(self) -> None:
        for bad in ("", ".", "..", "a/b", "a\\b", "a\x00b"):
            with self.assertRaises(ValueError):
                syntax_evidence_path(pipeline_root=Path("/pipe"), source_id=bad)

    def test_write_then_read_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stages = [dict(_PASS_STAGE)]
            write_syntax_evidence(pipeline_root=root, source_id="src_x",
                                  ok=True, stages=stages)
            doc = read_syntax_evidence(pipeline_root=root, source_id="src_x")
            assert doc is not None
            self.assertTrue(doc["ok"])
            self.assertEqual(doc["source_id"], "src_x")
            self.assertEqual(doc["stages"], stages)
            self.assertIn("checked_at", doc)

    def test_skipped_stage_needs_no_command_binding(self) -> None:
        # An optional target-compiler stage whose binary is absent records only
        # {compiler, status: skipped} — no command_id/command_log_ref required.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stages = [dict(_PASS_STAGE),
                      {"compiler": "frt", "status": "skipped",
                       "reason": "compiler not available: frt"}]
            write_syntax_evidence(pipeline_root=root, source_id="src_x",
                                  ok=True, stages=stages)
            doc = read_syntax_evidence(pipeline_root=root, source_id="src_x")
            assert doc is not None
            self.assertEqual(doc["stages"][1]["status"], "skipped")

    def test_pass_stage_without_command_binding_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_syntax_evidence(
                pipeline_root=root, source_id="src_x", ok=True,
                stages=[{"compiler": "gfortran", "status": "pass"}])
            with self.assertRaises(ValueError):
                read_syntax_evidence(pipeline_root=root, source_id="src_x")

    def test_read_absent_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                read_syntax_evidence(pipeline_root=Path(tmp), source_id="missing"))

    def test_read_malformed_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = syntax_evidence_path(pipeline_root=root, source_id="src_x")
            path.parent.mkdir(parents=True, exist_ok=True)
            # ok must be bool; here it is a string -> ValueError (fail-closed).
            path.write_text(
                '{"checked_at":"t","source_id":"src_x","ok":"true",'
                '"stages":[{"compiler":"gfortran","status":"pass",'
                '"command_id":"c","command_log_ref":"r"}]}',
                encoding="utf-8")
            with self.assertRaises(ValueError):
                read_syntax_evidence(pipeline_root=root, source_id="src_x")

    def test_read_empty_stages_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_syntax_evidence(pipeline_root=root, source_id="src_x",
                                  ok=True, stages=[])
            with self.assertRaises(ValueError):
                read_syntax_evidence(pipeline_root=root, source_id="src_x")

    def test_read_bad_status_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_syntax_evidence(
                pipeline_root=root, source_id="src_x", ok=True,
                stages=[{"compiler": "gfortran", "status": "ok",
                         "command_id": "c", "command_log_ref": "r"}])
            with self.assertRaises(ValueError):
                read_syntax_evidence(pipeline_root=root, source_id="src_x")


if __name__ == "__main__":
    unittest.main()
