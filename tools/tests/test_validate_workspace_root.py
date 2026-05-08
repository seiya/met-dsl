#!/usr/bin/env python3
"""Regression tests for workspace root validation."""

from __future__ import annotations

import json
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

    def test_write_scope_fails_closed_when_git_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            violations, _ = validate_with_scope(
                repo_root=repo_root,
                workspace_root="workspace",
                write_scope_baseline="workspace/write_scope_baseline.json",
                stage="Generate",
                node_key="problem/shallow_water2d@0.3.0",
                pipeline_id="pipe_001",
            )
            self.assertTrue(
                any("write_scope baseline capture failed" in v for v in violations)
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

    def test_allows_plan_dependency_ref_to_spec_deps_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            request = repo_root / "workspace" / "orchestrations" / "orch_001" / "launches" / "run.request.json"
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text(
                json.dumps(
                    {
                        "step": "plan",
                        "dependency_ref": "spec/component/example/deps.yaml",
                    }
                ),
                encoding="utf-8",
            )

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(any("dependency_ref" in v for v in violations))

    def test_rejects_generate_dependency_ref_to_spec_deps_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            request = repo_root / "workspace" / "orchestrations" / "orch_001" / "launches" / "run.request.json"
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text(
                json.dumps(
                    {
                        "step": "generate",
                        "dependency_ref": "spec/component/example/deps.yaml",
                    }
                ),
                encoding="utf-8",
            )

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("generate dependency_ref must start with workspace/" in v for v in violations)
            )

    def test_rejects_plan_dependency_ref_outside_spec_deps_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            request = repo_root / "workspace" / "orchestrations" / "orch_001" / "launches" / "run.request.json"
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text(
                json.dumps(
                    {
                        "step": "plan",
                        "dependency_ref": "workspace/plans/example/dependency.resolved.yaml",
                    }
                ),
                encoding="utf-8",
            )

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Plan dependency_ref must be spec/.../deps.yaml" in v for v in violations)
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

    def test_allows_valid_uuid_subdirectory_under_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            uuid_dir = repo_root / "workspace" / "tmp" / "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
            uuid_dir.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(any("workspace/tmp" in v for v in violations))

    def test_allows_non_uuid_but_runtime_safe_agent_run_id_under_tmp(self) -> None:
        """IDs like step_run_001 are accepted by runtime (_AGENT_RUN_ID_RE) and must also
        pass workspace validation — both patterns must be consistent."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            for safe_id in ["step_run_001", "orch-run-abc", "substep123"]:
                dir_path = repo_root / "workspace" / "tmp" / safe_id
                dir_path.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(any("invalid workspace/tmp/ subdirectory name" in v for v in violations))

    def test_rejects_dotted_subdirectory_under_tmp(self) -> None:
        """Names starting with '.' or containing '.' are not valid agent_run_ids."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            bad_dir = repo_root / "workspace" / "tmp" / "has.dot"
            bad_dir.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(any("invalid workspace/tmp/ subdirectory name" in v for v in violations))

    def test_rejects_file_directly_under_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "workspace" / "tmp").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace" / "tmp" / "stray.txt").write_text("x", encoding="utf-8")
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(any("non-directory entry directly under workspace/tmp/" in v for v in violations))


if __name__ == "__main__":
    unittest.main()
