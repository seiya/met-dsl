#!/usr/bin/env python3
"""Regression tests for workspace root validation."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.validate_workspace_root import validate, validate_with_scope


class ValidateWorkspaceRootTests(unittest.TestCase):
    def _init_git_repo(self, repo_root: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)

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

    def test_detects_quality_check_script_under_workspace(self) -> None:
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
            self.assertTrue(
                any(str(gen_qc) in v and "python script under workspace/ is forbidden" in v for v in violations)
            )
            self.assertTrue(
                any(str(build_qc) in v and "python script under workspace/ is forbidden" in v for v in violations)
            )
            self.assertFalse(created_workspace)

    def test_write_scope_detects_outside_workspace_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._init_git_repo(repo_root)

            violations, _ = validate_with_scope(
                repo_root=repo_root,
                workspace_root="workspace",
                write_scope_baseline="workspace/write_scope_baseline.json",
                stage="Generate",
                node_key="problem/shallow_water2d@0.3.0",
                pipeline_id="pipe_001",
            )
            self.assertEqual(violations, [])

            outside = repo_root / "tools" / "outside_change.txt"
            outside.parent.mkdir(parents=True, exist_ok=True)
            outside.write_text("forbidden\n", encoding="utf-8")

            violations, _ = validate_with_scope(
                repo_root=repo_root,
                workspace_root="workspace",
                write_scope_baseline="workspace/write_scope_baseline.json",
                stage="Generate",
                node_key="problem/shallow_water2d@0.3.0",
                pipeline_id="pipe_001",
            )
            self.assertTrue(
                any("write_scope_violation detected outside workspace" in v for v in violations)
            )

    def test_write_scope_allows_workspace_only_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._init_git_repo(repo_root)

            violations, _ = validate_with_scope(
                repo_root=repo_root,
                workspace_root="workspace",
                write_scope_baseline="workspace/write_scope_baseline.json",
                stage="Execute",
                node_key="component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0",
                pipeline_id="pipe_001",
            )
            self.assertEqual(violations, [])

            inside = repo_root / "workspace" / "pipelines" / "node" / "file.txt"
            inside.parent.mkdir(parents=True, exist_ok=True)
            inside.write_text("allowed\n", encoding="utf-8")

            violations, _ = validate_with_scope(
                repo_root=repo_root,
                workspace_root="workspace",
                write_scope_baseline="workspace/write_scope_baseline.json",
                stage="Execute",
                node_key="component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0",
                pipeline_id="pipe_001",
            )
            self.assertFalse(
                any("write_scope_violation detected outside workspace" in v for v in violations)
            )

    def test_rejects_noncanonical_workspace_root_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace/runs/sample")
            self.assertTrue(
                any("workspace_root must be exactly 'workspace'" in v for v in violations)
            )

    def test_detects_noncanonical_top_level_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            noncanonical_dir = repo_root / "workspace" / "custom_output_root" / "trial_001"
            noncanonical_dir.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("non-canonical workspace directory name" in v for v in violations)
            )

    def test_allows_orchestrations_top_level_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orchestration_dir = repo_root / "workspace" / "orchestrations" / "orch_001"
            orchestration_dir.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any("non-canonical workspace directory name" in v for v in violations)
            )

    def test_detects_invalid_node_key_safe_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            invalid_node = repo_root / "workspace" / "plans" / "shallow_water2d"
            invalid_node.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("invalid node_key_safe directory name" in v for v in violations)
            )

    def test_detects_invalid_plan_or_pipeline_id_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            node_safe = "problem__shallow_water2d__0.3.0"
            invalid_plan = repo_root / "workspace" / "plans" / node_safe / "plan_001"
            invalid_pipeline = repo_root / "workspace" / "pipelines" / node_safe / "pipeline_001"
            invalid_plan.mkdir(parents=True, exist_ok=True)
            invalid_pipeline.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("invalid plans id directory name" in v or "invalid pipelines id directory name" in v for v in violations)
            )


if __name__ == "__main__":
    unittest.main()
