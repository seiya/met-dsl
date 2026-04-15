#!/usr/bin/env python3
"""Regression tests for Codex orchestration runtime helpers."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools.codex_orchestration_runtime import (
    _build_artifact_hashes,
    _compute_sha256,
    _is_within_preflight_ttl,
    _live_preflight_mode,
    _live_preflight_ttl_seconds,
    _require_preflight_launchable,
    _update_preflight_probed_at,
    _validate_agent_summary_text,
    build_launch_prompt_text,
    build_skill_must_read_refs,
    check_step_completed,
    enable_checkpoint_resume,
    get_preflight_ttl_status,
    init_orchestration,
    main,
    parse_feature_list,
    probe_execution_platform,
    prepare_launch_request_payload,
    probe_codex_cli,
    record_agent_run,
    record_launch,
    render_launch_prompt_text,
    update_checkpoint,
    update_orchestration_status,
    verify_checkpoint_integrity,
    write_preflight,
    write_step_result,
)

_FIX_PLAN_REF = "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001"
_FIX_PIPE_REF = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001"
_FIX_DEP_REF = f"{_FIX_PLAN_REF}/dependency.resolved.yaml"


def _fixture_skill_must_read_refs_step(step: str) -> str:
    skill_name = f"workflow-{step}"
    return ",".join(
        build_skill_must_read_refs(
            {
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": step,
                "skill_name": skill_name,
                "skill_ref": f"skills/{skill_name}/SKILL.md",
                "plan_ref": _FIX_PLAN_REF,
                "pipeline_ref": _FIX_PIPE_REF,
                "dependency_ref": _FIX_DEP_REF,
            }
        )
    )


def _fixture_skill_must_read_refs_substep(step: str, substep: str, *, generation_id: str | None = None) -> str:
    skill_name = f"workflow-{step}-{substep}"
    payload: dict[str, str | None] = {
        "node_key": "problem/shallow_water2d@0.3.0",
        "step": step,
        "substep": substep,
        "skill_name": skill_name,
        "skill_ref": f"skills/{skill_name}/SKILL.md",
        "plan_ref": _FIX_PLAN_REF,
        "pipeline_ref": _FIX_PIPE_REF,
        "dependency_ref": _FIX_DEP_REF,
    }
    if generation_id:
        payload["generation_id"] = generation_id
    return ",".join(build_skill_must_read_refs(payload))


def _step_launch_prompt(node_key: str, step: str, agent_run_id: str) -> str:
    return f"""あなたは step agent である。
対象 node_key: {node_key}
対象 step: {step}
orchestration_id: orch_001
agent_run_id: {agent_run_id}
parent_agent_run_id: orch_run_001
plan_ref: workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001
pipeline_ref: workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001
dependency_ref: workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml
skill_name: workflow-{step}
skill_ref: skills/workflow-{step}/SKILL.md
skill_must_read_refs: {_fixture_skill_must_read_refs_step(step)}
issue_severity: none
repair_strategy: none
repair_target_agent_run_id: none
repair_reason: none

必須要件:
- 契約された step を完了すること。
"""


def _substep_launch_prompt(node_key: str, step: str, substep: str, agent_run_id: str) -> str:
    return f"""あなたは substep agent である。
対象 node_key: {node_key}
対象 step: {step}
対象 substep: {substep}
orchestration_id: orch_001
agent_run_id: {agent_run_id}
parent_agent_run_id: orch_run_001
plan_ref: workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001
pipeline_ref: workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001
dependency_ref: workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml
skill_name: workflow-{step}-{substep}
skill_ref: skills/workflow-{step}-{substep}/SKILL.md
skill_must_read_refs: {_fixture_skill_must_read_refs_substep(step, substep)}
issue_severity: none
repair_strategy: none
repair_target_agent_run_id: none
repair_reason: none

必須要件:
- 契約された substep を完了すること。
"""


def _spawn_response_payload(session_id: str) -> dict[str, object]:
    return {
        "agent_session_id": session_id,
        "accepted": True,
        "launch_reply": f"accepted: {session_id}",
    }


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class CodexOrchestrationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_live_preflight = os.environ.get("CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT")
        os.environ["CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT"] = "0"

    def tearDown(self) -> None:
        if self._old_live_preflight is None:
            os.environ.pop("CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT", None)
        else:
            os.environ["CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT"] = self._old_live_preflight

    def test_parse_feature_list_extracts_boolean_flags(self) -> None:
        raw = """
