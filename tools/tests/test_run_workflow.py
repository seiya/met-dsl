#!/usr/bin/env python3
"""Tests for workflow startup bootstrap script."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools import run_workflow


class RunWorkflowTests(unittest.TestCase):
    def test_spec_ref_matches_directory_and_file_path(self) -> None:
        self.assertTrue(run_workflow._spec_ref_matches("spec/problem", "spec/problem/test.md"))
        self.assertTrue(run_workflow._spec_ref_matches("spec/problem/test.md", "spec/problem"))
        self.assertTrue(
            run_workflow._spec_ref_matches(
                "spec/problem/controlled_spec.md",
                "spec/problem/test.md",
            )
        )

    def test_spec_ref_matches_distinguishes_other_directories(self) -> None:
        self.assertFalse(run_workflow._spec_ref_matches("spec/problem", "spec/another/test.md"))
        self.assertFalse(run_workflow._spec_ref_matches("spec/problem/test.md", "spec/another"))

    def test_normalize_phase_accepts_known_values(self) -> None:
        self.assertEqual(run_workflow._normalize_phase("plan"), "Plan")
        self.assertEqual(run_workflow._normalize_phase("PROMOTE"), "Promote")

    def test_normalize_phase_rejects_unknown_value(self) -> None:
        with self.assertRaises(ValueError):
            run_workflow._normalize_phase("spec")

    def test_new_orchestration_id_prefix(self) -> None:
        value = run_workflow._new_orchestration_id()
        self.assertTrue(value.startswith("orch_"))

    def test_preflight_pass_conditions(self) -> None:
        ok, detail = run_workflow._ensure_preflight_pass(
            {
                "status": "pass",
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
            }
        )
        self.assertTrue(ok)
        self.assertEqual(detail, "pass")

    def test_preflight_fail_conditions(self) -> None:
        ok, detail = run_workflow._ensure_preflight_pass(
            {
                "status": "fail",
                "can_launch_step_agents": False,
                "can_launch_substep_agents": True,
            }
        )
        self.assertFalse(ok)
        self.assertIn("status='fail'", detail)
        self.assertIn("can_launch_step_agents=False", detail)

    def test_prompt_contains_required_inputs(self) -> None:
        text = run_workflow._build_orchestration_prompt(
            orchestration_id="orch_test",
            spec_ref="spec/problem/sample.md",
            dependency_ref="workspace/plans/sample/sample_20260424_001/dependency.resolved.yaml",
            until_phase="Judge",
            workflow_mode="dev",
        )
        self.assertIn("orch_test", text)
        self.assertIn("spec/problem/sample.md", text)
        self.assertIn("Judge", text)
        self.assertIn("workflow_mode: `dev`", text)
        self.assertIn("dependency.resolved.yaml", text)
        self.assertIn("METDSL_WORKFLOW_MODE=1", text)
        self.assertIn("不足している場合は即停止", text)
        self.assertIn("issue_severity", text)

    def test_parse_args_defaults(self) -> None:
        ns = run_workflow._parse_args(["spec/problem.md", "generate"])
        self.assertEqual(ns.mode, "dev")
        self.assertEqual(ns.llm, "codex")
        self.assertTrue(ns.invoke_llm)

    def test_parse_args_supports_no_invoke_flag(self) -> None:
        ns = run_workflow._parse_args(
            [
                "spec/problem.md",
                "generate",
                "--no-invoke-llm",
            ]
        )
        self.assertFalse(ns.invoke_llm)

    def test_launch_command_for_codex_uses_exec_subcommand(self) -> None:
        command, stdin_text = run_workflow._launch_command_and_input(
            llm="codex",
            llm_command="codex",
            prompt_text="run workflow",
        )
        self.assertEqual(command, ["codex", "exec", "run workflow"])
        self.assertIsNone(stdin_text)

    def test_launch_command_for_claude_uses_stdin_prompt(self) -> None:
        command, stdin_text = run_workflow._launch_command_and_input(
            llm="claude",
            llm_command="claude",
            prompt_text="run workflow",
        )
        self.assertEqual(command, ["claude"])
        self.assertEqual(stdin_text, "run workflow")

    def test_main_writes_prompt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "workspace/plans/sample/sample_20260424_001/dependency.resolved.yaml"
            (repo_root / dep_ref).parent.mkdir(parents=True, exist_ok=True)
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            observed_calls: list[list[str]] = []

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                observed_calls.append(args)
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "orch_unit",
                        "--no-invoke-llm",
                    ]
                )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 0)
            self.assertTrue(any(call[0] == "init" for call in observed_calls))
            self.assertTrue(any(call[0] == "preflight" for call in observed_calls))
            prompt_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_unit"
                / "launches"
                / "orchestration.start.prompt.txt"
            )
            self.assertTrue(prompt_path.exists())

    def test_main_fails_when_spec_ref_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            dep_ref = "workspace/plans/sample/sample_20260424_001/dependency.resolved.yaml"
            (repo_root / dep_ref).parent.mkdir(parents=True, exist_ok=True)
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")
            code = run_workflow.main(
                [
                    "spec/problem/missing.md",
                    "build",
                    "--repo-root",
                    str(repo_root),
                    "--orchestration-id",
                    "orch_missing",
                    "--no-invoke-llm",
                ]
            )
            self.assertEqual(code, 2)

    def test_main_returns_structured_error_when_init_runtime_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "workspace/plans/sample/sample_20260424_001/dependency.resolved.yaml"
            (repo_root / dep_ref).parent.mkdir(parents=True, exist_ok=True)
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                if args[0] == "init":
                    raise RuntimeError("runtime command failed (init): boom")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            "orch_init_fail",
                            "--no-invoke-llm",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue().strip())
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["reason"], "runtime_command_failed")
            self.assertEqual(payload["orchestration_id"], "orch_init_fail")
            self.assertIn("init", payload["detail"])

    def test_main_returns_structured_error_when_preflight_runtime_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "workspace/plans/sample/sample_20260424_001/dependency.resolved.yaml"
            (repo_root / dep_ref).parent.mkdir(parents=True, exist_ok=True)
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                if args[0] == "preflight":
                    raise RuntimeError("runtime command failed (preflight): boom")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            "orch_preflight_fail",
                            "--no-invoke-llm",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue().strip())
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["reason"], "runtime_command_failed")
            self.assertEqual(payload["orchestration_id"], "orch_preflight_fail")
            self.assertIn("preflight", payload["detail"])

    def test_main_dev_mode_writes_failure_analysis_on_llm_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "workspace/plans/sample/sample_20260424_001/dependency.resolved.yaml"
            (repo_root / dep_ref).parent.mkdir(parents=True, exist_ok=True)
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            class DummyCompletedProcess:
                def __init__(self, returncode: int) -> None:
                    self.returncode = returncode

            original_runtime = run_workflow._runtime_command
            original_subprocess_run = run_workflow.subprocess.run
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                run_workflow.subprocess.run = lambda *args, **kwargs: DummyCompletedProcess(1)  # type: ignore[assignment]
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            "orch_dev_fail",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]
                run_workflow.subprocess.run = original_subprocess_run  # type: ignore[assignment]

            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue().strip())
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["reason"], "workflow_failed")
            self.assertEqual(payload["workflow_mode"], "dev")
            analysis_ref = payload.get("analysis_ref")
            self.assertIsInstance(analysis_ref, str)
            self.assertTrue((repo_root / str(analysis_ref)).exists())

    def test_main_auto_resolves_dependency_ref_from_orchestration_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "workspace/plans/sample/sample_20260424_001/dependency.resolved.yaml"
            (repo_root / dep_ref).parent.mkdir(parents=True, exist_ok=True)
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            prev_meta = repo_root / "workspace" / "orchestrations" / "orch_prev" / "orchestration_meta.json"
            prev_meta.parent.mkdir(parents=True, exist_ok=True)
            prev_meta.write_text(
                json.dumps(
                    {
                        "orchestration_id": "orch_prev",
                        "spec_ref": "spec/problem/test.md",
                        "dependency_ref": dep_ref,
                        "started_at": "2026-04-24T07:00:00Z",
                        "finished_at": "2026-04-24T07:10:00Z",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            observed_calls: list[list[str]] = []

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                observed_calls.append(args)
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "orch_auto_dep",
                        "--no-invoke-llm",
                    ]
                )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 0)
            init_call = next(call for call in observed_calls if call[0] == "init")
            self.assertIn("--dependency-ref", init_call)
            dep_idx = init_call.index("--dependency-ref") + 1
            self.assertEqual(init_call[dep_idx], dep_ref)

    def test_main_allows_startup_without_dependency_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")

            observed_calls: list[list[str]] = []

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                observed_calls.append(args)
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "orch_no_dep",
                        "--no-invoke-llm",
                    ]
                )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 0)
            init_call = next(call for call in observed_calls if call[0] == "init")
            self.assertNotIn("--dependency-ref", init_call)


if __name__ == "__main__":
    unittest.main()
