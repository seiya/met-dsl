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
            dependency_ref=None,
            until_phase="Judge",
        )
        self.assertIn("orch_test", text)
        self.assertIn("spec/problem/sample.md", text)
        self.assertIn("Judge", text)
        self.assertIn("METDSL_WORKFLOW_MODE=1", text)
        self.assertIn("不足している場合は即停止", text)

    def test_parse_args_defaults(self) -> None:
        ns = run_workflow._parse_args(["spec/problem.md", "generate"])
        self.assertEqual(ns.llm, "codex")
        self.assertTrue(ns.invoke_llm)

    def test_parse_args_supports_no_invoke_flag(self) -> None:
        ns = run_workflow._parse_args(["spec/problem.md", "generate", "--no-invoke-llm"])
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


if __name__ == "__main__":
    unittest.main()