multi_agent                      experimental       true
child_agents_md                  under development  false
shell_tool                       stable             true
"""
        parsed = parse_feature_list(raw)
        self.assertEqual(
            parsed,
            {
                "multi_agent": True,
                "child_agents_md": False,
                "shell_tool": True,
            },
        )

    def test_probe_codex_cli_passes_when_multi_agent_is_enabled(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="codex-cli 0.114.0\n")
            if args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(
                    0,
                    stdout="multi_agent experimental true\nchild_agents_md under development false\n",
                )
            raise AssertionError(args)

        result = probe_codex_cli(codex_command="codex", runner=runner)
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["can_launch_step_agents"])
        self.assertTrue(result["can_launch_substep_agents"])

    def test_probe_codex_cli_fails_when_multi_agent_is_disabled(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="codex-cli 0.114.0\n")
            if args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(
                    0,
                    stdout="multi_agent experimental false\n",
                )
            raise AssertionError(args)

        result = probe_codex_cli(codex_command="codex", runner=runner)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["can_launch_step_agents"])
        self.assertFalse(result["can_launch_substep_agents"])

    def test_probe_execution_platform_supports_cursor_backend(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[0] == "agent" and args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="agent 1.0.0\n")
            if args[0] == "agent" and args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(0, stdout="multi_agent experimental true\n")
            raise AssertionError(args)

        result = probe_execution_platform(backend="cursor", runner=runner)
        self.assertEqual(result["backend"], "cursor")
        self.assertEqual(result["probe_command"], "agent")
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["can_launch_step_agents"])
        by_name = {c["name"]: c for c in result["checks"]}
        self.assertIsNone(by_name["cursor_help_probe_available"]["pass"])
        self.assertIn("skipped", by_name["cursor_help_probe_available"]["detail"])

    def test_probe_execution_platform_cursor_fallback_when_features_list_unavailable(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[0] == "agent" and args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="agent 1.0.0\n")
            if args[0] == "agent" and args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(1, stderr="unknown command: features\n")
            if args[0] == "agent" and args[1:] == ["--help"]:
                return _FakeCompletedProcess(0, stdout="Usage: agent [options] [command] [prompt...]\n")
            raise AssertionError(args)

        result = probe_execution_platform(backend="cursor", runner=runner)
        self.assertEqual(result["backend"], "cursor")
        self.assertEqual(result["probe_command"], "agent")
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["can_launch_step_agents"])
        self.assertEqual(result["feature_states"].get("multi_agent"), True)

    def test_probe_execution_platform_cursor_fallback_when_features_list_has_no_multi_agent(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[0] == "agent" and args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="agent 1.0.0\n")
            if args[0] == "agent" and args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(0, stdout="some_feature stable true\n")
            if args[0] == "agent" and args[1:] == ["--help"]:
                return _FakeCompletedProcess(0, stdout="Usage: agent [options] [command] [prompt...]\n")
            raise AssertionError(args)

        result = probe_execution_platform(backend="cursor", runner=runner)
        self.assertEqual(result["backend"], "cursor")
        self.assertEqual(result["probe_command"], "agent")
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["can_launch_step_agents"])
        self.assertEqual(result["feature_states"].get("multi_agent"), True)

    def test_probe_execution_platform_supports_claude_backend(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[0] == "claude" and args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="2.1.0 (Claude Code)\n")
            if args[0] == "claude" and args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(1, stderr="unknown command\n")
            if args[0] == "claude" and args[1:] == ["--help"]:
                return _FakeCompletedProcess(0, stdout="Usage: claude [options] [command] [prompt]\n")
            raise AssertionError(args)

        result = probe_execution_platform(backend="claude", runner=runner)
        self.assertEqual(result["backend"], "claude")
        self.assertEqual(result["probe_command"], "claude")
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["can_launch_step_agents"])
        self.assertTrue(result["can_launch_substep_agents"])
        self.assertEqual(result["feature_states"].get("multi_agent"), True)

    def test_probe_execution_platform_claude_fallback_when_features_list_has_no_multi_agent(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[0] == "claude" and args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="2.1.0 (Claude Code)\n")
            if args[0] == "claude" and args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(0, stdout="multi_agent experimental false\n")
            if args[0] == "claude" and args[1:] == ["--help"]:
                return _FakeCompletedProcess(0, stdout="Usage: claude [options] [command] [prompt]\n")
            raise AssertionError(args)

        result = probe_execution_platform(backend="claude", runner=runner)
        self.assertEqual(result["backend"], "claude")
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["can_launch_step_agents"])
        self.assertEqual(result["feature_states"].get("multi_agent"), True)

    def test_probe_execution_platform_claude_fails_when_help_also_unavailable(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[0] == "claude" and args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="2.1.0 (Claude Code)\n")
            if args[0] == "claude" and args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(1, stderr="error\n")
            if args[0] == "claude" and args[1:] == ["--help"]:
                return _FakeCompletedProcess(1, stderr="error\n")
            raise AssertionError(args)

        result = probe_execution_platform(backend="claude", runner=runner)
        self.assertEqual(result["backend"], "claude")
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["can_launch_substep_agents"])

    def test_probe_execution_platform_uses_explicit_agent_command(self) -> None:
        seen = {"command": ""}

        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            seen["command"] = args[0]
            if args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="custom 1.0.0\n")
            if args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(0, stdout="multi_agent experimental true\n")
            raise AssertionError(args)

        result = probe_execution_platform(
            backend="codex",
            agent_command="custom-codex",
            runner=runner,
        )
        self.assertEqual(seen["command"], "custom-codex")
        self.assertEqual(result["probe_command"], "custom-codex")

    def test_probe_execution_platform_rejects_backend_command_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "agent_command/backend mismatch"):
            probe_execution_platform(
                backend="cursor",
                agent_command="codex",
            )

    def test_probe_execution_platform_uses_backend_default_when_no_override(self) -> None:
        seen = {"command": ""}

        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            seen["command"] = args[0]
            if args[0] == "agent" and args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="agent 1.0.0\n")
            if args[0] == "agent" and args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(0, stdout="multi_agent experimental true\n")
            raise AssertionError(args)

        result = probe_execution_platform(
            backend="cursor",
            agent_command=None,
            runner=runner,
        )
        self.assertEqual(seen["command"], "agent")
        self.assertEqual(result["probe_command"], "agent")

    def test_probe_codex_backend_calls_features_list(self) -> None:
        """_probe_codex_backend が features list コマンドを呼び、multi_agent を検出すること。"""
        import subprocess as _subprocess
        from tools.codex_orchestration_runtime import _probe_codex_backend
        calls: list[list[str]] = []

        def runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(list(cmd))
            if cmd[-1] == "--version":
                return _FakeCompletedProcess(0, stdout="codex 1.0.0")
            if cmd[-2:] == ["features", "list"]:
                return _FakeCompletedProcess(0, stdout="multi_agent  available  true")
            return _FakeCompletedProcess(1, stdout="")

        checks, features, multi_agent_enabled, agent_version = _probe_codex_backend(
            "codex", "codex", runner
        )
        self.assertTrue(multi_agent_enabled)
        self.assertTrue(features.get("multi_agent"))
        called_cmds = [" ".join(c) for c in calls]
        self.assertTrue(any("features" in c for c in called_cmds))
        by_name = {c["name"]: c for c in checks}
        self.assertIn("codex_features_list_available", by_name)
        self.assertNotIn("codex_features_available", by_name)

    def test_all_strict_boolean_probe_checks_pass_skips_none_pass(self) -> None:
        """`pass: None` の check は未実行扱いとし、それ以外がすべて True なら合格とする。"""
        from tools.codex_orchestration_runtime import _all_strict_boolean_probe_checks_pass

        checks_ok = [
            {"name": "codex_version_available", "pass": True},
            {"name": "codex_features_list_available", "pass": True},
            {"name": "codex_help_probe_available", "pass": None},
            {"name": "multi_agent_enabled", "pass": True},
        ]
        self.assertTrue(_all_strict_boolean_probe_checks_pass(checks_ok))

        checks_bad = [
            {"name": "codex_version_available", "pass": True},
            {"name": "codex_features_list_available", "pass": False},
            {"name": "codex_help_probe_available", "pass": None},
            {"name": "multi_agent_enabled", "pass": True},
        ]
        self.assertFalse(_all_strict_boolean_probe_checks_pass(checks_bad))

    def test_all_strict_boolean_probe_checks_pass_requires_pass_key(self) -> None:
        from tools.codex_orchestration_runtime import _all_strict_boolean_probe_checks_pass

        self.assertFalse(
            _all_strict_boolean_probe_checks_pass([{"name": "x"}])
        )

    def test_probe_help_fallback_uses_help_when_features_list_fails(self) -> None:
        """_probe_help_fallback_backend が features list 失敗時に --help fallback を試みること。"""
        from tools.codex_orchestration_runtime import _probe_help_fallback_backend
        calls: list[list[str]] = []

        def runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(list(cmd))
            if cmd[-1] == "--version":
                return _FakeCompletedProcess(0, stdout="claude 1.0.0")
            if cmd[-2:] == ["features", "list"]:
                return _FakeCompletedProcess(1, stdout="")  # features list 失敗
            if cmd[-1] == "--help":
                return _FakeCompletedProcess(0, stdout="Usage: claude ...")
            return _FakeCompletedProcess(1, stdout="")

        checks, features, multi_agent_enabled, agent_version = _probe_help_fallback_backend(
            "claude", "claude", runner
        )
        self.assertTrue(multi_agent_enabled)
        called_cmds = [" ".join(c) for c in calls]
        self.assertTrue(any("--help" in c for c in called_cmds))
        by_name = {c["name"]: c for c in checks}
        self.assertFalse(by_name["claude_features_list_available"]["pass"])
        self.assertTrue(by_name["claude_help_probe_available"]["pass"])

    def test_probe_help_fallback_skips_help_when_features_list_confirms_multi_agent(self) -> None:
        """features list で multi_agent が分かる場合は --help を走らせず、help プローブは pass=null とする。"""
        from tools.codex_orchestration_runtime import (
            _can_launch_from_help_fallback_checks,
            _probe_help_fallback_backend,
        )

        calls: list[list[str]] = []

        def runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(list(cmd))
            if cmd[-1] == "--version":
                return _FakeCompletedProcess(0, stdout="claude 1.0.0")
            if cmd[-2:] == ["features", "list"]:
                return _FakeCompletedProcess(0, stdout="multi_agent experimental true\n")
            raise AssertionError(cmd)

        checks, features, multi_agent_enabled, agent_version = _probe_help_fallback_backend(
            "claude", "claude", runner
        )
        self.assertTrue(multi_agent_enabled)
        self.assertFalse(any("--help" in c for c in calls))
        by_name = {c["name"]: c for c in checks}
        self.assertIsNone(by_name["claude_help_probe_available"]["pass"])
        self.assertTrue(_can_launch_from_help_fallback_checks("claude", checks))

    def test_prepare_launch_request_payload_fills_verify_defaults(self) -> None:
        payload = prepare_launch_request_payload(
            {
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "plan",
                "substep": "verify",
                "orchestration_id": "orch_001",
                "agent_run_id": "substep_run_plan_verify_001",
                "parent_agent_run_id": "orch_run_001",
                "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
            }
        )
        self.assertEqual(payload["skill_name"], "workflow-plan-verify")
        self.assertEqual(payload["skill_ref"], "skills/workflow-plan-verify/SKILL.md")
        self.assertEqual(payload["issue_severity"], "none")
        self.assertIn("docs/workflow/WORKFLOW_CORE.md", payload["skill_must_read_refs"])
        self.assertIn("docs/workflow/phases/phase_01_plan.md", payload["skill_must_read_refs"])
        self.assertIn("docs/ORCHESTRATION.md", payload["skill_must_read_refs"])
        self.assertIn("skills/workflow-plan-verify/SKILL.md", payload["skill_must_read_refs"])
        self.assertIn(
            "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/derived_contract.json",
            payload["skill_must_read_refs"],
        )
        self.assertIn("必須要件:", payload["launch_prompt_full"])

    def test_render_launch_prompt_text_renders_full_template_body(self) -> None:
        prompt = render_launch_prompt_text(
            {
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "build",
                "orchestration_id": "orch_001",
                "agent_run_id": "step_run_build_001",
                "parent_agent_run_id": "orch_run_001",
                "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                "skill_name": "workflow-build",
                "skill_ref": "skills/workflow-build/SKILL.md",
                "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                "issue_severity": "none",
                "repair_strategy": "none",
                "repair_target_agent_run_id": "none",
                "repair_reason": "none",
            }
        )
        self.assertIn("あなたは step agent である。", prompt)
        self.assertIn("必須要件:", prompt)
        self.assertIn("完了返答には `launch_reply`", prompt)

    def test_writes_orchestration_artifacts_in_canonical_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(
                repo_root=repo_root,
                orchestration_id="orch_001",
                spec_ref="spec/problem/shallow_water2d/controlled_spec.md",
                dependency_ref="workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
            )
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [
                        {"name": "multi_agent_enabled", "pass": True},
                    ],
                },
            )
            launch_refs = record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="substep_run_plan_generate_001",
                request_payload={
                    "agent_run_id": "substep_run_plan_generate_001",
                    "agent_role": "substep",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "launch_prompt_full": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "substep_run_plan_generate_001",
                    ),
                },
                response_payload={
                    "agent_run_id": "substep_run_plan_generate_001",
                    **_spawn_response_payload("sess_substep_plan_generate_001"),
                },
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="step_run_build_001",
                request_payload={
                    "agent_run_id": "step_run_build_001",
                    "agent_role": "step",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "build",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": "",
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload={
                    "agent_run_id": "step_run_build_001",
                    **_spawn_response_payload("sess_step_build_001"),
                },
            )

            record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "orch_run_001",
                    "agent_role": "orchestration",
                    "status": "running",
                    "agent_backend": "claude",
                    "started_at": "2026-03-11T00:00:00Z",
                },
            )
            record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "substep_run_plan_generate_001",
                    "parent_agent_run_id": "orch_run_001",
                    "agent_role": "substep",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "status": "pass",
                    "agent_backend": "codex",
                    "agent_model": "gpt-5-codex",
                    "context_id": "ctx_substep_plan_generate_001",
                    "agent_session_id": "sess_substep_plan_generate_001",
                    "launch_request_ref": launch_refs["launch_request_ref"],
                    "launch_response_ref": launch_refs["launch_response_ref"],
                    "launch_prompt_ref": launch_refs["launch_prompt_ref"],
                    "launch_reply_ref": launch_refs["launch_reply_ref"],
                    "started_at": "2026-03-11T00:00:10Z",
                    "finished_at": "2026-03-11T00:00:50Z",
                    "output_refs": [
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/case.resolved.yaml",
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json",
                    ],
                },
            )
            record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "agent_role": "step",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "build",
                    "status": "pass",
                    "agent_backend": "codex",
                    "agent_model": "gpt-5-codex",
                    "context_id": "ctx_step_build_001",
                    "agent_session_id": "sess_step_build_001",
                    "started_at": "2026-03-11T00:00:20Z",
                    "finished_at": "2026-03-11T00:01:10Z",
                    "output_refs": [
                        "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate"
                    ],
                },
            )

            plan_meta_path = (
                repo_root
                / "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json"
            )
            plan_meta_path.parent.mkdir(parents=True, exist_ok=True)
            plan_meta_path.write_text(
                json.dumps({"attempt_count": 1, "verification_status": "pass", "context_isolated": True}),
                encoding="utf-8",
            )
            write_step_result(
                repo_root=repo_root,
                orchestration_id="orch_001",
                node_key="problem/shallow_water2d@0.3.0",
                step="plan",
                agent_run_id="orch_run_001",
                payload={
                    "status": "pass",
                    "required_outputs": [
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/case.resolved.yaml"
                    ],
                    "failed_substeps": [],
                    "substep_agent_run_ids": ["substep_run_plan_generate_001"],
                    "executor_agent_run_id": "orch_run_001",
                },
            )
            write_step_result(
                repo_root=repo_root,
                orchestration_id="orch_001",
                node_key="problem/shallow_water2d@0.3.0",
                step="build",
                agent_run_id="step_run_build_001",
                payload={
                    "status": "pass",
                    "validation_stage": "post_build",
                    "required_outputs": [
                        "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate"
                    ],
                    "failed_substeps": [],
                    "substep_agent_run_ids": [],
                    "executor_agent_run_id": "step_run_build_001",
                },
            )
            update_orchestration_status(
                repo_root=repo_root,
                orchestration_id="orch_001",
                status="pass",
            )

            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            self.assertTrue((orch_root / "orchestration_meta.json").exists())
            self.assertTrue((orch_root / "preflight.json").exists())
            self.assertTrue((orch_root / "agent_graph.json").exists())
            self.assertTrue((orch_root / "launches" / "substep_run_plan_generate_001.request.json").exists())
            self.assertTrue((orch_root / "launches" / "substep_run_plan_generate_001.prompt.txt").exists())
            self.assertTrue((orch_root / "launches" / "substep_run_plan_generate_001.reply.txt").exists())
            self.assertTrue(
                (
                    orch_root
                    / "agents"
                    / "substep_run_plan_generate_001"
                    / "dialogs"
                    / "child.request.json"
                ).exists()
            )
            self.assertTrue(
                (
                    orch_root
                    / "agents"
                    / "substep_run_plan_generate_001"
                    / "dialogs"
                    / "child.response.json"
                ).exists()
            )
            self.assertTrue(
                (
                    orch_root
                    / "agents"
                    / "substep_run_plan_generate_001"
                    / "dialogs"
                    / "child.prompt.txt"
                ).exists()
            )
            self.assertTrue(
                (
                    orch_root
                    / "agents"
                    / "substep_run_plan_generate_001"
                    / "dialogs"
                    / "child.reply.txt"
                ).exists()
            )
            self.assertTrue(
                (
                    orch_root
                    / "agents"
                    / "substep_run_plan_generate_001"
                    / "dialogs"
                    / "agent.result.json"
                ).exists()
            )
            self.assertTrue(
                (
                    orch_root
                    / "agents"
                    / "substep_run_plan_generate_001"
                    / "dialogs"
                    / "agent.summary.txt"
                ).exists()
            )
            self.assertTrue(
                (
                    orch_root
                    / "steps"
                    / "problem__shallow_water2d__0.3.0"
                    / "plan"
                    / "orch_run_001"
                    / "step_result.json"
                ).exists()
            )

            runs_text = (orch_root / "agent_runs.jsonl").read_text(encoding="utf-8")
            self.assertIn('"agent_run_id": "substep_run_plan_generate_001"', runs_text)
            self.assertIn('"agent_session_id": "sess_step_build_001"', runs_text)
            self.assertIn('"launch_prompt_ref": "workspace/orchestrations/orch_001/launches/step_run_build_001.prompt.txt"', runs_text)
            self.assertIn('"launch_reply_ref": "workspace/orchestrations/orch_001/launches/step_run_build_001.reply.txt"', runs_text)
            self.assertIn('"agent_result_ref": "workspace/orchestrations/orch_001/agents/step_run_build_001/dialogs/agent.result.json"', runs_text)
            self.assertIn('"agent_summary_ref": "workspace/orchestrations/orch_001/agents/step_run_build_001/dialogs/agent.summary.txt"', runs_text)
            request_payload = json.loads(
                (
                    orch_root / "launches" / "substep_run_plan_generate_001.request.json"
                ).read_text(encoding="utf-8")
            )
            response_payload = json.loads(
                (
                    orch_root / "launches" / "substep_run_plan_generate_001.response.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                request_payload["child_launch_request_ref"],
                "workspace/orchestrations/orch_001/agents/substep_run_plan_generate_001/dialogs/child.request.json",
            )
            self.assertEqual(
                request_payload["child_launch_prompt_ref"],
                "workspace/orchestrations/orch_001/agents/substep_run_plan_generate_001/dialogs/child.prompt.txt",
            )
            self.assertEqual(
                response_payload["child_launch_response_ref"],
                "workspace/orchestrations/orch_001/agents/substep_run_plan_generate_001/dialogs/child.response.json",
            )
            self.assertEqual(
                response_payload["child_launch_reply_ref"],
                "workspace/orchestrations/orch_001/agents/substep_run_plan_generate_001/dialogs/child.reply.txt",
            )
            self.assertEqual(
                (orch_root / "launches" / "substep_run_plan_generate_001.prompt.txt").read_text(
                    encoding="utf-8"
                ),
                (
                    orch_root
                    / "agents"
                    / "substep_run_plan_generate_001"
                    / "dialogs"
                    / "child.prompt.txt"
                ).read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (orch_root / "launches" / "substep_run_plan_generate_001.reply.txt").read_text(
                    encoding="utf-8"
                ),
                (
                    orch_root
                    / "agents"
                    / "substep_run_plan_generate_001"
                    / "dialogs"
                    / "child.reply.txt"
                ).read_text(encoding="utf-8"),
            )
            result_payload = json.loads(
                (
                    orch_root
                    / "agents"
                    / "substep_run_plan_generate_001"
                    / "dialogs"
                    / "agent.result.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(result_payload["agent_run_id"], "substep_run_plan_generate_001")
            self.assertEqual(result_payload["status"], "pass")
            summary_text = (
                orch_root
                / "agents"
                / "substep_run_plan_generate_001"
                / "dialogs"
                / "agent.summary.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("agent_run_id: substep_run_plan_generate_001", summary_text)
            self.assertIn("output_refs:", summary_text)

    def test_record_launch_prefers_prompt_over_launch_prompt_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            full_prompt = _substep_launch_prompt(
                "problem/shallow_water2d@0.3.0",
                "plan",
                "generate",
                "substep_run_plan_generate_001",
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="substep_run_plan_generate_001",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "launch_prompt": "short summary",
                    "prompt": full_prompt,
                },
                response_payload=_spawn_response_payload("sess_substep_plan_generate_001"),
            )
            prompt_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_001"
                / "launches"
                / "substep_run_plan_generate_001.prompt.txt"
            )
            self.assertEqual(prompt_path.read_text(encoding="utf-8"), full_prompt)

    def test_record_launch_prefers_launch_prompt_full_over_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="substep_run_plan_generate_001",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "launch_prompt": "summary",
                    "prompt": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "substep_run_plan_generate_001",
                    ),
                    "launch_prompt_full": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "substep_run_plan_generate_001",
                    )
                    + "\n追加指示: 最詳細 prompt を保存すること。",
                },
                response_payload=_spawn_response_payload("sess_substep_plan_generate_001"),
            )
            prompt_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_001"
                / "launches"
                / "substep_run_plan_generate_001.prompt.txt"
            )
            self.assertEqual(
                prompt_path.read_text(encoding="utf-8"),
                _substep_launch_prompt(
                    "problem/shallow_water2d@0.3.0",
                    "plan",
                    "generate",
                    "substep_run_plan_generate_001",
                )
                + "\n追加指示: 最詳細 prompt を保存すること。\n",
            )

    def test_record_launch_uses_spawn_request_task_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="substep_run_plan_generate_001",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "launch_prompt": "summary only",
                    "spawn_request": {
                        "task": _substep_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "plan",
                            "generate",
                            "substep_run_plan_generate_001",
                        ),
                    },
                },
                response_payload=_spawn_response_payload("sess_substep_plan_generate_001"),
            )
            prompt_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_001"
                / "launches"
                / "substep_run_plan_generate_001.prompt.txt"
            )
            self.assertEqual(
                prompt_path.read_text(encoding="utf-8"),
                _substep_launch_prompt(
                    "problem/shallow_water2d@0.3.0",
                    "plan",
                    "generate",
                    "substep_run_plan_generate_001",
                ),
            )

    def test_rejects_non_template_launch_prompt_for_step_or_substep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            with self.assertRaisesRegex(ValueError, "template markers"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_build_001",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "build",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "step_run_build_001",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                        "skill_name": "workflow-build",
                        "skill_ref": "skills/workflow-build/SKILL.md",
                        "skill_must_read_refs": "",
                        "issue_severity": "none",
                        "repair_strategy": "none",
                        "repair_target_agent_run_id": "none",
                        "repair_reason": "none",
                        "launch_prompt_full": "Build step for node problem/shallow_water2d@0.3.0",
                    },
                    response_payload=_spawn_response_payload("sess_step_build_001"),
                )

    def test_record_launch_autofills_verify_required_resolved_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            launch_refs = record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="substep_run_generate_verify_001",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "Generate",
                    "substep": "verify",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "substep_run_generate_verify_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                    "generation_id": "gen_001",
                    "skill_name": "workflow-generate-verify",
                    "skill_ref": "skills/workflow-generate-verify/SKILL.md",
                    "skill_must_read_refs": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_001/generate_meta.json",
                },
                response_payload=_spawn_response_payload("sess_substep_run_generate_verify_001"),
            )
            request_payload = json.loads(
                (repo_root / launch_refs["launch_request_ref"]).read_text(encoding="utf-8")
            )
            self.assertIn(
                "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/case.resolved.yaml",
                request_payload["skill_must_read_refs"],
            )
            self.assertIn(
                "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/derived_contract.json",
                request_payload["skill_must_read_refs"],
            )
            self.assertIn(
                "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/lineage.json",
                request_payload["skill_must_read_refs"],
            )
            self.assertIn(
                "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_001/generate_meta.json",
                request_payload["skill_must_read_refs"],
            )

    def test_rejects_launch_with_placeholder_plan_or_pipeline_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            with self.assertRaisesRegex(ValueError, "plan_ref must not contain placeholder"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="substep_run_plan_generate_001",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "plan",
                        "substep": "generate",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "substep_run_plan_generate_001",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/<agent-determined-plan-id>",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                        "skill_name": "workflow-plan-generate",
                        "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                        "skill_must_read_refs": "",
                        "launch_prompt_full": _substep_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "plan",
                            "generate",
                            "substep_run_plan_generate_001",
                        ),
                    },
                    response_payload=_spawn_response_payload("sess_substep_plan_generate_001"),
                )

    def test_rejects_launch_when_pipeline_ref_is_not_pipeline_root_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            bad_pipeline = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/generate/gen_001/generate_meta.json"
            )
            with self.assertRaisesRegex(ValueError, "pipeline_ref must be exactly"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="substep_bad_pipeline_001",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "generate",
                        "substep": "generate",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "substep_bad_pipeline_001",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "pipeline_ref": bad_pipeline,
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                        "skill_name": "workflow-generate-generate",
                        "skill_ref": "skills/workflow-generate-generate/SKILL.md",
                        "skill_must_read_refs": "",
                        "launch_prompt_full": "prompt",
                    },
                    response_payload=_spawn_response_payload("sess_bad_pipeline_001"),
                )

    def test_rejects_generate_verify_without_generation_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            with self.assertRaisesRegex(ValueError, "generation_id"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="substep_gen_verify_no_gid",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "generate",
                        "substep": "verify",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "substep_gen_verify_no_gid",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                        "skill_name": "workflow-generate-verify",
                        "skill_ref": "skills/workflow-generate-verify/SKILL.md",
                        "skill_must_read_refs": "",
                        "launch_prompt_full": "gv verify",
                    },
                    response_payload=_spawn_response_payload("sess_gv_no_gid"),
                )

    def test_record_launch_autofills_prompt_and_skill_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            launch_refs = record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="substep_run_plan_verify_001",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "verify",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "substep_run_plan_verify_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                },
                response_payload=_spawn_response_payload("sess_substep_run_plan_verify_001"),
            )
            request_path = repo_root / launch_refs["launch_request_ref"]
            prompt_path = repo_root / launch_refs["launch_prompt_ref"]
            request_payload = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request_payload["skill_name"], "workflow-plan-verify")
            self.assertEqual(request_payload["skill_ref"], "skills/workflow-plan-verify/SKILL.md")
            self.assertIn(
                "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/derived_contract.json",
                request_payload["skill_must_read_refs"],
            )
            prompt_text = prompt_path.read_text(encoding="utf-8")
            self.assertIn("必須要件:", prompt_text)
            self.assertIn("skill_name: workflow-plan-verify", prompt_text)

    def test_rejects_launch_prompt_when_field_values_do_not_match_request_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            base = {
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "plan",
                "substep": "verify",
                "orchestration_id": "orch_001",
                "agent_run_id": "substep_run_plan_verify_001",
                "parent_agent_run_id": "orch_run_001",
                "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                "skill_name": "workflow-plan-verify",
                "skill_ref": "skills/workflow-plan-verify/SKILL.md",
                "issue_severity": "none",
                "repair_strategy": "none",
                "repair_target_agent_run_id": "none",
                "repair_reason": "none",
            }
            prepared = prepare_launch_request_payload(dict(base))
            prompt = build_launch_prompt_text(prepared).replace(
                "skill_name: workflow-plan-verify",
                "skill_name: workflow-plan-generate",
            ) + "\n\n必須要件:\n- 契約された substep を完了すること。\n"
            with self.assertRaisesRegex(ValueError, "template field values"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="substep_run_plan_verify_001",
                    request_payload={**prepared, "launch_prompt_full": prompt},
                    response_payload=_spawn_response_payload("sess_substep_run_plan_verify_001"),
                )

    def test_rejects_pass_step_result_when_required_outputs_are_missing_from_substeps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "orch_run_001",
                    "agent_role": "orchestration",
                    "status": "running",
                    "agent_backend": "claude",
                },
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="substep_run_plan_generate_001",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "substep_run_plan_generate_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "launch_prompt_full": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "substep_run_plan_generate_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_substep_plan_generate_001"),
            )
            record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "substep_run_plan_generate_001",
                    "parent_agent_run_id": "orch_run_001",
                    "agent_role": "substep",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "status": "pass",
                    "agent_backend": "codex",
                    "agent_model": "gpt-5-codex",
                    "context_id": "ctx_substep_plan_generate_001",
                    "agent_session_id": "sess_substep_plan_generate_001",
                    "output_refs": [
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/impl.resolved.yaml",
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json",
                    ],
                },
            )
            plan_meta_path2 = (
                repo_root
                / "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json"
            )
            plan_meta_path2.parent.mkdir(parents=True, exist_ok=True)
            plan_meta_path2.write_text(
                json.dumps({"attempt_count": 1, "verification_status": "pass", "context_isolated": True}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "required_outputs must be satisfied"):
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="plan",
                    agent_run_id="orch_run_001",
                    payload={
                        "status": "pass",
                        "required_outputs": [
                            "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/case.resolved.yaml"
                        ],
                        "failed_substeps": [],
                        "substep_agent_run_ids": ["substep_run_plan_generate_001"],
                    },
                )

    def test_record_agent_run_writes_informative_summary_when_result_summary_is_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            payload = record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "orch_run_001",
                    "agent_role": "orchestration",
                    "status": "fail",
                    "agent_backend": "claude",
                    "agent_model": "gpt-5-codex",
                    "context_id": "ctx_orch_run_001",
                    "result_summary": "compile diagnostics show missing dependency metadata",
                },
            )
            summary_path = repo_root / payload["agent_summary_ref"]
            summary_text = summary_path.read_text(encoding="utf-8")
            self.assertIn("status: fail", summary_text)
            self.assertIn(
                "result_summary: compile diagnostics show missing dependency metadata",
                summary_text,
            )

    def test_rejects_launch_response_without_child_agent_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            with self.assertRaisesRegex(ValueError, "child agent identifier"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_build_001",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "build",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "step_run_build_001",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                        "skill_name": "workflow-build",
                        "skill_ref": "skills/workflow-build/SKILL.md",
                        "skill_must_read_refs": "",
                        "launch_prompt_full": _step_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "build",
                            "step_run_build_001",
                        ),
                    },
                    response_payload={"launch_reply": "accepted: missing-id"},
                )

    def test_rejects_agent_run_when_launch_response_session_id_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="step_run_build_001",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "build",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": "",
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_build_001"),
            )
            with self.assertRaisesRegex(ValueError, "agent_session_id must match"):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "step_run_build_001",
                        "agent_role": "step",
                        "parent_agent_run_id": "orch_run_001",
                        "step": "build",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "status": "pass",
                        "agent_backend": "codex",
                        "agent_model": "gpt-5-codex",
                        "context_id": "ctx_step_build_001",
                        "agent_session_id": "sess_step_build_999",
                        "output_refs": [
                            "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate"
                        ],
                    },
                )

    def test_rejects_duplicate_agent_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [
                        {"name": "multi_agent_enabled", "pass": True},
                    ],
                },
            )
            payload = {
                "agent_run_id": "orch_run_001",
                "agent_role": "orchestration",
                "status": "pass",
                "agent_backend": "claude",
                "agent_model": "gpt-5-codex",
                "context_id": "ctx_orch_run_001",
                "result_summary": "fixture orchestration summary for duplicate agent_run_id test",
            }
            record_agent_run(repo_root=repo_root, orchestration_id="orch_001", payload=payload)
            with self.assertRaisesRegex(ValueError, "duplicate agent_run_id"):
                record_agent_run(repo_root=repo_root, orchestration_id="orch_001", payload=payload)

    def test_rejects_launch_when_preflight_cannot_launch_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "fail",
                    "can_launch_step_agents": False,
                    "can_launch_substep_agents": False,
                    "feature_states": {"multi_agent": False},
                    "checks": [
                        {"name": "multi_agent_enabled", "pass": False},
                    ],
                },
            )
            with self.assertRaisesRegex(RuntimeError, "preflight gate failed"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_plan_001",
                    request_payload={"step": "plan"},
                    response_payload=_spawn_response_payload("sess_step_plan_001"),
                )
            meta = json.loads(
                (repo_root / "workspace" / "orchestrations" / "orch_001" / "orchestration_meta.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(meta.get("status"), "fail")
            self.assertIsInstance(meta.get("finished_at"), str)

    def test_rejects_step_agent_run_when_preflight_cannot_launch_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "fail",
                    "can_launch_step_agents": False,
                    "can_launch_substep_agents": False,
                },
            )
            with self.assertRaisesRegex(RuntimeError, "preflight gate failed"):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "step_run_build_001",
                        "agent_role": "step",
                        "parent_agent_run_id": "orch_run_001",
                        "step": "build",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "status": "pass",
                        "agent_backend": "codex",
                        "agent_model": "gpt-5-codex",
                        "context_id": "ctx_step_plan_001",
                        "agent_session_id": "sess_step_plan_001",
                        "output_refs": ["workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate"],
                    },
                )

    def test_rejects_inconsistent_preflight_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            with self.assertRaisesRegex(ValueError, "feature_states.multi_agent=false"):
                write_preflight(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "status": "pass",
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                        "feature_states": {"multi_agent": False},
                        "checks": [
                            {"name": "multi_agent_enabled", "pass": False},
                        ],
                    },
                )

    def test_record_launch_runs_live_probe_when_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            os.environ["CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT"] = "1"
            with patch("tools.codex_orchestration_runtime.probe_execution_platform") as probe_mock:
                probe_mock.return_value = {
                    "checked_at": "2026-04-15T12:00:00Z",
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                }
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="substep_run_plan_generate_001",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "plan",
                        "substep": "generate",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "substep_run_plan_generate_001",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                        "skill_name": "workflow-plan-generate",
                        "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                        "skill_must_read_refs": "",
                        "issue_severity": "none",
                        "repair_strategy": "none",
                        "repair_target_agent_run_id": "none",
                        "repair_reason": "none",
                        "launch_prompt_full": _substep_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "plan",
                            "generate",
                            "substep_run_plan_generate_001",
                        ),
                    },
                    response_payload=_spawn_response_payload("sess_substep_plan_generate_001"),
                )
                self.assertEqual(probe_mock.call_count, 1)

    def test_record_and_finalize_do_not_run_live_probe_after_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            os.environ["CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT"] = "0"
            with patch(
                "tools.codex_orchestration_runtime.probe_execution_platform",
                side_effect=AssertionError("live probe must not run"),
            ):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "orch_run_001",
                        "agent_role": "orchestration",
                        "status": "running",
                        "agent_backend": "claude",
                        "started_at": "2026-03-11T00:00:00Z",
                    },
                )
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_build_001",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "build",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "step_run_build_001",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                        "skill_name": "workflow-build",
                        "skill_ref": "skills/workflow-build/SKILL.md",
                        "skill_must_read_refs": "",
                        "launch_prompt_full": _step_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "build",
                            "step_run_build_001",
                        ),
                    },
                    response_payload=_spawn_response_payload("sess_step_build_001"),
                )
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "step_run_build_001",
                        "agent_role": "step",
                        "parent_agent_run_id": "orch_run_001",
                        "step": "build",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "status": "pass",
                        "agent_backend": "codex",
                        "agent_model": "gpt-5-codex",
                        "context_id": "ctx_step_build_001",
                        "agent_session_id": "sess_step_build_001",
                        "output_refs": ["workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate"],
                    },
                )
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="build",
                    agent_run_id="step_run_build_001",
                    payload={
                        "status": "pass",
                        "validation_stage": "post_build",
                        "required_outputs": [
                            "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate"
                        ],
                        "failed_substeps": [],
                        "substep_agent_run_ids": [],
                    },
                )
                update_orchestration_status(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    status="pass",
                )

    def test_rejects_pass_status_when_graph_child_run_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "orch_run_001",
                    "agent_role": "orchestration",
                    "status": "running",
                    "agent_backend": "claude",
                },
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="step_run_build_001",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "build",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": "",
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_build_001"),
            )
            with self.assertRaisesRegex(RuntimeError, "child_agent_run_id missing from agent_runs.jsonl"):
                update_orchestration_status(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    status="pass",
                )

    def test_record_agent_run_requires_agent_backend(self) -> None:
        """agent_backend を含まないペイロードが ValueError を上げること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            with self.assertRaisesRegex(ValueError, "agent_backend must be non-empty string"):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "orch_run_001",
                        "agent_role": "orchestration",
                        "status": "running",
                    },
                )

    def test_record_agent_run_rejects_unknown_backend(self) -> None:
        """agent_backend に未知のバックエンド名を指定すると ValueError を上げること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            with self.assertRaisesRegex(ValueError, "agent_backend must be one of"):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "orch_run_001",
                        "agent_role": "orchestration",
                        "status": "running",
                        "agent_backend": "unknown_backend",
                    },
                )

    def test_record_agent_run_accepts_valid_backends(self) -> None:
        """codex / cursor / claude がそれぞれ受け付けられること。"""
        for backend in ("codex", "cursor", "claude"):
            with self.subTest(backend=backend):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_root = Path(tmp)
                    init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
                    payload = record_agent_run(
                        repo_root=repo_root,
                        orchestration_id="orch_001",
                        payload={
                            "agent_run_id": f"orch_run_{backend}_001",
                            "agent_role": "orchestration",
                            "status": "running",
                            "agent_backend": backend,
                        },
                    )
                    self.assertEqual(payload["agent_backend"], backend)

    def _setup_preflight_and_orch_agent(self, repo_root: Path) -> None:
        """共通セットアップ: init_orchestration + write_preflight + orchestration record_agent_run。"""
        init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
        write_preflight(
            repo_root=repo_root,
            orchestration_id="orch_001",
            payload={
                "status": "pass",
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
                "feature_states": {"multi_agent": True},
                "checks": [{"name": "multi_agent_enabled", "pass": True}],
            },
        )
        record_agent_run(
            repo_root=repo_root,
            orchestration_id="orch_001",
            payload={
                "agent_run_id": "orch_run_001",
                "agent_role": "orchestration",
                "status": "running",
                "agent_backend": "claude",
            },
        )

    def test_write_step_result_requires_validation_stage_for_build_pass(self) -> None:
        """validation_stage のない pass build step_result が ValueError を上げること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            with self.assertRaisesRegex(ValueError, "validation_stage"):
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="build",
                    agent_run_id="step_run_build_001",
                    payload={
                        "status": "pass",
                        "required_outputs": [
                            "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate"
                        ],
                        "failed_substeps": [],
                        "substep_agent_run_ids": [],
                    },
                )

    def test_write_step_result_accepts_valid_validation_stage_for_build(self) -> None:
        """validation_stage="post_build" を持つ pass build step_result が通ること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            # validation_stage="post_build" を含む payload で write_step_result が成功することを確認
            write_step_result(
                repo_root=repo_root,
                orchestration_id="orch_001",
                node_key="problem/shallow_water2d@0.3.0",
                step="build",
                agent_run_id="step_run_build_001",
                payload={
                    "status": "pass",
                    "validation_stage": "post_build",
                    "required_outputs": [
                        "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate"
                    ],
                    "failed_substeps": [],
                    "substep_agent_run_ids": [],
                },
            )

    def test_write_step_result_requires_validation_stage_for_execute_pass(self) -> None:
        """validation_stage のない pass execute step_result が ValueError を上げること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            with self.assertRaisesRegex(ValueError, "validation_stage"):
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="execute",
                    agent_run_id="step_run_execute_001",
                    payload={
                        "status": "pass",
                        "required_outputs": [
                            "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/execute/run_001/results.json"
                        ],
                        "failed_substeps": [],
                        "substep_agent_run_ids": [],
                    },
                )

    def test_write_step_result_does_not_require_validation_stage_for_plan_pass(self) -> None:
        """plan step の pass step_result には validation_stage を要求しないこと（後方互換）。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            # substep record が必要なので agent_runs.jsonl に直接追記する
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            substep_record = {
                "agent_run_id": "substep_run_plan_generate_001",
                "parent_agent_run_id": "orch_run_001",
                "agent_role": "substep",
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "plan",
                "substep": "generate",
                "status": "pass",
                "agent_backend": "claude",
                "output_refs": [
                    "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/case.resolved.yaml",
                    "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json",
                ],
            }
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(substep_record) + "\n")
            # plan_meta.json をディスクに作成する
            plan_meta_path = repo_root / "workspace" / "plans" / "problem__shallow_water2d__0.3.0" / "shallow-water2d_20260415_001" / "plan_meta.json"
            plan_meta_path.parent.mkdir(parents=True, exist_ok=True)
            plan_meta_path.write_text(
                json.dumps({
                    "attempt_count": 1,
                    "verification_status": "pass",
                    "context_isolated": True,
                }),
                encoding="utf-8",
            )
            # plan step には validation_stage がなくても成功することを確認
            write_step_result(
                repo_root=repo_root,
                orchestration_id="orch_001",
                node_key="problem/shallow_water2d@0.3.0",
                step="plan",
                agent_run_id="orch_run_001",
                payload={
                    "status": "pass",
                    "required_outputs": [
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/case.resolved.yaml",
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json",
                    ],
                    "failed_substeps": [],
                    "substep_agent_run_ids": ["substep_run_plan_generate_001"],
                },
            )

    def test_record_agent_run_normalizes_backend_to_lowercase(self) -> None:
        """大文字混在・前後スペース付きの agent_backend が小文字・トリム済みに正規化されること。"""
        cases = [
            ("Claude", "claude"),
            ("  Claude  ", "claude"),
            ("CODEX", "codex"),
            ("Cursor", "cursor"),
        ]
        for idx, (raw, expected) in enumerate(cases):
            with self.subTest(raw=raw):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_root = Path(tmp)
                    init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
                    payload = record_agent_run(
                        repo_root=repo_root,
                        orchestration_id="orch_001",
                        payload={
                            "agent_run_id": f"orch_run_{idx:03d}",
                            "agent_role": "orchestration",
                            "status": "running",
                            "agent_backend": raw,
                        },
                    )
                    self.assertEqual(payload["agent_backend"], expected)


    def test_write_step_result_requires_generate_meta_in_substep_outputs(self) -> None:
        """generate pass step_result で generate_meta.json が substep output_refs にない場合 ValueError。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            # substep record: generate_meta.json を含まない
            substep_record = {
                "agent_run_id": "substep_run_gen_verify_001",
                "agent_role": "substep",
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "generate",
                "substep": "verify",
                "status": "pass",
                "agent_backend": "claude",
                "output_refs": [
                    "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/src/model.f90"
                ],
            }
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(substep_record) + "\n")
            with self.assertRaisesRegex(ValueError, "generate_meta.json"):
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="generate",
                    agent_run_id="orch_run_001",
                    payload={
                        "status": "pass",
                        "validation_stage": "post_generate",
                        "required_outputs": [
                            "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/src/model.f90"
                        ],
                        "failed_substeps": [],
                        "substep_agent_run_ids": ["substep_run_gen_verify_001"],
                    },
                )

    def test_write_step_result_validates_generate_meta_required_keys(self) -> None:
        """generate_meta.json に必須キーが欠けている場合 ValueError。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/generate_meta.json"
            # 不完全な generate_meta.json を作成（attempt_count のみ）
            meta_path = repo_root / meta_ref
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps({"attempt_count": 1}), encoding="utf-8")
            substep_record = {
                "agent_run_id": "substep_run_gen_verify_001",
                "agent_role": "substep",
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "generate",
                "substep": "verify",
                "status": "pass",
                "agent_backend": "claude",
                "output_refs": [meta_ref],
            }
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(substep_record) + "\n")
            with self.assertRaisesRegex(ValueError, "missing required keys"):
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="generate",
                    agent_run_id="orch_run_001",
                    payload={
                        "status": "pass",
                        "validation_stage": "post_generate",
                        "required_outputs": [meta_ref],
                        "failed_substeps": [],
                        "substep_agent_run_ids": ["substep_run_gen_verify_001"],
                    },
                )

    def test_write_step_result_validates_plan_meta_required_keys(self) -> None:
        """plan_meta.json に必須キーが欠けている場合 ValueError。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            meta_ref = "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json"
            # 不完全な plan_meta.json（verification_status が欠けている）
            meta_path = repo_root / meta_ref
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps({"attempt_count": 1, "context_isolated": True}), encoding="utf-8")
            substep_record = {
                "agent_run_id": "substep_run_plan_generate_001",
                "agent_role": "substep",
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "plan",
                "substep": "generate",
                "status": "pass",
                "agent_backend": "claude",
                "output_refs": [meta_ref],
            }
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(substep_record) + "\n")
            with self.assertRaisesRegex(ValueError, "missing required keys"):
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="plan",
                    agent_run_id="orch_run_001",
                    payload={
                        "status": "pass",
                        "required_outputs": [meta_ref],
                        "failed_substeps": [],
                        "substep_agent_run_ids": ["substep_run_plan_generate_001"],
                    },
                )

    def test_write_step_result_accepts_valid_generate_meta(self) -> None:
        """必須キーがすべて揃った generate_meta.json を含む pass generate step_result が成功する。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/generate_meta.json"
            # 完全な generate_meta.json を作成
            meta_path = repo_root / meta_ref
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(
                json.dumps({
                    "attempt_count": 1,
                    "verification_status": "pass",
                    "last_fail_reason": None,
                    "debug_mode": False,
                    "context_isolated": True,
                }),
                encoding="utf-8",
            )
            substep_record = {
                "agent_run_id": "substep_run_gen_verify_001",
                "agent_role": "substep",
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "generate",
                "substep": "verify",
                "status": "pass",
                "agent_backend": "claude",
                "output_refs": [meta_ref],
            }
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(substep_record) + "\n")
            # 例外なく完了することを確認
            result = write_step_result(
                repo_root=repo_root,
                orchestration_id="orch_001",
                node_key="problem/shallow_water2d@0.3.0",
                step="generate",
                agent_run_id="orch_run_001",
                payload={
                    "status": "pass",
                    "validation_stage": "post_generate",
                    "required_outputs": [meta_ref],
                    "failed_substeps": [],
                    "substep_agent_run_ids": ["substep_run_gen_verify_001"],
                },
            )
            self.assertEqual(result.get("status"), "pass")


    def _minimal_preflight_setup(self, repo_root: Path) -> None:
        """orchestration / preflight / orchestration agent run を最小構成でセットアップする。"""
        init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
        write_preflight(
            repo_root=repo_root,
            orchestration_id="orch_001",
            payload={
                "status": "pass",
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
                "feature_states": {"multi_agent": True},
                "checks": [{"name": "multi_agent_enabled", "pass": True}],
            },
        )
        record_agent_run(
            repo_root=repo_root,
            orchestration_id="orch_001",
            payload={
                "agent_run_id": "orch_run_001",
                "agent_role": "orchestration",
                "status": "running",
                "agent_backend": "claude",
            },
        )

    def _minimal_request_payload(self, **overrides: object) -> dict[str, object]:
        """repair_strategy / issue_severity テスト用の最小 request_payload を返す。
        node_key / step を含まないことで plan_ref の canonical format 検証をスキップする。
        """
        base: dict[str, object] = {
            "orchestration_id": "orch_001",
            "agent_run_id": "step_run_repair_001",
            "parent_agent_run_id": "orch_run_001",
            "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/swec-plan_20260413_001",
            "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/swec-pl_20260413_001",
            "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/swec-plan_20260413_001/dependency.resolved.yaml",
            "issue_severity": "none",
            "repair_strategy": "none",
            "repair_target_agent_run_id": "none",
            "repair_reason": "none",
            "launch_prompt_full": "orchestration summary for repair validation test\nstatus: running\n",
        }
        base.update(overrides)
        return base

    def test_record_launch_rejects_invalid_repair_strategy(self) -> None:
        """repair_strategy に未定義の値を渡すと ValueError が発生すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._minimal_preflight_setup(repo_root)
            with self.assertRaisesRegex(ValueError, "repair_strategy"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_repair_001",
                    request_payload=self._minimal_request_payload(repair_strategy="retry"),
                    response_payload=_spawn_response_payload("sess_step_repair_001"),
                )

    def test_record_launch_rejects_invalid_issue_severity(self) -> None:
        """issue_severity に未定義の値を渡すと ValueError が発生すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._minimal_preflight_setup(repo_root)
            with self.assertRaisesRegex(ValueError, "issue_severity"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_repair_001",
                    request_payload=self._minimal_request_payload(issue_severity="blocker"),
                    response_payload=_spawn_response_payload("sess_step_repair_001"),
                )

    def test_record_launch_requires_repair_target_for_reuse(self) -> None:
        """repair_strategy=reuse で repair_target_agent_run_id が "none" のとき ValueError。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._minimal_preflight_setup(repo_root)
            with self.assertRaisesRegex(ValueError, "repair_target_agent_run_id"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_repair_001",
                    request_payload=self._minimal_request_payload(
                        issue_severity="minor",
                        repair_strategy="reuse",
                        repair_target_agent_run_id="none",
                        repair_reason="fix indentation",
                    ),
                    response_payload=_spawn_response_payload("sess_step_repair_001"),
                )

    def test_record_launch_accepts_none_strategy_without_repair_fields(self) -> None:
        """repair_strategy=none では repair_target と repair_reason が "none" でも成功する。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._minimal_preflight_setup(repo_root)
            # repair_strategy=none では repair フィールドが "none" でも成功すること
            result = record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="step_run_repair_001",
                request_payload=self._minimal_request_payload(
                    repair_strategy="none",
                    repair_target_agent_run_id="none",
                    repair_reason="none",
                ),
                response_payload=_spawn_response_payload("sess_step_repair_001"),
            )
            self.assertIsInstance(result, dict)


class CheckpointResumeRuntimeTests(unittest.TestCase):
    """Item 8: orchestration checkpoint / resume のユニットテスト。"""

    _NK = "component/solver@0.1.0"
    _OUT = "workspace/plans/component__solver__0.1.0/solver_20260415_001/out.txt"

    def _setup_preflight_and_orch_agent(self, repo_root: Path) -> None:
        init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
        write_preflight(
            repo_root=repo_root,
            orchestration_id="orch_001",
            payload={
                "status": "pass",
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
                "feature_states": {"multi_agent": True},
                "checks": [{"name": "multi_agent_enabled", "pass": True}],
            },
        )
        record_agent_run(
            repo_root=repo_root,
            orchestration_id="orch_001",
            payload={
                "agent_run_id": "orch_run_001",
                "agent_role": "orchestration",
                "status": "running",
                "agent_backend": "claude",
            },
        )

    def test_compute_sha256_returns_consistent_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.bin"
            p.write_bytes(b"hello")
            self.assertEqual(_compute_sha256(p), _compute_sha256(p))

    def test_compute_sha256_returns_missing_for_nonexistent_file(self) -> None:
        p = Path("/nonexistent/path/that/does/not/exist_12345.bin")
        self.assertEqual(_compute_sha256(p), "sha256:missing")

    def test_compute_sha256_detects_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.bin"
            p.write_bytes(b"v1")
            h1 = _compute_sha256(p)
            p.write_bytes(b"v2")
            h2 = _compute_sha256(p)
            self.assertNotEqual(h1, h2)

    def test_build_artifact_hashes_maps_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            rel = "workspace/x/y.txt"
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text("z", encoding="utf-8")
            h = _build_artifact_hashes(repo, [rel, "", "  "])
            self.assertIn(rel, h)
            self.assertTrue(h[rel].startswith("sha256:"))

    def test_update_checkpoint_writes_entry_on_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            out = repo / self._OUT
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("data", encoding="utf-8")
            entry = update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="run-1",
                result={
                    "status": "pass",
                    "required_outputs": [self._OUT],
                    "plan_ref": "workspace/plans/component__solver__0.1.0/solver_20260415_001",
                    "pipeline_ref": "",
                },
            )
            self.assertEqual(entry.get("step"), "plan")
            cp = repo / "workspace/orchestrations/o1/orchestration_checkpoint.json"
            self.assertTrue(cp.exists())
            data = json.loads(cp.read_text(encoding="utf-8"))
            self.assertEqual(data["orchestration_id"], "o1")
            self.assertEqual(len(data["completed_steps"]), 1)

    def test_update_checkpoint_fills_refs_from_launch_request_when_result_refs_are_none(
        self,
    ) -> None:
        """plan_ref / pipeline_ref が JSON で明示的に null のとき、launch_request から補完する。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            out = repo / self._OUT
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("data", encoding="utf-8")
            lr_rel = "workspace/orchestrations/o1/step_launch.request.json"
            lr_path = repo / lr_rel
            lr_path.parent.mkdir(parents=True, exist_ok=True)
            exp_plan = "workspace/plans/component__solver__0.1.0/solver_20260415_001"
            exp_pipe = (
                "workspace/pipelines/component__solver__0.1.0/solver_20260415_001"
            )
            lr_path.write_text(
                json.dumps({"plan_ref": exp_plan, "pipeline_ref": exp_pipe}),
                encoding="utf-8",
            )
            update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="run-1",
                result={
                    "status": "pass",
                    "required_outputs": [self._OUT],
                    "plan_ref": None,
                    "pipeline_ref": None,
                    "launch_request_ref": lr_rel,
                },
            )
            data = json.loads(
                (
                    repo / "workspace/orchestrations/o1/orchestration_checkpoint.json"
                ).read_text(encoding="utf-8")
            )
            step0 = data["completed_steps"][0]
            self.assertEqual(step0["plan_ref"], exp_plan)
            self.assertEqual(step0["pipeline_ref"], exp_pipe)

    def test_update_checkpoint_skips_on_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            r = update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="run-1",
                result={"status": "fail"},
            )
            self.assertEqual(r, {})
            self.assertFalse(
                (repo / "workspace/orchestrations/o1/orchestration_checkpoint.json").exists()
            )

    def test_update_checkpoint_overwrites_same_node_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            out = repo / self._OUT
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("a", encoding="utf-8")
            base = {
                "status": "pass",
                "required_outputs": [self._OUT],
                "plan_ref": "workspace/plans/component__solver__0.1.0/solver_20260415_001",
                "pipeline_ref": "",
            }
            update_checkpoint(
                repo, "o1", node_key=self._NK, step="plan", agent_run_id="r1", result=base
            )
            out.write_text("b", encoding="utf-8")
            update_checkpoint(
                repo, "o1", node_key=self._NK, step="plan", agent_run_id="r2", result=base
            )
            data = json.loads(
                (repo / "workspace/orchestrations/o1/orchestration_checkpoint.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(len(data["completed_steps"]), 1)
            self.assertEqual(data["completed_steps"][0]["agent_run_id"], "r2")

    def test_update_checkpoint_computes_artifact_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            out = repo / self._OUT
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("fixed", encoding="utf-8")
            entry = update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="r1",
                result={
                    "status": "pass",
                    "required_outputs": [self._OUT],
                    "plan_ref": "p",
                    "pipeline_ref": "",
                },
            )
            self.assertEqual(entry["artifact_hashes"][self._OUT], _compute_sha256(out))

    def test_update_checkpoint_handles_missing_output_ref_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            missing_ref = "workspace/plans/component__solver__0.1.0/solver_20260415_001/missing.txt"
            entry = update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="r1",
                result={
                    "status": "pass",
                    "required_outputs": [missing_ref],
                    "plan_ref": "p",
                    "pipeline_ref": "",
                },
            )
            self.assertEqual(entry["artifact_hashes"][missing_ref], "sha256:missing")

    def test_verify_checkpoint_integrity_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            out = repo / self._OUT
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("ok", encoding="utf-8")
            update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="r1",
                result={
                    "status": "pass",
                    "required_outputs": [self._OUT],
                    "plan_ref": "p",
                    "pipeline_ref": "",
                },
            )
            vr = verify_checkpoint_integrity(repo, "o1")
            self.assertTrue(vr["valid"])
            self.assertEqual(vr["steps"][0]["integrity"], "ok")

    def test_verify_checkpoint_integrity_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            out = repo / self._OUT
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("v1", encoding="utf-8")
            update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="r1",
                result={
                    "status": "pass",
                    "required_outputs": [self._OUT],
                    "plan_ref": "p",
                    "pipeline_ref": "",
                },
            )
            out.write_text("v2", encoding="utf-8")
            vr = verify_checkpoint_integrity(repo, "o1")
            self.assertFalse(vr["valid"])
            self.assertEqual(vr["steps"][0]["integrity"], "stale")

    def test_verify_checkpoint_integrity_missing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            path = repo / "workspace/orchestrations/o1/orchestration_checkpoint.json"
            path.write_text(
                json.dumps({
                    "orchestration_id": "o1",
                    "schema_version": "1",
                    "last_updated_at": "2026-04-15T00:00:00Z",
                    "completed_steps": [
                        {
                            "node_key": self._NK,
                            "node_key_safe": "component__solver__0.1.0",
                            "step": "plan",
                            "agent_run_id": "r1",
                            "status": "pass",
                            "completed_at": "2026-04-15T00:00:00Z",
                            "plan_ref": "p",
                            "pipeline_ref": "",
                            "output_refs": ["x"],
                            "artifact_hashes": {"x": "sha256:missing"},
                        }
                    ],
                }),
                encoding="utf-8",
            )
            vr = verify_checkpoint_integrity(repo, "o1")
            self.assertFalse(vr["valid"])
            self.assertEqual(vr["steps"][0]["integrity"], "missing_artifacts")

    def test_verify_checkpoint_integrity_no_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            vr = verify_checkpoint_integrity(repo, "o1")
            self.assertFalse(vr["valid"])
            self.assertIn("error", vr)

    def test_check_step_completed_returns_none_when_no_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            meta = json.loads(
                (repo / "workspace/orchestrations/o1/orchestration_meta.json").read_text(
                    encoding="utf-8"
                )
            )
            meta["resume_enabled"] = True
            (repo / "workspace/orchestrations/o1/orchestration_meta.json").write_text(
                json.dumps(meta), encoding="utf-8"
            )
            self.assertIsNone(
                check_step_completed(
                    repo, "o1", node_key=self._NK, step="plan", verify_integrity=True
                )
            )

    def test_check_step_completed_returns_none_when_resume_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            out = repo / self._OUT
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("x", encoding="utf-8")
            update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="r1",
                result={
                    "status": "pass",
                    "required_outputs": [self._OUT],
                    "plan_ref": "p",
                    "pipeline_ref": "",
                },
            )
            self.assertIsNone(
                check_step_completed(
                    repo, "o1", node_key=self._NK, step="plan", verify_integrity=True
                )
            )

    def test_check_step_completed_returns_entry_when_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            enable_checkpoint_resume(repo, "o1")
            out = repo / self._OUT
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("x", encoding="utf-8")
            update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="r1",
                result={
                    "status": "pass",
                    "required_outputs": [self._OUT],
                    "plan_ref": "p",
                    "pipeline_ref": "",
                },
            )
            info = check_step_completed(
                repo, "o1", node_key=self._NK, step="plan", verify_integrity=True
            )
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info["integrity"], "ok")
            self.assertEqual(info["agent_run_id"], "r1")

    def test_check_step_completed_allows_resume_when_stored_hash_is_sha256_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            enable_checkpoint_resume(repo, "o1")
            missing_output = (
                "workspace/plans/component__solver__0.1.0/solver_20260415_001/absent.txt"
            )
            update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="r1",
                result={
                    "status": "pass",
                    "required_outputs": [missing_output],
                    "plan_ref": "p",
                    "pipeline_ref": "",
                },
            )
            self.assertEqual(
                _compute_sha256(repo / missing_output),
                "sha256:missing",
            )
            info = check_step_completed(
                repo, "o1", node_key=self._NK, step="plan", verify_integrity=True
            )
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info["integrity"], "ok")
            self.assertEqual(info["agent_run_id"], "r1")

    def test_check_step_completed_returns_none_on_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            enable_checkpoint_resume(repo, "o1")
            out = repo / self._OUT
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("x", encoding="utf-8")
            update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="r1",
                result={
                    "status": "pass",
                    "required_outputs": [self._OUT],
                    "plan_ref": "p",
                    "pipeline_ref": "",
                },
            )
            out.write_text("y", encoding="utf-8")
            self.assertIsNone(
                check_step_completed(
                    repo, "o1", node_key=self._NK, step="plan", verify_integrity=True
                )
            )

    def test_check_step_completed_returns_none_for_uncompleted_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            enable_checkpoint_resume(repo, "o1")
            self.assertIsNone(
                check_step_completed(
                    repo, "o1", node_key=self._NK, step="build", verify_integrity=True
                )
            )

    def test_check_step_completed_skip_integrity_returns_stale_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            enable_checkpoint_resume(repo, "o1")
            out = repo / self._OUT
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("x", encoding="utf-8")
            update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="r1",
                result={
                    "status": "pass",
                    "required_outputs": [self._OUT],
                    "plan_ref": "p",
                    "pipeline_ref": "",
                },
            )
            out.write_text("y", encoding="utf-8")
            info = check_step_completed(
                repo, "o1", node_key=self._NK, step="plan", verify_integrity=False
            )
            self.assertIsNotNone(info)

    def test_enable_checkpoint_resume_sets_resume_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            meta = enable_checkpoint_resume(repo, "o1")
            self.assertTrue(meta.get("resume_enabled"))
            self.assertIn("resumed_at", meta)

    def test_enable_checkpoint_resume_raises_for_nonexistent_orchestration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "orchestration not found"):
                enable_checkpoint_resume(repo, "missing")

    def test_enable_checkpoint_resume_preserves_existing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(
                repo_root=repo,
                orchestration_id="o1",
                spec_ref="spec/a.md",
                dependency_ref="dep.yaml",
            )
            enable_checkpoint_resume(repo, "o1")
            meta = json.loads(
                (repo / "workspace/orchestrations/o1/orchestration_meta.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(meta.get("spec_ref"), "spec/a.md")
            self.assertTrue(meta.get("resume_enabled"))

    def test_write_step_result_updates_checkpoint_on_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            out_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/build/build_001/bin/simulate"
            )
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"\x00")
            write_step_result(
                repo_root=repo_root,
                orchestration_id="orch_001",
                node_key="problem/shallow_water2d@0.3.0",
                step="build",
                agent_run_id="step_run_build_001",
                payload={
                    "status": "pass",
                    "validation_stage": "post_build",
                    "required_outputs": [out_ref],
                    "failed_substeps": [],
                    "substep_agent_run_ids": [],
                },
            )
            cp = (
                repo_root
                / "workspace/orchestrations/orch_001/orchestration_checkpoint.json"
            )
            self.assertTrue(cp.exists())
            data = json.loads(cp.read_text(encoding="utf-8"))
            self.assertTrue(any(s.get("step") == "build" for s in data["completed_steps"]))

    def test_write_step_result_does_not_update_checkpoint_on_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            out_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/build/build_001/bin/simulate"
            )
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"\x00")
            write_step_result(
                repo_root=repo_root,
                orchestration_id="orch_001",
                node_key="problem/shallow_water2d@0.3.0",
                step="build",
                agent_run_id="step_run_build_001",
                payload={
                    "status": "fail",
                    "required_outputs": [out_ref],
                    "failed_substeps": [],
                    "substep_agent_run_ids": [],
                },
            )
            cp = (
                repo_root
                / "workspace/orchestrations/orch_001/orchestration_checkpoint.json"
            )
            self.assertFalse(cp.exists())

    def test_write_step_result_succeeds_even_if_checkpoint_update_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            out_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/build/build_001/bin/simulate"
            )
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"\x00")
            stderr = io.StringIO()
            with patch(
                "tools.codex_orchestration_runtime.update_checkpoint",
                side_effect=RuntimeError("boom"),
            ), patch("tools.codex_orchestration_runtime.sys.stderr", stderr):
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="build",
                    agent_run_id="step_run_build_001",
                    payload={
                        "status": "pass",
                        "validation_stage": "post_build",
                        "required_outputs": [out_ref],
                        "failed_substeps": [],
                        "substep_agent_run_ids": [],
                    },
                )
            self.assertIn("checkpoint update failed", stderr.getvalue())
            step_path = (
                repo_root
                / "workspace/orchestrations/orch_001/steps/"
                "problem__shallow_water2d__0.3.0/build/step_run_build_001/step_result.json"
            )
            self.assertTrue(step_path.exists())

    def test_init_resume_from_checkpoint_sets_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(
                    [
                        "init",
                        "--repo-root",
                        str(repo),
                        "--orchestration-id",
                        "o1",
                        "--resume-from-checkpoint",
                    ]
                )
            self.assertEqual(rc, 0)
            meta = json.loads(buf.getvalue())
            self.assertTrue(meta.get("resume_enabled"))

    def test_init_resume_from_checkpoint_fails_if_orchestration_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with self.assertRaises(RuntimeError):
                main(
                    [
                        "init",
                        "--repo-root",
                        str(repo),
                        "--orchestration-id",
                        "ghost",
                        "--resume-from-checkpoint",
                    ]
                )

    def test_init_resume_from_checkpoint_does_not_overwrite_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(
                repo_root=repo,
                orchestration_id="o1",
                spec_ref="keep-me",
            )
            main(
                [
                    "init",
                    "--repo-root",
                    str(repo),
                    "--orchestration-id",
                    "o1",
                    "--resume-from-checkpoint",
                ]
            )
            meta = json.loads(
                (repo / "workspace/orchestrations/o1/orchestration_meta.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(meta.get("spec_ref"), "keep-me")

    def test_check_step_completed_cli_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            enable_checkpoint_resume(repo, "o1")
            out = repo / self._OUT
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("x", encoding="utf-8")
            update_checkpoint(
                repo,
                "o1",
                node_key=self._NK,
                step="plan",
                agent_run_id="r1",
                result={
                    "status": "pass",
                    "required_outputs": [self._OUT],
                    "plan_ref": "p",
                    "pipeline_ref": "",
                },
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(
                    [
                        "check-step-completed",
                        "--repo-root",
                        str(repo),
                        "--orchestration-id",
                        "o1",
                        "--node-key",
                        self._NK,
                        "--step",
                        "plan",
                    ]
                )
            self.assertEqual(rc, 0)
            outj = json.loads(buf.getvalue())
            self.assertTrue(outj["completed"])

    def test_read_checkpoint_cli_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(
                    [
                        "read-checkpoint",
                        "--repo-root",
                        str(repo),
                        "--orchestration-id",
                        "o1",
                    ]
                )
            self.assertEqual(rc, 0)
            outj = json.loads(buf.getvalue())
            self.assertEqual(outj["completed_steps"], [])

    def test_verify_checkpoint_integrity_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(
                    [
                        "verify-checkpoint-integrity",
                        "--repo-root",
                        str(repo),
                        "--orchestration-id",
                        "o1",
                    ]
                )
            self.assertEqual(rc, 0)
            outj = json.loads(buf.getvalue())
            self.assertFalse(outj["valid"])

    def test_record_agent_run_accepts_skipped_by_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            record_agent_run(
                repo_root=repo,
                orchestration_id="o1",
                payload={
                    "agent_run_id": "skip-001",
                    "agent_role": "skipped_by_checkpoint",
                    "status": "skipped",
                    "agent_backend": "codex",
                    "node_key": self._NK,
                    "step": "plan",
                    "skipped_step": "plan",
                    "reason": "checkpoint_integrity_ok",
                    "checkpoint_agent_run_id": "orig-run-1",
                    "result_summary": "skipped by checkpoint",
                },
            )
            runs = (repo / "workspace/orchestrations/o1/agent_runs.jsonl").read_text(encoding="utf-8")
            self.assertIn("skipped_by_checkpoint", runs)
            self.assertIn("skip-001", runs)

    def test_validate_agent_summary_text_skipped_by_checkpoint_allows_single_line(
        self,
    ) -> None:
        _validate_agent_summary_text(
            {"agent_role": "skipped_by_checkpoint", "status": "skipped"},
            "skipped by checkpoint resume marker",
        )

    def test_validate_agent_summary_text_orchestration_rejects_single_line(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "single-line"):
            _validate_agent_summary_text(
                {"agent_role": "orchestration", "status": "running"},
                "status: running",
            )


def _iso_utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _launchable_preflight_dict(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": "pass",
        "backend": "codex",
        "probe_command": "codex",
        "can_launch_step_agents": True,
        "can_launch_substep_agents": True,
        "feature_states": {"multi_agent": True},
        "checks": [{"name": "multi_agent_enabled", "pass": True}],
    }
    base.update(extra)
    return base


class PreflightLiveProbeTtlTests(unittest.TestCase):
    def test_live_preflight_mode_never_on_zero(self) -> None:
        with patch.dict(os.environ, {"CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "0"}):
            self.assertEqual(_live_preflight_mode(), "never")

    def test_live_preflight_mode_never_on_false(self) -> None:
        with patch.dict(os.environ, {"CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "false"}):
            self.assertEqual(_live_preflight_mode(), "never")

    def test_live_preflight_mode_always_on_one(self) -> None:
        with patch.dict(os.environ, {"CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "1"}):
            self.assertEqual(_live_preflight_mode(), "always")

    def test_live_preflight_mode_ttl_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT", None)
            self.assertEqual(_live_preflight_mode(), "ttl")

    def test_live_preflight_mode_ttl_on_unknown_value(self) -> None:
        with patch.dict(os.environ, {"CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto"}):
            self.assertEqual(_live_preflight_mode(), "ttl")

    def test_live_preflight_ttl_seconds_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_PREFLIGHT_TTL_SECONDS", None)
            self.assertEqual(_live_preflight_ttl_seconds(), 1800)

    def test_live_preflight_ttl_seconds_custom(self) -> None:
        with patch.dict(os.environ, {"CODEX_PREFLIGHT_TTL_SECONDS": "300"}):
            self.assertEqual(_live_preflight_ttl_seconds(), 300)

    def test_live_preflight_ttl_seconds_zero(self) -> None:
        with patch.dict(os.environ, {"CODEX_PREFLIGHT_TTL_SECONDS": "0"}):
            self.assertEqual(_live_preflight_ttl_seconds(), 0)

    def test_live_preflight_ttl_seconds_invalid_value(self) -> None:
        with patch.dict(os.environ, {"CODEX_PREFLIGHT_TTL_SECONDS": "abc"}):
            self.assertEqual(_live_preflight_ttl_seconds(), 1800)

    def test_live_preflight_ttl_seconds_negative(self) -> None:
        with patch.dict(os.environ, {"CODEX_PREFLIGHT_TTL_SECONDS": "-1"}):
            self.assertEqual(_live_preflight_ttl_seconds(), 0)

    def test_is_within_preflight_ttl_true_when_recent(self) -> None:
        ts = _iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=10))
        self.assertTrue(_is_within_preflight_ttl(ts, 1800))

    def test_is_within_preflight_ttl_false_when_expired(self) -> None:
        ts = _iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=2000))
        self.assertFalse(_is_within_preflight_ttl(ts, 1800))

    def test_is_within_preflight_ttl_false_when_ttl_zero(self) -> None:
        ts = _iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=1))
        self.assertFalse(_is_within_preflight_ttl(ts, 0))

    def test_is_within_preflight_ttl_false_on_invalid_timestamp(self) -> None:
        self.assertFalse(_is_within_preflight_ttl("not-a-date", 1800))

    def test_write_preflight_adds_probed_at_from_checked_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            checked = "2026-04-15T10:00:00Z"
            out = write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(checked_at=checked),
            )
            self.assertEqual(out["probed_at"], checked)
            raw = json.loads(
                (repo / "workspace/orchestrations/o1/preflight.json").read_text(encoding="utf-8")
            )
            self.assertEqual(raw["probed_at"], checked)

    def test_write_preflight_keeps_explicit_probed_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            checked = "2026-04-15T10:00:00Z"
            explicit = "2026-04-15T09:00:00Z"
            out = write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(checked_at=checked, probed_at=explicit),
            )
            self.assertEqual(out["probed_at"], explicit)

    def test_write_preflight_falls_back_to_utc_now_when_no_checked_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            out = write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(),
            )
            self.assertIn("probed_at", out)
            self.assertIsInstance(out["probed_at"], str)
            self.assertGreater(len(out["probed_at"]), 10)

    def test_update_preflight_probed_at_updates_only_probed_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(
                    checked_at="2026-04-15T10:00:00Z",
                    status="pass",
                ),
            )
            path = repo / "workspace/orchestrations/o1/preflight.json"
            before = json.loads(path.read_text(encoding="utf-8"))
            _update_preflight_probed_at(repo, "o1", "2026-04-16T12:00:00Z")
            after = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(after["probed_at"], "2026-04-16T12:00:00Z")
            self.assertEqual(after["status"], before["status"])
            self.assertEqual(after["can_launch_step_agents"], before["can_launch_step_agents"])

    def test_update_preflight_probed_at_noop_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            _update_preflight_probed_at(repo, "o1", "2026-04-16T12:00:00Z")

    def test_update_preflight_probed_at_noop_on_corrupted_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            path = repo / "workspace/orchestrations/o1/preflight.json"
            path.write_text("{not-json", encoding="utf-8")
            _update_preflight_probed_at(repo, "o1", "2026-04-16T12:00:00Z")
            self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")

    def test_require_preflight_launchable_skips_probe_within_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(
                    checked_at="2026-04-15T10:00:00Z",
                    probed_at=_iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=10)),
                ),
            )
            env = {
                "CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "CODEX_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch(
                    "tools.codex_orchestration_runtime.probe_execution_platform",
                    side_effect=AssertionError("probe must not run"),
                ):
                    _require_preflight_launchable(repo, "o1", enforce_live_probe=True)

    def test_require_preflight_launchable_probes_when_ttl_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(
                    checked_at="2026-04-15T10:00:00Z",
                    probed_at=_iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=2000)),
                ),
            )
            env = {
                "CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "CODEX_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch("tools.codex_orchestration_runtime.probe_execution_platform") as probe_mock:
                    probe_mock.return_value = _launchable_preflight_dict(
                        checked_at="2026-04-15T11:00:00Z",
                    )
                    _require_preflight_launchable(repo, "o1", enforce_live_probe=True)
                    self.assertEqual(probe_mock.call_count, 1)

    def test_require_preflight_launchable_probes_when_no_probed_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            path = repo / "workspace/orchestrations/o1/preflight.json"
            body = _launchable_preflight_dict(checked_at="2026-04-15T10:00:00Z")
            path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            env = {
                "CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "CODEX_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch("tools.codex_orchestration_runtime.probe_execution_platform") as probe_mock:
                    probe_mock.return_value = _launchable_preflight_dict(
                        checked_at="2026-04-15T11:00:00Z",
                    )
                    _require_preflight_launchable(repo, "o1", enforce_live_probe=True)
                    self.assertEqual(probe_mock.call_count, 1)

    def test_require_preflight_launchable_always_probes_in_always_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(
                    checked_at="2026-04-15T10:00:00Z",
                    probed_at=_iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=5)),
                ),
            )
            with patch.dict(os.environ, {"CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "1"}):
                with patch("tools.codex_orchestration_runtime.probe_execution_platform") as probe_mock:
                    probe_mock.return_value = _launchable_preflight_dict(
                        checked_at="2026-04-15T11:00:00Z",
                    )
                    _require_preflight_launchable(repo, "o1", enforce_live_probe=True)
                    self.assertEqual(probe_mock.call_count, 1)

    def test_require_preflight_launchable_skips_probe_in_never_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(
                    probed_at=_iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=2000)),
                ),
            )
            with patch.dict(os.environ, {"CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "0"}):
                with patch(
                    "tools.codex_orchestration_runtime.probe_execution_platform",
                    side_effect=AssertionError("probe must not run"),
                ):
                    _require_preflight_launchable(repo, "o1", enforce_live_probe=True)

    def test_require_preflight_launchable_updates_probed_at_after_ttl_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(
                    probed_at=_iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=2000)),
                ),
            )
            new_checked = "2026-04-15T15:30:00Z"
            env = {
                "CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "CODEX_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch("tools.codex_orchestration_runtime.probe_execution_platform") as probe_mock:
                    probe_mock.return_value = _launchable_preflight_dict(checked_at=new_checked)
                    _require_preflight_launchable(repo, "o1", enforce_live_probe=True)
            raw = json.loads(
                (repo / "workspace/orchestrations/o1/preflight.json").read_text(encoding="utf-8")
            )
            self.assertEqual(raw["probed_at"], new_checked)

    def test_require_preflight_launchable_missing_checked_at_falls_back_to_utc_now(self) -> None:
        """live probe が checked_at を返さない場合でも probed_at 更新で KeyError としない。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(
                    probed_at=_iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=2000)),
                ),
            )
            fallback = "2099-01-01T00:00:00Z"
            env = {
                "CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "CODEX_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch("tools.codex_orchestration_runtime.probe_execution_platform") as probe_mock:
                    probe_ret = _launchable_preflight_dict()
                    self.assertNotIn("checked_at", probe_ret)
                    probe_mock.return_value = probe_ret
                    with patch(
                        "tools.codex_orchestration_runtime._utc_now_iso",
                        return_value=fallback,
                    ):
                        _require_preflight_launchable(repo, "o1", enforce_live_probe=True)
            raw = json.loads(
                (repo / "workspace/orchestrations/o1/preflight.json").read_text(encoding="utf-8")
            )
            self.assertEqual(raw["probed_at"], fallback)

    def test_require_preflight_launchable_ttl_zero_always_probes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(
                    probed_at=_iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=1)),
                ),
            )
            env = {
                "CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "CODEX_PREFLIGHT_TTL_SECONDS": "0",
            }
            with patch.dict(os.environ, env):
                with patch("tools.codex_orchestration_runtime.probe_execution_platform") as probe_mock:
                    probe_mock.return_value = _launchable_preflight_dict(
                        checked_at="2026-04-15T11:00:00Z",
                    )
                    _require_preflight_launchable(repo, "o1", enforce_live_probe=True)
                    self.assertEqual(probe_mock.call_count, 1)

    def test_record_launch_skips_probe_on_second_call_within_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="orch_001")
            path = repo / "workspace/orchestrations/orch_001/preflight.json"
            path.write_text(
                json.dumps(_launchable_preflight_dict(checked_at="2026-04-15T10:00:00Z"), indent=2)
                + "\n",
                encoding="utf-8",
            )
            probe_ret = _launchable_preflight_dict(
                checked_at=_iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=30)),
            )
            env = {
                "CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "CODEX_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch("tools.codex_orchestration_runtime.probe_execution_platform") as probe_mock:
                    probe_mock.return_value = dict(probe_ret)
                    record_launch(
                        repo_root=repo,
                        orchestration_id="orch_001",
                        parent_agent_run_id="orch_run_001",
                        child_agent_run_id="substep_run_plan_generate_001",
                        request_payload={
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "plan",
                            "substep": "generate",
                            "orchestration_id": "orch_001",
                            "agent_run_id": "substep_run_plan_generate_001",
                            "parent_agent_run_id": "orch_run_001",
                            "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                            "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                            "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                            "skill_name": "workflow-plan-generate",
                            "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                            "skill_must_read_refs": "",
                            "issue_severity": "none",
                            "repair_strategy": "none",
                            "repair_target_agent_run_id": "none",
                            "repair_reason": "none",
                            "launch_prompt_full": _substep_launch_prompt(
                                "problem/shallow_water2d@0.3.0",
                                "plan",
                                "generate",
                                "substep_run_plan_generate_001",
                            ),
                        },
                        response_payload=_spawn_response_payload("sess_substep_plan_generate_001"),
                    )
                    self.assertEqual(probe_mock.call_count, 1)
                    record_launch(
                        repo_root=repo,
                        orchestration_id="orch_001",
                        parent_agent_run_id="orch_run_001",
                        child_agent_run_id="step_run_build_001",
                        request_payload={
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "build",
                            "orchestration_id": "orch_001",
                            "agent_run_id": "step_run_build_001",
                            "parent_agent_run_id": "orch_run_001",
                            "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                            "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                            "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                            "skill_name": "workflow-build",
                            "skill_ref": "skills/workflow-build/SKILL.md",
                            "skill_must_read_refs": "",
                            "launch_prompt_full": _step_launch_prompt(
                                "problem/shallow_water2d@0.3.0",
                                "build",
                                "step_run_build_001",
                            ),
                        },
                        response_payload=_spawn_response_payload("sess_step_build_001"),
                    )
                    self.assertEqual(probe_mock.call_count, 1)

    def test_record_launch_re_probes_after_ttl_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="orch_001")
            path = repo / "workspace/orchestrations/orch_001/preflight.json"
            path.write_text(
                json.dumps(_launchable_preflight_dict(checked_at="2026-04-15T10:00:00Z"), indent=2)
                + "\n",
                encoding="utf-8",
            )
            env = {
                "CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "CODEX_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch("tools.codex_orchestration_runtime.probe_execution_platform") as probe_mock:
                    probe_mock.return_value = _launchable_preflight_dict(
                        checked_at=_iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=30)),
                    )
                    record_launch(
                        repo_root=repo,
                        orchestration_id="orch_001",
                        parent_agent_run_id="orch_run_001",
                        child_agent_run_id="substep_run_plan_generate_001",
                        request_payload={
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "plan",
                            "substep": "generate",
                            "orchestration_id": "orch_001",
                            "agent_run_id": "substep_run_plan_generate_001",
                            "parent_agent_run_id": "orch_run_001",
                            "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                            "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                            "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                            "skill_name": "workflow-plan-generate",
                            "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                            "skill_must_read_refs": "",
                            "issue_severity": "none",
                            "repair_strategy": "none",
                            "repair_target_agent_run_id": "none",
                            "repair_reason": "none",
                            "launch_prompt_full": _substep_launch_prompt(
                                "problem/shallow_water2d@0.3.0",
                                "plan",
                                "generate",
                                "substep_run_plan_generate_001",
                            ),
                        },
                        response_payload=_spawn_response_payload("sess_substep_plan_generate_001"),
                    )
                    self.assertEqual(probe_mock.call_count, 1)
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    raw["probed_at"] = _iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=2000))
                    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
                    record_launch(
                        repo_root=repo,
                        orchestration_id="orch_001",
                        parent_agent_run_id="orch_run_001",
                        child_agent_run_id="step_run_build_001",
                        request_payload={
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "build",
                            "orchestration_id": "orch_001",
                            "agent_run_id": "step_run_build_001",
                            "parent_agent_run_id": "orch_run_001",
                            "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                            "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                            "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                            "skill_name": "workflow-build",
                            "skill_ref": "skills/workflow-build/SKILL.md",
                            "skill_must_read_refs": "",
                            "launch_prompt_full": _step_launch_prompt(
                                "problem/shallow_water2d@0.3.0",
                                "build",
                                "step_run_build_001",
                            ),
                        },
                        response_payload=_spawn_response_payload("sess_step_build_001"),
                    )
                    self.assertEqual(probe_mock.call_count, 2)

    def test_get_preflight_ttl_status_within_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(
                    probed_at=_iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=10)),
                ),
            )
            with patch.dict(
                os.environ,
                {
                    "CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                    "CODEX_PREFLIGHT_TTL_SECONDS": "1800",
                },
            ):
                st = get_preflight_ttl_status(repo, "o1")
            self.assertTrue(st["preflight_exists"])
            self.assertTrue(st["within_ttl"])
            self.assertTrue(st["probe_skippable"])
            self.assertIsNotNone(st["ttl_remaining_seconds"])

    def test_get_preflight_ttl_status_no_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            with patch.dict(os.environ, {"CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto"}):
                st = get_preflight_ttl_status(repo, "o1")
            self.assertFalse(st["preflight_exists"])
            self.assertFalse(st["probe_skippable"])

    def test_get_preflight_ttl_status_always_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(
                    probed_at=_iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=5)),
                ),
            )
            with patch.dict(os.environ, {"CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "1"}):
                st = get_preflight_ttl_status(repo, "o1")
            self.assertFalse(st["probe_skippable"])

    def test_preflight_status_cli_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o1")
            write_preflight(
                repo_root=repo,
                orchestration_id="o1",
                payload=_launchable_preflight_dict(
                    probed_at=_iso_utc_z(datetime.now(timezone.utc) - timedelta(seconds=10)),
                ),
            )
            buf = io.StringIO()
            with patch.dict(
                os.environ,
                {
                    "CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                    "CODEX_PREFLIGHT_TTL_SECONDS": "1800",
                },
            ):
                with redirect_stdout(buf):
                    rc = main(
                        [
                            "preflight-status",
                            "--repo-root",
                            str(repo),
                            "--orchestration-id",
                            "o1",
                        ]
                    )
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["orchestration_id"], "o1")
            self.assertTrue(out["preflight_exists"])
            self.assertIn("ttl_remaining_seconds", out)
            self.assertTrue(out["probe_skippable"])


if __name__ == "__main__":
    unittest.main()
