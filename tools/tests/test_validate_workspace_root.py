#!/usr/bin/env python3
"""Regression tests for workspace root validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.validate_workspace_root import validate


class ValidateWorkspaceRootTests(unittest.TestCase):
    def test_detects_forbidden_python_script_under_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            forbidden = (
                repo_root
                / "workspace"
                / "pipelines"
                / "node"
                / "pipe"
                / "execute"
                / "exec_001"
                / "problem"
                / "shallow_water2d@0.3.0"
                / "manual_writer.py"
            )
            forbidden.parent.mkdir(parents=True, exist_ok=True)
            forbidden.write_text("print('forbidden script')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("python script under workspace/ is forbidden" in v for v in violations)
            )

    def test_allows_only_approved_quality_check_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            gen_qc = (
                repo_root
                / "workspace"
                / "pipelines"
                / "node"
                / "pipe"
                / "generate"
                / "gen_001"
                / "src"
                / "quality_check.py"
            )
            build_qc = (
                repo_root
                / "workspace"
                / "pipelines"
                / "node"
                / "pipe"
                / "build"
                / "build_001"
                / "bin"
                / "quality_check.py"
            )
            gen_qc.parent.mkdir(parents=True, exist_ok=True)
            build_qc.parent.mkdir(parents=True, exist_ok=True)
            gen_qc.write_text("print('ok')\n", encoding="utf-8")
            build_qc.write_text("print('ok')\n", encoding="utf-8")

            violations, created_workspace = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertEqual([], violations)
            self.assertFalse(created_workspace)


if __name__ == "__main__":
    unittest.main()
