#!/usr/bin/env python3
"""Regression tests for Codex orchestration runtime helpers."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mcp_servers.build_runtime_server import tool_compile_project

from tools.orchestration_runtime import (
    CLI_MANAGED_EXTENSIONS,
    TERMINAL_STATUSES,
    _allowed_file_tool_paths_for_launch,
    _allowed_output_paths_for_launch,
    _effective_pass_substep_run_ids,
    _is_direct_write_path,
    _validate_paths_against_allowed_output_manifest,
    _required_launch_prompt_constraint_lines,
    _pre_phase_complete_judge_checks,
    _required_child_agent_kind,
    _build_artifact_hashes,
    _compute_sha256,
    _is_within_preflight_ttl,
    _live_preflight_mode,
    _live_preflight_ttl_seconds,
    _require_preflight_launchable,
    _write_run_write_baseline,
    _write_roots_for_launch,
    _update_preflight_probed_at,
    _validate_agent_summary_text,
    build_launch_prompt_text,
    build_skill_must_read_refs,
    check_step_completed,
    enable_checkpoint_resume,
    gate_apply_patch_writes,
    get_preflight_ttl_status,
    guarded_apply_patch,
    init_orchestration,
    log_orchestration_read,
    main,
    merge_phase_state_for_resume,
    parse_feature_list,
    pre_orchestration_start,
    pre_phase_launch,
    probe_execution_platform,
    prepare_launch_request_payload,
    probe_codex_cli,
    read_checkpoint,
    record_agent_run,
    record_launch,
    record_timeout,
    reserve_phase_root,
    render_launch_prompt_text,
    run_gate,
    update_checkpoint,
    update_orchestration_status,
    validate_mcp_build_tool_invocation,
    verify_checkpoint_integrity,
    workflow_launch_check,
    write_preflight,
    write_step_result,
)

_FIX_PLAN_REF = "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001"
_FIX_PIPE_REF = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001"
_FIX_DEP_REF = f"{_FIX_PLAN_REF}/dependency.resolved.yaml"
_FIX_PLAN_STEP_DEP_REF = "spec/problem/shallow_water2d/deps.yaml"


def _dep_ref_for_step(step: str) -> str:
    return _FIX_PLAN_STEP_DEP_REF if step.strip().lower() == "plan" else _FIX_DEP_REF


def _fixture_generate_downstream_ready(repo_root: Path, *, generation_id: str = "gen_fixture_001") -> None:
    """`build` の `pre_phase_launch` downstream gate 用に verification pass の generate ツリーを置く。"""
    gen_dir = repo_root / _FIX_PIPE_REF / "generate" / generation_id
    gen_dir.mkdir(parents=True, exist_ok=True)
    (gen_dir / "generate_meta.json").write_text(
        json.dumps({"verification_status": "pass"}),
        encoding="utf-8",
    )


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
        "dependency_ref": _dep_ref_for_step(step),
    }
    if generation_id:
        payload["generation_id"] = generation_id
    return ",".join(build_skill_must_read_refs(payload))


def _step_launch_prompt(node_key: str, step: str, agent_run_id: str) -> str:
    return render_launch_prompt_text({
        "node_key": node_key,
        "step": step,
        "agent_run_id": agent_run_id,
        "orchestration_id": "orch_001",
        "parent_agent_run_id": "orch_run_001",
        "workflow_mode": "dev",
        "plan_ref": _FIX_PLAN_REF,
        "pipeline_ref": _FIX_PIPE_REF,
        "dependency_ref": _FIX_DEP_REF,
        "skill_name": f"workflow-{step}",
        "skill_ref": f"skills/workflow-{step}/SKILL.md",
        "skill_must_read_refs": _fixture_skill_must_read_refs_step(step),
        "issue_severity": "none",
        "repair_strategy": "none",
        "repair_target_agent_run_id": "none",
        "repair_reason": "none",
    })


def _substep_launch_prompt(node_key: str, step: str, substep: str, agent_run_id: str) -> str:
    return render_launch_prompt_text({
        "node_key": node_key,
        "step": step,
        "substep": substep,
        "agent_run_id": agent_run_id,
        "orchestration_id": "orch_001",
        "parent_agent_run_id": "orch_run_001",
        "workflow_mode": "dev",
        "plan_ref": _FIX_PLAN_REF,
        "pipeline_ref": _FIX_PIPE_REF,
        "dependency_ref": _dep_ref_for_step(step),
        "skill_name": f"workflow-{step}-{substep}",
        "skill_ref": f"skills/workflow-{step}-{substep}/SKILL.md",
        "skill_must_read_refs": _fixture_skill_must_read_refs_substep(step, substep),
        "issue_severity": "none",
        "repair_strategy": "none",
        "repair_target_agent_run_id": "none",
        "repair_reason": "none",
    })


def _spawn_response_payload(session_id: str) -> dict[str, object]:
    return {
        "agent_session_id": session_id,
        "accepted": True,
        "launch_reply": f"accepted: {session_id}",
    }


def _plant_impl_resolved_yaml_make(repo_root: Path, plan_ref: str = _FIX_PLAN_REF) -> None:
    """Plant impl.resolved.yaml with toolchain.build_system: make.

    Required for tests that exercise cross-phase canonical placement: record_launch
    reads this file to gate cross-phase auto-inject on Make toolchain.
    """
    p = repo_root / plan_ref / "impl.resolved.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "toolchain:\n  language: fortran\n  build_system: make\n",
        encoding="utf-8",
    )


def _write_apply_patch_gate_evidence(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    actor_role: str,
    changed_paths: list[str],
) -> None:
    gate_path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "gates"
        / agent_run_id
        / "apply_patch_writes.json"
    )
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    gate_path.write_text(
        json.dumps(
            {
                "orchestration_id": orchestration_id,
                "agent_run_id": agent_run_id,
                "gate": "apply_patch_writes",
                "args_json": {"actor_role": actor_role, "changed_paths": changed_paths},
                "status": "pass",
                "exit_code": 0,
                "violations": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class CodexOrchestrationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_live_preflight = os.environ.get("METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT")
        self._old_assume_bwrap = os.environ.get("METDSL_ORCHESTRATION_ASSUME_BWRAP")
        self._old_codex_home = os.environ.get("METDSL_HOME")
        os.environ["METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT"] = "0"
        os.environ["METDSL_ORCHESTRATION_ASSUME_BWRAP"] = "1"
        os.environ["METDSL_HOME"] = "/tmp/codex-orchestration-test-home"

    def tearDown(self) -> None:
        if self._old_live_preflight is None:
            os.environ.pop("METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT", None)
        else:
            os.environ["METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT"] = self._old_live_preflight
        if self._old_assume_bwrap is None:
            os.environ.pop("METDSL_ORCHESTRATION_ASSUME_BWRAP", None)
        else:
            os.environ["METDSL_ORCHESTRATION_ASSUME_BWRAP"] = self._old_assume_bwrap
        if self._old_codex_home is None:
            os.environ.pop("METDSL_HOME", None)
        else:
            os.environ["METDSL_HOME"] = self._old_codex_home

    def test_terminal_statuses_do_not_include_fail_closed(self) -> None:
        self.assertEqual(TERMINAL_STATUSES, {"pass", "fail", "blocked", "timeout", "cancel"})

    def test_effective_pass_substep_run_ids_uses_violation_file_without_nameerror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orchestration_id = "orch_retry"
            violation_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / orchestration_id
                / "violations"
                / "sub_old.noncanonical_phase_write_attempt.json"
            )
            violation_path.parent.mkdir(parents=True, exist_ok=True)
            violation_path.write_text("{}", encoding="utf-8")
            payload = {
                "substep_agent_run_ids": ["sub_old", "sub_new"],
                "failed_substeps": [],
                "retry_decisions": [
                    {
                        "issue_severity": "major",
                        "repair_strategy": "reuse",
                        "repair_target_agent_run_id": "sub_old",
                        "new_agent_run_id": "sub_new",
                        "repair_reason": "repair",
                    }
                ],
            }
            run_records = {
                "sub_old": {"agent_role": "substep", "node_key": "problem/shallow_water2d@0.3.0", "step": "plan", "status": "fail"},
                "sub_new": {"agent_role": "substep", "node_key": "problem/shallow_water2d@0.3.0", "step": "plan", "status": "pass"},
            }
            with self.assertRaisesRegex(ValueError, "must use repair_strategy='restart'"):
                _effective_pass_substep_run_ids(
                    payload,
                    repo_root=repo_root,
                    orchestration_id=orchestration_id,
                    run_records=run_records,
                    node_key="problem/shallow_water2d@0.3.0",
                    step_token="plan",
                )

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
                    stdout=(
                        "multi_agent experimental true\n"
                        "codex_hooks under-development true\n"
                        "child_agents_md under development false\n"
                    ),
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
                    stdout="multi_agent experimental false\ncodex_hooks under-development true\n",
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
                return _FakeCompletedProcess(
                    0, stdout="multi_agent experimental true\ncodex_hooks under-development true\n"
                )
            raise AssertionError(args)

        result = probe_execution_platform(
            backend="codex",
            agent_command="custom-codex",
            runner=runner,
        )
        self.assertEqual(seen["command"], "custom-codex")
        self.assertEqual(result["probe_command"], "custom-codex")

    def test_probe_execution_platform_fails_when_bwrap_is_unavailable(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="codex-cli 0.114.0\n")
            if args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(
                    0,
                    stdout="multi_agent experimental true\ncodex_hooks under-development true\n",
                )
            raise AssertionError(args)

        with patch.dict(os.environ, {"METDSL_ORCHESTRATION_ASSUME_BWRAP": "0"}):
            with patch("tools.orchestration_runtime.shutil.which", return_value=None):
                result = probe_execution_platform(backend="codex", runner=runner)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["can_launch_step_agents"])
        by_name = {c["name"]: c for c in result["checks"]}
        self.assertFalse(by_name["sandbox_bwrap_available"]["pass"])

    def test_probe_execution_platform_fails_when_codex_home_is_not_writable(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="codex-cli 0.114.0\n")
            if args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(
                    0,
                    stdout="multi_agent experimental true\ncodex_hooks under-development true\n",
                )
            raise AssertionError(args)

        with patch.dict(
            os.environ,
            {
                "METDSL_HOME": "/__codex_preflight_missing_parent__/home/.codex",
                "METDSL_ORCHESTRATION_ASSUME_BWRAP": "1",
            },
        ):
            result = probe_execution_platform(backend="codex", runner=runner)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["can_launch_step_agents"])
        by_name = {c["name"]: c for c in result["checks"]}
        self.assertIn("codex_home_writable", by_name)
        self.assertFalse(by_name["codex_home_writable"]["pass"])

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
        from tools.orchestration_runtime import _probe_codex_backend
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

    def test_probe_codex_cli_fails_when_codex_hooks_is_disabled(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[1:] == ["--version"]:
                return _FakeCompletedProcess(0, stdout="codex-cli 0.120.0\n")
            if args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(
                    0,
                    stdout="multi_agent stable true\ncodex_hooks under-development false\n",
                )
            raise AssertionError(args)

        result = probe_codex_cli(codex_command="codex", runner=runner)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["can_launch_step_agents"])
        by_name = {c["name"]: c for c in result["checks"]}
        self.assertIn("codex_hooks_enabled", by_name)
        self.assertFalse(by_name["codex_hooks_enabled"]["pass"])

    def test_all_strict_boolean_probe_checks_pass_skips_none_pass(self) -> None:
        """`pass: None` の check は未実行扱いとし、それ以外がすべて True なら合格とする。"""
        from tools.orchestration_runtime import _all_strict_boolean_probe_checks_pass

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
        from tools.orchestration_runtime import _all_strict_boolean_probe_checks_pass

        self.assertFalse(
            _all_strict_boolean_probe_checks_pass([{"name": "x"}])
        )

    def test_probe_help_fallback_uses_help_when_features_list_fails(self) -> None:
        """_probe_help_fallback_backend が features list 失敗時に --help fallback を試みること。"""
        from tools.orchestration_runtime import _probe_help_fallback_backend
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
        from tools.orchestration_runtime import (
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
        self.assertNotIn(
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
        self.assertIn("guarded-apply-patch", prompt)

    def test_writes_orchestration_artifacts_in_canonical_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(
                repo_root=repo_root,
                orchestration_id="orch_001",
                spec_ref="spec/problem/shallow_water2d/controlled_spec.md",
                source_dependency_ref="spec/problem/shallow_water2d/deps.yaml",
            )
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
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
                    "dependency_ref": "spec/problem/shallow_water2d/deps.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": [
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/case.resolved.yaml",
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json",
                    ],
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
                    "allowed_output_paths": [
                        "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate",
                    ],
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
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="substep_run_plan_generate_001",
                actor_role="substep",
                changed_paths=[
                    "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/case.resolved.yaml",
                    "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json",
                ],
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
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="step_run_build_001",
                actor_role="step",
                changed_paths=[
                    "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate"
                ],
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
                json.dumps(self._valid_plan_meta()),
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
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/case.resolved.yaml",
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json",
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

    def test_record_launch_strips_child_agent_run_id_for_tmp_and_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}],
                },
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="  substep_run_strip_001  ",
                request_payload={
                    "agent_run_id": "substep_run_strip_001",
                    "agent_role": "substep",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "spec/problem/shallow_water2d/deps.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": [
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/case.resolved.yaml",
                    ],
                    "launch_prompt_full": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "substep_run_strip_001",
                    ),
                },
                response_payload={
                    "agent_run_id": "substep_run_strip_001",
                    **_spawn_response_payload("sess_substep_strip"),
                },
            )
            ok_manifest = (
                repo_root
                / "workspace/orchestrations/orch_001/output_manifests/substep_run_strip_001.json"
            )
            spaced_manifest = (
                repo_root
                / "workspace/orchestrations/orch_001/output_manifests/  substep_run_strip_001  .json"
            )
            self.assertTrue(ok_manifest.exists())
            self.assertFalse(spaced_manifest.exists())
            manifest = json.loads(ok_manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest.get("allowed_tmp_root"), "workspace/tmp/substep_run_strip_001")
            self.assertTrue((repo_root / "workspace/tmp/substep_run_strip_001").is_dir())

    def test_record_launch_prefers_prompt_over_launch_prompt_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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

    def test_record_launch_succeeds_with_generate_directory_allowed_output_path(self) -> None:
        """record_launch for step=generate with a directory allowed_output_path must not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [
                        {"name": "multi_agent_enabled", "pass": True},
                        {"name": "codex_hooks_enabled", "pass": True},
                        {"name": "codex_home_writable", "pass": True},
                        {"name": "sandbox_bwrap_available", "pass": True},
                        {"name": "sandbox_bwrap_userns", "pass": True},
                    ],
                },
            )
            gen_id = "gen_20260508_001"
            src_dir = f"{_FIX_PIPE_REF}/generate/{gen_id}/src/"
            launch_refs = record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="step_run_gen_dir_001",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "generate",
                    "agent_role": "step",
                    "agent_run_id": "step_run_gen_dir_001",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": f"{_FIX_PLAN_REF}/dependency.resolved.yaml",
                    "generation_id": gen_id,
                    "allowed_output_paths": [src_dir],
                    "skill_name": "workflow-generate",
                    "skill_ref": "skills/workflow-generate/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("generate"),
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "generate",
                        "step_run_gen_dir_001",
                    ),
                },
                response_payload={
                    "agent_run_id": "step_run_gen_dir_001",
                    **_spawn_response_payload("sess_step_gen_dir_001"),
                },
            )
            manifest_path = (
                repo_root
                / "workspace/orchestrations/orch_001/output_manifests/step_run_gen_dir_001.json"
            )
            self.assertTrue(manifest_path.exists(), "output manifest must be written")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertIn(src_dir, manifest["allowed_output_paths"])

    def _record_generate_launch_with_outputs(
        self,
        *,
        repo_root: Path,
        orchestration_id: str,
        child_agent_run_id: str,
        gen_id: str,
        allowed_output_paths: list[str],
    ) -> Path:
        init_orchestration(repo_root=repo_root, orchestration_id=orchestration_id)
        write_preflight(
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            payload={
                "status": "pass",
                "sandbox_runtime": "bwrap",
                "sandbox_enforced": True,
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
                "feature_states": {"multi_agent": True, "codex_hooks": True},
                "checks": [
                    {"name": "multi_agent_enabled", "pass": True},
                    {"name": "codex_hooks_enabled", "pass": True},
                    {"name": "codex_home_writable", "pass": True},
                    {"name": "sandbox_bwrap_available", "pass": True},
                    {"name": "sandbox_bwrap_userns", "pass": True},
                ],
            },
        )
        record_launch(
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            parent_agent_run_id="orch_run_001",
            child_agent_run_id=child_agent_run_id,
            request_payload={
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "generate",
                "agent_role": "step",
                "agent_run_id": child_agent_run_id,
                "orchestration_id": orchestration_id,
                "parent_agent_run_id": "orch_run_001",
                "plan_ref": _FIX_PLAN_REF,
                "pipeline_ref": _FIX_PIPE_REF,
                "dependency_ref": f"{_FIX_PLAN_REF}/dependency.resolved.yaml",
                "generation_id": gen_id,
                "allowed_output_paths": allowed_output_paths,
                "skill_name": "workflow-generate",
                "skill_ref": "skills/workflow-generate/SKILL.md",
                "skill_must_read_refs": _fixture_skill_must_read_refs_step("generate"),
                "launch_prompt_full": _step_launch_prompt(
                    "problem/shallow_water2d@0.3.0",
                    "generate",
                    child_agent_run_id,
                ),
            },
            response_payload={
                "agent_run_id": child_agent_run_id,
                **_spawn_response_payload(f"sess_{child_agent_run_id}"),
            },
        )
        return (
            repo_root
            / f"workspace/orchestrations/{orchestration_id}/output_manifests/{child_agent_run_id}.json"
        )

    def test_allowed_output_paths_auto_injects_mcp_command_log(self) -> None:
        """Generate step launches must auto-inject <gen>/src/mcp_command_log.jsonl."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            gen_id = "gen_20260510_001"
            src_dir = f"{_FIX_PIPE_REF}/generate/{gen_id}/src/"
            manifest_path = self._record_generate_launch_with_outputs(
                repo_root=repo_root,
                orchestration_id="orch_001",
                child_agent_run_id="run_auto_inject_dir",
                gen_id=gen_id,
                allowed_output_paths=[src_dir],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            log_path = f"{src_dir}mcp_command_log.jsonl"
            self.assertIn(log_path, manifest["allowed_output_paths"])

    def test_auto_inject_idempotent_when_already_listed(self) -> None:
        """Pre-listing the log path must not duplicate it after auto-inject."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            gen_id = "gen_20260510_002"
            src_dir = f"{_FIX_PIPE_REF}/generate/{gen_id}/src/"
            log_path = f"{src_dir}mcp_command_log.jsonl"
            manifest_path = self._record_generate_launch_with_outputs(
                repo_root=repo_root,
                orchestration_id="orch_001",
                child_agent_run_id="run_auto_inject_idempotent",
                gen_id=gen_id,
                allowed_output_paths=[src_dir, log_path],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            occurrences = [p for p in manifest["allowed_output_paths"] if p == log_path]
            self.assertEqual(len(occurrences), 1)

    def test_auto_inject_extracts_gen_id_from_file_entry(self) -> None:
        """File-only entries under <gen>/src/ must still trigger log path injection."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            gen_id = "gen_20260510_003"
            src_file = f"{_FIX_PIPE_REF}/generate/{gen_id}/src/main.f90"
            manifest_path = self._record_generate_launch_with_outputs(
                repo_root=repo_root,
                orchestration_id="orch_001",
                child_agent_run_id="run_auto_inject_file_entry",
                gen_id=gen_id,
                allowed_output_paths=[src_file],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected = f"{_FIX_PIPE_REF}/generate/{gen_id}/src/mcp_command_log.jsonl"
            self.assertIn(expected, manifest["allowed_output_paths"])

    def test_auto_inject_skipped_when_no_src_entry(self) -> None:
        """Without any src/ entry the log path must NOT be injected."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            gen_id = "gen_20260510_004"
            generate_meta = (
                f"{_FIX_PIPE_REF}/generate/{gen_id}/generate_meta.json"
            )
            manifest_path = self._record_generate_launch_with_outputs(
                repo_root=repo_root,
                orchestration_id="orch_001",
                child_agent_run_id="run_auto_inject_no_src",
                gen_id=gen_id,
                allowed_output_paths=[
                    f"{_FIX_PIPE_REF}/lineage.json",
                    generate_meta,
                ],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for p in manifest["allowed_output_paths"]:
                self.assertFalse(
                    p.endswith("mcp_command_log.jsonl"),
                    f"unexpected auto-inject without src/ entry: {p}",
                )

    def test_generate_launch_rejects_listed_paths_for_multiple_generations(self) -> None:
        """A generate launch must target exactly one generation_id.

        Listing paths under two distinct <gen_id>/src/ prefixes would let the
        launch silently auto-inject MCP-owned audit logs for both, granting
        write authority across sibling generations and breaking provenance
        isolation.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            gen1 = "gen_20260510_005"
            gen2 = "gen_20260510_006"
            src1 = f"{_FIX_PIPE_REF}/generate/{gen1}/src/"
            src2 = f"{_FIX_PIPE_REF}/generate/{gen2}/src/"
            with self.assertRaisesRegex(
                ValueError, "must target a single generation_id"
            ):
                self._record_generate_launch_with_outputs(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    child_agent_run_id="run_auto_inject_multi",
                    gen_id=gen1,
                    allowed_output_paths=[src1, src2],
                )

    def test_generate_launch_rejects_listed_path_for_other_generation(self) -> None:
        """If request.generation_id is set, listed paths must use that gen_id only."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            real_gen = "gen_20260510_007"
            other_gen = "gen_20260510_008"
            src_other = f"{_FIX_PIPE_REF}/generate/{other_gen}/src/"
            with self.assertRaisesRegex(
                ValueError,
                "must target a single generation_id|does not match request generation_id",
            ):
                self._record_generate_launch_with_outputs(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    child_agent_run_id="run_gen_mismatch",
                    gen_id=real_gen,
                    allowed_output_paths=[src_other],
                )

    def test_execute_launch_rejects_listed_path_for_other_execution_id(self) -> None:
        """If request.execution_id is set, listed paths must use that exec_id only."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch

        node_safe = "problem__shallow_water2d__0.3.0"
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "execute",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "execution_id": "exec_real_001",
            "allowed_output_paths": [
                # Caller declares execution_id=exec_real_001 but lists a path
                # under a different execution_id.
                f"{_FIX_PIPE_REF}/execute/exec_other_002/{node_safe}/diagnostics.json",
            ],
        }
        with self.assertRaisesRegex(
            ValueError,
            "must target a single execution_id|does not match request execution_id",
        ):
            _allowed_output_paths_for_launch(
                request_payload=req,
                write_roots=[f"{_FIX_PIPE_REF}/execute/"],
            )

    def test_build_launch_accepts_cross_phase_log_for_make_build(self) -> None:
        """In-source Make builds (Fortran/C family) run compile_project with
        project_dir=<gen>/src/, so the MCP audit log lands in the generate
        tree. The build launch must auto-inject the cross-phase canonical
        placement when generation_id is provided.
        """
        from tools.orchestration_runtime import (
            _allowed_output_paths_for_launch,
            _canonical_mcp_audit_log_paths_for_request,
        )

        gen_id = "gen_make_build_001"
        build_id = "build_make_001"
        cross_log = (
            f"{_FIX_PIPE_REF}/generate/{gen_id}/src/mcp_command_log.jsonl"
        )
        in_phase_log = (
            f"{_FIX_PIPE_REF}/build/{build_id}/mcp_command_log.jsonl"
        )
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "build",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "generation_id": gen_id,
            # `_resolved_build_system` is normally injected by record_launch
            # from impl.resolved.yaml; for direct helper tests we set it.
            "_resolved_build_system": "make",
            "allowed_output_paths": [
                f"{_FIX_PIPE_REF}/build/{build_id}/build_meta.json",
                f"{_FIX_PIPE_REF}/build/{build_id}/bin/main",
            ],
        }
        out = _allowed_output_paths_for_launch(
            request_payload=req,
            write_roots=[
                f"{_FIX_PIPE_REF}/build/",
                f"{_FIX_PIPE_REF}/generate/",
            ],
        )
        # Both placements (in-phase and cross-phase) auto-injected.
        self.assertIn(in_phase_log, out)
        self.assertIn(cross_log, out)
        canonical = set(_canonical_mcp_audit_log_paths_for_request(req, out))
        self.assertIn(in_phase_log, canonical)
        self.assertIn(cross_log, canonical)

    def test_build_launch_skips_cross_phase_log_for_non_make_toolchain(self) -> None:
        """Cross-phase canonical placement is Make-only.

        For CMake/Meson/Ninja or any out-of-source build, the cross-phase
        generate-tree audit log must NOT be auto-injected even when
        generation_id is provided. Otherwise non-Make builds would gain
        write authority into the generate tree contrary to spec.
        """
        from tools.orchestration_runtime import _allowed_output_paths_for_launch

        gen_id = "gen_cmake_001"
        build_id = "build_cmake_001"
        cross_log = (
            f"{_FIX_PIPE_REF}/generate/{gen_id}/src/mcp_command_log.jsonl"
        )
        in_phase_log = (
            f"{_FIX_PIPE_REF}/build/{build_id}/mcp_command_log.jsonl"
        )
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "build",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "generation_id": gen_id,
            # Non-Make toolchain → cross-phase not authorized.
            "_resolved_build_system": "cmake",
            "allowed_output_paths": [
                f"{_FIX_PIPE_REF}/build/{build_id}/build_meta.json",
                f"{_FIX_PIPE_REF}/build/{build_id}/bin/main",
            ],
        }
        out = _allowed_output_paths_for_launch(
            request_payload=req,
            write_roots=[
                f"{_FIX_PIPE_REF}/build/",
            ],
        )
        # In-phase log is auto-injected; cross-phase is NOT.
        self.assertIn(in_phase_log, out)
        self.assertNotIn(cross_log, out)

    def test_build_launch_rejects_cross_phase_log_for_failed_generation(self) -> None:
        """Build cross-phase log authorization mirrors execute: failed/stale
        generation must be rejected at record_launch (verification_status=fail)
        before write authority is granted.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            _plant_impl_resolved_yaml_make(repo_root)
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
            failed_gen = "gen_failed_for_build"
            build_id = "build_against_failed_001"
            # Pre_phase_launch requires AT LEAST ONE pass-state generation
            # under the pipeline. Plant one so the gate doesn't fire before
            # our cross-phase verification reaches the failed gen the launch
            # actually targets.
            ok_gen_dir = repo_root / f"{_FIX_PIPE_REF}/generate/gen_ok_001"
            ok_gen_dir.mkdir(parents=True, exist_ok=True)
            (ok_gen_dir / "generate_meta.json").write_text(
                '{"verification_status": "pass"}\n', encoding="utf-8"
            )
            gen_dir = repo_root / f"{_FIX_PIPE_REF}/generate/{failed_gen}"
            gen_dir.mkdir(parents=True, exist_ok=True)
            (gen_dir / "generate_meta.json").write_text(
                '{"verification_status": "fail"}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ValueError, "verification_status='fail'"
            ):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_build_failed_gen",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "build",
                        "agent_role": "step",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "step_run_build_failed_gen",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_DEP_REF,
                        "generation_id": failed_gen,
                        "skill_name": "workflow-build",
                        "skill_ref": "skills/workflow-build/SKILL.md",
                        "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                        "allowed_output_paths": [
                            f"{_FIX_PIPE_REF}/build/{build_id}/build_meta.json",
                        ],
                        "launch_prompt_full": _step_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "build",
                            "step_run_build_failed_gen",
                        ),
                    },
                    response_payload=_spawn_response_payload("sess_step_build_failed_gen"),
                )

    def test_build_launch_rejects_listed_paths_for_multiple_build_ids(self) -> None:
        """A build launch must target exactly one build_id."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch

        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "build",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [
                f"{_FIX_PIPE_REF}/build/build_a/build_meta.json",
                f"{_FIX_PIPE_REF}/build/build_b/build_meta.json",
            ],
        }
        with self.assertRaisesRegex(ValueError, "must target a single build_id"):
            _allowed_output_paths_for_launch(
                request_payload=req,
                write_roots=[f"{_FIX_PIPE_REF}/build/"],
            )

    def test_auto_inject_log_excluded_from_allowed_file_tool_paths(self) -> None:
        """Auto-injected MCP audit log must not become directly Edit/Write-eligible.

        validate_pipeline_semantics.py reads mcp_command_log.jsonl as the source
        of truth that MCP run_linter actually executed. Direct file-tool writes
        would let a child forge ok=true entries and bypass static-lint
        verification.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            gen_id = "gen_20260510_007"
            src_dir = f"{_FIX_PIPE_REF}/generate/{gen_id}/src/"
            manifest_path = self._record_generate_launch_with_outputs(
                repo_root=repo_root,
                orchestration_id="orch_001",
                child_agent_run_id="run_log_not_filetool",
                gen_id=gen_id,
                allowed_output_paths=[src_dir],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            log_path = f"{src_dir}mcp_command_log.jsonl"
            self.assertIn(log_path, manifest["allowed_output_paths"])
            self.assertNotIn(log_path, manifest["allowed_file_tool_paths"])
            for p in manifest["allowed_file_tool_paths"]:
                self.assertFalse(
                    p.endswith("mcp_command_log.jsonl"),
                    f"integrity-protected log leaked into allowed_file_tool_paths: {p}",
                )

    def test_explicit_file_tool_listing_of_mcp_log_rejected(self) -> None:
        """Caller cannot bypass the protection by explicitly listing the canonical log path."""
        from tools.orchestration_runtime import _allowed_file_tool_paths_for_launch

        log_path = (
            f"{_FIX_PIPE_REF}/generate/gen_20260510_008/src/mcp_command_log.jsonl"
        )
        req = {
            "agent_role": "step",
            "step": "generate",
            "node_key": "problem/shallow_water2d@0.3.0",
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_file_tool_paths": [log_path],
        }
        with self.assertRaisesRegex(ValueError, "canonical MCP audit log"):
            _allowed_file_tool_paths_for_launch(
                request_payload=req,
                allowed_output_paths=[log_path],
            )

    def test_noncanonical_mcp_log_basename_remains_writable(self) -> None:
        """Files with the basename mcp_command_log.jsonl at NON-canonical paths
        (e.g. nested under src/subdir/) must be treated as ordinary outputs:
        Edit/Write-eligible and not protected as MCP-owned. The canonical
        placement under <gen>/src/ is still classified MCP-owned, but a
        sibling path one directory deeper is not.
        """
        from tools.orchestration_runtime import (
            _allowed_file_tool_paths_for_launch,
            _canonical_mcp_audit_log_paths_for_request,
        )

        gen_id = "gen_20260510_009"
        canonical_path = (
            f"{_FIX_PIPE_REF}/generate/{gen_id}/src/mcp_command_log.jsonl"
        )
        noncanonical_path = (
            f"{_FIX_PIPE_REF}/generate/{gen_id}/src/notes/mcp_command_log.jsonl"
        )
        req = {
            "agent_role": "step",
            "step": "generate",
            "node_key": "problem/shallow_water2d@0.3.0",
            "pipeline_ref": _FIX_PIPE_REF,
        }
        canonical_set = set(
            _canonical_mcp_audit_log_paths_for_request(req, [noncanonical_path])
        )
        # The noncanonical path is NOT in the canonical set even though its
        # basename matches; only the canonical placement is.
        self.assertNotIn(noncanonical_path, canonical_set)
        self.assertIn(canonical_path, canonical_set)
        # Auto-derived file_tool_paths must include the noncanonical path
        # because .jsonl is not CLI-managed and the path is not MCP-owned.
        out = _allowed_file_tool_paths_for_launch(
            request_payload=req,
            allowed_output_paths=[noncanonical_path],
        )
        self.assertIn(noncanonical_path, out)
        # The canonical placement, if also listed, is excluded from file_tool.
        out2 = _allowed_file_tool_paths_for_launch(
            request_payload=req,
            allowed_output_paths=[noncanonical_path, canonical_path],
        )
        self.assertIn(noncanonical_path, out2)
        self.assertNotIn(canonical_path, out2)

    def test_build_phase_auto_injects_and_accepts_mcp_command_log(self) -> None:
        """Build step must auto-inject <build_id>/mcp_command_log.jsonl (compile_project log)."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch

        build_id = "build_20260510_001"
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "build",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [
                f"{_FIX_PIPE_REF}/build/{build_id}/bin/main",
                f"{_FIX_PIPE_REF}/build/{build_id}/build_meta.json",
            ],
        }
        out = _allowed_output_paths_for_launch(
            request_payload=req,
            write_roots=[f"{_FIX_PIPE_REF}/build/"],
        )
        expected_log = f"{_FIX_PIPE_REF}/build/{build_id}/mcp_command_log.jsonl"
        self.assertIn(expected_log, out)

    def test_build_phase_rejects_log_in_unrecognized_subdir(self) -> None:
        """Build phase only accepts the log directly under <build_id>/, not under arbitrary subdirs."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch

        build_id = "build_20260510_002"
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "build",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [
                # Log path under an unrecognized subdir (not /bin/, not directly under build_id).
                f"{_FIX_PIPE_REF}/build/{build_id}/logs/mcp_command_log.jsonl",
            ],
        }
        with self.assertRaisesRegex(ValueError, "outside phase contract"):
            _allowed_output_paths_for_launch(
                request_payload=req,
                write_roots=[f"{_FIX_PIPE_REF}/build/"],
            )

    def test_execute_phase_auto_injects_and_accepts_mcp_command_log(self) -> None:
        """Execute step must auto-inject <exec_id>/<node_safe>/mcp_command_log.jsonl."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch

        exec_id = "exec_20260510_001"
        node_safe = "problem__shallow_water2d__0.3.0"
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "execute",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [
                f"{_FIX_PIPE_REF}/execute/{exec_id}/{node_safe}/diagnostics.json",
                f"{_FIX_PIPE_REF}/execute/{exec_id}/{node_safe}/perf.json",
            ],
        }
        out = _allowed_output_paths_for_launch(
            request_payload=req,
            write_roots=[f"{_FIX_PIPE_REF}/execute/"],
        )
        expected_log = (
            f"{_FIX_PIPE_REF}/execute/{exec_id}/{node_safe}/mcp_command_log.jsonl"
        )
        self.assertIn(expected_log, out)

    def test_build_log_excluded_from_allowed_file_tool_paths(self) -> None:
        """Auto-injected build log must remain integrity-protected (not Edit/Write-eligible)."""
        from tools.orchestration_runtime import (
            _allowed_file_tool_paths_for_launch,
            _allowed_output_paths_for_launch,
        )

        build_id = "build_20260510_003"
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "build",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [
                f"{_FIX_PIPE_REF}/build/{build_id}/bin/main",
                f"{_FIX_PIPE_REF}/build/{build_id}/build_meta.json",
            ],
        }
        allowed = _allowed_output_paths_for_launch(
            request_payload=req,
            write_roots=[f"{_FIX_PIPE_REF}/build/"],
        )
        file_tool = _allowed_file_tool_paths_for_launch(
            request_payload=req,
            allowed_output_paths=allowed,
        )
        log_path = f"{_FIX_PIPE_REF}/build/{build_id}/mcp_command_log.jsonl"
        self.assertIn(log_path, allowed)
        self.assertNotIn(log_path, file_tool)

    def test_rejects_launch_with_placeholder_plan_or_pipeline_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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

    def test_rejects_launch_when_dependency_ref_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            with self.assertRaisesRegex(ValueError, "launch request must include non-empty dependency_ref"):
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

    def test_rejects_launch_with_placeholder_dependency_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            with self.assertRaisesRegex(ValueError, "launch request dependency_ref must not contain placeholder tokens"):
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
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/<agent-determined-dependency-ref>",
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
                },
                response_payload=_spawn_response_payload("sess_substep_run_plan_verify_001"),
            )
            request_path = repo_root / launch_refs["launch_request_ref"]
            prompt_path = repo_root / launch_refs["launch_prompt_ref"]
            request_payload = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request_payload["skill_name"], "workflow-plan-verify")
            self.assertEqual(request_payload["skill_ref"], "skills/workflow-plan-verify/SKILL.md")
            self.assertNotIn(
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
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
            with self.assertRaisesRegex(
                ValueError, "must preserve workflow-orchestration template field values"
            ):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="substep_run_plan_verify_001",
                    request_payload={**prepared, "launch_prompt_full": prompt},
                    response_payload=_spawn_response_payload("sess_substep_run_plan_verify_001"),
                )

    def test_rejects_launch_prompt_when_shell_write_constraint_line_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            prepared = prepare_launch_request_payload(
                {
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "build",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "issue_severity": "none",
                    "repair_strategy": "none",
                    "repair_target_agent_run_id": "none",
                    "repair_reason": "none",
                }
            )
            prompt = "\n".join(
                line
                for line in render_launch_prompt_text(prepared).splitlines()
                if "`run-gate --gate apply_patch_writes`" not in line
                and "`output_manifests/" not in line
            )
            with self.assertRaisesRegex(ValueError, "shell-write constraints"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_build_001",
                    request_payload={**prepared, "launch_prompt_full": prompt},
                    response_payload=_spawn_response_payload("sess_step_build_001"),
                )

    def test_rejects_launch_prompt_when_capability_token_constraint_line_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            prepared = prepare_launch_request_payload(
                {
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "build",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "issue_severity": "none",
                    "repair_strategy": "none",
                    "repair_target_agent_run_id": "none",
                    "repair_reason": "none",
                }
            )
            prompt = "\n".join(
                line
                for line in render_launch_prompt_text(prepared).splitlines()
                if "/capabilities/" not in line
                and "`capability_token` が未取得または不一致の場合" not in line
            )
            with self.assertRaisesRegex(ValueError, "shell-write constraints"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_build_001",
                    request_payload={**prepared, "launch_prompt_full": prompt},
                    response_payload=_spawn_response_payload("sess_step_build_001"),
                )

    def test_rejects_launch_prompt_when_output_manifest_constraint_line_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            prepared = prepare_launch_request_payload(
                {
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "build",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "issue_severity": "none",
                    "repair_strategy": "none",
                    "repair_target_agent_run_id": "none",
                    "repair_reason": "none",
                }
            )
            prompt = "\n".join(
                line
                for line in render_launch_prompt_text(prepared).splitlines()
                if "`output_manifests/" not in line
            )
            with self.assertRaisesRegex(ValueError, "shell-write constraints"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_build_001",
                    request_payload={**prepared, "launch_prompt_full": prompt},
                    response_payload=_spawn_response_payload("sess_step_build_001"),
                )

    def test_constraint_line_selector_excludes_read_manifest_canonical_source_line(self) -> None:
        prepared = prepare_launch_request_payload(
            {
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "build",
                "orchestration_id": "orch_001",
                "agent_run_id": "step_run_build_001",
                "parent_agent_run_id": "orch_run_001",
                "plan_ref": _FIX_PLAN_REF,
                "pipeline_ref": _FIX_PIPE_REF,
                "dependency_ref": _FIX_DEP_REF,
                "skill_name": "workflow-build",
                "skill_ref": "skills/workflow-build/SKILL.md",
                "issue_severity": "none",
                "repair_strategy": "none",
                "repair_target_agent_run_id": "none",
                "repair_reason": "none",
            }
        )
        constraint_lines = _required_launch_prompt_constraint_lines(prepared)
        self.assertTrue(constraint_lines)
        # The read-manifest canonical-source line ("直接読み取ってよい") is a permission
        # guidance line, not a constraint — it must be excluded.
        self.assertFalse(any("読み取ってよい" in line for line in constraint_lines))
        # The cross-agent artifact prohibition is a security constraint — it must be included.
        self.assertTrue(any("他 agent の内部 artifact" in line for line in constraint_lines))
        self.assertTrue(any("`.json` と `.txt` の出力は" in line for line in constraint_lines))
        self.assertTrue(
            any(
                "`.yaml` / `.yml` / `.md` および source code 等の上記以外の出力は" in line
                for line in constraint_lines
            )
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "agent_role": "substep",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "substep_run_plan_generate_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": [
                        "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/impl.resolved.yaml",
                    ],
                    "launch_prompt_full": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "substep_run_plan_generate_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_substep_plan_generate_001"),
            )
            impl_path = (
                repo_root
                / "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/impl.resolved.yaml"
            )
            impl_path.parent.mkdir(parents=True, exist_ok=True)
            impl_path.write_text("{}\n", encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="substep_run_plan_generate_001",
                actor_role="substep",
                changed_paths=[
                    "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/impl.resolved.yaml",
                ],
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
                    ],
                },
            )
            plan_meta_path2 = (
                repo_root
                / "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json"
            )
            plan_meta_path2.parent.mkdir(parents=True, exist_ok=True)
            plan_meta_path2.write_text(
                json.dumps(self._valid_plan_meta()),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "effective substep output_refs"):
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="plan",
                    agent_run_id="orch_run_001",
                    payload={
                        "status": "pass",
                        "required_outputs": [
                            "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json"
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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

    def test_record_agent_run_rejects_orchestration_pass_output_refs_without_gate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            with self.assertRaisesRegex(
                ValueError, "pass status for orchestration requires apply_patch_writes gate evidence"
            ):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "orch_run_001",
                        "agent_role": "orchestration",
                        "status": "pass",
                        "agent_backend": "claude",
                        "output_refs": [
                            "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json"
                        ],
                    },
                )

    def test_record_agent_run_accepts_orchestration_pass_output_refs_with_gate_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            out_ref = "workspace/orchestrations/orch_001/logs/orchestrator.note.txt"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("note\n", encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="orch_run_001",
                actor_role="orchestration",
                changed_paths=[out_ref],
            )
            payload = record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "orch_run_001",
                    "agent_role": "orchestration",
                    "status": "pass",
                    "agent_backend": "claude",
                    "output_refs": [out_ref],
                },
            )
            self.assertEqual(payload["output_refs"], [out_ref])

    def test_record_agent_run_accepts_orchestration_pass_without_output_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            payload = record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "orch_run_001",
                    "agent_role": "orchestration",
                    "status": "pass",
                    "agent_backend": "claude",
                    "result_summary": "orchestration completed without direct artifact edits",
                },
            )
            self.assertEqual(payload["status"], "pass")
            self.assertNotIn("output_refs", payload)

    def test_record_agent_run_rejects_orchestration_pass_when_gate_paths_do_not_cover_output_refs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            out_ref = "workspace/orchestrations/orch_001/logs/orchestrator.note.txt"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("note\n", encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="orch_run_001",
                actor_role="orchestration",
                changed_paths=["workspace/orchestrations/orch_001/logs/other.note.txt"],
            )
            with self.assertRaisesRegex(
                ValueError, "apply_patch_writes gate does not cover terminal output_refs"
            ):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "orch_run_001",
                        "agent_role": "orchestration",
                        "status": "pass",
                        "agent_backend": "claude",
                        "output_refs": [out_ref],
                    },
                )

    def test_record_agent_run_rejects_orchestration_pass_when_gate_actor_role_mismatches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            out_ref = "workspace/orchestrations/orch_001/logs/orchestrator.note.txt"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("note\n", encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="orch_run_001",
                actor_role="step",
                changed_paths=[out_ref],
            )
            with self.assertRaisesRegex(ValueError, "apply_patch_writes gate actor_role mismatch"):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "orch_run_001",
                        "agent_role": "orchestration",
                        "status": "pass",
                        "agent_backend": "claude",
                        "output_refs": [out_ref],
                    },
                )

    def test_record_agent_run_rejects_orchestration_terminal_with_noncanonical_phase_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            bad_ref = (
                "workspace/plans/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/plan_meta.json"
            )
            bad_path = repo_root / bad_ref
            bad_path.parent.mkdir(parents=True, exist_ok=True)
            bad_path.write_text('{"verification_status":"pass"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "terminal run has unauthorized write paths"):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "orch_run_001",
                        "agent_role": "orchestration",
                        "status": "fail",
                        "agent_backend": "claude",
                        "result_summary": "orchestration wrote a phase artifact directly",
                    },
                )

    def test_record_agent_run_rejects_step_terminal_with_undeclared_actual_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                    "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/build_meta.json"],
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_build_001"),
            )
            out_ref = f"{_FIX_PIPE_REF}/build/build_001/build_meta.json"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text('{"status":"ok"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "terminal run has unauthorized write paths"):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "step_run_build_001",
                        "agent_role": "step",
                        "parent_agent_run_id": "orch_run_001",
                        "step": "build",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "status": "fail",
                        "agent_backend": "codex",
                        "agent_model": "gpt-5-codex",
                        "context_id": "ctx_step_build_001",
                        "agent_session_id": "sess_step_build_001",
                        "result_summary": "shell write bypassed apply_patch gate",
                    },
                )

    def test_record_agent_run_rejects_step_pass_output_ref_write_without_gate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                    "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/build_meta.json"],
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_build_001"),
            )
            out_ref = f"{_FIX_PIPE_REF}/build/build_001/build_meta.json"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text('{"status":"ok"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "terminal run has unauthorized write paths"):
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
                        "output_refs": [out_ref],
                    },
                )

    def test_record_agent_run_rejects_step_terminal_write_outside_gate_changed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                    "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/build_meta.json"],
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_build_001"),
            )
            out_ref = f"{_FIX_PIPE_REF}/build/build_001/build_meta.json"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text('{"status":"ok"}\n', encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="step_run_build_001",
                actor_role="step",
                changed_paths=[f"{_FIX_PIPE_REF}/build/build_001/bin/other"],
            )
            with self.assertRaisesRegex(ValueError, "terminal run has unauthorized write paths"):
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
                        "output_refs": [out_ref],
                    },
                )

    def test_record_agent_run_accepts_step_terminal_when_gate_matches_actual_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                    "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"],
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_build_001"),
            )
            out_ref = f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("binary\n", encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="step_run_build_001",
                actor_role="step",
                changed_paths=[out_ref],
            )
            payload = record_agent_run(
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
                    "output_refs": [out_ref],
                },
            )
            self.assertEqual(payload["output_refs"], [out_ref])

    def test_noncanonical_mcp_log_in_allowed_output_paths_is_not_mcp_trusted(self) -> None:
        """A manifest entry with the basename mcp_command_log.jsonl at a
        non-canonical path (e.g. <gen>/src/notes/mcp_command_log.jsonl) must
        NOT be persisted as an MCP-owned audit log. Only canonical placements
        under <gen>/src/ directly (alongside model/runner sources) qualify.
        Defense against an over-broad manifest silently auto-trusting an
        arbitrary file.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            gen_id = "gen_20260510_010"
            canonical_log = (
                f"{_FIX_PIPE_REF}/generate/{gen_id}/src/mcp_command_log.jsonl"
            )
            noncanonical = (
                f"{_FIX_PIPE_REF}/generate/{gen_id}/src/notes/mcp_command_log.jsonl"
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="step_run_noncanon",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "generate",
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_noncanon",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": f"{_FIX_PLAN_REF}/dependency.resolved.yaml",
                    "generation_id": gen_id,
                    # Caller declares both a canonical and a noncanonical
                    # mcp_command_log.jsonl. Phase contract for /src/ is
                    # loose enough to accept the noncanonical entry.
                    "allowed_output_paths": [
                        f"{_FIX_PIPE_REF}/generate/{gen_id}/src/main.f90",
                        noncanonical,
                    ],
                    "skill_name": "workflow-generate",
                    "skill_ref": "skills/workflow-generate/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("generate"),
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "generate",
                        "step_run_noncanon",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_run_noncanon"),
            )
            manifest = json.loads(
                (
                    repo_root
                    / "workspace/orchestrations/orch_001/output_manifests/step_run_noncanon.json"
                ).read_text(encoding="utf-8")
            )
            # The canonical placement is auto-injected and recorded as
            # MCP-owned; the noncanonical path is treated as a normal output.
            self.assertIn(canonical_log, manifest["mcp_owned_audit_logs"])
            self.assertNotIn(noncanonical, manifest["mcp_owned_audit_logs"])
            # The noncanonical path is auto-derived as Edit/Write-eligible
            # (regular .jsonl, not MCP-owned).
            self.assertIn(noncanonical, manifest["allowed_file_tool_paths"])
            # The canonical placement is excluded from file_tool paths.
            self.assertNotIn(canonical_log, manifest["allowed_file_tool_paths"])

    def test_record_agent_run_accepts_mcp_command_log_without_gate_provenance(self) -> None:
        """Terminal validation must accept MCP-written mcp_command_log.jsonl.

        MCP server writes the log directly (no guarded-apply-patch, no
        Edit/Write tool invocation). It is auto-injected into
        allowed_output_paths but excluded from allowed_file_tool_paths for
        integrity. _validate_actual_write_paths() must recognize integrity-
        protected MCP audit log entries in the manifest as authorized
        MCP-owned outputs, otherwise a successful build/execute would be
        fail-closed for the very file the MCP tool just produced.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
            build_id = "build_20260510_001"
            bin_ref = f"{_FIX_PIPE_REF}/build/{build_id}/bin/simulate"
            log_ref = f"{_FIX_PIPE_REF}/build/{build_id}/mcp_command_log.jsonl"
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="step_run_build_mcp_log",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "build",
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_mcp_log",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                    "allowed_output_paths": [bin_ref],
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_mcp_log",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_build_mcp_log"),
            )
            # Confirm auto-inject placed log in allowed_output_paths but not in file_tool list.
            manifest = json.loads(
                (
                    repo_root
                    / "workspace/orchestrations/orch_001/output_manifests/step_run_build_mcp_log.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn(log_ref, manifest["allowed_output_paths"])
            self.assertNotIn(log_ref, manifest["allowed_file_tool_paths"])

            # Simulate build artefacts on disk:
            #   - the binary, written via guarded-apply-patch (gate provenance)
            #   - the MCP audit log, written by MCP server directly (no gate)
            bin_path = repo_root / bin_ref
            bin_path.parent.mkdir(parents=True, exist_ok=True)
            bin_path.write_text("binary\n", encoding="utf-8")
            log_path = repo_root / log_ref
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                '{"command_id":"abc","tool_name":"compile_project","ok":true}\n',
                encoding="utf-8",
            )
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="step_run_build_mcp_log",
                actor_role="step",
                changed_paths=[bin_ref],
            )
            payload = record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "step_run_build_mcp_log",
                    "agent_role": "step",
                    "parent_agent_run_id": "orch_run_001",
                    "step": "build",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "status": "pass",
                    "agent_backend": "codex",
                    "agent_model": "gpt-5-codex",
                    "context_id": "ctx_step_build_mcp_log",
                    "agent_session_id": "sess_step_build_mcp_log",
                    "output_refs": [bin_ref, log_ref],
                },
            )
            self.assertEqual(payload["status"], "pass")
            self.assertIn(log_ref, payload["output_refs"])
            # No unauthorized_write_violation file should have been written.
            violation_path = (
                repo_root
                / "workspace/orchestrations/orch_001/violations"
                / "step_run_build_mcp_log.unauthorized_write_violation.json"
            )
            self.assertFalse(violation_path.exists())

    def test_execute_run_quality_checks_cross_phase_mcp_log_authorized(self) -> None:
        """Execute's run_quality_checks runs with project_dir=generate/<gen>/src/
        per skills/workflow-execute/SKILL.md L20. The MCP server's default
        command_log_path resolves to project_dir/mcp_command_log.jsonl, so the
        audit log lands under the generate tree even though the agent role is
        execute. The cross-phase canonical placement must be:
          - auto-injected when execute requests include `generation_id`
          - phase-contract accepted
          - terminal-validated as authorized despite execute's write_roots
            being scoped to <pipeline_ref>/execute/.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            _plant_impl_resolved_yaml_make(repo_root)
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
            gen_id = "gen_20260510_011"
            exec_id = "exec_20260510_001"
            node_safe = "problem__shallow_water2d__0.3.0"
            # Cross-phase log auto-inject requires the referenced generation
            # to have actually run (generate_meta.json must exist). In a real
            # workflow generate completes before execute launches.
            gen_meta_dir = repo_root / f"{_FIX_PIPE_REF}/generate/{gen_id}"
            gen_meta_dir.mkdir(parents=True, exist_ok=True)
            (gen_meta_dir / "generate_meta.json").write_text(
                '{"verification_status": "pass"}\n', encoding="utf-8"
            )
            # Pipeline build phase must also exist for execute pre-launch
            # readiness check (downstream_phase_launch_gate). The build_meta
            # must record source_generation_id for the cross-phase lineage
            # bind to authorize the request's generation_id.
            build_id_for_lineage = "build_20260510_001"
            build_dir = (
                repo_root / f"{_FIX_PIPE_REF}/build/{build_id_for_lineage}"
            )
            (build_dir / "bin").mkdir(parents=True, exist_ok=True)
            (build_dir / "bin/main").write_text("binary\n", encoding="utf-8")
            (build_dir / "build_meta.json").write_text(
                json.dumps({
                    "build_system": "make",
                    "compiler": "gfortran",
                    "build_log_ref": "...",
                    "status": "pass",
                    "source_generation_id": gen_id,
                }) + "\n",
                encoding="utf-8",
            )
            cross_phase_log = (
                f"{_FIX_PIPE_REF}/generate/{gen_id}/src/mcp_command_log.jsonl"
            )
            in_phase_log = (
                f"{_FIX_PIPE_REF}/execute/{exec_id}/{node_safe}/mcp_command_log.jsonl"
            )
            diagnostics_ref = (
                f"{_FIX_PIPE_REF}/execute/{exec_id}/{node_safe}/diagnostics.json"
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="step_run_exec_qc",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "execute",
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_exec_qc",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "execution_id": exec_id,
                    # generation_id triggers cross-phase log auto-inject for execute.
                    "generation_id": gen_id,
                    "source_build_id": build_id_for_lineage,
                    "skill_name": "workflow-execute",
                    "skill_ref": "skills/workflow-execute/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("execute"),
                    "allowed_output_paths": [diagnostics_ref],
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "execute",
                        "step_run_exec_qc",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_exec_qc"),
            )
            manifest = json.loads(
                (
                    repo_root
                    / "workspace/orchestrations/orch_001/output_manifests/step_run_exec_qc.json"
                ).read_text(encoding="utf-8")
            )
            # Both the in-phase and cross-phase canonical placements are auto-injected.
            self.assertIn(in_phase_log, manifest["allowed_output_paths"])
            self.assertIn(cross_phase_log, manifest["allowed_output_paths"])
            # Both are in mcp_owned_audit_logs and excluded from file_tool list.
            self.assertIn(in_phase_log, manifest["mcp_owned_audit_logs"])
            self.assertIn(cross_phase_log, manifest["mcp_owned_audit_logs"])
            self.assertNotIn(in_phase_log, manifest["allowed_file_tool_paths"])
            self.assertNotIn(cross_phase_log, manifest["allowed_file_tool_paths"])

            # Simulate the artefacts on disk:
            #   - diagnostics.json written via guarded-apply-patch (gate provenance)
            #   - cross-phase mcp_command_log.jsonl written by MCP run_quality_checks
            #     (no gate provenance; cross-phase placement under generate/)
            diag_path = repo_root / diagnostics_ref
            diag_path.parent.mkdir(parents=True, exist_ok=True)
            diag_path.write_text("{}\n", encoding="utf-8")
            log_path = repo_root / cross_phase_log
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                '{"command_id":"qc1","tool_name":"run_quality_checks","ok":true}\n',
                encoding="utf-8",
            )
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="step_run_exec_qc",
                actor_role="step",
                changed_paths=[diagnostics_ref],
            )
            payload = record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "step_run_exec_qc",
                    "agent_role": "step",
                    "parent_agent_run_id": "orch_run_001",
                    "step": "execute",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "status": "pass",
                    "agent_backend": "codex",
                    "agent_model": "gpt-5-codex",
                    "context_id": "ctx_step_run_exec_qc",
                    "agent_session_id": "sess_step_exec_qc",
                    "output_refs": [diagnostics_ref, cross_phase_log],
                },
            )
            self.assertEqual(payload["status"], "pass")
            violation_path = (
                repo_root
                / "workspace/orchestrations/orch_001/violations"
                / "step_run_exec_qc.unauthorized_write_violation.json"
            )
            self.assertFalse(violation_path.exists())

    def test_execute_launch_rejects_listed_path_for_unrelated_generation(self) -> None:
        """Defense against authorization-by-listing of an unrelated generation.

        If an execute launch lists `<pipeline_ref>/generate/<other>/src/...`
        in allowed_output_paths (a generation different from the request's
        `generation_id`), the launch must be rejected. Otherwise an older or
        sibling generation's audit log could gain MCP-owned write authority.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
            real_gen = "gen_real_001"
            other_gen = "gen_other_002"
            exec_id = "exec_unrelated_001"
            node_safe = "problem__shallow_water2d__0.3.0"
            # Both generations exist on disk (e.g., older sibling under same pipeline).
            for gid in (real_gen, other_gen):
                gdir = repo_root / f"{_FIX_PIPE_REF}/generate/{gid}"
                gdir.mkdir(parents=True, exist_ok=True)
                (gdir / "generate_meta.json").write_text(
                    '{"verification_status": "pass"}\n', encoding="utf-8"
                )
            (repo_root / f"{_FIX_PIPE_REF}/build/build_x/bin").mkdir(
                parents=True, exist_ok=True
            )
            (repo_root / f"{_FIX_PIPE_REF}/build/build_x/bin/main").write_text(
                "binary\n", encoding="utf-8"
            )
            diagnostics_ref = (
                f"{_FIX_PIPE_REF}/execute/{exec_id}/{node_safe}/diagnostics.json"
            )
            other_log_ref = (
                f"{_FIX_PIPE_REF}/generate/{other_gen}/src/mcp_command_log.jsonl"
            )
            # Launch with generation_id=real_gen but ALSO list a path under
            # the unrelated other_gen tree. The phase contract for execute
            # should reject the unrelated generate path because it does not
            # match the request's generation_id.
            with self.assertRaisesRegex(
                ValueError, "outside phase contract|under capability write_roots"
            ):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_exec_unrelated",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "execute",
                        "agent_role": "step",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "step_run_exec_unrelated",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_DEP_REF,
                        "execution_id": exec_id,
                        "generation_id": real_gen,
                        "skill_name": "workflow-execute",
                        "skill_ref": "skills/workflow-execute/SKILL.md",
                        "skill_must_read_refs": _fixture_skill_must_read_refs_step("execute"),
                        "allowed_output_paths": [diagnostics_ref, other_log_ref],
                        "launch_prompt_full": _step_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "execute",
                            "step_run_exec_unrelated",
                        ),
                    },
                    response_payload=_spawn_response_payload("sess_step_exec_unrelated"),
                )

    def test_execute_launch_accepts_when_legacy_build_coexists_with_recorded_build(self) -> None:
        """Legacy build directories without source_generation_id must NOT
        block valid execute launches as long as at least one current build
        records the lineage and matches the request's generation_id.

        Defense against operational dead-end: pipelines accumulate historical
        build dirs over retries, and over-broad strict bind would render the
        pipeline unusable for future executes once any single build_meta is
        missing the field.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            _plant_impl_resolved_yaml_make(repo_root)
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
            real_gen = "gen_real_001"
            exec_id = "exec_test_001"
            node_safe = "problem__shallow_water2d__0.3.0"
            gd = repo_root / f"{_FIX_PIPE_REF}/generate/{real_gen}"
            gd.mkdir(parents=True, exist_ok=True)
            (gd / "generate_meta.json").write_text(
                '{"verification_status": "pass"}\n', encoding="utf-8"
            )
            # Legacy build dir WITHOUT source_generation_id (older retry).
            legacy_dir = repo_root / f"{_FIX_PIPE_REF}/build/build_legacy_999"
            (legacy_dir / "bin").mkdir(parents=True, exist_ok=True)
            (legacy_dir / "bin/main").write_text("legacy_binary\n", encoding="utf-8")
            (legacy_dir / "build_meta.json").write_text(
                json.dumps({
                    "build_system": "make",
                    "compiler": "gfortran",
                    "build_log_ref": "...",
                    "status": "pass",
                }) + "\n",
                encoding="utf-8",
            )
            # Current build with source_generation_id matching the request.
            current_dir = repo_root / f"{_FIX_PIPE_REF}/build/build_current_001"
            (current_dir / "bin").mkdir(parents=True, exist_ok=True)
            (current_dir / "bin/main").write_text("current_binary\n", encoding="utf-8")
            (current_dir / "build_meta.json").write_text(
                json.dumps({
                    "build_system": "make",
                    "compiler": "gfortran",
                    "build_log_ref": "...",
                    "status": "pass",
                    "source_generation_id": real_gen,
                }) + "\n",
                encoding="utf-8",
            )
            diagnostics_ref = (
                f"{_FIX_PIPE_REF}/execute/{exec_id}/{node_safe}/diagnostics.json"
            )
            # Execute launch with generation_id=real_gen must succeed even
            # though legacy_build is missing source_generation_id.
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="step_run_exec_legacy_coexist",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "execute",
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_exec_legacy_coexist",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "execution_id": exec_id,
                    "generation_id": real_gen,
                    "source_build_id": "build_current_001",
                    "skill_name": "workflow-execute",
                    "skill_ref": "skills/workflow-execute/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("execute"),
                    "allowed_output_paths": [diagnostics_ref],
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "execute",
                        "step_run_exec_legacy_coexist",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_exec_legacy_coexist"),
            )
            manifest = json.loads(
                (
                    repo_root
                    / "workspace/orchestrations/orch_001/output_manifests/step_run_exec_legacy_coexist.json"
                ).read_text(encoding="utf-8")
            )
            cross_log = (
                f"{_FIX_PIPE_REF}/generate/{real_gen}/src/mcp_command_log.jsonl"
            )
            self.assertIn(cross_log, manifest["mcp_owned_audit_logs"])

    def test_execute_launch_requires_source_build_id_for_in_phase_log_only(self) -> None:
        """source_build_id is required for ALL execute launches, not only
        cross-phase quality_check authorization. An execute that uses only
        in-phase logging must still be bound to a specific build to prevent
        attributing evidence to an arbitrary build that happens to record a
        matching source_generation_id.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
            real_gen = "gen_real_001"
            exec_id = "exec_in_phase_001"
            node_safe = "problem__shallow_water2d__0.3.0"
            gd = repo_root / f"{_FIX_PIPE_REF}/generate/{real_gen}"
            gd.mkdir(parents=True, exist_ok=True)
            (gd / "generate_meta.json").write_text(
                '{"verification_status": "pass"}\n', encoding="utf-8"
            )
            bd = repo_root / f"{_FIX_PIPE_REF}/build/build_x"
            (bd / "bin").mkdir(parents=True, exist_ok=True)
            (bd / "bin/main").write_text("binary\n", encoding="utf-8")
            (bd / "build_meta.json").write_text(
                json.dumps({
                    "build_system": "make",
                    "compiler": "gfortran",
                    "build_log_ref": "...",
                    "status": "pass",
                    "source_generation_id": real_gen,
                }) + "\n",
                encoding="utf-8",
            )
            diagnostics_ref = (
                f"{_FIX_PIPE_REF}/execute/{exec_id}/{node_safe}/diagnostics.json"
            )
            # Launch WITHOUT source_build_id → reject regardless of cross-phase status.
            with self.assertRaisesRegex(
                ValueError,
                "execute launch requires `source_build_id`",
            ):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_exec_no_build_id",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "execute",
                        "agent_role": "step",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "step_run_exec_no_build_id",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_DEP_REF,
                        "execution_id": exec_id,
                        "generation_id": real_gen,
                        # No source_build_id, no cross-phase log declared.
                        "skill_name": "workflow-execute",
                        "skill_ref": "skills/workflow-execute/SKILL.md",
                        "skill_must_read_refs": _fixture_skill_must_read_refs_step("execute"),
                        "allowed_output_paths": [diagnostics_ref],
                        "launch_prompt_full": _step_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "execute",
                            "step_run_exec_no_build_id",
                        ),
                    },
                    response_payload=_spawn_response_payload("sess_step_exec_no_build_id"),
                )

    def test_execute_launch_rejects_unrelated_generation_when_build_meta_records_lineage(self) -> None:
        """Cross-phase generation_id must match an actual build's
        source_generation_id. If build_meta.json under the pipeline records
        source_generation_id and the request's generation_id does not match
        any of them, the cross-phase write authorization is rejected.

        Defense against attributing quality_check evidence to a passing
        sibling generation that was not used by any build.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            _plant_impl_resolved_yaml_make(repo_root)
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
            real_gen = "gen_real_001"
            unrelated_gen = "gen_unrelated_999"
            build_id = "build_real_001"
            exec_id = "exec_test_001"
            node_safe = "problem__shallow_water2d__0.3.0"
            # Two pass-state generations + one build_meta that records ONLY
            # real_gen as its source_generation_id.
            for gid in (real_gen, unrelated_gen):
                gd = repo_root / f"{_FIX_PIPE_REF}/generate/{gid}"
                gd.mkdir(parents=True, exist_ok=True)
                (gd / "generate_meta.json").write_text(
                    '{"verification_status": "pass"}\n', encoding="utf-8"
                )
            bd = repo_root / f"{_FIX_PIPE_REF}/build/{build_id}"
            (bd / "bin").mkdir(parents=True, exist_ok=True)
            (bd / "bin/main").write_text("binary\n", encoding="utf-8")
            (bd / "build_meta.json").write_text(
                json.dumps({
                    "build_system": "make",
                    "compiler": "gfortran",
                    "build_log_ref": "...",
                    "status": "pass",
                    "source_generation_id": real_gen,
                }) + "\n",
                encoding="utf-8",
            )
            diagnostics_ref = (
                f"{_FIX_PIPE_REF}/execute/{exec_id}/{node_safe}/diagnostics.json"
            )
            with self.assertRaisesRegex(
                ValueError,
                "does not match build .* source_generation_id",
            ):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_exec_unrelated_gen",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "execute",
                        "agent_role": "step",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "step_run_exec_unrelated_gen",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_DEP_REF,
                        "execution_id": exec_id,
                        # Request points at the real build but claims
                        # unrelated_gen — should fail because build_real_001's
                        # source_generation_id is real_gen, not unrelated_gen.
                        "generation_id": unrelated_gen,
                        "source_build_id": build_id,
                        "skill_name": "workflow-execute",
                        "skill_ref": "skills/workflow-execute/SKILL.md",
                        "skill_must_read_refs": _fixture_skill_must_read_refs_step("execute"),
                        "allowed_output_paths": [diagnostics_ref],
                        "launch_prompt_full": _step_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "execute",
                            "step_run_exec_unrelated_gen",
                        ),
                    },
                    response_payload=_spawn_response_payload("sess_step_exec_unrelated_gen"),
                )

    def test_execute_launch_rejects_cross_phase_log_for_failed_generation(self) -> None:
        """Defense against authorizing writes into a failed generation's tree.

        Even when generate_meta.json exists for the referenced generation_id,
        record_launch must reject the cross-phase MCP audit log authorization
        if verification_status is not 'pass'. Otherwise an Execute run could
        mutate provenance files inside a failed/stale generation before the
        run is later rejected by post_execute, contaminating cross-phase
        artifacts irreversibly.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            _plant_impl_resolved_yaml_make(repo_root)
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
            failed_gen = "gen_failed_001"
            exec_id = "exec_against_failed_001"
            node_safe = "problem__shallow_water2d__0.3.0"
            # Plant a failed generation (verification_status=fail).
            gen_dir = repo_root / f"{_FIX_PIPE_REF}/generate/{failed_gen}"
            gen_dir.mkdir(parents=True, exist_ok=True)
            (gen_dir / "generate_meta.json").write_text(
                '{"verification_status": "fail"}\n', encoding="utf-8"
            )
            # build_x's build_meta.json records source_generation_id=failed_gen
            # so the mandatory lineage bind passes and the cross-phase
            # verification_status check is reached.
            build_x_dir = repo_root / f"{_FIX_PIPE_REF}/build/build_x"
            (build_x_dir / "bin").mkdir(parents=True, exist_ok=True)
            (build_x_dir / "bin/main").write_text("binary\n", encoding="utf-8")
            (build_x_dir / "build_meta.json").write_text(
                json.dumps({
                    "build_system": "make",
                    "compiler": "gfortran",
                    "build_log_ref": "...",
                    "status": "pass",
                    "source_generation_id": failed_gen,
                }) + "\n",
                encoding="utf-8",
            )
            diagnostics_ref = (
                f"{_FIX_PIPE_REF}/execute/{exec_id}/{node_safe}/diagnostics.json"
            )
            with self.assertRaisesRegex(
                ValueError, "verification_status='fail'"
            ):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_exec_failed_gen",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "execute",
                        "agent_role": "step",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "step_run_exec_failed_gen",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_DEP_REF,
                        "execution_id": exec_id,
                        "generation_id": failed_gen,
                        "source_build_id": "build_x",
                        "skill_name": "workflow-execute",
                        "skill_ref": "skills/workflow-execute/SKILL.md",
                        "skill_must_read_refs": _fixture_skill_must_read_refs_step("execute"),
                        "allowed_output_paths": [diagnostics_ref],
                        "launch_prompt_full": _step_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "execute",
                            "step_run_exec_failed_gen",
                        ),
                    },
                    response_payload=_spawn_response_payload("sess_step_exec_failed_gen"),
                )

    def test_execute_launch_rejects_cross_phase_log_for_unknown_generation_id(self) -> None:
        """Defense against authorization-by-injection of cross-phase log paths.

        If the launch request's `generation_id` does not correspond to an
        actual generate run on disk (`generate_meta.json` missing),
        record_launch must refuse rather than silently auto-inject a writable
        cross-phase log path that targets an unrelated generation's audit log.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            _plant_impl_resolved_yaml_make(repo_root)
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
            forged_gen_id = "gen_20260510_xxx_unknown"
            exec_id = "exec_20260510_002"
            node_safe = "problem__shallow_water2d__0.3.0"
            # Plant a build_meta that records the forged gen so the
            # mandatory source_build_id lineage check passes; the cross-phase
            # generate_meta absence then triggers the unknown-generation
            # rejection in the cross-phase loop.
            build_dir = repo_root / f"{_FIX_PIPE_REF}/build/build_for_forged"
            (build_dir / "bin").mkdir(parents=True, exist_ok=True)
            (build_dir / "bin/main").write_text("binary\n", encoding="utf-8")
            (build_dir / "build_meta.json").write_text(
                json.dumps({
                    "build_system": "make",
                    "compiler": "gfortran",
                    "build_log_ref": "...",
                    "status": "pass",
                    "source_generation_id": forged_gen_id,
                }) + "\n",
                encoding="utf-8",
            )
            diagnostics_ref = (
                f"{_FIX_PIPE_REF}/execute/{exec_id}/{node_safe}/diagnostics.json"
            )
            with self.assertRaisesRegex(
                ValueError, "unknown cross-phase generation_id"
            ):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_exec_forged",
                    request_payload={
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "execute",
                        "agent_role": "step",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "step_run_exec_forged",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_DEP_REF,
                        "execution_id": exec_id,
                        "generation_id": forged_gen_id,
                        "source_build_id": "build_for_forged",
                        "skill_name": "workflow-execute",
                        "skill_ref": "skills/workflow-execute/SKILL.md",
                        "skill_must_read_refs": _fixture_skill_must_read_refs_step("execute"),
                        "allowed_output_paths": [diagnostics_ref],
                        "launch_prompt_full": _step_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "execute",
                            "step_run_exec_forged",
                        ),
                    },
                    response_payload=_spawn_response_payload("sess_step_exec_forged"),
                )

    def test_record_agent_run_accepts_substep_terminal_when_tmp_file_is_under_allowed_tmp_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                child_agent_run_id="substep_run_gen_001",
                request_payload={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "agent_role": "substep",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "substep_run_gen_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_substep("plan", "generate"),
                    "allowed_output_paths": [f"{_FIX_PLAN_REF}/plan_meta.json"],
                    "launch_prompt_full": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "substep_run_gen_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_substep_gen_001"),
            )
            out_ref = f"{_FIX_PLAN_REF}/plan_meta.json"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text('{"status":"ok"}\n', encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="substep_run_gen_001",
                actor_role="substep",
                changed_paths=[out_ref],
            )
            tmp_file_path = repo_root / "workspace" / "tmp" / "substep_run_gen_001" / "guarded_patch_plan_meta.txt"
            tmp_file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_file_path.write_text("patch metadata\n", encoding="utf-8")
            payload = record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "substep_run_gen_001",
                    "agent_role": "substep",
                    "parent_agent_run_id": "orch_run_001",
                    "step": "plan",
                    "substep": "generate",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "status": "pass",
                    "agent_backend": "claude",
                    "agent_model": "claude-sonnet-4-6",
                    "context_id": "ctx_substep_gen_001",
                    "agent_session_id": "sess_substep_gen_001",
                    "output_refs": [out_ref],
                },
            )
            self.assertEqual(payload["output_refs"], [out_ref])

    def test_record_agent_run_rejects_substep_terminal_when_allowed_tmp_root_is_overbroad(self) -> None:
        from tools.orchestration_runtime import _write_allowed_output_manifest

        for overbroad in ("workspace/tmp", "workspace", ".", "workspace/tmp/other_run_id"):
            with self.subTest(overbroad=overbroad):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_root = Path(tmp)
                    init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
                    write_preflight(
                        repo_root=repo_root,
                        orchestration_id="orch_001",
                        payload={
                            "status": "pass",
                            "sandbox_runtime": "bwrap",
                            "sandbox_enforced": True,
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                            "feature_states": {"multi_agent": True, "codex_hooks": True},
                            "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                        child_agent_run_id="substep_run_gen_001",
                        request_payload={
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "plan",
                            "substep": "generate",
                            "agent_role": "substep",
                            "orchestration_id": "orch_001",
                            "agent_run_id": "substep_run_gen_001",
                            "parent_agent_run_id": "orch_run_001",
                            "plan_ref": _FIX_PLAN_REF,
                            "pipeline_ref": _FIX_PIPE_REF,
                            "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
                            "skill_name": "workflow-plan-generate",
                            "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                            "skill_must_read_refs": _fixture_skill_must_read_refs_substep("plan", "generate"),
                            "allowed_output_paths": [f"{_FIX_PLAN_REF}/plan_meta.json"],
                            "launch_prompt_full": _substep_launch_prompt(
                                "problem/shallow_water2d@0.3.0",
                                "plan",
                                "generate",
                                "substep_run_gen_001",
                            ),
                        },
                        response_payload=_spawn_response_payload("sess_substep_gen_001"),
                    )
                    out_ref = f"{_FIX_PLAN_REF}/plan_meta.json"
                    out_path = repo_root / out_ref
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text('{"status":"ok"}\n', encoding="utf-8")
                    _write_apply_patch_gate_evidence(
                        repo_root,
                        orchestration_id="orch_001",
                        agent_run_id="substep_run_gen_001",
                        actor_role="substep",
                        changed_paths=[out_ref],
                    )
                    # Overwrite the manifest with an over-broad allowed_tmp_root
                    _write_allowed_output_manifest(
                        repo_root,
                        orchestration_id="orch_001",
                        agent_run_id="substep_run_gen_001",
                        allowed_output_paths=[out_ref],
                        allowed_tmp_root=overbroad,
                    )
                    # Write a file that would only be skipped if the over-broad tmp root
                    # were accepted (path is outside the correct per-run tmp root)
                    bad_path = repo_root / "workspace" / "tmp" / "other_run_id" / "sneaky.txt"
                    bad_path.parent.mkdir(parents=True, exist_ok=True)
                    bad_path.write_text("unauthorized\n", encoding="utf-8")
                    with self.assertRaises(ValueError):
                        record_agent_run(
                            repo_root=repo_root,
                            orchestration_id="orch_001",
                            payload={
                                "agent_run_id": "substep_run_gen_001",
                                "agent_role": "substep",
                                "parent_agent_run_id": "orch_run_001",
                                "step": "plan",
                                "substep": "generate",
                                "node_key": "problem/shallow_water2d@0.3.0",
                                "status": "pass",
                                "agent_backend": "claude",
                                "agent_model": "claude-sonnet-4-6",
                                "context_id": "ctx_substep_gen_001",
                                "agent_session_id": "sess_substep_gen_001",
                                "output_refs": [out_ref],
                            },
                        )

    def test_record_agent_run_accepts_step_terminal_when_gate_changed_paths_uses_directory_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                    "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"],
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_build_001"),
            )
            out_ref = f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("binary\n", encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="step_run_build_001",
                actor_role="step",
                changed_paths=[f"{_FIX_PIPE_REF}/build/build_001/bin/"],
            )
            payload = record_agent_run(
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
                    "output_refs": [out_ref],
                },
            )
            self.assertEqual(payload["output_refs"], [out_ref])

    def test_record_agent_run_accepts_orchestration_terminal_when_child_deleted_file_matches_tombstone_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            out_ref = f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("binary-baseline\n", encoding="utf-8")
            _fixture_generate_downstream_ready(repo_root)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                    "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"],
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_build_001"),
            )
            out_path.unlink()
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="step_run_build_001",
                actor_role="step",
                changed_paths=[out_ref],
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
                    "output_refs": [out_ref],
                },
            )
            payload = record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "orch_run_002",
                    "agent_role": "orchestration",
                    "status": "pass",
                    "agent_backend": "claude",
                    "result_summary": "child deletion remained unchanged after terminal snapshot",
                },
            )
            self.assertEqual(payload["status"], "pass")

    def test_record_agent_run_accepts_orchestration_terminal_when_only_child_declared_writes_changed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                    "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"],
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_build_001"),
            )
            out_ref = f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("binary\n", encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="step_run_build_001",
                actor_role="step",
                changed_paths=[out_ref],
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
                    "output_refs": [out_ref],
                },
            )
            payload = record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "agent_run_id": "orch_run_002",
                    "agent_role": "orchestration",
                    "status": "pass",
                    "agent_backend": "claude",
                    "result_summary": "child-managed writes were excluded from orchestration diff",
                },
            )
            self.assertEqual(payload["status"], "pass")

    def test_record_agent_run_rejects_orchestration_terminal_when_it_overwrites_child_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                    "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"],
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_build_001"),
            )
            out_ref = f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("binary-v1\n", encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="step_run_build_001",
                actor_role="step",
                changed_paths=[out_ref],
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
                    "output_refs": [out_ref],
                },
            )
            out_path.write_text("binary-v2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "terminal run has unauthorized write paths"):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "orch_run_002",
                        "agent_role": "orchestration",
                        "status": "fail",
                        "agent_backend": "claude",
                        "result_summary": "orchestration overwrote child output",
                    },
                )

    def test_record_agent_run_rejects_orchestration_terminal_when_it_overwrites_existing_child_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            out_ref = f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"
            out_path = repo_root / out_ref
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("binary-baseline\n", encoding="utf-8")
            _fixture_generate_downstream_ready(repo_root)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                    "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"],
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload=_spawn_response_payload("sess_step_build_001"),
            )
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="step_run_build_001",
                actor_role="step",
                changed_paths=[out_ref],
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
                    "output_refs": [out_ref],
                },
            )
            out_path.write_text("binary-overwritten\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "terminal run has unauthorized write paths"):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "orch_run_002",
                        "agent_role": "orchestration",
                        "status": "fail",
                        "agent_backend": "claude",
                        "result_summary": "orchestration overwrote existing child output",
                    },
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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

    def test_write_roots_for_launch_includes_tune_canonical_root(self) -> None:
        self.assertEqual(
            _write_roots_for_launch(
                role="substep",
                step="tune",
                orchestration_id="orch_001",
                plan_ref=_FIX_PLAN_REF,
                pipeline_ref=_FIX_PIPE_REF,
            ),
            [f"{_FIX_PIPE_REF}/tune/"],
        )

    def test_write_roots_for_launch_promote_scoped_to_node_spec_subtree(self) -> None:
        """Regression: promote write_roots MUST be scoped to the current node's
        spec subtree. A wide `releases/` write_root would let a promote agent
        for spec_x mutate any release artifact under the entire releases/
        tree via the bwrap sandbox bind, before terminal-time validation can
        reject the path. Scope must come from node_key."""
        self.assertEqual(
            _write_roots_for_launch(
                role="step",
                step="promote",
                orchestration_id="orch_001",
                plan_ref=_FIX_PLAN_REF,
                pipeline_ref=_FIX_PIPE_REF,
                node_key="problem/dom.fam.spec_x@1.0",
            ),
            ["releases/problem/dom/fam/spec_x/", "spec/registry/spec_catalog.yaml"],
        )

    def test_write_roots_for_launch_promote_without_node_key_returns_empty(self) -> None:
        """Without node_key we cannot derive a per-spec subtree. We refuse
        rather than fall back to the wide releases/ tree. The fail-fast
        invariant in build_capability_document then raises."""
        self.assertEqual(
            _write_roots_for_launch(
                role="step",
                step="promote",
                orchestration_id="orch_001",
                plan_ref=_FIX_PLAN_REF,
                pipeline_ref=_FIX_PIPE_REF,
            ),
            [],
        )

    def test_allowed_output_paths_for_launch_promote_accepts_release_artifact_and_catalog(self) -> None:
        """Regression: promote step must pass phase contract validation for
        canonical write paths (release tree + spec_catalog.yaml). Without this
        branch, _matches_phase_contract falls through to False and breaks
        record-launch for promote."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch
        out = _allowed_output_paths_for_launch(
            request_payload={
                "agent_role": "step",
                "step": "promote",
                "plan_ref": _FIX_PLAN_REF,
                "pipeline_ref": _FIX_PIPE_REF,
                "node_key": "problem/dom.fam.spec_x@1.0",
                "allowed_output_paths": [
                    "releases/problem/dom/fam/spec_x/aarch64/fortran/r_001/artifact.tar.gz",
                    "spec/registry/spec_catalog.yaml",
                ],
            },
            write_roots=["releases/", "spec/registry/spec_catalog.yaml"],
        )
        self.assertEqual(
            out,
            [
                "releases/problem/dom/fam/spec_x/aarch64/fortran/r_001/artifact.tar.gz",
                "spec/registry/spec_catalog.yaml",
            ],
        )

    def test_allowed_output_paths_for_launch_promote_rejects_unrelated_path(self) -> None:
        """Promote contract must still reject paths outside releases/ and the
        canonical catalog file."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch
        with self.assertRaisesRegex(ValueError, "outside phase contract"):
            _allowed_output_paths_for_launch(
                request_payload={
                    "agent_role": "step",
                    "step": "promote",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "node_key": "problem/dom.fam.spec_x@1.0",
                    "allowed_output_paths": ["workspace/random.json"],
                },
                write_roots=["releases/", "spec/registry/spec_catalog.yaml", "workspace/"],
            )

    def test_build_capability_document_rejects_traversal_node_key(self) -> None:
        """Regression: node_key flows into write_roots / release path prefixes,
        so malformed values containing path-traversal sequences (`../`),
        embedded slashes, null bytes, or other non-canonical structure must be
        rejected at capability-build time. Without strict validation a node_key
        like `../etc/passwd@1.0.0` produces a write_root of
        `releases/../etc/passwd/`, escaping the intended release subtree."""
        from tools.orchestration_runtime import build_capability_document

        base_payload: dict[str, object] = {
            "agent_role": "step",
            "step": "promote",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
        }
        malicious = [
            "../etc/passwd@1.0.0",
            "problem/../../../etc/passwd@1.0.0",
            "problem/foo\x00bar@1.0.0",
            "problem/foo@bar/1.0.0",
            "   ../etc   /passwd@1.0.0",
            "problem/.@1.0.0",
            "problem/legit@../../1.0.0",
            "problem/foo..bar@1.0.0",
            "problem/foo/bar@1.0.0",
            "problem/Foo@1.0.0",
            "problem/.foo@1.0.0",
            "problem/foo.@1.0.0",
            "/abs/path@1.0.0",
            "..\\..\\windows@1.0.0",
        ]
        for nk in malicious:
            with self.assertRaises(ValueError, msg=f"should reject node_key={nk!r}"):
                build_capability_document(
                    agent_run_id="r1",
                    orchestration_id="o1",
                    request_payload={**base_payload, "node_key": nk},
                )

    def test_build_capability_document_accepts_canonical_node_keys(self) -> None:
        """Sanity guard for the strict node_key validator: canonical forms used
        across the codebase (single-segment spec_id, dotted spec_id, components
        with hyphens/underscores) must continue to be accepted."""
        from tools.orchestration_runtime import build_capability_document

        base_payload: dict[str, object] = {
            "agent_role": "step",
            "step": "promote",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
        }
        for nk in (
            "problem/shallow_water2d@0.3.0",
            "component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0",
            "problem/dom.fam.spec_x@1.0",
        ):
            cap = build_capability_document(
                agent_run_id="r1",
                orchestration_id="o1",
                request_payload={**base_payload, "node_key": nk},
            )
            self.assertEqual(cap["node_key"], nk)
            self.assertTrue(cap["write_roots"], f"write_roots empty for {nk!r}")
            for root in cap["write_roots"]:
                self.assertNotIn("..", root.split("/"))

    def test_allowed_output_paths_for_launch_promote_rejects_cross_spec_release(self) -> None:
        """Regression: a promote agent for spec_x must NOT be allowed to write
        into another spec's release tree, even though the path lives under
        releases/. The release prefix must be locked to this node's spec."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch
        with self.assertRaisesRegex(ValueError, "outside phase contract"):
            _allowed_output_paths_for_launch(
                request_payload={
                    "agent_role": "step",
                    "step": "promote",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "node_key": "problem/dom.fam.spec_x@1.0",
                    # Different spec_id → outside this node's release subtree
                    "allowed_output_paths": [
                        "releases/problem/other_dom/other_fam/other_spec/aarch64/fortran/r_001/x.tar.gz",
                    ],
                },
                write_roots=["releases/", "spec/registry/spec_catalog.yaml"],
            )

    def test_guarded_apply_patch_cli_emits_json_on_argument_error(self) -> None:
        """Regression: argument validation failures must also emit a JSON
        envelope on stderr, not plain text. Agents following the documented
        recovery contract parse stderr — early-failure paths breaking that
        contract are a real footgun."""
        import subprocess
        proc = subprocess.run(
            [
                "python3",
                "tools/orchestration_runtime.py",
                "guarded-apply-patch",
                "--repo-root", ".",
                "--orchestration-id", "orch",
                "--actor-role", "step",
                "--agent-run-id", "00000000-0000-0000-0000-000000000000",
                "--paths-json", '["x"]',
                "--patch-text", "a",
                "--patch-file", "/tmp/anywhere",
                "--capability-token", "b",
            ],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        try:
            payload = json.loads(proc.stderr.strip())
        except json.JSONDecodeError:
            self.fail(f"argument-error stderr is not JSON: {proc.stderr!r}")
        self.assertEqual(payload["error"], "guarded_apply_patch_failed")
        self.assertEqual(payload["exception_type"], "ArgumentError")
        self.assertEqual(payload["reason_code"], "mutually_exclusive_patch_source")

    def test_guarded_apply_patch_cli_emits_json_on_invalid_uuid(self) -> None:
        """Regression: invalid agent_run_id (UUID) failure must emit JSON envelope."""
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            patch_file = Path(tmp) / "x.patch"
            patch_file.write_text("a", encoding="utf-8")
            proc = subprocess.run(
                [
                    "python3",
                    "tools/orchestration_runtime.py",
                    "guarded-apply-patch",
                    "--repo-root", ".",
                    "--orchestration-id", "orch",
                    "--actor-role", "step",
                    "--agent-run-id", "not-a-uuid",
                    "--paths-json", '["x"]',
                    "--patch-file", str(patch_file),
                    "--capability-token", "b",
                ],
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stderr.strip())
        self.assertEqual(payload["reason_code"], "invalid_agent_run_id")

    def test_guarded_apply_patch_cli_rejects_traversal_agent_run_id_on_patch_text(self) -> None:
        """Regression: agent_run_id traversal validation must apply to BOTH
        --patch-text and --patch-file paths. Previously the check was nested
        under `if args.patch_file is not None`, leaving --patch-text
        bypassable with traversal segments in --agent-run-id."""
        import subprocess
        proc = subprocess.run(
            [
                "python3",
                "tools/orchestration_runtime.py",
                "guarded-apply-patch",
                "--repo-root", ".",
                "--orchestration-id", "orch",
                "--actor-role", "step",
                "--agent-run-id", "../../../etc",
                "--paths-json", '["x"]',
                "--patch-text", "a",
                "--capability-token", "b",
            ],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stderr.strip())
        self.assertEqual(payload["reason_code"], "invalid_agent_run_id")
        self.assertIn("path-traversal", payload["message"])

    def test_guarded_apply_patch_cli_accepts_legacy_identifier_agent_run_id_on_patch_text(self) -> None:
        """Legacy test fixtures use non-UUID identifiers like `build_child_rg1`.
        These are valid identifiers (no traversal) and must still pass the
        --patch-text path's argument validation; UUID strictness only applies
        to --patch-file (where agent_run_id becomes a tmp-root path)."""
        import subprocess
        proc = subprocess.run(
            [
                "python3",
                "tools/orchestration_runtime.py",
                "guarded-apply-patch",
                "--repo-root", ".",
                "--orchestration-id", "rg1",
                "--actor-role", "step",
                "--agent-run-id", "build_child_rg1",
                "--paths-json", '["x"]',
                "--patch-text", "a",
                "--capability-token", "b",
            ],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stderr.strip())
        # MUST NOT be rejected at agent_run_id validation; should fail later
        self.assertNotEqual(payload.get("reason_code"), "invalid_agent_run_id")

    def test_guarded_apply_patch_cli_emits_json_on_failure(self) -> None:
        """Regression: docs/ORCHESTRATION.md guarantees stderr carries a JSON
        envelope with violations[] on guarded-apply-patch failure. Without this,
        agents following the documented recovery path cannot parse failure
        detail."""
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "tools").mkdir()
            # Force a failure by passing a bogus capability + invalid patch
            proc = subprocess.run(
                [
                    "python3",
                    "tools/orchestration_runtime.py",
                    "guarded-apply-patch",
                    "--repo-root", ".",
                    "--orchestration-id", "orch_does_not_exist",
                    "--actor-role", "step",
                    "--agent-run-id", "00000000-0000-0000-0000-000000000000",
                    "--paths-json", '["x"]',
                    "--patch-text", "bogus",
                    "--capability-token", "nope",
                ],
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(proc.returncode, 0)
        # stderr must be parseable JSON with the documented envelope
        try:
            payload = json.loads(proc.stderr.strip())
        except json.JSONDecodeError:
            self.fail(f"stderr is not JSON: {proc.stderr!r}")
        self.assertEqual(payload.get("error"), "guarded_apply_patch_failed")
        self.assertIn("exception_type", payload)
        self.assertIn("violations", payload)
        self.assertIsInstance(payload["violations"], list)
        self.assertGreaterEqual(len(payload["violations"]), 1)

    def test_allowed_output_paths_for_launch_promote_rejects_shallow_path_under_spec(self) -> None:
        """Regression: a path directly under the spec subtree (e.g. README.md
        without arch/lang/release_id) must be rejected. The previous startswith-only
        check let arbitrary files anywhere under releases/<spec>/ pass."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch
        with self.assertRaisesRegex(ValueError, "outside phase contract"):
            _allowed_output_paths_for_launch(
                request_payload={
                    "agent_role": "step",
                    "step": "promote",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "node_key": "problem/dom.fam.spec_x@1.0",
                    "allowed_output_paths": [
                        "releases/problem/dom/fam/spec_x/README.md",
                    ],
                },
                write_roots=["releases/", "spec/registry/spec_catalog.yaml"],
            )

    def test_allowed_output_paths_for_launch_promote_rejects_traversal_in_release_path(self) -> None:
        """Regression: path-traversal segments (../, .hidden, etc.) under the
        spec subtree must be rejected."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch
        # `.hidden` survives the contract check until the identifier rule rejects it.
        with self.assertRaisesRegex(ValueError, "outside phase contract"):
            _allowed_output_paths_for_launch(
                request_payload={
                    "agent_role": "step",
                    "step": "promote",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "node_key": "problem/dom.fam.spec_x@1.0",
                    "allowed_output_paths": [
                        "releases/problem/dom/fam/spec_x/.hidden/lang/r/x.tar.gz",
                    ],
                },
                write_roots=["releases/", "spec/registry/spec_catalog.yaml"],
            )

    def test_allowed_output_paths_for_launch_rejects_dotdot_segments(self) -> None:
        """Regression: `..` segments must be rejected BEFORE prefix/contract
        checks. Otherwise a path like
        `releases/<spec>/aarch64/fortran/r1/../../somewhere_else` would pass
        the startswith() check and the identifier check on the first three
        tail segments, even though it resolves outside the canonical layout."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch
        with self.assertRaisesRegex(ValueError, "must not contain '\\.'/'\\.\\.' segments"):
            _allowed_output_paths_for_launch(
                request_payload={
                    "agent_role": "step",
                    "step": "promote",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "node_key": "problem/dom.fam.spec_x@1.0",
                    "allowed_output_paths": [
                        "releases/problem/dom/fam/spec_x/aarch64/fortran/r1/../../spec/registry/spec_catalog.yaml",
                    ],
                },
                write_roots=["releases/problem/dom/fam/spec_x/", "spec/registry/spec_catalog.yaml"],
            )

    def test_allowed_output_paths_for_launch_rejects_single_dot_segments(self) -> None:
        from tools.orchestration_runtime import _allowed_output_paths_for_launch
        with self.assertRaisesRegex(ValueError, "must not contain '\\.'/'\\.\\.' segments"):
            _allowed_output_paths_for_launch(
                request_payload={
                    "agent_role": "step",
                    "step": "promote",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "node_key": "problem/dom.fam.spec_x@1.0",
                    "allowed_output_paths": [
                        "releases/problem/dom/fam/spec_x/./aarch64/fortran/r1/x.tar.gz",
                    ],
                },
                write_roots=["releases/problem/dom/fam/spec_x/", "spec/registry/spec_catalog.yaml"],
            )

    def test_allowed_output_paths_for_launch_promote_accepts_deep_artifact_path(self) -> None:
        """Positive case: arbitrary depth under <release_id>/ is allowed for
        complex artifact layouts."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch
        out = _allowed_output_paths_for_launch(
            request_payload={
                "agent_role": "step",
                "step": "promote",
                "plan_ref": _FIX_PLAN_REF,
                "pipeline_ref": _FIX_PIPE_REF,
                "node_key": "problem/dom.fam.spec_x@1.0",
                "allowed_output_paths": [
                    "releases/problem/dom/fam/spec_x/aarch64/fortran/r1/sub/dir/x.tar.gz",
                ],
            },
            write_roots=["releases/", "spec/registry/spec_catalog.yaml"],
        )
        self.assertEqual(
            out,
            ["releases/problem/dom/fam/spec_x/aarch64/fortran/r1/sub/dir/x.tar.gz"],
        )

    def test_allowed_output_paths_for_launch_promote_rejects_cross_kind_release(self) -> None:
        """Regression: a promote agent for spec_kind=problem must NOT write to
        a different spec_kind subtree."""
        from tools.orchestration_runtime import _allowed_output_paths_for_launch
        with self.assertRaisesRegex(ValueError, "outside phase contract"):
            _allowed_output_paths_for_launch(
                request_payload={
                    "agent_role": "step",
                    "step": "promote",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "node_key": "problem/dom.fam.spec_x@1.0",
                    "allowed_output_paths": [
                        "releases/algorithm/dom/fam/spec_x/aarch64/fortran/r_001/x.tar.gz",
                    ],
                },
                write_roots=["releases/", "spec/registry/spec_catalog.yaml"],
            )

    def test_record_agent_run_rejects_tune_substep_terminal_write_outside_tune_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
            cap_path = (
                repo_root
                / "workspace/orchestrations/orch_001/capabilities/substep_run_tune_001.json"
            )
            cap_path.parent.mkdir(parents=True, exist_ok=True)
            cap_path.write_text(
                json.dumps(
                    {
                        "agent_run_id": "substep_run_tune_001",
                        "capability_token": "tok_tune_001",
                        "orchestration_id": "orch_001",
                        "agent_role": "substep",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "tune",
                        "substep": "generate",
                        "write_roots": [f"{_FIX_PIPE_REF}/tune/"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            launch_request_path = (
                repo_root
                / "workspace/orchestrations/orch_001/launches/substep_run_tune_001.request.json"
            )
            launch_request_path.parent.mkdir(parents=True, exist_ok=True)
            launch_request_path.write_text(
                json.dumps(
                    {
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "tune",
                        "substep": "generate",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "substep_run_tune_001",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_DEP_REF,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            launch_response_path = (
                repo_root
                / "workspace/orchestrations/orch_001/launches/substep_run_tune_001.response.json"
            )
            sandbox_profile_path = (
                repo_root
                / "workspace/orchestrations/orch_001/sandbox_profiles/substep_run_tune_001.json"
            )
            sandbox_profile_path.parent.mkdir(parents=True, exist_ok=True)
            sandbox_profile_path.write_text(
                json.dumps(
                    {
                        "orchestration_id": "orch_001",
                        "agent_run_id": "substep_run_tune_001",
                        "sandbox_runtime": "bwrap",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            launch_response_path.write_text(
                json.dumps(
                    {
                        "agent_run_id": "substep_run_tune_001",
                        **_spawn_response_payload("sess_substep_tune_001"),
                        "sandbox_runtime": "bwrap",
                        "sandbox_enforced": True,
                        "sandbox_profile_ref": (
                            "workspace/orchestrations/orch_001/"
                            "sandbox_profiles/substep_run_tune_001.json"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            launch_prompt_path = (
                repo_root
                / "workspace/orchestrations/orch_001/launches/substep_run_tune_001.prompt.txt"
            )
            launch_prompt_path.write_text(
                _substep_launch_prompt(
                    "problem/shallow_water2d@0.3.0",
                    "tune",
                    "generate",
                    "substep_run_tune_001",
                ),
                encoding="utf-8",
            )
            launch_reply_path = (
                repo_root
                / "workspace/orchestrations/orch_001/launches/substep_run_tune_001.reply.txt"
            )
            launch_reply_path.write_text("accepted: sess_substep_tune_001\n", encoding="utf-8")
            _write_run_write_baseline(
                repo_root,
                "orch_001",
                agent_run_id="substep_run_tune_001",
            )
            bad_ref = f"{_FIX_PIPE_REF}/generate/leaked.txt"
            bad_path = repo_root / bad_ref
            bad_path.parent.mkdir(parents=True, exist_ok=True)
            bad_path.write_text("leak\n", encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="substep_run_tune_001",
                actor_role="substep",
                changed_paths=[bad_ref],
            )
            with self.assertRaisesRegex(ValueError, "terminal run has unauthorized write paths"):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "agent_run_id": "substep_run_tune_001",
                        "agent_role": "substep",
                        "parent_agent_run_id": "orch_run_001",
                        "step": "tune",
                        "substep": "generate",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "status": "pass",
                        "agent_backend": "codex",
                        "agent_model": "gpt-5-codex",
                        "context_id": "ctx_substep_tune_001",
                        "agent_session_id": "sess_substep_tune_001",
                        "output_refs": [bad_ref],
                    },
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": ["workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate"],
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
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
                        "sandbox_runtime": "bwrap",
                        "sandbox_enforced": True,
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                        "feature_states": {"multi_agent": False},
                        "checks": [
                            {"name": "multi_agent_enabled", "pass": False},
                        ],
                    },
                )

    def test_rejects_codex_launchable_preflight_when_codex_hooks_state_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            with self.assertRaisesRegex(ValueError, "feature_states.codex_hooks=true"):
                write_preflight(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "status": "pass",
                        "backend": "codex",
                        "sandbox_runtime": "bwrap",
                        "sandbox_enforced": True,
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                        "feature_states": {"multi_agent": True},
                        "checks": [
                            {"name": "multi_agent_enabled", "pass": True},
                            {"name": "codex_hooks_enabled", "pass": True},
                            {"name": "codex_home_writable", "pass": True},
                        ],
                    },
                )

    def test_rejects_codex_launchable_preflight_when_codex_hooks_check_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            with self.assertRaisesRegex(ValueError, "checks.codex_hooks_enabled.pass=true"):
                write_preflight(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "status": "pass",
                        "backend": "codex",
                        "sandbox_runtime": "bwrap",
                        "sandbox_enforced": True,
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                        "feature_states": {"multi_agent": True, "codex_hooks": True},
                        "checks": [
                            {"name": "multi_agent_enabled", "pass": True},
                        ],
                    },
                )

    def test_rejects_codex_launchable_preflight_when_codex_home_check_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            with self.assertRaisesRegex(ValueError, "checks.codex_home_writable.pass=true"):
                write_preflight(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    payload={
                        "status": "pass",
                        "backend": "codex",
                        "sandbox_runtime": "bwrap",
                        "sandbox_enforced": True,
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                        "feature_states": {"multi_agent": True, "codex_hooks": True},
                        "checks": [
                            {"name": "multi_agent_enabled", "pass": True},
                            {"name": "codex_hooks_enabled", "pass": True},
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            os.environ["METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT"] = "1"
            with patch("tools.orchestration_runtime.probe_execution_platform") as probe_mock:
                probe_mock.return_value = {
                    "checked_at": "2026-04-15T12:00:00Z",
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                        "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            os.environ["METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT"] = "0"
            with patch(
                "tools.orchestration_runtime.probe_execution_platform",
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
                        "agent_role": "step",
                        "orchestration_id": "orch_001",
                        "agent_run_id": "step_run_build_001",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                        "skill_name": "workflow-build",
                        "skill_ref": "skills/workflow-build/SKILL.md",
                        "skill_must_read_refs": "",
                        "allowed_output_paths": [
                            "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate",
                        ],
                        "launch_prompt_full": _step_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "build",
                            "step_run_build_001",
                        ),
                    },
                    response_payload=_spawn_response_payload("sess_step_build_001"),
                )
                _write_apply_patch_gate_evidence(
                    repo_root,
                    orchestration_id="orch_001",
                    agent_run_id="step_run_build_001",
                    actor_role="step",
                    changed_paths=[
                        "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate"
                    ],
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
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
                    "agent_role": "step",
                    "orchestration_id": "orch_001",
                    "agent_run_id": "step_run_build_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": ["workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/bin/simulate"],
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
                "sandbox_runtime": "bwrap",
                "sandbox_enforced": True,
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
                "feature_states": {"multi_agent": True, "codex_hooks": True},
                "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
        phase_state_path = repo_root / "workspace/orchestrations/orch_001/phase_state.json"
        phase_state = json.loads(phase_state_path.read_text(encoding="utf-8"))
        phase_state["node_states"]["problem__shallow_water2d__0.3.0"] = {
            "plan": "child_finished",
            "generate": "child_finished",
            "build": "child_finished",
            "execute": "child_finished",
            "judge": "child_finished",
        }
        phase_state_path.write_text(
            json.dumps(phase_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _valid_plan_meta(*, context_isolated: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "attempt_count": 1,
            "verification_status": "pass",
            "last_fail_reason": None,
            "debug_mode": False,
            "context_isolated": context_isolated,
        }
        if not context_isolated:
            payload["constraint_reason"] = "shared verifier context"
        return payload

    @staticmethod
    def _valid_generate_meta(*, context_isolated: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "attempt_count": 1,
            "verification_status": "pass",
            "last_fail_reason": None,
            "debug_mode": False,
            "context_isolated": context_isolated,
            "lint_command_ref": {
                "run_linter": [
                    {
                        "command_id": "lint_001",
                        "command_log_ref": "workspace/orchestrations/orch_001/gates/lint_001.json",
                        "preset": "fortitude",
                    }
                ]
            },
        }
        if not context_isolated:
            payload["constraint_reason"] = "shared verifier context"
        return payload

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

    def test_write_step_result_rejects_when_phase_not_child_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            with self.assertRaisesRegex(RuntimeError, "write_step_result phase gate"):
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="build",
                    agent_run_id="step_run_build_001",
                    payload={
                        "status": "fail",
                        "validation_stage": "post_build",
                        "required_outputs": [],
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
                json.dumps(self._valid_plan_meta()),
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

    def test_write_step_result_rejects_generate_meta_attempt_count_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/generate_meta.json"
            meta_path = repo_root / meta_ref
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_payload = self._valid_generate_meta()
            meta_payload["attempt_count"] = "1"
            meta_payload["verification_status"] = "fail"
            del meta_payload["lint_command_ref"]
            meta_path.write_text(json.dumps(meta_payload), encoding="utf-8")
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_gen_verify_001",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "generate",
                            "substep": "verify",
                            "status": "pass",
                            "agent_backend": "claude",
                            "output_refs": [meta_ref],
                        }
                    )
                    + "\n"
                )
            with self.assertRaisesRegex(ValueError, "attempt_count must be integer"):
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

    def test_write_step_result_requires_generate_meta_lint_command_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/generate_meta.json"
            meta_path = repo_root / meta_ref
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_payload = self._valid_generate_meta()
            del meta_payload["lint_command_ref"]
            meta_path.write_text(json.dumps(meta_payload), encoding="utf-8")
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_gen_verify_001",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "generate",
                            "substep": "verify",
                            "status": "pass",
                            "agent_backend": "claude",
                            "output_refs": [meta_ref],
                        }
                    )
                    + "\n"
                )
            with self.assertRaisesRegex(ValueError, "lint_command_ref"):
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

    def test_write_step_result_allows_generate_meta_without_lint_when_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/generate_meta.json"
            meta_path = repo_root / meta_ref
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_payload = self._valid_generate_meta()
            meta_payload["verification_status"] = "fail"
            del meta_payload["lint_command_ref"]
            meta_path.write_text(json.dumps(meta_payload), encoding="utf-8")
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_gen_verify_001",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "generate",
                            "substep": "verify",
                            "status": "pass",
                            "agent_backend": "claude",
                            "output_refs": [meta_ref],
                        }
                    )
                    + "\n"
                )
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

    def test_write_step_result_requires_final_meta_in_required_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/generate_meta.json"
            src_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/src/model.f90"
            meta_path = repo_root / meta_ref
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps(self._valid_generate_meta()), encoding="utf-8")
            src_path = repo_root / src_ref
            src_path.parent.mkdir(parents=True, exist_ok=True)
            src_path.write_text("program model\nend program model\n", encoding="utf-8")
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_gen_verify_001",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "generate",
                            "substep": "verify",
                            "status": "pass",
                            "agent_backend": "claude",
                            "output_refs": [src_ref, meta_ref],
                        }
                    )
                    + "\n"
                )
            with self.assertRaisesRegex(ValueError, "required_outputs to include final generate_meta.json"):
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="generate",
                    agent_run_id="orch_run_001",
                    payload={
                        "status": "pass",
                        "validation_stage": "post_generate",
                        "required_outputs": [src_ref],
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

    def test_write_step_result_rejects_plan_meta_last_fail_reason_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            meta_ref = "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json"
            meta_path = repo_root / meta_ref
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_payload = self._valid_plan_meta()
            meta_payload["last_fail_reason"] = 1
            meta_path.write_text(json.dumps(meta_payload), encoding="utf-8")
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_plan_generate_001",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "plan",
                            "substep": "generate",
                            "status": "pass",
                            "agent_backend": "claude",
                            "output_refs": [meta_ref],
                        }
                    )
                    + "\n"
                )
            with self.assertRaisesRegex(ValueError, "last_fail_reason must be string or null"):
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

    def test_write_step_result_requires_constraint_reason_when_context_not_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            meta_ref = "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json"
            meta_path = repo_root / meta_ref
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_payload = self._valid_plan_meta(context_isolated=False)
            del meta_payload["constraint_reason"]
            meta_path.write_text(json.dumps(meta_payload), encoding="utf-8")
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_plan_generate_001",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "plan",
                            "substep": "generate",
                            "status": "pass",
                            "agent_backend": "claude",
                            "output_refs": [meta_ref],
                        }
                    )
                    + "\n"
                )
            with self.assertRaisesRegex(ValueError, "constraint_reason"):
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
                json.dumps(self._valid_generate_meta()),
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

    def test_write_step_result_accepts_retry_pass_when_old_failed_substep_is_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            old_meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/generate_meta.json"
            new_meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_002/generate_meta.json"
            old_meta_path = repo_root / old_meta_ref
            old_meta_path.parent.mkdir(parents=True, exist_ok=True)
            old_meta_path.write_text(json.dumps(self._valid_generate_meta()), encoding="utf-8")
            new_meta_path = repo_root / new_meta_ref
            new_meta_path.parent.mkdir(parents=True, exist_ok=True)
            new_meta_path.write_text(json.dumps(self._valid_generate_meta()), encoding="utf-8")
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_gen_generate_001",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "generate",
                            "substep": "generate",
                            "status": "fail",
                            "agent_backend": "claude",
                            "output_refs": [old_meta_ref],
                        }
                    )
                    + "\n"
                )
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_gen_generate_002",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "generate",
                            "substep": "generate",
                            "status": "pass",
                            "agent_backend": "claude",
                            "output_refs": [new_meta_ref],
                        }
                    )
                    + "\n"
                )
            result = write_step_result(
                repo_root=repo_root,
                orchestration_id="orch_001",
                node_key="problem/shallow_water2d@0.3.0",
                step="generate",
                agent_run_id="orch_run_001",
                payload={
                    "status": "pass",
                    "validation_stage": "post_generate",
                    "required_outputs": [new_meta_ref],
                    "failed_substeps": ["substep_run_gen_generate_001"],
                    "substep_agent_run_ids": [
                        "substep_run_gen_generate_001",
                        "substep_run_gen_generate_002",
                    ],
                    "retry_decisions": [
                        {
                            "issue_severity": "major",
                            "repair_strategy": "restart",
                            "repair_target_agent_run_id": "substep_run_gen_generate_001",
                            "new_agent_run_id": "substep_run_gen_generate_002",
                            "repair_reason": "retry after failed generation",
                        }
                    ],
                },
            )
            self.assertEqual(result.get("status"), "pass")

    def test_write_step_result_rejects_failed_substeps_that_reference_pass_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/generate_meta.json"
            meta_path = repo_root / meta_ref
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps(self._valid_generate_meta()), encoding="utf-8")
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_gen_generate_001",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "generate",
                            "substep": "generate",
                            "status": "pass",
                            "agent_backend": "claude",
                            "output_refs": [meta_ref],
                        }
                    )
                    + "\n"
                )
            with self.assertRaisesRegex(ValueError, "actual non-pass run"):
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
                        "failed_substeps": ["substep_run_gen_generate_001"],
                        "substep_agent_run_ids": ["substep_run_gen_generate_001"],
                    },
                )

    def test_write_step_result_rejects_retry_target_that_references_pass_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            old_meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/generate_meta.json"
            new_meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_002/generate_meta.json"
            old_meta_path = repo_root / old_meta_ref
            old_meta_path.parent.mkdir(parents=True, exist_ok=True)
            old_meta_path.write_text(json.dumps(self._valid_generate_meta()), encoding="utf-8")
            new_meta_path = repo_root / new_meta_ref
            new_meta_path.parent.mkdir(parents=True, exist_ok=True)
            new_meta_path.write_text(json.dumps(self._valid_generate_meta()), encoding="utf-8")
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_gen_generate_001",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "generate",
                            "substep": "generate",
                            "status": "pass",
                            "agent_backend": "claude",
                            "output_refs": [old_meta_ref],
                        }
                    )
                    + "\n"
                )
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_gen_generate_002",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "generate",
                            "substep": "generate",
                            "status": "pass",
                            "agent_backend": "claude",
                            "output_refs": [new_meta_ref],
                        }
                    )
                    + "\n"
                )
            with self.assertRaisesRegex(ValueError, "actual non-pass run"):
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="generate",
                    agent_run_id="orch_run_001",
                    payload={
                        "status": "pass",
                        "validation_stage": "post_generate",
                        "required_outputs": [new_meta_ref],
                        "failed_substeps": [],
                        "substep_agent_run_ids": [
                            "substep_run_gen_generate_001",
                            "substep_run_gen_generate_002",
                        ],
                        "retry_decisions": [
                            {
                                "issue_severity": "major",
                                "repair_strategy": "restart",
                                "repair_target_agent_run_id": "substep_run_gen_generate_001",
                                "new_agent_run_id": "substep_run_gen_generate_002",
                                "repair_reason": "invalid retry declaration",
                            }
                        ],
                    },
                )

    def test_write_step_result_rejects_required_outputs_covered_only_by_failed_retry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_preflight_and_orch_agent(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_001"
            runs_path = orch_root / "agent_runs.jsonl"
            old_meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_001/generate_meta.json"
            new_meta_ref = "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/generate/gen_20260413_002/generate_meta.json"
            old_meta_path = repo_root / old_meta_ref
            old_meta_path.parent.mkdir(parents=True, exist_ok=True)
            old_meta_path.write_text(json.dumps(self._valid_generate_meta()), encoding="utf-8")
            new_meta_path = repo_root / new_meta_ref
            new_meta_path.parent.mkdir(parents=True, exist_ok=True)
            new_meta_path.write_text(json.dumps(self._valid_generate_meta()), encoding="utf-8")
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_gen_generate_001",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "generate",
                            "substep": "generate",
                            "status": "fail",
                            "agent_backend": "claude",
                            "output_refs": [old_meta_ref],
                        }
                    )
                    + "\n"
                )
                fh.write(
                    json.dumps(
                        {
                            "agent_run_id": "substep_run_gen_generate_002",
                            "agent_role": "substep",
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "generate",
                            "substep": "generate",
                            "status": "pass",
                            "agent_backend": "claude",
                            "output_refs": [new_meta_ref],
                        }
                    )
                    + "\n"
                )
            with self.assertRaisesRegex(ValueError, "effective substep output_refs"):
                write_step_result(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    node_key="problem/shallow_water2d@0.3.0",
                    step="generate",
                    agent_run_id="orch_run_001",
                    payload={
                        "status": "pass",
                        "validation_stage": "post_generate",
                        "required_outputs": [old_meta_ref],
                        "failed_substeps": ["substep_run_gen_generate_001"],
                        "substep_agent_run_ids": [
                            "substep_run_gen_generate_001",
                            "substep_run_gen_generate_002",
                        ],
                        "retry_decisions": [
                            {
                                "issue_severity": "major",
                                "repair_strategy": "restart",
                                "repair_target_agent_run_id": "substep_run_gen_generate_001",
                                "new_agent_run_id": "substep_run_gen_generate_002",
                                "repair_reason": "retry after failed generation",
                            }
                        ],
                    },
                )


    def _minimal_preflight_setup(self, repo_root: Path) -> None:
        """orchestration / preflight / orchestration agent run を最小構成でセットアップする。"""
        init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
        write_preflight(
            repo_root=repo_root,
            orchestration_id="orch_001",
            payload={
                "status": "pass",
                "sandbox_runtime": "bwrap",
                "sandbox_enforced": True,
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
                "feature_states": {"multi_agent": True, "codex_hooks": True},
                "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
        """repair_strategy / issue_severity テスト用の最小 request_payload を返す。"""
        base: dict[str, object] = {
            "orchestration_id": "orch_001",
            "agent_run_id": "step_run_repair_001",
            "parent_agent_run_id": "orch_run_001",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "build",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "dependency_ref": _FIX_DEP_REF,
            "skill_name": "workflow-build",
            "skill_ref": "skills/workflow-build/SKILL.md",
            "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
            "issue_severity": "none",
            "repair_strategy": "none",
            "repair_target_agent_run_id": "none",
            "repair_reason": "none",
        }
        base.update(overrides)
        if "launch_prompt_full" not in base:
            base["launch_prompt_full"] = render_launch_prompt_text(base)
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

    def test_record_launch_rejects_traversal_in_child_agent_run_id(self) -> None:
        """child_agent_run_id containing path separators must be rejected before path construction."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._minimal_preflight_setup(repo_root)
            for bad_id in ["../secret", "../../etc/passwd", "foo/bar", "foo\\bar", "foo.bar"]:
                with self.subTest(bad_id=bad_id):
                    with self.assertRaisesRegex(ValueError, "child_agent_run_id contains invalid characters"):
                        record_launch(
                            repo_root=repo_root,
                            orchestration_id="orch_001",
                            parent_agent_run_id="orch_run_001",
                            child_agent_run_id=bad_id,
                            request_payload=self._minimal_request_payload(),
                            response_payload=_spawn_response_payload("sess_001"),
                        )

    def test_record_launch_rejects_traversal_in_parent_agent_run_id(self) -> None:
        """parent_agent_run_id containing path separators must be rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._minimal_preflight_setup(repo_root)
            for bad_id in ["../orch", "orch/evil", "orch\\evil"]:
                with self.subTest(bad_id=bad_id):
                    with self.assertRaisesRegex(ValueError, "parent_agent_run_id contains invalid characters"):
                        record_launch(
                            repo_root=repo_root,
                            orchestration_id="orch_001",
                            parent_agent_run_id=bad_id,
                            child_agent_run_id="step_run_001",
                            request_payload=self._minimal_request_payload(),
                            response_payload=_spawn_response_payload("sess_001"),
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

    def test_main_help_run_gate_includes_args_json_schema_examples(self) -> None:
        """`run-gate --help` が gate 別 args_json schema 要約を表示すること。"""
        import re as _re
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as ctx:
                main(["run-gate", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        out = _re.sub(r"\n\s+", " ", stdout.getvalue())
        self.assertIn("orchestration_read => {'read_path': 'docs/...'}", out)
        self.assertIn("validate_pipeline_semantics => {'stage':", out)

    def test_main_help_write_step_result_includes_result_json_schema(self) -> None:
        """`write-step-result --help` が result_json schema 要約を表示すること。"""
        import re as _re
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as ctx:
                main(["write-step-result", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        out = _re.sub(r"\n\s+", " ", stdout.getvalue())
        self.assertIn("required_outputs (list[str])", out)
        self.assertIn("validation_stage is required", out)

    def test_main_help_record_launch_includes_dependency_ref_phase_rule(self) -> None:
        """`record-launch --help` が dependency_ref の phase 規則を表示すること。"""
        import re as _re
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as ctx:
                main(["record-launch", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        out = _re.sub(r"\n\s+", " ", stdout.getvalue())
        self.assertIn("Plan => spec/.../deps.yaml; Generate+ => workspace phase root", out)

    def test_render_launch_prompt_includes_capability_token_source_line(self) -> None:
        """launch prompt が capability_token の取得元と fail-fast 条件を含むこと。"""
        payload = {
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "build",
            "orchestration_id": "orch_001",
            "agent_run_id": "step_run_build_001",
            "parent_agent_run_id": "orch_run_001",
            "workflow_mode": "dev",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "dependency_ref": _FIX_DEP_REF,
            "skill_name": "workflow-build",
            "skill_ref": "skills/workflow-build/SKILL.md",
            "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
            "issue_severity": "none",
            "repair_strategy": "none",
            "repair_target_agent_run_id": "none",
            "repair_reason": "none",
        }
        prompt = render_launch_prompt_text(payload)
        self.assertIn("capabilities/step_run_build_001.json", prompt)
        self.assertIn("`capability_token` が未取得または不一致の場合", prompt)


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
                "sandbox_runtime": "bwrap",
                "sandbox_enforced": True,
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
                "feature_states": {"multi_agent": True, "codex_hooks": True},
                "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
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
        phase_state_path = repo_root / "workspace/orchestrations/orch_001/phase_state.json"
        phase_state = json.loads(phase_state_path.read_text(encoding="utf-8"))
        phase_state["node_states"]["problem__shallow_water2d__0.3.0"] = {
            "plan": "child_finished",
            "generate": "child_finished",
            "build": "child_finished",
            "execute": "child_finished",
            "judge": "child_finished",
        }
        phase_state["node_states"]["component__solver__0.1.0"] = {
            "plan": "child_finished",
            "generate": "child_finished",
            "build": "child_finished",
            "execute": "child_finished",
            "judge": "child_finished",
        }
        phase_state_path.write_text(
            json.dumps(phase_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
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
                source_dependency_ref="dep.yaml",
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
                "tools.orchestration_runtime.update_checkpoint",
                side_effect=RuntimeError("boom"),
            ), patch("tools.orchestration_runtime.sys.stderr", stderr):
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

    def test_read_checkpoint_forbidden_without_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o2")
            ck_path = repo / "workspace/orchestrations/o2/orchestration_checkpoint.json"
            ck_path.write_text(
                json.dumps(
                    {
                        "orchestration_id": "o2",
                        "schema_version": "1",
                        "completed_steps": [{"node_key": "problem/shallow_water2d@0.3.0", "step": "plan"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "read_checkpoint forbidden"):
                read_checkpoint(repo_root=repo, orchestration_id="o2")

    def test_read_checkpoint_allowed_when_resume_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="o3")
            enable_checkpoint_resume(repo_root=repo, orchestration_id="o3")
            ck_path = repo / "workspace/orchestrations/o3/orchestration_checkpoint.json"
            ck_path.write_text(
                json.dumps(
                    {
                        "orchestration_id": "o3",
                        "schema_version": "1",
                        "completed_steps": [{"node_key": "problem/shallow_water2d@0.3.0", "step": "plan"}],
                    }
                ),
                encoding="utf-8",
            )
            out = read_checkpoint(repo_root=repo, orchestration_id="o3")
            self.assertIsInstance(out, dict)
            self.assertEqual(len(out.get("completed_steps", [])), 1)

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
        "sandbox_runtime": "bwrap",
        "sandbox_enforced": True,
        "can_launch_step_agents": True,
        "can_launch_substep_agents": True,
        "session_policy": {
            "allow_step_agent_launch": True,
            "allow_substep_agent_launch": True,
        },
        "session_policy_launchable": True,
        "feature_states": {"multi_agent": True, "codex_hooks": True},
        "checks": [
            {"name": "multi_agent_enabled", "pass": True},
            {"name": "codex_hooks_enabled", "pass": True},
            {"name": "codex_home_writable", "pass": True},
            {"name": "sandbox_bwrap_available", "pass": True},
            {"name": "sandbox_bwrap_userns", "pass": True},
        ],
    }
    base.update(extra)
    return base


class OrchestrationMetaAndJudgeHookTests(unittest.TestCase):
    def test_write_preflight_persists_parallel_nodes_meta_without_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            orch = "preflight_only_orch"
            with patch.dict(os.environ, {"METDSL_ALLOW_PARALLEL_NODES": "1"}):
                write_preflight(
                    repo_root=repo,
                    orchestration_id=orch,
                    payload=_launchable_preflight_dict(checked_at="2026-04-15T10:00:00Z"),
                )
            meta_path = repo / "workspace" / "orchestrations" / orch / "orchestration_meta.json"
            self.assertTrue(meta_path.is_file())
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertTrue(meta.get("parallel_nodes_explicit"))
            self.assertEqual(meta.get("parallel_nodes_policy"), "sequential_default")

    def test_init_orchestration_merges_parallel_nodes_from_preflight_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            orch = "preflight_then_init"
            with patch.dict(os.environ, {"METDSL_ALLOW_PARALLEL_NODES": "true"}):
                write_preflight(
                    repo_root=repo,
                    orchestration_id=orch,
                    payload=_launchable_preflight_dict(checked_at="2026-04-15T10:00:00Z"),
                )
            init_orchestration(repo_root=repo, orchestration_id=orch)
            meta_path = repo / "workspace" / "orchestrations" / orch / "orchestration_meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertTrue(meta.get("parallel_nodes_explicit"))
            self.assertEqual(meta.get("parallel_nodes_policy"), "sequential_default")
            self.assertEqual(meta.get("orchestration_id"), orch)

    def test_init_orchestration_reuses_existing_orchestration_agent_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            orch = "orch_reinit_same_run_id"
            init_orchestration(repo_root=repo, orchestration_id=orch)
            meta_path = repo / "workspace" / "orchestrations" / orch / "orchestration_meta.json"
            first_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            first_run_id = str(first_meta.get("orchestration_agent_run_id", "")).strip()
            self.assertTrue(first_run_id)

            init_orchestration(repo_root=repo, orchestration_id=orch)
            second_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            second_run_id = str(second_meta.get("orchestration_agent_run_id", "")).strip()
            self.assertEqual(second_run_id, first_run_id)

            read_manifest_path = (
                repo / "workspace" / "orchestrations" / orch / "read_manifests" / f"{first_run_id}.json"
            )
            self.assertTrue(read_manifest_path.is_file())
            output_manifest_path = (
                repo / "workspace" / "orchestrations" / orch / "output_manifests" / f"{first_run_id}.json"
            )
            self.assertTrue(output_manifest_path.is_file())
            output_manifest = json.loads(output_manifest_path.read_text(encoding="utf-8"))
            expected_failure_analysis = f"workspace/orchestrations/{orch}/failure_analysis.json"
            self.assertIn(expected_failure_analysis, output_manifest.get("allowed_output_paths", []))
            self.assertIn(expected_failure_analysis, output_manifest.get("allowed_file_tool_paths", []))

    def test_init_orchestration_does_not_duplicate_orchestration_running_entry_on_reinit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            orch = "orch_reinit_no_duplicate_running"
            init_orchestration(repo_root=repo, orchestration_id=orch)
            init_orchestration(repo_root=repo, orchestration_id=orch)

            meta_path = repo / "workspace" / "orchestrations" / orch / "orchestration_meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            run_id = str(meta.get("orchestration_agent_run_id", "")).strip()
            self.assertTrue(run_id)

            runs_path = repo / "workspace" / "orchestrations" / orch / "agent_runs.jsonl"
            lines = [line.strip() for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            matching_running = []
            for line in lines:
                item = json.loads(line)
                if (
                    str(item.get("agent_run_id", "")).strip() == run_id
                    and item.get("agent_role") == "orchestration"
                    and item.get("status") == "running"
                ):
                    matching_running.append(item)
            self.assertEqual(len(matching_running), 1)

    def test_init_orchestration_writes_session_run_index_for_orchestration_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            orch = "orch_session_index_init_001"
            meta = init_orchestration(repo_root=repo, orchestration_id=orch)
            run_id = str(meta.get("orchestration_agent_run_id", "")).strip()
            self.assertTrue(run_id)
            index_path = repo / "workspace" / "orchestrations" / orch / "session_run_index.json"
            self.assertTrue(index_path.is_file())
            index_doc = json.loads(index_path.read_text(encoding="utf-8"))
            entries = index_doc.get("entries")
            self.assertIsInstance(entries, list)
            matched = [
                item
                for item in entries
                if isinstance(item, dict) and str(item.get("agent_run_id", "")).strip() == run_id
            ]
            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0].get("agent_session_id"), run_id)
            self.assertEqual(matched[0].get("agent_role"), "orchestration")
            self.assertEqual(matched[0].get("status"), "running")

    def test_record_launch_and_terminal_record_agent_run_update_session_run_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "0"}
        ):
            repo = Path(tmp)
            orch = "orch_session_index_launch_001"
            parent = "orch_parent_001"
            child = "step_run_session_index_001"
            session = "sess_step_session_index_001"
            init_orchestration(repo_root=repo, orchestration_id=orch)
            write_preflight(
                repo_root=repo,
                orchestration_id=orch,
                payload=_launchable_preflight_dict(checked_at="2026-04-15T10:00:00Z"),
            )
            record_launch(
                repo_root=repo,
                orchestration_id=orch,
                parent_agent_run_id=parent,
                child_agent_run_id=child,
                request_payload={
                    "agent_run_id": child,
                    "parent_agent_run_id": parent,
                    "agent_role": "step",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "build",
                    "context_id": "ctx_step_session_index_001",
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": [
                        "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/build_001/build_meta.json"
                    ],
                },
                response_payload={
                    "agent_run_id": child,
                    **_spawn_response_payload(session),
                },
            )
            index_path = repo / "workspace" / "orchestrations" / orch / "session_run_index.json"
            index_doc = json.loads(index_path.read_text(encoding="utf-8"))
            entries = index_doc.get("entries", [])
            launch_entry = next(
                item
                for item in entries
                if isinstance(item, dict) and str(item.get("agent_run_id", "")).strip() == child
            )
            self.assertEqual(launch_entry.get("agent_session_id"), session)
            self.assertEqual(launch_entry.get("status"), "running")

            record_agent_run(
                repo_root=repo,
                orchestration_id=orch,
                payload={
                    "agent_run_id": child,
                    "agent_role": "step",
                    "agent_backend": "codex",
                    "agent_model": "gpt-5.3-codex",
                    "agent_session_id": session,
                    "context_id": "ctx_step_session_index_001",
                    "status": "blocked",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "build",
                    "launch_request_ref": f"workspace/orchestrations/{orch}/launches/{child}.request.json",
                    "launch_response_ref": f"workspace/orchestrations/{orch}/launches/{child}.response.json",
                    "launch_prompt_ref": f"workspace/orchestrations/{orch}/launches/{child}.prompt.txt",
                    "launch_reply_ref": f"workspace/orchestrations/{orch}/launches/{child}.reply.txt",
                    "result_summary": "step completed",
                    "output_refs": [],
                },
            )
            updated = json.loads(index_path.read_text(encoding="utf-8"))
            updated_entries = updated.get("entries", [])
            terminal_entry = next(
                item
                for item in updated_entries
                if isinstance(item, dict) and str(item.get("agent_run_id", "")).strip() == child
            )
            self.assertEqual(terminal_entry.get("status"), "blocked")

    def test_pre_orchestration_start_logs_persisted_parallel_nodes_explicit(self) -> None:
        """setdefault で保持した値と hook 返却・ログ用 detail が一致すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            orch = "orch_parallel_audit"
            with patch.dict(os.environ, {"METDSL_ALLOW_PARALLEL_NODES": "1"}):
                out1 = pre_orchestration_start(repo, orch, event="init")
            self.assertTrue(out1["parallel_nodes_explicit"])
            meta_path = repo / "workspace" / "orchestrations" / orch / "orchestration_meta.json"
            meta1 = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertTrue(meta1.get("parallel_nodes_explicit"))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("METDSL_ALLOW_PARALLEL_NODES", None)
                out2 = pre_orchestration_start(repo, orch, event="preflight")
            self.assertTrue(out2["parallel_nodes_explicit"])
            meta2 = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta2.get("parallel_nodes_explicit"), meta1.get("parallel_nodes_explicit"))

    def test_pre_phase_complete_judge_checks_rejects_pass_decision_with_fail_or_blocked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            node_key = "problem/shallow_water2d@0.3.0"
            nk_safe = "problem__shallow_water2d__0.3.0"
            pipe_rel = "workspace/pipelines/judge_test_pipe"
            base = repo / pipe_rel / "execute" / "ex_j1" / nk_safe
            base.mkdir(parents=True)
            (base / "semantic_review.json").write_text(
                json.dumps({"decision": "pass"}),
                encoding="utf-8",
            )
            lr_rel = "workspace/launches/judge_lr.json"
            (repo / lr_rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / lr_rel).write_text(
                json.dumps({"pipeline_ref": pipe_rel, "execution_id": "ex_j1"}),
                encoding="utf-8",
            )
            payload = {"launch_request_ref": lr_rel}
            for bad_status in ("fail", "blocked"):
                with self.assertRaisesRegex(
                    ValueError,
                    "decision=pass cannot accompany fail or blocked",
                ):
                    _pre_phase_complete_judge_checks(
                        repo,
                        node_key=node_key,
                        status_token=bad_status,
                        payload=payload,
                    )

    def test_pre_phase_complete_judge_checks_skips_semantic_review_on_timeout_or_cancel(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            node_key = "problem/shallow_water2d@0.3.0"
            nk_safe = "problem__shallow_water2d__0.3.0"
            pipe_rel = "workspace/pipelines/judge_timeout_pipe"
            base = repo / pipe_rel / "execute" / "ex_to1" / nk_safe
            base.mkdir(parents=True)
            lr_rel = "workspace/launches/judge_lr_timeout.json"
            (repo / lr_rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / lr_rel).write_text(
                json.dumps({"pipeline_ref": pipe_rel, "execution_id": "ex_to1"}),
                encoding="utf-8",
            )
            payload = {"launch_request_ref": lr_rel}
            for st in ("timeout", "cancel"):
                _pre_phase_complete_judge_checks(
                    repo,
                    node_key=node_key,
                    status_token=st,
                    payload=payload,
                )


class PreflightLiveProbeTtlTests(unittest.TestCase):
    def test_live_preflight_mode_never_on_zero(self) -> None:
        with patch.dict(os.environ, {"METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "0"}):
            self.assertEqual(_live_preflight_mode(), "never")

    def test_live_preflight_mode_never_on_false(self) -> None:
        with patch.dict(os.environ, {"METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "false"}):
            self.assertEqual(_live_preflight_mode(), "never")

    def test_live_preflight_mode_always_on_one(self) -> None:
        with patch.dict(os.environ, {"METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "1"}):
            self.assertEqual(_live_preflight_mode(), "always")

    def test_live_preflight_mode_ttl_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT", None)
            self.assertEqual(_live_preflight_mode(), "ttl")

    def test_live_preflight_mode_ttl_on_unknown_value(self) -> None:
        with patch.dict(os.environ, {"METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto"}):
            self.assertEqual(_live_preflight_mode(), "ttl")

    def test_live_preflight_ttl_seconds_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("METDSL_PREFLIGHT_TTL_SECONDS", None)
            self.assertEqual(_live_preflight_ttl_seconds(), 1800)

    def test_live_preflight_ttl_seconds_custom(self) -> None:
        with patch.dict(os.environ, {"METDSL_PREFLIGHT_TTL_SECONDS": "300"}):
            self.assertEqual(_live_preflight_ttl_seconds(), 300)

    def test_live_preflight_ttl_seconds_zero(self) -> None:
        with patch.dict(os.environ, {"METDSL_PREFLIGHT_TTL_SECONDS": "0"}):
            self.assertEqual(_live_preflight_ttl_seconds(), 0)

    def test_live_preflight_ttl_seconds_invalid_value(self) -> None:
        with patch.dict(os.environ, {"METDSL_PREFLIGHT_TTL_SECONDS": "abc"}):
            self.assertEqual(_live_preflight_ttl_seconds(), 1800)

    def test_live_preflight_ttl_seconds_negative(self) -> None:
        with patch.dict(os.environ, {"METDSL_PREFLIGHT_TTL_SECONDS": "-1"}):
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
                "METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "METDSL_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch(
                    "tools.orchestration_runtime.probe_execution_platform",
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
                "METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "METDSL_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch("tools.orchestration_runtime.probe_execution_platform") as probe_mock:
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
                "METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "METDSL_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch("tools.orchestration_runtime.probe_execution_platform") as probe_mock:
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
            with patch.dict(os.environ, {"METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "1"}):
                with patch("tools.orchestration_runtime.probe_execution_platform") as probe_mock:
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
            with patch.dict(os.environ, {"METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "0"}):
                with patch(
                    "tools.orchestration_runtime.probe_execution_platform",
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
                "METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "METDSL_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch("tools.orchestration_runtime.probe_execution_platform") as probe_mock:
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
                "METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "METDSL_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch("tools.orchestration_runtime.probe_execution_platform") as probe_mock:
                    probe_ret = _launchable_preflight_dict()
                    self.assertNotIn("checked_at", probe_ret)
                    probe_mock.return_value = probe_ret
                    with patch(
                        "tools.orchestration_runtime._utc_now_iso",
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
                "METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "METDSL_PREFLIGHT_TTL_SECONDS": "0",
            }
            with patch.dict(os.environ, env):
                with patch("tools.orchestration_runtime.probe_execution_platform") as probe_mock:
                    probe_mock.return_value = _launchable_preflight_dict(
                        checked_at="2026-04-15T11:00:00Z",
                    )
                    _require_preflight_launchable(repo, "o1", enforce_live_probe=True)
                    self.assertEqual(probe_mock.call_count, 1)

    def test_record_launch_skips_probe_on_second_call_within_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_orchestration(repo_root=repo, orchestration_id="orch_001")
            meta_path = repo / "workspace/orchestrations/orch_001/orchestration_meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["dependency_readiness"] = {
                "direct_dependency_plan_readiness": True,
                "direct_dependency_execution_readiness": True,
                "detail": {
                    "plan_ref_verified": True,
                    "pipeline_ref_verified": True,
                    "aggregate_verdict_verified": True,
                },
            }
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
                "METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "METDSL_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch("tools.orchestration_runtime.probe_execution_platform") as probe_mock:
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
                            "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
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
            meta_path = repo / "workspace/orchestrations/orch_001/orchestration_meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["dependency_readiness"] = {
                "direct_dependency_plan_readiness": True,
                "direct_dependency_execution_readiness": True,
                "detail": {
                    "plan_ref_verified": True,
                    "pipeline_ref_verified": True,
                    "aggregate_verdict_verified": True,
                },
            }
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            path = repo / "workspace/orchestrations/orch_001/preflight.json"
            path.write_text(
                json.dumps(_launchable_preflight_dict(checked_at="2026-04-15T10:00:00Z"), indent=2)
                + "\n",
                encoding="utf-8",
            )
            env = {
                "METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                "METDSL_PREFLIGHT_TTL_SECONDS": "1800",
            }
            with patch.dict(os.environ, env):
                with patch("tools.orchestration_runtime.probe_execution_platform") as probe_mock:
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
                            "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
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
                    "METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                    "METDSL_PREFLIGHT_TTL_SECONDS": "1800",
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
            with patch.dict(os.environ, {"METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto"}):
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
            with patch.dict(os.environ, {"METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "1"}):
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
                    "METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "auto",
                    "METDSL_PREFLIGHT_TTL_SECONDS": "1800",
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


class TestPhase1RuleSourceAudit(unittest.TestCase):
    def test_phase1_init_preflight_record_launch_writes_audit_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            orch = repo_root / "workspace/orchestrations/orch_001"
            self.assertTrue((orch / "access_policies").is_dir())
            self.assertTrue((orch / "access_logs").is_dir())
            self.assertTrue((orch / "violations").is_dir())
            ps0 = json.loads((orch / "phase_state.json").read_text(encoding="utf-8"))
            self.assertEqual(ps0.get("current_state"), "initialized")
            self.assertEqual(ps0.get("orchestration_id"), "orch_001")
            self.assertIsInstance(ps0.get("node_states"), dict)

            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            ps1 = json.loads((orch / "phase_state.json").read_text(encoding="utf-8"))
            self.assertEqual(ps1.get("current_state"), "preflight_passed")
            log_lines = (orch / "phase_state_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertGreaterEqual(len(log_lines), 2)
            last = json.loads(log_lines[-1])
            self.assertEqual(last.get("event"), "preflight_written")
            self.assertEqual(last.get("to"), "preflight_passed")

            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="substep_p1_001",
                request_payload={
                    "agent_run_id": "substep_p1_001",
                    "agent_role": "substep",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": [f"{_FIX_PLAN_REF}/case.resolved.yaml"],
                    "launch_prompt_full": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "substep_p1_001",
                    ),
                },
                response_payload={
                    "agent_run_id": "substep_p1_001",
                    **_spawn_response_payload("sess_substep_p1"),
                },
            )
            pol_path = orch / "access_policies" / "substep_p1_001.json"
            self.assertTrue(pol_path.exists())
            policy = json.loads(pol_path.read_text(encoding="utf-8"))
            self.assertEqual(policy.get("agent_run_id"), "substep_p1_001")
            self.assertEqual(policy.get("step"), "plan")
            self.assertEqual(policy.get("substep"), "generate")
            self.assertEqual(policy.get("denied_read_roots"), ["tools/"])
            self.assertIn("docs/", policy.get("allowed_read_roots", []))
            self.assertIn("spec/", policy.get("allowed_read_roots", []))
            self.assertIn(
                "workspace/tmp/substep_p1_001/",
                policy.get("allowed_read_roots", []),
            )
            self.assertNotIn(
                "workspace/tmp/",
                policy.get("allowed_read_roots", []),
            )
            self.assertIn(
                _FIX_PLAN_REF.rstrip("/") + "/",
                policy.get("allowed_read_roots", []),
            )
            self.assertIn(
                _FIX_PIPE_REF.rstrip("/") + "/",
                policy.get("allowed_read_roots", []),
            )
            self.assertIn(
                "skills/workflow-plan-generate/SKILL.md/",
                policy.get("allowed_read_roots", []),
            )
            self.assertEqual(
                policy.get("allowed_gate_services"),
                [
                    "validate_pipeline_semantics",
                    "check_artifact_syntax",
                    "validate_workspace_root",
                    "orchestration_read",
                ],
            )
            cap_path = orch / "capabilities" / "substep_p1_001.json"
            self.assertTrue(cap_path.exists())
            cap = json.loads(cap_path.read_text(encoding="utf-8"))
            self.assertEqual(cap.get("agent_run_id"), "substep_p1_001")
            self.assertEqual(cap.get("step"), "plan")
            self.assertTrue(isinstance(cap.get("capability_token"), str) and cap["capability_token"])
            self.assertIn(_FIX_PLAN_REF.rstrip("/") + "/", cap.get("write_roots", []))
            manifest_path = orch / "output_manifests" / "substep_p1_001.json"
            self.assertTrue(manifest_path.exists())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest.get("agent_run_id"), "substep_p1_001")
            self.assertEqual(
                manifest.get("allowed_output_paths"),
                [f"{_FIX_PLAN_REF}/case.resolved.yaml"],
            )
            read_manifest_path = orch / "read_manifests" / "substep_p1_001.json"
            self.assertTrue(read_manifest_path.exists())
            read_manifest = json.loads(read_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(read_manifest.get("agent_run_id"), "substep_p1_001")
            self.assertIn("docs/", read_manifest.get("allowed_read_roots", []))
            self.assertIn("tools/", read_manifest.get("denied_read_roots", []))
            ps_launch = json.loads((orch / "phase_state.json").read_text(encoding="utf-8"))
            node_safe = "problem__shallow_water2d__0.3.0"
            self.assertEqual(
                ps_launch.get("node_states", {}).get(node_safe, {}).get("plan"),
                "child_running",
            )

    def test_phase2_orchestration_read_denied_tools_emits_rule_source_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "tools" / "p1_dummy.txt").write_text("dummy-tools-read\n", encoding="utf-8")
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="child_p1r",
                request_payload={
                    "agent_run_id": "child_p1r",
                    "agent_role": "substep",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": [
                        f"{_FIX_PLAN_REF}/case.resolved.yaml",
                        f"{_FIX_PLAN_REF}/plan_meta.json",
                    ],
                    "launch_prompt_full": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "child_p1r",
                    ),
                },
                response_payload={"agent_run_id": "child_p1r", **_spawn_response_payload("sess_p1r")},
            )
            with self.assertRaisesRegex(RuntimeError, "orchestration-read denied"):
                log_orchestration_read(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    agent_run_id="child_p1r",
                    read_path="tools/p1_dummy.txt",
                )
            viol = (
                repo_root
                / "workspace/orchestrations/orch_001/violations/child_p1r.rule_source_violation.json"
            )
            self.assertTrue(viol.exists())
            vdoc = json.loads(viol.read_text(encoding="utf-8"))
            self.assertEqual(vdoc.get("kind"), "rule_source_violation")
            self.assertEqual(vdoc.get("read_path"), "tools/p1_dummy.txt")
            meta = json.loads(
                (repo_root / "workspace/orchestrations/orch_001/orchestration_meta.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(meta.get("status"), "fail")
            log_path = (
                repo_root
                / "workspace/orchestrations/orch_001/access_logs/child_p1r.jsonl"
            )
            self.assertTrue(log_path.exists())
            log_entry = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertTrue(log_entry.get("denied_match"))
            self.assertEqual(log_entry.get("path"), "tools/p1_dummy.txt")

    def test_phase2_orchestration_read_rejects_path_outside_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "plans").mkdir(parents=True, exist_ok=True)
            (repo_root / "plans" / "outside.txt").write_text("ng\n", encoding="utf-8")
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="child_p1r",
                request_payload={
                    "agent_run_id": "child_p1r",
                    "agent_role": "substep",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": [
                        f"{_FIX_PLAN_REF}/case.resolved.yaml",
                        f"{_FIX_PLAN_REF}/plan_meta.json",
                    ],
                    "launch_prompt_full": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "child_p1r",
                    ),
                },
                response_payload={"agent_run_id": "child_p1r", **_spawn_response_payload("sess_p1r")},
            )
            with self.assertRaisesRegex(RuntimeError, "outside allowed_read_roots"):
                log_orchestration_read(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    agent_run_id="child_p1r",
                    read_path="plans/outside.txt",
                )
            log_path = (
                repo_root
                / "workspace/orchestrations/orch_001/access_logs/child_p1r.jsonl"
            )
            self.assertTrue(log_path.exists())
            log_entry = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertFalse(log_entry.get("allowed_match"))
            self.assertFalse(log_entry.get("denied_match"))
            self.assertEqual(log_entry.get("path"), "plans/outside.txt")

    def test_phase2_orchestration_read_allows_skill_ref_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "skills" / "workflow-plan-generate").mkdir(parents=True, exist_ok=True)
            (repo_root / "skills" / "workflow-plan-generate" / "SKILL.md").write_text(
                "# workflow-plan-generate\n", encoding="utf-8"
            )
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="child_p1r",
                request_payload={
                    "agent_run_id": "child_p1r",
                    "agent_role": "substep",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": [
                        f"{_FIX_PLAN_REF}/case.resolved.yaml",
                        f"{_FIX_PLAN_REF}/plan_meta.json",
                    ],
                    "launch_prompt_full": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "child_p1r",
                    ),
                },
                response_payload={"agent_run_id": "child_p1r", **_spawn_response_payload("sess_p1r")},
            )
            out = log_orchestration_read(
                repo_root=repo_root,
                orchestration_id="orch_001",
                agent_run_id="child_p1r",
                read_path="skills/workflow-plan-generate/SKILL.md",
            )
            self.assertTrue(out.get("file_exists"))
            self.assertEqual(out.get("read_path"), "skills/workflow-plan-generate/SKILL.md")
            self.assertIn("workflow-plan-generate", str(out.get("content")))

    def test_phase2_orchestration_read_rejects_when_read_manifest_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "docs").mkdir(parents=True, exist_ok=True)
            (repo_root / "docs" / "probe.txt").write_text("ok\n", encoding="utf-8")
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="child_p1r",
                request_payload={
                    "agent_run_id": "child_p1r",
                    "agent_role": "substep",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": [
                        f"{_FIX_PLAN_REF}/case.resolved.yaml",
                        f"{_FIX_PLAN_REF}/plan_meta.json",
                    ],
                    "launch_prompt_full": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "child_p1r",
                    ),
                },
                response_payload={"agent_run_id": "child_p1r", **_spawn_response_payload("sess_p1r")},
            )
            manifest = (
                repo_root
                / "workspace/orchestrations/orch_001/read_manifests/child_p1r.json"
            )
            manifest.unlink()
            with self.assertRaisesRegex(FileNotFoundError, "read access manifest not found"):
                log_orchestration_read(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    agent_run_id="child_p1r",
                    read_path="docs/probe.txt",
                )

    def test_phase1_resume_missing_phase_state_infers_preflight_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_p1m")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_p1m",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            orch = repo_root / "workspace/orchestrations/orch_p1m"
            (orch / "phase_state.json").unlink()
            (orch / "phase_state_log.jsonl").unlink()
            doc = merge_phase_state_for_resume(repo_root, "orch_p1m")
            self.assertEqual(doc.get("current_state"), "preflight_passed")

    def test_phase1_orchestration_read_cli_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "docs").mkdir(parents=True, exist_ok=True)
            (repo_root / "docs" / "p1_doc.txt").write_text("ok\n", encoding="utf-8")
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            record_launch(
                repo_root=repo_root,
                orchestration_id="orch_001",
                parent_agent_run_id="orch_run_001",
                child_agent_run_id="c_cli",
                request_payload={
                    "agent_run_id": "c_cli",
                    "agent_role": "substep",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "plan",
                    "substep": "generate",
                    "orchestration_id": "orch_001",
                    "parent_agent_run_id": "orch_run_001",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": [
                        f"{_FIX_PLAN_REF}/case.resolved.yaml",
                        f"{_FIX_PLAN_REF}/plan_meta.json",
                    ],
                    "launch_prompt_full": _substep_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "plan",
                        "generate",
                        "c_cli",
                    ),
                },
                response_payload={"agent_run_id": "c_cli", **_spawn_response_payload("s_cli")},
            )
            cap = json.loads(
                (
                    repo_root / "workspace/orchestrations/orch_001/capabilities/c_cli.json"
                ).read_text(encoding="utf-8")
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(
                    [
                        "orchestration-read",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "orch_001",
                        "--agent-run-id",
                        "c_cli",
                        "--read-path",
                        "docs/p1_doc.txt",
                        "--capability-token",
                        str(cap["capability_token"]),
                    ]
                )
            self.assertEqual(rc, 0)
            cli_out = json.loads(buf.getvalue())
            self.assertFalse(cli_out.get("denied_match"))
            self.assertEqual(cli_out.get("content"), "ok\n")


class TestPhase2PlanGuardsIntegration(unittest.TestCase):
    def test_required_child_agent_kind_plan_and_build(self) -> None:
        self.assertEqual(_required_child_agent_kind("plan"), "substep")
        self.assertEqual(_required_child_agent_kind("build"), "step")

    def test_workflow_launch_check_fail_closed_by_session_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="wf1")
            dep_path = repo_root / _FIX_DEP_REF
            dep_path.parent.mkdir(parents=True, exist_ok=True)
            dep_path.write_text("ok\n", encoding="utf-8")
            meta_path = repo_root / "workspace/orchestrations/wf1/orchestration_meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["dependency_ref"] = _FIX_DEP_REF
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="wf1",
                payload={
                    "status": "pass",
                    "backend": "codex",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "session_policy": {"allow_substep_agent_launch": False},
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            out = workflow_launch_check(
                repo_root,
                orchestration_id="wf1",
                node_key="problem/shallow_water2d@0.3.0",
                step="plan",
                backend="codex",
                require_child_agent="substep",
            )
            self.assertEqual(out.get("status"), "fail_closed")
            self.assertEqual(out.get("reason_code"), "child_agent_forbidden_by_session_policy")
            self.assertEqual(out.get("next_action"), "stop_before_phase_body")

    def test_workflow_launch_check_fail_closed_when_session_policy_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="wf2")
            dep_path = repo_root / _FIX_DEP_REF
            dep_path.parent.mkdir(parents=True, exist_ok=True)
            dep_path.write_text("ok\n", encoding="utf-8")
            meta_path = repo_root / "workspace/orchestrations/wf2/orchestration_meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["dependency_ref"] = _FIX_DEP_REF
            meta["dependency_readiness"] = {
                "direct_dependency_plan_readiness": True,
                "direct_dependency_execution_readiness": True,
                "detail": {
                    "plan_ref_verified": True,
                    "pipeline_ref_verified": True,
                    "aggregate_verdict_verified": True,
                },
            }
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            # Simulate a legacy/manual preflight payload that has no session policy fields.
            preflight_path = repo_root / "workspace/orchestrations/wf2/preflight.json"
            preflight_path.parent.mkdir(parents=True, exist_ok=True)
            preflight_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "backend": "codex",
                        "sandbox_runtime": "bwrap",
                        "sandbox_enforced": True,
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                        "feature_states": {"multi_agent": True, "codex_hooks": True},
                        "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            out = workflow_launch_check(
                repo_root,
                orchestration_id="wf2",
                node_key="problem/shallow_water2d@0.3.0",
                step="plan",
                backend="codex",
                require_child_agent="substep",
            )
            self.assertEqual(out.get("status"), "fail_closed")
            self.assertEqual(out.get("reason_code"), "child_agent_forbidden_by_session_policy")
            self.assertEqual(out.get("blocking_policy_scope"), "session_policy_missing")

    def test_record_launch_rejects_missing_step_or_node_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="wf5")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="wf5",
                payload={
                    "status": "pass",
                    "backend": "codex",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "session_policy": {
                        "allow_step_agent_launch": True,
                        "allow_substep_agent_launch": True,
                    },
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            base = {
                "agent_run_id": "child_missing_fields",
                "agent_role": "substep",
                "orchestration_id": "wf5",
                "parent_agent_run_id": "orch_wf5",
                "plan_ref": _FIX_PLAN_REF,
                "pipeline_ref": _FIX_PIPE_REF,
                "dependency_ref": _FIX_DEP_REF,
                "skill_name": "workflow-plan-generate",
                "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                "skill_must_read_refs": _fixture_skill_must_read_refs_substep("plan", "generate"),
                "issue_severity": "none",
                "repair_strategy": "none",
                "repair_target_agent_run_id": "none",
                "repair_reason": "none",
            }
            for missing_key in ("step", "node_key"):
                req = dict(base)
                req["step"] = "plan"
                req["substep"] = "generate"
                req["node_key"] = "problem/shallow_water2d@0.3.0"
                del req[missing_key]
                req["launch_prompt_full"] = render_launch_prompt_text(req)
                with self.subTest(missing_key=missing_key):
                    with self.assertRaisesRegex(ValueError, f"non-empty {missing_key}"):
                        record_launch(
                            repo_root=repo_root,
                            orchestration_id="wf5",
                            parent_agent_run_id="orch_wf5",
                            child_agent_run_id=f"child_missing_{missing_key}",
                            request_payload=req,
                            response_payload={
                                "agent_run_id": f"child_missing_{missing_key}",
                                **_spawn_response_payload(f"sess_child_missing_{missing_key}"),
                            },
                        )
            index_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "wf5"
                / "session_run_index.json"
            )
            if index_path.exists():
                index_doc = json.loads(index_path.read_text(encoding="utf-8"))
                entries = index_doc.get("entries", [])
                self.assertFalse(
                    any(
                        isinstance(item, dict)
                        and str(item.get("agent_run_id", "")).startswith("child_missing_")
                        for item in entries
                    )
                )

    def test_workflow_launch_check_fail_closed_without_readiness_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="wf3")

            def runner(args, **kwargs):  # type: ignore[no-untyped-def]
                if args[1:] == ["--version"]:
                    return _FakeCompletedProcess(0, stdout="codex-cli 0.114.0\n")
                if args[1:] == ["features", "list"]:
                    return _FakeCompletedProcess(
                        0,
                        stdout="multi_agent experimental true\ncodex_hooks under-development true\n",
                    )
                raise AssertionError(args)

            with patch.dict(
                os.environ,
                {
                    "METDSL_HOME": "/tmp/codex-orchestration-test-home",
                    "METDSL_ORCHESTRATION_ASSUME_BWRAP": "1",
                },
            ):
                preflight_payload = probe_execution_platform(backend="codex", runner=runner)
            preflight_path = repo_root / "workspace/orchestrations/wf3/preflight.json"
            preflight_path.parent.mkdir(parents=True, exist_ok=True)
            preflight_path.write_text(
                json.dumps(preflight_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            phase_state_path = repo_root / "workspace/orchestrations/wf3/phase_state.json"
            phase_state = json.loads(phase_state_path.read_text(encoding="utf-8"))
            phase_state["current_state"] = "preflight_passed"
            phase_state_path.write_text(
                json.dumps(phase_state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            out = workflow_launch_check(
                repo_root,
                orchestration_id="wf3",
                node_key="problem/shallow_water2d@0.3.0",
                step="plan",
                backend="codex",
                require_child_agent="substep",
            )
            self.assertEqual(out.get("status"), "fail_closed")
            self.assertEqual(out.get("reason_code"), "dependency_not_ready")
            self.assertEqual(out.get("reason_detail"), "dependency_readiness_missing")
            self.assertEqual(out.get("next_action"), "stop_before_phase_body")

    def test_record_launch_enforces_workflow_launch_check_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="wf4")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="wf4",
                payload={
                    "status": "pass",
                    "backend": "codex",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "session_policy": {"allow_substep_agent_launch": False},
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            req = {
                "agent_run_id": "plan_sub_fail_closed",
                "agent_role": "substep",
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "plan",
                "substep": "generate",
                "orchestration_id": "wf4",
                "parent_agent_run_id": "orch_wf4",
                "plan_ref": _FIX_PLAN_REF,
                "pipeline_ref": _FIX_PIPE_REF,
                "dependency_ref": _FIX_DEP_REF,
                "skill_name": "workflow-plan-generate",
                "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                "skill_must_read_refs": _fixture_skill_must_read_refs_substep("plan", "generate"),
                "issue_severity": "none",
                "repair_strategy": "none",
                "repair_target_agent_run_id": "none",
                "repair_reason": "none",
                "launch_prompt_full": render_launch_prompt_text(
                    {
                        "agent_run_id": "plan_sub_fail_closed",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "plan",
                        "substep": "generate",
                        "orchestration_id": "wf4",
                        "parent_agent_run_id": "orch_wf4",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_DEP_REF,
                        "skill_name": "workflow-plan-generate",
                        "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                        "skill_must_read_refs": _fixture_skill_must_read_refs_substep(
                            "plan", "generate"
                        ),
                        "issue_severity": "none",
                        "repair_strategy": "none",
                        "repair_target_agent_run_id": "none",
                        "repair_reason": "none",
                    }
                ),
            }
            with self.assertRaisesRegex(RuntimeError, "workflow-launch-check"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="wf4",
                    parent_agent_run_id="orch_wf4",
                    child_agent_run_id="plan_sub_fail_closed",
                    request_payload=req,
                    response_payload={
                        "agent_run_id": "plan_sub_fail_closed",
                        **_spawn_response_payload("sess_plan_sub_fail_closed"),
                    },
                )
            meta = json.loads(
                (repo_root / "workspace/orchestrations/wf4/orchestration_meta.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(meta.get("status"), "fail_closed")
            self.assertEqual(meta.get("reason_code"), "child_agent_forbidden_by_session_policy")

    def test_validate_mcp_rejects_when_launch_response_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="g1")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="g1",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            with self.assertRaises(RuntimeError) as ctx:
                validate_mcp_build_tool_invocation(
                    repo_root,
                    orchestration_id="g1",
                    agent_run_id="ghost_child",
                    capability_token="unused",
                    tool_name="compile_project",
                )
            self.assertIn("record-launch", str(ctx.exception).lower())

    def test_validate_mcp_accepts_after_record_launch_build_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="g2")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="g2",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            g2_req = {
                "agent_run_id": "build_child_1",
                "agent_role": "step",
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "build",
                "orchestration_id": "g2",
                "parent_agent_run_id": "orch_g2",
                "plan_ref": _FIX_PLAN_REF,
                "pipeline_ref": _FIX_PIPE_REF,
                "dependency_ref": _FIX_DEP_REF,
                "skill_name": "workflow-build",
                "skill_ref": "skills/workflow-build/SKILL.md",
                "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                "issue_severity": "none",
                "repair_strategy": "none",
                "repair_target_agent_run_id": "none",
                "repair_reason": "none",
                "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"],
                "launch_prompt_full": render_launch_prompt_text(
                    {
                        "agent_run_id": "build_child_1",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "build",
                        "orchestration_id": "g2",
                        "parent_agent_run_id": "orch_g2",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_DEP_REF,
                        "skill_name": "workflow-build",
                        "skill_ref": "skills/workflow-build/SKILL.md",
                        "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                        "issue_severity": "none",
                        "repair_strategy": "none",
                        "repair_target_agent_run_id": "none",
                        "repair_reason": "none",
                    }
                ),
            }
            record_launch(
                repo_root=repo_root,
                orchestration_id="g2",
                parent_agent_run_id="orch_g2",
                child_agent_run_id="build_child_1",
                request_payload=g2_req,
                response_payload={
                    "agent_run_id": "build_child_1",
                    **_spawn_response_payload("sess_build_child_1"),
                },
            )
            cap_path = repo_root / "workspace/orchestrations/g2/capabilities/build_child_1.json"
            cap = json.loads(cap_path.read_text(encoding="utf-8"))
            validate_mcp_build_tool_invocation(
                repo_root,
                orchestration_id="g2",
                agent_run_id="build_child_1",
                capability_token=str(cap["capability_token"]),
                tool_name="compile_project",
            )

    def test_tool_compile_project_enforces_gate_when_orchestration_id_set(self) -> None:
        """L-FOURTH-1: exercise the REAL `validate_mcp_build_tool_invocation`
        path (no stubbed runtime). When an orchestration is initialized but
        `record-launch` was never called for the agent_run_id, the production
        gate raises a RuntimeError mentioning "record-launch" — the test
        assertion is satisfied by the real production message rather than a
        self-fulfilling stub."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="g3")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="g3",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            # No record-launch for agent_run_id="nolaunch" → real
            # validate_mcp_build_tool_invocation raises:
            # "MCP phase gate: record-launch did not complete (missing
            #  launches/*.response.json) for agent_run_id='nolaunch'"
            with self.assertRaises(RuntimeError) as ctx:
                tool_compile_project(
                    {
                        "project_dir": str(repo_root),
                        "language": "python",
                        "build_system": "poetry",
                        "orchestration_id": "g3",
                        "agent_run_id": "nolaunch",
                        "capability_token": "x",
                        "repo_root": str(repo_root),
                    }
                )
            self.assertIn("record-launch", str(ctx.exception).lower())

    def test_apply_patch_gate_orchestration_rejects_plan_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="g4")
            bad = f"{_FIX_PLAN_REF}/case.resolved.yaml"
            with self.assertRaises(RuntimeError):
                gate_apply_patch_writes(
                    repo_root,
                    orchestration_id="g4",
                    actor_role="orchestration",
                    changed_paths=[bad],
                    agent_run_id="orch_actor",
                    capability_token=None,
                )
            vio = repo_root / "workspace/orchestrations/g4/violations/orch_actor.noncanonical_phase_write_attempt.json"
            self.assertTrue(vio.exists())

    def test_apply_patch_gate_orchestration_allows_orchestration_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="g5")
            out = gate_apply_patch_writes(
                repo_root,
                orchestration_id="g5",
                actor_role="orchestration",
                changed_paths=["workspace/orchestrations/g5/orchestration_meta.json"],
                agent_run_id="orch_actor",
                capability_token=None,
            )
            self.assertTrue(out.get("allowed"))

    def test_apply_patch_gate_plan_child_rejects_pipeline_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="g6")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="g6",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            g6_req = {
                "agent_run_id": "plan_sub_1",
                "agent_role": "substep",
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "plan",
                "substep": "generate",
                "orchestration_id": "g6",
                "parent_agent_run_id": "orch_g6",
                "plan_ref": _FIX_PLAN_REF,
                "pipeline_ref": _FIX_PIPE_REF,
                "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
                "skill_name": "workflow-plan-generate",
                "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                "skill_must_read_refs": _fixture_skill_must_read_refs_substep("plan", "generate"),
                "issue_severity": "none",
                "repair_strategy": "none",
                "repair_target_agent_run_id": "none",
                "repair_reason": "none",
                "allowed_output_paths": [
                    f"{_FIX_PLAN_REF}/case.resolved.yaml",
                    f"{_FIX_PLAN_REF}/plan_meta.json",
                ],
                "launch_prompt_full": render_launch_prompt_text(
                    {
                        "agent_run_id": "plan_sub_1",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "plan",
                        "substep": "generate",
                        "orchestration_id": "g6",
                        "parent_agent_run_id": "orch_g6",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_PLAN_STEP_DEP_REF,
                        "skill_name": "workflow-plan-generate",
                        "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                        "skill_must_read_refs": _fixture_skill_must_read_refs_substep("plan", "generate"),
                        "issue_severity": "none",
                        "repair_strategy": "none",
                        "repair_target_agent_run_id": "none",
                        "repair_reason": "none",
                    }
                ),
            }
            record_launch(
                repo_root=repo_root,
                orchestration_id="g6",
                parent_agent_run_id="orch_g6",
                child_agent_run_id="plan_sub_1",
                request_payload=g6_req,
                response_payload={"agent_run_id": "plan_sub_1", **_spawn_response_payload("sess_ps1")},
            )
            cap = json.loads(
                (repo_root / "workspace/orchestrations/g6/capabilities/plan_sub_1.json").read_text(
                    encoding="utf-8"
                )
            )
            bad = f"{_FIX_PIPE_REF}/generate/out.txt"
            with self.assertRaises(RuntimeError):
                gate_apply_patch_writes(
                    repo_root,
                    orchestration_id="g6",
                    actor_role="substep",
                    changed_paths=[bad],
                    agent_run_id="plan_sub_1",
                    capability_token=str(cap["capability_token"]),
                )

    def test_apply_patch_gate_rejects_plan_write_before_child_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="g7")
            phase_state_path = repo_root / "workspace/orchestrations/g7/phase_state.json"
            phase_state = json.loads(phase_state_path.read_text(encoding="utf-8"))
            node_safe = "problem__shallow_water2d__0.3.0"
            phase_state["current_state"] = "preflight_passed"
            phase_state["node_states"][node_safe] = {
                "plan": "launch_recorded",
                "generate": "not_started",
                "build": "not_started",
                "execute": "not_started",
                "judge": "not_started",
            }
            phase_state_path.write_text(json.dumps(phase_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            cap_path = repo_root / "workspace/orchestrations/g7/capabilities/prelaunch_sub.json"
            cap_path.parent.mkdir(parents=True, exist_ok=True)
            cap_path.write_text(
                json.dumps(
                    {
                        "agent_run_id": "prelaunch_sub",
                        "capability_token": "tok_prelaunch",
                        "orchestration_id": "g7",
                        "agent_role": "substep",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "plan",
                        "write_roots": [_FIX_PLAN_REF + "/"],
                        "mcp_permissions": [],
                        "expires_at": "2099-01-01T00:00:00Z",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                gate_apply_patch_writes(
                    repo_root,
                    orchestration_id="g7",
                    actor_role="substep",
                    changed_paths=[f"{_FIX_PLAN_REF}/case.resolved.yaml"],
                    agent_run_id="prelaunch_sub",
                    capability_token="tok_prelaunch",
                )
            vio = (
                repo_root
                / "workspace/orchestrations/g7/violations/prelaunch_sub.noncanonical_phase_write_attempt.json"
            )
            self.assertTrue(vio.exists())

    def test_reserve_phase_root_allows_reservation_but_not_phase_root_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="g8")
            reserved = reserve_phase_root(
                repo_root,
                orchestration_id="g8",
                node_key="problem/shallow_water2d@0.3.0",
                step="plan",
                reserved_id="sw_flux_rusanov_p0_20260415_001",
                reserved_by_agent_run_id="orch_run_001",
            )
            self.assertEqual(reserved.get("status"), "reserved")
            reservation_path = (
                repo_root
                / "workspace/orchestrations/g8/reservations/problem__shallow_water2d__0.3.0/plan.json"
            )
            self.assertTrue(reservation_path.exists())
            with self.assertRaises(RuntimeError):
                gate_apply_patch_writes(
                    repo_root,
                    orchestration_id="g8",
                    actor_role="orchestration",
                    changed_paths=[f"{_FIX_PLAN_REF}/case.resolved.yaml"],
                    agent_run_id="orch_run_001",
                    capability_token=None,
                )

    def test_set_status_fail_closed_persists_reason_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="g9")
            meta = update_orchestration_status(
                repo_root=repo_root,
                orchestration_id="g9",
                status="fail_closed",
                reason_code="child_agent_forbidden_by_session_policy",
                reason_detail="session policy denied substep launch",
                blocking_policy_scope="session_policy.allow_substep_agent_launch",
            )
            self.assertEqual(meta.get("status"), "fail_closed")
            self.assertEqual(meta.get("reason_code"), "child_agent_forbidden_by_session_policy")
            self.assertEqual(meta.get("blocking_policy_scope"), "session_policy.allow_substep_agent_launch")

    def test_set_status_terminal_writes_cleanup_committed_marker_after_cleanup(self) -> None:
        """Adv-35 invariant: set-status terminal must commit (a) terminal
        meta status, (b) tmp cleanup, and (c) cleanup_committed marker.
        The committed marker is what the validator uses to decide that
        exemption can be revoked — without it, the validator keeps the
        orchestration's tmp scratch exempt as cleanup-pending."""
        from tools.orchestration_runtime import _cleanup_committed_marker_path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_committed")
            meta_path = (
                repo_root / "workspace" / "orchestrations" / "orch_committed"
                / "orchestration_meta.json"
            )
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            orch_arid = meta["orchestration_agent_run_id"]
            tmp_dir = repo_root / "workspace" / "tmp" / orch_arid
            (tmp_dir / "leaked.py").write_text("print('x')\n", encoding="utf-8")

            update_orchestration_status(
                repo_root=repo_root,
                orchestration_id="orch_committed",
                status="fail",
                reason_detail="adv-35 invariant test",
            )

            # 1) terminal meta is published.
            meta_after = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta_after.get("status"), "fail")
            # 2) tmp dir is cleaned.
            self.assertFalse(tmp_dir.exists(), "set-status terminal must clean orch tmp")
            # 3) cleanup_committed marker is written.
            committed = _cleanup_committed_marker_path(repo_root, "orch_committed", orch_arid)
            self.assertTrue(committed.is_file(),
                            "cleanup_committed marker must be written after cleanup")

    def test_set_status_terminal_cleans_orchestration_tmp_root(self) -> None:
        """Adv-13: when set-status flips to a terminal value (fail/fail_closed
        /timeout/etc.), the orchestration's own workspace/tmp/<orch_arid>/
        scratch must be cleaned. Otherwise validate_workspace_root flags any
        leftover *.py — Adv-9 only exempts that dir while status='running'."""
        for terminal_status, reason_code in (
            ("fail", None),
            ("fail_closed", "child_agent_forbidden_by_session_policy"),
            ("timeout", None),
            ("cancel", None),
        ):
            with self.subTest(status=terminal_status):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_root = Path(tmp)
                    orch_id = f"orch_set_status_cleanup_{terminal_status}"
                    init_orchestration(repo_root=repo_root, orchestration_id=orch_id)
                    meta_path = (
                        repo_root / "workspace" / "orchestrations" / orch_id
                        / "orchestration_meta.json"
                    )
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    orch_arid = meta["orchestration_agent_run_id"]
                    tmp_dir = repo_root / "workspace" / "tmp" / orch_arid
                    self.assertTrue(tmp_dir.is_dir(),
                                    "init_orchestration must have created the orch tmp dir")
                    leaked = tmp_dir / "leaked_helper.py"
                    leaked.write_text("print('helper')\n", encoding="utf-8")

                    update_orchestration_status(
                        repo_root=repo_root,
                        orchestration_id=orch_id,
                        status=terminal_status,
                        reason_code=reason_code,
                        reason_detail="adv-13 test",
                    )
                    self.assertFalse(
                        tmp_dir.exists(),
                        f"set-status {terminal_status!r} must clean the orchestration tmp dir; "
                        f"leftover scripts would be flagged by validate_workspace_root.",
                    )


class TestPhase3RunGate(unittest.TestCase):
    def _setup_run_gate_fixture(self, repo_root: Path) -> str:
        (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
        (repo_root / "workspace" / "probe.json").write_text('{"ok": true}\n', encoding="utf-8")
        init_orchestration(repo_root=repo_root, orchestration_id="rg1")
        write_preflight(
            repo_root=repo_root,
            orchestration_id="rg1",
            payload={
                "status": "pass",
                "sandbox_runtime": "bwrap",
                "sandbox_enforced": True,
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
                "feature_states": {"multi_agent": True, "codex_hooks": True},
                "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
            },
        )
        req = {
            "agent_run_id": "build_child_rg1",
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "build",
            "orchestration_id": "rg1",
            "parent_agent_run_id": "orch_rg1",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "dependency_ref": _FIX_DEP_REF,
            "skill_name": "workflow-build",
            "skill_ref": "skills/workflow-build/SKILL.md",
            "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
            "issue_severity": "none",
            "repair_strategy": "none",
            "repair_target_agent_run_id": "none",
            "repair_reason": "none",
            "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/build_meta.json"],
            "launch_prompt_full": render_launch_prompt_text(
                {
                    "agent_run_id": "build_child_rg1",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "build",
                    "orchestration_id": "rg1",
                    "parent_agent_run_id": "orch_rg1",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                    "issue_severity": "none",
                    "repair_strategy": "none",
                    "repair_target_agent_run_id": "none",
                    "repair_reason": "none",
                }
            ),
        }
        record_launch(
            repo_root=repo_root,
            orchestration_id="rg1",
            parent_agent_run_id="orch_rg1",
            child_agent_run_id="build_child_rg1",
            request_payload=req,
            response_payload={
                "agent_run_id": "build_child_rg1",
                **_spawn_response_payload("sess_build_child_rg1"),
            },
        )
        cap = json.loads(
            (
                repo_root
                / "workspace/orchestrations/rg1/capabilities/build_child_rg1.json"
            ).read_text(encoding="utf-8")
        )
        return str(cap["capability_token"])

    def test_record_launch_rejects_allowed_output_paths_outside_phase_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="rg_bad")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="rg_bad",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            bad_req = {
                "agent_run_id": "build_bad_001",
                "agent_role": "step",
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "build",
                "orchestration_id": "rg_bad",
                "parent_agent_run_id": "orch_bad",
                "plan_ref": _FIX_PLAN_REF,
                "pipeline_ref": _FIX_PIPE_REF,
                "dependency_ref": _FIX_DEP_REF,
                "skill_name": "workflow-build",
                "skill_ref": "skills/workflow-build/SKILL.md",
                "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                "issue_severity": "none",
                "repair_strategy": "none",
                "repair_target_agent_run_id": "none",
                "repair_reason": "none",
                "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/test3.tmp"],
                "launch_prompt_full": render_launch_prompt_text(
                    {
                        "agent_run_id": "build_bad_001",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "build",
                        "orchestration_id": "rg_bad",
                        "parent_agent_run_id": "orch_bad",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_DEP_REF,
                        "skill_name": "workflow-build",
                        "skill_ref": "skills/workflow-build/SKILL.md",
                        "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
                        "issue_severity": "none",
                        "repair_strategy": "none",
                        "repair_target_agent_run_id": "none",
                        "repair_reason": "none",
                    }
                ),
            }
            with self.assertRaisesRegex(ValueError, "outside phase contract outputs"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="rg_bad",
                    parent_agent_run_id="orch_bad",
                    child_agent_run_id="build_bad_001",
                    request_payload=bad_req,
                    response_payload={"agent_run_id": "build_bad_001", **_spawn_response_payload("sess_bad")},
                )

    def test_record_launch_rejects_execute_path_without_node_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="rg_exec_bad")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="rg_exec_bad",
                payload={
                    "status": "pass",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            bad_req = {
                "agent_run_id": "execute_bad_001",
                "agent_role": "step",
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "execute",
                "orchestration_id": "rg_exec_bad",
                "parent_agent_run_id": "orch_exec_bad",
                "plan_ref": _FIX_PLAN_REF,
                "pipeline_ref": _FIX_PIPE_REF,
                "dependency_ref": _FIX_DEP_REF,
                "skill_name": "workflow-execute",
                "skill_ref": "skills/workflow-execute/SKILL.md",
                "skill_must_read_refs": _fixture_skill_must_read_refs_step("execute"),
                "issue_severity": "none",
                "repair_strategy": "none",
                "repair_target_agent_run_id": "none",
                "repair_reason": "none",
                "allowed_output_paths": [f"{_FIX_PIPE_REF}/execute/exec_001/diagnostics.json"],
                "launch_prompt_full": render_launch_prompt_text(
                    {
                        "agent_run_id": "execute_bad_001",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "execute",
                        "orchestration_id": "rg_exec_bad",
                        "parent_agent_run_id": "orch_exec_bad",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_DEP_REF,
                        "skill_name": "workflow-execute",
                        "skill_ref": "skills/workflow-execute/SKILL.md",
                        "skill_must_read_refs": _fixture_skill_must_read_refs_step("execute"),
                        "issue_severity": "none",
                        "repair_strategy": "none",
                        "repair_target_agent_run_id": "none",
                        "repair_reason": "none",
                    }
                ),
            }
            with self.assertRaisesRegex(ValueError, "outside phase contract outputs"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="rg_exec_bad",
                    parent_agent_run_id="orch_exec_bad",
                    child_agent_run_id="execute_bad_001",
                    request_payload=bad_req,
                    response_payload={"agent_run_id": "execute_bad_001", **_spawn_response_payload("sess_exec_bad")},
                )

    def test_allowed_output_paths_for_launch_allows_tune_contract_output_path(self) -> None:
        req = {
            "agent_role": "substep",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "tune",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [f"{_FIX_PIPE_REF}/tune/trial_001/impl.resolved.yaml"],
        }
        out = _allowed_output_paths_for_launch(
            request_payload=req,
            write_roots=[f"{_FIX_PIPE_REF}/tune/"],
        )
        self.assertEqual(out, [f"{_FIX_PIPE_REF}/tune/trial_001/impl.resolved.yaml"])

    def test_allowed_output_paths_for_launch_rejects_nested_tune_subdirectory(self) -> None:
        req = {
            "agent_role": "substep",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "tune",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [f"{_FIX_PIPE_REF}/tune/trial_001/subdir/impl.resolved.yaml"],
        }
        with self.assertRaisesRegex(ValueError, "outside phase contract outputs"):
            _allowed_output_paths_for_launch(
                request_payload=req,
                write_roots=[f"{_FIX_PIPE_REF}/tune/"],
            )

    def test_allowed_output_paths_for_launch_allows_judge_contract_path(self) -> None:
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "judge",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [
                f"{_FIX_PIPE_REF}/execute/ex_001/problem__shallow_water2d__0.3.0/summary.json"
            ],
        }
        out = _allowed_output_paths_for_launch(
            request_payload=req,
            write_roots=[f"{_FIX_PIPE_REF}/execute/"],
        )
        self.assertEqual(
            out,
            [f"{_FIX_PIPE_REF}/execute/ex_001/problem__shallow_water2d__0.3.0/summary.json"],
        )

    def test_allowed_output_paths_for_launch_rejects_judge_path_under_legacy_judge_root(self) -> None:
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "judge",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [
                f"{_FIX_PIPE_REF}/judge/jdg_001/problem__shallow_water2d__0.3.0/summary.json"
            ],
        }
        with self.assertRaisesRegex(ValueError, "must be under capability write_roots"):
            _allowed_output_paths_for_launch(
                request_payload=req,
                write_roots=[f"{_FIX_PIPE_REF}/execute/"],
            )

    def test_allowed_output_paths_for_launch_allows_generate_src_directory(self) -> None:
        gen_id = "gen_20260508_001"
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "generate",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [f"{_FIX_PIPE_REF}/generate/{gen_id}/src/"],
        }
        out = _allowed_output_paths_for_launch(
            request_payload=req,
            write_roots=[f"{_FIX_PIPE_REF}/generate/"],
        )
        # Generate-step launches receive a defensive auto-inject for the
        # MCP run_linter side-effect log under <gen_id>/src/.
        self.assertEqual(
            out,
            [
                f"{_FIX_PIPE_REF}/generate/{gen_id}/src/",
                f"{_FIX_PIPE_REF}/generate/{gen_id}/src/mcp_command_log.jsonl",
            ],
        )

    def test_allowed_output_paths_for_launch_rejects_generate_srcmal_directory(self) -> None:
        gen_id = "gen_20260508_001"
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "generate",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [f"{_FIX_PIPE_REF}/generate/{gen_id}/srcmal/"],
        }
        with self.assertRaisesRegex(ValueError, "outside phase contract outputs"):
            _allowed_output_paths_for_launch(
                request_payload=req,
                write_roots=[f"{_FIX_PIPE_REF}/generate/"],
            )

    def test_allowed_output_paths_for_launch_rejects_generateevil_prefix_bypass(self) -> None:
        """A directory whose name starts with 'generate' but is a sibling (e.g. generateevil/) must be rejected."""
        gen_id = "gen_20260508_001"
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "generate",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [f"{_FIX_PIPE_REF}/generateevil/{gen_id}/src/"],
        }
        with self.assertRaisesRegex(ValueError, "outside phase contract outputs"):
            _allowed_output_paths_for_launch(
                request_payload=req,
                write_roots=[f"{_FIX_PIPE_REF}/generateevil/"],
            )

    def test_allowed_output_paths_for_launch_rejects_generate_root_directory(self) -> None:
        req = {
            "agent_role": "step",
            "node_key": "problem/shallow_water2d@0.3.0",
            "step": "generate",
            "plan_ref": _FIX_PLAN_REF,
            "pipeline_ref": _FIX_PIPE_REF,
            "allowed_output_paths": [f"{_FIX_PIPE_REF}/generate/"],
        }
        with self.assertRaisesRegex(ValueError, "outside phase contract outputs"):
            _allowed_output_paths_for_launch(
                request_payload=req,
                write_roots=[f"{_FIX_PIPE_REF}/generate/"],
            )

    def test_write_manifest_preserves_directory_entry_trailing_slash(self) -> None:
        """_write_allowed_output_manifest must persist directory entries with trailing slash intact."""
        from tools.orchestration_runtime import (
            _load_allowed_output_manifest,
            _write_allowed_output_manifest,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            src_dir = f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src/"
            _write_allowed_output_manifest(
                repo_root,
                orchestration_id="rg_persist",
                agent_run_id="gen_persist",
                allowed_output_paths=[src_dir],
            )
            manifest = _load_allowed_output_manifest(
                repo_root,
                orchestration_id="rg_persist",
                agent_run_id="gen_persist",
            )
            self.assertIn(src_dir.rstrip("/") + "/", manifest["allowed_output_paths"])

    def test_write_manifest_then_validate_allows_file_under_directory_entry(self) -> None:
        """Full path: write manifest with directory entry, then validate a file written under it."""
        from tools.orchestration_runtime import (
            _validate_paths_against_allowed_output_manifest,
            _write_allowed_output_manifest,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            src_dir = f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src/"
            _write_allowed_output_manifest(
                repo_root,
                orchestration_id="rg_e2e",
                agent_run_id="gen_e2e",
                allowed_output_paths=[src_dir],
            )
            _validate_paths_against_allowed_output_manifest(
                repo_root,
                orchestration_id="rg_e2e",
                agent_run_id="gen_e2e",
                paths=[
                    f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src/main.f90",
                    f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src/mod/util.f90",
                ],
            )

    def test_validate_paths_against_allowed_output_manifest_allows_file_under_directory_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            src_dir = f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src/"
            manifest_path = (
                repo_root
                / "workspace/orchestrations/rg_dir_ok/output_manifests/gen_child_dir.json"
            )
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "orchestration_id": "rg_dir_ok",
                        "agent_run_id": "gen_child_dir",
                        "allowed_output_paths": [src_dir],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            # Files under the allowed directory must pass without raising
            _validate_paths_against_allowed_output_manifest(
                repo_root,
                orchestration_id="rg_dir_ok",
                agent_run_id="gen_child_dir",
                paths=[
                    f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src/main.f90",
                    f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src/module/util.f90",
                ],
            )

    def test_validate_paths_against_allowed_output_manifest_rejects_file_outside_directory_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            src_dir = f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src/"
            manifest_path = (
                repo_root
                / "workspace/orchestrations/rg_dir_rej/output_manifests/gen_child_rej.json"
            )
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "orchestration_id": "rg_dir_rej",
                        "agent_run_id": "gen_child_rej",
                        "allowed_output_paths": [src_dir],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                _validate_paths_against_allowed_output_manifest(
                    repo_root,
                    orchestration_id="rg_dir_rej",
                    agent_run_id="gen_child_rej",
                    paths=[f"{_FIX_PIPE_REF}/generate/gen_20260508_001/other/main.f90"],
                )

    def test_validate_paths_against_allowed_output_manifest_rejects_empty_normalized_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            manifest_path = (
                repo_root
                / "workspace/orchestrations/rg_invalid/output_manifests/build_child_rg1.json"
            )
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "orchestration_id": "rg_invalid",
                        "agent_run_id": "build_child_rg1",
                        "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/build_meta.json"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                _validate_paths_against_allowed_output_manifest(
                    repo_root,
                    orchestration_id="rg_invalid",
                    agent_run_id="build_child_rg1",
                    paths=["/"],
                )

    def test_run_gate_writes_artifact_and_cli_stdout_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            out = run_gate(
                repo_root,
                orchestration_id="rg1",
                gate_name="check_artifact_syntax",
                agent_run_id="build_child_rg1",
                args_json={"paths": ["workspace/probe.json"]},
                capability_token=token,
            )
            self.assertEqual(list(out.keys()), ["violations", "gate_result_ref"])
            self.assertEqual(out["violations"], [])
            gate_ref = out["gate_result_ref"]
            self.assertEqual(
                gate_ref,
                "workspace/orchestrations/rg1/gates/build_child_rg1/check_artifact_syntax.json",
            )
            gate_path = repo_root / gate_ref
            self.assertTrue(gate_path.exists())
            gate_doc = json.loads(gate_path.read_text(encoding="utf-8"))
            self.assertEqual(gate_doc.get("gate"), "check_artifact_syntax")
            self.assertEqual(gate_doc.get("status"), "pass")
            self.assertEqual(gate_doc.get("violations"), [])
            self.assertNotIn("arg_validation_error", gate_doc)

            buf = io.StringIO()
            rc = None
            with redirect_stdout(buf):
                rc = main(
                    [
                        "run-gate",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "rg1",
                        "--gate",
                        "check_artifact_syntax",
                        "--agent-run-id",
                        "build_child_rg1",
                        "--args-json",
                        json.dumps({"paths": ["workspace/probe.json"]}),
                        "--capability-token",
                        token,
                    ]
                )
            self.assertEqual(rc, 0)
            cli_out = json.loads(buf.getvalue())
            self.assertEqual(set(cli_out.keys()), {"violations", "gate_result_ref"})

    def test_run_gate_check_artifact_syntax_rejects_legacy_path_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            out = run_gate(
                repo_root,
                orchestration_id="rg1",
                gate_name="check_artifact_syntax",
                agent_run_id="build_child_rg1",
                args_json={"path": "workspace/probe.json", "expect_top": "object"},
                capability_token=token,
            )
            self.assertEqual(len(out.get("violations", [])), 1)
            self.assertIn("args-json validation failed", out["violations"][0])
            self.assertIn("single 'path' is unsupported", out["violations"][0])
            gate_doc = json.loads(
                (
                    repo_root
                    / "workspace/orchestrations/rg1/gates/build_child_rg1/check_artifact_syntax.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(gate_doc.get("status"), "fail")
            self.assertEqual(gate_doc.get("exit_code"), 2)
            self.assertIn("single 'path' is unsupported", gate_doc.get("arg_validation_error", ""))
            self.assertEqual(gate_doc.get("violations"), out.get("violations"))

    def test_run_gate_check_artifact_syntax_rejects_empty_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            out = run_gate(
                repo_root,
                orchestration_id="rg1",
                gate_name="check_artifact_syntax",
                agent_run_id="build_child_rg1",
                args_json={"paths": [], "expect_top": "object"},
                capability_token=token,
            )
            self.assertEqual(len(out.get("violations", [])), 1)
            self.assertIn("paths must be a non-empty list", out["violations"][0])
            gate_doc = json.loads(
                (
                    repo_root
                    / "workspace/orchestrations/rg1/gates/build_child_rg1/check_artifact_syntax.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(gate_doc.get("status"), "fail")
            self.assertEqual(gate_doc.get("exit_code"), 2)
            self.assertIn("paths must be a non-empty list", gate_doc.get("arg_validation_error", ""))

    def test_run_gate_check_artifact_syntax_rejects_non_string_path_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            out = run_gate(
                repo_root,
                orchestration_id="rg1",
                gate_name="check_artifact_syntax",
                agent_run_id="build_child_rg1",
                args_json={"paths": ["workspace/probe.json", 1], "expect_top": "object"},
                capability_token=token,
            )
            self.assertEqual(len(out.get("violations", [])), 1)
            self.assertIn("paths[1] must be non-empty string", out["violations"][0])
            gate_doc = json.loads(
                (
                    repo_root
                    / "workspace/orchestrations/rg1/gates/build_child_rg1/check_artifact_syntax.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(gate_doc.get("status"), "fail")
            self.assertEqual(gate_doc.get("exit_code"), 2)
            self.assertIn("paths[1] must be non-empty string", gate_doc.get("arg_validation_error", ""))

    def test_run_gate_orchestration_read_uses_inline_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "docs").mkdir(parents=True, exist_ok=True)
            (repo_root / "docs" / "inline_gate.txt").write_text("inline\n", encoding="utf-8")
            token = self._setup_run_gate_fixture(repo_root)
            out = run_gate(
                repo_root,
                orchestration_id="rg1",
                gate_name="orchestration_read",
                agent_run_id="build_child_rg1",
                args_json={"read_path": "docs/inline_gate.txt"},
                capability_token=token,
            )
            self.assertEqual(out.get("violations"), [])
            gate_ref = out.get("gate_result_ref")
            self.assertEqual(
                gate_ref,
                "workspace/orchestrations/rg1/gates/build_child_rg1/orchestration_read.json",
            )
            self.assertEqual(out.get("result", {}).get("content"), "inline\n")
            gate_doc = json.loads((repo_root / str(gate_ref)).read_text(encoding="utf-8"))
            self.assertEqual(gate_doc.get("status"), "pass")
            self.assertEqual(gate_doc.get("result", {}).get("content"), "inline\n")

    def test_run_gate_rejects_apply_patch_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            with self.assertRaisesRegex(ValueError, "unsupported gate name"):
                run_gate(
                    repo_root,
                    orchestration_id="rg1",
                    gate_name="apply_patch_writes",
                    agent_run_id="build_child_rg1",
                    args_json={
                        "actor_role": "step",
                        "changed_paths": [f"{_FIX_PIPE_REF}/build/new_artifact.json"],
                    },
                    capability_token=token,
                )

    def test_guarded_apply_patch_calls_git_apply_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            target_path = f"{_FIX_PIPE_REF}/build/build_001/build_meta.json"
            patch_text = "\n".join(
                [
                    f"diff --git a/{target_path} b/{target_path}",
                    f"--- a/{target_path}",
                    f"+++ b/{target_path}",
                    "@@ -0,0 +1 @@",
                    "+{\"ok\": true}",
                    "",
                ]
            )
            numstat_output = f"1\t0\t{target_path}\0".encode()
            with patch("tools.orchestration_runtime.subprocess.run") as run_mock:
                run_mock.side_effect = [
                    _FakeCompletedProcess(returncode=0, stdout=numstat_output, stderr=b""),
                    _FakeCompletedProcess(returncode=0, stdout="", stderr=""),
                ]
                out = guarded_apply_patch(
                    repo_root,
                    orchestration_id="rg1",
                    actor_role="step",
                    agent_run_id="build_child_rg1",
                    changed_paths=[target_path],
                    patch_text=patch_text,
                    capability_token=token,
                )
            self.assertTrue(out.get("applied"))
            self.assertEqual(
                out.get("gate_result_ref"),
                "workspace/orchestrations/rg1/gates/build_child_rg1/apply_patch_writes.json",
            )
            self.assertEqual(run_mock.call_count, 2)

    def test_guarded_apply_patch_rejects_mcp_command_log_mutation(self) -> None:
        """Step/substep agents must not patch integrity-protected MCP audit logs.

        The log path is auto-injected into allowed_output_paths so MCP-side
        writes pass record-agent-run, but a forged guarded-apply-patch must
        be rejected before manifest validation — otherwise a child could
        fabricate `tool_name=run_linter, ok=true` records and bypass
        validate_pipeline_semantics.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            log_path = f"{_FIX_PIPE_REF}/build/build_001/mcp_command_log.jsonl"
            patch_text = "\n".join(
                [
                    f"diff --git a/{log_path} b/{log_path}",
                    f"--- a/{log_path}",
                    f"+++ b/{log_path}",
                    "@@ -0,0 +1 @@",
                    '+{"command_id": "forged", "tool_name": "run_linter", "ok": true}',
                    "",
                ]
            )
            with (
                patch(
                    "tools.orchestration_runtime._numstat_targets", return_value=[log_path]
                ),
                patch("tools.orchestration_runtime.subprocess.run") as run_mock,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "MCP-owned audit logs"
                ):
                    guarded_apply_patch(
                        repo_root,
                        orchestration_id="rg1",
                        actor_role="step",
                        agent_run_id="build_child_rg1",
                        changed_paths=[log_path],
                        patch_text=patch_text,
                        capability_token=token,
                    )
            # git apply must not be invoked.
            self.assertEqual(run_mock.call_count, 0)

    def test_guarded_apply_patch_rejects_file_under_directory_when_not_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            target = f"{_FIX_PIPE_REF}/build/subdir/new_artifact.json"
            patch_text = "\n".join(
                [
                    f"diff --git a/{target} b/{target}",
                    f"--- a/{target}",
                    f"+++ b/{target}",
                    "@@ -0,0 +1 @@",
                    "+{\"ok\": true}",
                    "",
                ]
            )
            with (
                patch(
                    "tools.orchestration_runtime._numstat_targets", return_value=[target]
                ),
                patch("tools.orchestration_runtime.subprocess.run") as run_mock,
            ):
                with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                    guarded_apply_patch(
                        repo_root,
                        orchestration_id="rg1",
                        actor_role="step",
                        agent_run_id="build_child_rg1",
                        changed_paths=[target],
                        patch_text=patch_text,
                        capability_token=token,
                    )
            self.assertEqual(run_mock.call_count, 0)

    def test_guarded_apply_patch_rejects_patch_outside_declared_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            outside_target = "workspace/orchestrations/rg1/orchestration_meta.json"
            patch_text = "\n".join(
                [
                    f"diff --git a/{outside_target} b/{outside_target}",
                    f"--- a/{outside_target}",
                    f"+++ b/{outside_target}",
                    "@@ -1 +1 @@",
                    "-{}",
                    "+{\"status\": \"running\"}",
                    "",
                ]
            )
            # _numstat_targets returns the outside target for both strip levels;
            # _select_patch_strip cannot find coverage in changed_paths → raises.
            with (
                patch(
                    "tools.orchestration_runtime._numstat_targets", return_value=[outside_target]
                ),
                patch("tools.orchestration_runtime.subprocess.run") as run_mock,
            ):
                with self.assertRaisesRegex(RuntimeError, "cannot determine patch strip level"):
                    guarded_apply_patch(
                        repo_root,
                        orchestration_id="rg1",
                        actor_role="step",
                        agent_run_id="build_child_rg1",
                        changed_paths=[f"{_FIX_PIPE_REF}/build/"],
                        patch_text=patch_text,
                        capability_token=token,
                    )
            self.assertEqual(run_mock.call_count, 0)

    def test_guarded_apply_patch_rejects_patch_outside_output_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            target = "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/case.resolved.yaml"
            patch_text = "\n".join(
                [
                    f"diff --git a/{target} b/{target}",
                    f"--- a/{target}",
                    f"+++ b/{target}",
                    "@@ -0,0 +1 @@",
                    "+cases: []",
                    "",
                ]
            )
            with (
                patch(
                    "tools.orchestration_runtime._numstat_targets", return_value=[target]
                ),
                patch("tools.orchestration_runtime.subprocess.run") as run_mock,
            ):
                with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                    guarded_apply_patch(
                        repo_root,
                        orchestration_id="rg1",
                        actor_role="step",
                        agent_run_id="build_child_rg1",
                        changed_paths=["workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/"],
                        patch_text=patch_text,
                        capability_token=token,
                    )
            self.assertEqual(run_mock.call_count, 0)

    def test_guarded_apply_patch_validates_manifest_with_declared_changed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            target_path = f"{_FIX_PIPE_REF}/build/new_artifact.json"
            patch_text = "\n".join(
                [
                    f"diff --git a/{target_path} b/{target_path}",
                    f"--- a/{target_path}",
                    f"+++ b/{target_path}",
                    "@@ -0,0 +1 @@",
                    "+{\"ok\": true}",
                    "",
                ]
            )
            numstat_output = f"1\t0\t{target_path}\0".encode()
            with (
                patch(
                    "tools.orchestration_runtime._validate_paths_against_allowed_output_manifest"
                ) as manifest_mock,
                patch("tools.orchestration_runtime.gate_apply_patch_writes") as gate_mock,
                patch("tools.orchestration_runtime._write_apply_patch_gate_evidence") as evidence_mock,
                patch("tools.orchestration_runtime.subprocess.run") as run_mock,
            ):
                gate_mock.return_value = {"allowed": True, "checked_paths": [f"{_FIX_PIPE_REF}/build/"]}
                evidence_mock.return_value = "workspace/orchestrations/rg1/gates/build_child_rg1/apply_patch_writes.json"
                run_mock.side_effect = [
                    _FakeCompletedProcess(returncode=0, stdout=numstat_output, stderr=b""),
                    _FakeCompletedProcess(returncode=0, stdout="", stderr=""),
                ]
                guarded_apply_patch(
                    repo_root,
                    orchestration_id="rg1",
                    actor_role="step",
                    agent_run_id="build_child_rg1",
                    changed_paths=[f"{_FIX_PIPE_REF}/build/"],
                    patch_text=patch_text,
                    capability_token="token",
                )
            self.assertEqual(manifest_mock.call_count, 1)
            self.assertEqual(
                manifest_mock.call_args.kwargs.get("paths"),
                [f"{_FIX_PIPE_REF}/build"],
            )

    def test_guarded_apply_patch_no_prefix_patch_writes_declared_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            subprocess.run(["git", "init", "--quiet"], cwd=str(repo_root), check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"], cwd=str(repo_root), check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=str(repo_root), check=True
            )
            target = "workspace/foo/bar.json"
            (repo_root / "workspace" / "foo").mkdir(parents=True)
            patch_text = "\n".join(
                [
                    f"diff --git {target} {target}",
                    "new file mode 100644",
                    "--- /dev/null",
                    f"+++ {target}",
                    "@@ -0,0 +1 @@",
                    '+{"created": true}',
                    "",
                ]
            )
            with (
                patch("tools.orchestration_runtime.gate_apply_patch_writes") as gate_mock,
                patch(
                    "tools.orchestration_runtime._write_apply_patch_gate_evidence"
                ) as evidence_mock,
            ):
                gate_mock.return_value = {"allowed": True}
                evidence_mock.return_value = (
                    "workspace/orchestrations/rg1/gates/ag1/apply_patch_writes.json"
                )
                out = guarded_apply_patch(
                    repo_root,
                    orchestration_id="rg1",
                    actor_role="orchestration",
                    agent_run_id="ag1",
                    changed_paths=[target],
                    patch_text=patch_text,
                    capability_token="tok",
                )
            self.assertTrue(out.get("applied"))
            self.assertTrue((repo_root / target).exists(), "file must be at workspace/foo/bar.json")
            self.assertFalse(
                (repo_root / "foo" / "bar.json").exists(),
                "git apply must not strip 'workspace/' prefix",
            )

    def test_guarded_apply_patch_bare_path_starting_with_a_slash(self) -> None:
        # Regression: a bare-path patch targeting a file under a directory named 'a/'
        # must not be confused with the git a/b prefix format.
        # 'diff --git a/results.json a/results.json' has the same prefix on both sides,
        # so _detect_patch_prefix_strip must infer strip=0 (bare), not strip=1.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            subprocess.run(["git", "init", "--quiet"], cwd=str(repo_root), check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"], cwd=str(repo_root), check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=str(repo_root), check=True
            )
            target = "a/results.json"
            (repo_root / "a").mkdir(parents=True)
            patch_text = "\n".join(
                [
                    f"diff --git {target} {target}",
                    "new file mode 100644",
                    "--- /dev/null",
                    f"+++ {target}",
                    "@@ -0,0 +1 @@",
                    '+{"ok": true}',
                    "",
                ]
            )
            with (
                patch("tools.orchestration_runtime.gate_apply_patch_writes") as gate_mock,
                patch(
                    "tools.orchestration_runtime._write_apply_patch_gate_evidence"
                ) as evidence_mock,
            ):
                gate_mock.return_value = {"allowed": True}
                evidence_mock.return_value = (
                    "workspace/orchestrations/rg1/gates/ag1/apply_patch_writes.json"
                )
                out = guarded_apply_patch(
                    repo_root,
                    orchestration_id="rg1",
                    actor_role="orchestration",
                    agent_run_id="ag1",
                    changed_paths=[target],
                    patch_text=patch_text,
                    capability_token="tok",
                )
            self.assertTrue(out.get("applied"))
            self.assertTrue((repo_root / target).exists(), "file must be at a/results.json")
            self.assertFalse(
                (repo_root / "results.json").exists(),
                "git apply must not strip 'a/' as if it were a git prefix",
            )

    def test_guarded_apply_patch_special_char_filename_no_false_mismatch(self) -> None:
        # Regression: git apply --numstat (without -z) quotes filenames containing
        # double-quotes, backslashes, tabs, etc., e.g.
        #   '1\t0\t"dir/file \"quoted\".json"'
        # The old parser returned the quoted string verbatim, causing a mismatch
        # against _extract_patch_target_paths which returned the raw path.
        # With -z, git outputs raw NUL-terminated bytes — no quoting needed.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            subprocess.run(["git", "init", "--quiet"], cwd=str(repo_root), check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"], cwd=str(repo_root), check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=str(repo_root), check=True
            )
            # Filename with double-quote triggers git's C-escaping in non-z numstat output
            target = 'workspace/results/file "quoted".json'
            (repo_root / "workspace" / "results").mkdir(parents=True)
            (repo_root / target).write_text("old\n", encoding="utf-8")
            patch_text = "\n".join(
                [
                    f'diff --git a/{target} b/{target}',
                    f'--- a/{target}',
                    f'+++ b/{target}',
                    "@@ -1 +1 @@",
                    "-old",
                    "+new",
                    "",
                ]
            )
            with (
                patch("tools.orchestration_runtime.gate_apply_patch_writes") as gate_mock,
                patch(
                    "tools.orchestration_runtime._write_apply_patch_gate_evidence"
                ) as evidence_mock,
            ):
                gate_mock.return_value = {"allowed": True}
                evidence_mock.return_value = (
                    "workspace/orchestrations/rg1/gates/ag1/apply_patch_writes.json"
                )
                out = guarded_apply_patch(
                    repo_root,
                    orchestration_id="rg1",
                    actor_role="orchestration",
                    agent_run_id="ag1",
                    changed_paths=[target],
                    patch_text=patch_text,
                    capability_token="tok",
                )
            self.assertTrue(out.get("applied"))
            self.assertEqual((repo_root / target).read_text(encoding="utf-8"), "new\n")

    def test_guarded_apply_patch_mode_only_patch_applies(self) -> None:
        # Regression: mode-only patches have no '+++ ' lines, so _extract_patch_target_paths
        # returns []. Old code raised ValueError("must include at least one '+++ <path>'").
        # New code uses numstat_targets as the authoritative path set, allowing mode-only patches.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            subprocess.run(["git", "init", "--quiet"], cwd=str(repo_root), check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"], cwd=str(repo_root), check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=str(repo_root), check=True
            )
            target = "workspace/foo/script.sh"
            (repo_root / "workspace" / "foo").mkdir(parents=True)
            (repo_root / target).write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
            subprocess.run(["git", "add", "-f", target], cwd=str(repo_root), check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo_root), check=True)
            patch_text = "\n".join(
                [
                    f"diff --git a/{target} b/{target}",
                    "old mode 100644",
                    "new mode 100755",
                    "",
                ]
            )
            with (
                patch("tools.orchestration_runtime.gate_apply_patch_writes") as gate_mock,
                patch(
                    "tools.orchestration_runtime._write_apply_patch_gate_evidence"
                ) as evidence_mock,
            ):
                gate_mock.return_value = {"allowed": True}
                evidence_mock.return_value = (
                    "workspace/orchestrations/rg1/gates/ag1/apply_patch_writes.json"
                )
                out = guarded_apply_patch(
                    repo_root,
                    orchestration_id="rg1",
                    actor_role="orchestration",
                    agent_run_id="ag1",
                    changed_paths=[target],
                    patch_text=patch_text,
                    capability_token="tok",
                )
            self.assertTrue(out.get("applied"))
            self.assertEqual(out.get("patch_targets"), [target])

    def test_guarded_apply_patch_rename_only_patch_applies(self) -> None:
        # Regression: pure rename patches have no '+++ ' content lines either.
        # Old code raised ValueError; new code uses numstat to resolve the destination.
        # Both the rename source (deleted) and destination (created) must be in changed_paths.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            subprocess.run(["git", "init", "--quiet"], cwd=str(repo_root), check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"], cwd=str(repo_root), check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=str(repo_root), check=True
            )
            old = "workspace/foo/old_name.json"
            new = "workspace/foo/new_name.json"
            (repo_root / "workspace" / "foo").mkdir(parents=True)
            (repo_root / old).write_text('{"v": 1}\n', encoding="utf-8")
            subprocess.run(["git", "add", "-f", old], cwd=str(repo_root), check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo_root), check=True)
            patch_text = "\n".join(
                [
                    f"diff --git a/{old} b/{new}",
                    "similarity index 100%",
                    f"rename from {old}",
                    f"rename to {new}",
                    "",
                ]
            )
            with (
                patch("tools.orchestration_runtime.gate_apply_patch_writes") as gate_mock,
                patch(
                    "tools.orchestration_runtime._write_apply_patch_gate_evidence"
                ) as evidence_mock,
            ):
                gate_mock.return_value = {"allowed": True}
                evidence_mock.return_value = (
                    "workspace/orchestrations/rg1/gates/ag1/apply_patch_writes.json"
                )
                out = guarded_apply_patch(
                    repo_root,
                    orchestration_id="rg1",
                    actor_role="orchestration",
                    agent_run_id="ag1",
                    # Both source (deleted) and destination (created) must be declared.
                    changed_paths=[old, new],
                    patch_text=patch_text,
                    capability_token="tok",
                )
            self.assertTrue(out.get("applied"))
            self.assertEqual(out.get("patch_targets"), [new])
            self.assertTrue((repo_root / new).exists())
            self.assertFalse((repo_root / old).exists())

    def test_guarded_apply_patch_rename_from_out_of_scope_rejected(self) -> None:
        # Security: rename from an out-of-scope path must be rejected even when the
        # destination is within changed_paths.  A rename deletes the source file,
        # which is a destructive side-effect that requires explicit authorization.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            subprocess.run(["git", "init", "--quiet"], cwd=str(repo_root), check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"], cwd=str(repo_root), check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=str(repo_root), check=True
            )
            src = "sensitive/outside.json"
            dst = "workspace/allowed/new.json"
            (repo_root / "sensitive").mkdir(parents=True)
            (repo_root / "workspace" / "allowed").mkdir(parents=True)
            (repo_root / src).write_text('{"secret": true}\n', encoding="utf-8")
            subprocess.run(["git", "add", "-f", src], cwd=str(repo_root), check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo_root), check=True)
            patch_text = "\n".join(
                [
                    f"diff --git a/{src} b/{dst}",
                    "similarity index 100%",
                    f"rename from {src}",
                    f"rename to {dst}",
                    "",
                ]
            )
            with (
                patch("tools.orchestration_runtime.gate_apply_patch_writes") as gate_mock,
                patch("tools.orchestration_runtime._write_apply_patch_gate_evidence"),
            ):
                gate_mock.return_value = {"allowed": True}
                with self.assertRaisesRegex(
                    RuntimeError, "rename source paths are not covered by changed_paths"
                ):
                    guarded_apply_patch(
                        repo_root,
                        orchestration_id="rg1",
                        actor_role="orchestration",
                        agent_run_id="ag1",
                        changed_paths=[dst],  # only destination — source is out of scope
                        patch_text=patch_text,
                        capability_token="tok",
                    )
            # Ensure the source file was NOT deleted
            self.assertTrue(
                (repo_root / src).exists(),
                "out-of-scope source must not be deleted",
            )

    def test_guarded_apply_patch_mixed_prefix_rejected(self) -> None:
        # A patch mixing git-prefix hunks (a/x.json b/x.json) and bare-path hunks
        # (workspace/y.json) cannot be consistently applied with a single -p<strip>.
        # strip=1 produces ["x.json", "y.json"] (strips 'workspace/' from bare hunk).
        # strip=0 produces ["b/x.json", "workspace/y.json"] (keeps b/ from git hunk).
        # Neither result is fully covered by changed_paths → _select_patch_strip raises.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            patch_text = "\n".join(
                [
                    "diff --git a/x.json b/x.json",
                    "--- a/x.json",
                    "+++ b/x.json",
                    "@@ -0,0 +1 @@",
                    '+{"x": 1}',
                    "diff --git workspace/y.json workspace/y.json",
                    "--- /dev/null",
                    "+++ workspace/y.json",
                    "@@ -0,0 +1 @@",
                    '+{"y": 1}',
                    "",
                ]
            )
            # Mock _numstat_targets to return the inconsistent results for each strip level
            # without actually running git (the repo_root is not a git repo).
            def fake_numstat(repo_root: object, patch_text: object, strip: int) -> list[str]:
                return ["x.json", "y.json"] if strip == 1 else ["b/x.json", "workspace/y.json"]

            with (
                patch("tools.orchestration_runtime._numstat_targets", side_effect=fake_numstat),
                patch("tools.orchestration_runtime.subprocess.run") as run_mock,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "cannot determine patch strip level"
                ):
                    guarded_apply_patch(
                        repo_root,
                        orchestration_id="rg1",
                        actor_role="orchestration",
                        agent_run_id="ag1",
                        changed_paths=["x.json", "workspace/y.json"],
                        patch_text=patch_text,
                        capability_token="tok",
                    )
            self.assertEqual(run_mock.call_count, 0, "git apply must not be called")

    def test_guarded_apply_patch_numstat_mismatch_raises(self) -> None:
        # Verify that when _numstat_targets returns a path that is covered by changed_paths
        # but differs from _extract_patch_target_paths, the cross-validation catches it.
        # changed_paths uses a directory prefix so the DIFFERENT filename is still "covered"
        # by _select_patch_strip, letting the cross-validation check run.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            target = "workspace/foo/bar.json"
            patch_text = "\n".join(
                [
                    f"diff --git a/{target} b/{target}",
                    f"--- a/{target}",
                    f"+++ b/{target}",
                    "@@ -0,0 +1 @@",
                    '+{"ok": true}',
                    "",
                ]
            )
            apply_spy = _FakeCompletedProcess(returncode=0, stdout="", stderr="")
            with (
                patch(
                    "tools.orchestration_runtime._numstat_targets",
                    return_value=["workspace/foo/DIFFERENT.json"],
                ),
                patch("tools.orchestration_runtime.subprocess.run", return_value=apply_spy) as run_mock,
            ):
                with self.assertRaisesRegex(RuntimeError, "headers declare paths absent from git-apply numstat"):
                    guarded_apply_patch(
                        repo_root,
                        orchestration_id="rg1",
                        actor_role="orchestration",
                        agent_run_id="ag1",
                        # Use a directory prefix so DIFFERENT.json is covered by _select_patch_strip,
                        # allowing the cross-validation (patch_targets != numstat_targets) to run.
                        changed_paths=["workspace/foo/"],
                        patch_text=patch_text,
                        capability_token="tok",
                    )
            self.assertEqual(run_mock.call_count, 0, "git apply must not be called after mismatch")

    def test_main_guarded_apply_patch_returns_nonzero_on_gate_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # git init is required so _numstat_targets can run git apply --numstat --check
            subprocess.run(["git", "init", "--quiet"], cwd=str(repo_root), check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"], cwd=str(repo_root), check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=str(repo_root), check=True
            )
            token = self._setup_run_gate_fixture(repo_root)
            outside_target = "workspace/orchestrations/rg1/orchestration_meta.json"
            # Pre-create the file so git apply --check can evaluate the modification patch
            (repo_root / "workspace" / "orchestrations" / "rg1").mkdir(parents=True, exist_ok=True)
            (repo_root / outside_target).write_text("{}\n", encoding="utf-8")
            patch_text = "\n".join(
                [
                    f"diff --git a/{outside_target} b/{outside_target}",
                    f"--- a/{outside_target}",
                    f"+++ b/{outside_target}",
                    "@@ -1 +1 @@",
                    "-{}",
                    "+{\"status\": \"running\"}",
                    "",
                ]
            )
            err = io.StringIO()
            with redirect_stderr(err):
                rc = main(
                    [
                        "guarded-apply-patch",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "rg1",
                        "--actor-role",
                        "step",
                        "--agent-run-id",
                        "build_child_rg1",
                        "--paths-json",
                        json.dumps([f"{_FIX_PIPE_REF}/build/new_artifact.json"]),
                        "--patch-text",
                        patch_text,
                        "--capability-token",
                        token,
                    ]
                )
            self.assertEqual(rc, 1)
            self.assertIn("cannot determine patch strip level", err.getvalue())

    def test_run_gate_rejects_gate_not_allowed_by_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            pol = repo_root / "workspace/orchestrations/rg1/access_policies/build_child_rg1.json"
            body = json.loads(pol.read_text(encoding="utf-8"))
            body["allowed_gate_services"] = ["validate_workspace_root"]
            pol.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not permitted by access policy"):
                run_gate(
                    repo_root,
                    orchestration_id="rg1",
                    gate_name="check_artifact_syntax",
                    agent_run_id="build_child_rg1",
                    args_json={"paths": ["workspace/probe.json"]},
                    capability_token=token,
                )

    def test_run_gate_validate_pipeline_semantics_requires_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            with self.assertRaisesRegex(ValueError, "pre_command_execute hook"):
                run_gate(
                    repo_root,
                    orchestration_id="rg1",
                    gate_name="validate_pipeline_semantics",
                    agent_run_id="build_child_rg1",
                    args_json={"pipeline-root": _FIX_PIPE_REF},
                    capability_token=token,
                )

    def test_run_gate_validate_pipeline_semantics_rejects_wrong_stage_for_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            with self.assertRaisesRegex(ValueError, "pre_command_execute hook"):
                run_gate(
                    repo_root,
                    orchestration_id="rg1",
                    gate_name="validate_pipeline_semantics",
                    agent_run_id="build_child_rg1",
                    args_json={"stage": "plan", "pipeline-root": _FIX_PIPE_REF},
                    capability_token=token,
                )

    def test_pre_phase_launch_blocks_build_when_generate_meta_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="pl1")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="pl1",
                payload={
                    "status": "pass",
                    "backend": "codex",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}, {"name": "codex_home_writable", "pass": True}, {"name": "sandbox_bwrap_available", "pass": True}, {"name": "sandbox_bwrap_userns", "pass": True}],
                },
            )
            pipe = repo_root / _FIX_PIPE_REF
            (pipe / "generate" / "g1").mkdir(parents=True, exist_ok=True)
            (pipe / "generate" / "g1" / "generate_meta.json").write_text(
                json.dumps({"verification_status": "fail"}),
                encoding="utf-8",
            )
            out = pre_phase_launch(
                repo_root,
                orchestration_id="pl1",
                node_key="problem/shallow_water2d@0.3.0",
                step="build",
                backend="codex",
                require_child_agent="step",
                launch_request={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "build",
                    "pipeline_ref": _FIX_PIPE_REF,
                    "plan_ref": _FIX_PLAN_REF,
                    "dependency_ref": _FIX_DEP_REF,
                },
            )
            self.assertEqual(out.get("status"), "fail_closed")
            self.assertEqual(out.get("reason_code"), "downstream_artifact_not_ready")

    def test_run_gate_stdout_does_not_expose_input_file_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            secret = "SENSITIVE_PAYLOAD_DO_NOT_EXPOSE"
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace" / "broken.json").write_text(secret + "\n", encoding="utf-8")
            token = self._setup_run_gate_fixture(repo_root)
            out_buf = io.StringIO()
            rc = None
            with redirect_stdout(out_buf):
                rc = main(
                    [
                        "run-gate",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "rg1",
                        "--gate",
                        "check_artifact_syntax",
                        "--agent-run-id",
                        "build_child_rg1",
                        "--args-json",
                        json.dumps({"paths": ["workspace/broken.json"]}),
                        "--capability-token",
                        token,
                    ]
                )
            self.assertEqual(rc, 0)
            stdout_text = out_buf.getvalue()
            self.assertNotIn(secret, stdout_text)
            cli_out = json.loads(stdout_text)
            self.assertIn("violations", cli_out)
            self.assertGreaterEqual(len(cli_out["violations"]), 1)

    def test_record_launch_claude_creates_active_child_file_and_rejects_parallel_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "backend": "claude",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [
                        {"name": "multi_agent_enabled", "pass": True},
                        {"name": "sandbox_bwrap_available", "pass": True},
                        {"name": "sandbox_bwrap_userns", "pass": True},
                    ],
                },
            )
            _fixture_generate_downstream_ready(repo_root)
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
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"],
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
            active_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_001"
                / "active_child_agent_run_id.txt"
            )
            self.assertTrue(active_path.is_file())
            self.assertEqual(active_path.read_text(encoding="utf-8").strip(), "step_run_build_001")
            with self.assertRaisesRegex(RuntimeError, "sequential violation"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="step_run_execute_001",
                    request_payload={
                        "agent_run_id": "step_run_execute_001",
                        "agent_role": "step",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "execute",
                        "orchestration_id": "orch_001",
                        "parent_agent_run_id": "orch_run_001",
                        "plan_ref": _FIX_PLAN_REF,
                        "pipeline_ref": _FIX_PIPE_REF,
                        "dependency_ref": _FIX_DEP_REF,
                        "skill_name": "workflow-execute",
                        "skill_ref": "skills/workflow-execute/SKILL.md",
                        "skill_must_read_refs": "",
                        "launch_prompt_full": _step_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "execute",
                            "step_run_execute_001",
                        ),
                    },
                    response_payload={
                        "agent_run_id": "step_run_execute_001",
                        **_spawn_response_payload("sess_step_execute_001"),
                    },
                )
            meta_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_001"
                / "orchestration_meta.json"
            )
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta.get("status"), "fail_closed")
            self.assertEqual(meta.get("reason_code"), "parallel_nodes_not_explicitly_allowed")

    def test_record_agent_run_claude_terminal_status_clears_active_child_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_001",
                payload={
                    "status": "pass",
                    "backend": "claude",
                    "sandbox_runtime": "bwrap",
                    "sandbox_enforced": True,
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True},
                    "checks": [
                        {"name": "multi_agent_enabled", "pass": True},
                        {"name": "sandbox_bwrap_available", "pass": True},
                        {"name": "sandbox_bwrap_userns", "pass": True},
                    ],
                },
            )
            _fixture_generate_downstream_ready(repo_root)
            launch_refs = record_launch(
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
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": "",
                    "allowed_output_paths": [f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"],
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
            active_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_001"
                / "active_child_agent_run_id.txt"
            )
            self.assertTrue(active_path.exists())
            out_ref = f"{_FIX_PIPE_REF}/build/build_001/bin/simulate"
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_001",
                agent_run_id="step_run_build_001",
                actor_role="step",
                changed_paths=[out_ref],
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
                    "agent_backend": "claude",
                    "agent_model": "claude-opus",
                    "context_id": "ctx_step_build_001",
                    "agent_session_id": "sess_step_build_001",
                    "launch_request_ref": launch_refs["launch_request_ref"],
                    "launch_response_ref": launch_refs["launch_response_ref"],
                    "launch_prompt_ref": launch_refs["launch_prompt_ref"],
                    "launch_reply_ref": launch_refs["launch_reply_ref"],
                    "output_refs": [out_ref],
                },
            )
            self.assertFalse(active_path.exists())


class DirectWritePathExtensionPolicyTests(unittest.TestCase):
    """書き込み path の extension 別 policy helper の挙動確認。"""

    def test_cli_managed_extensions_are_json_and_txt(self) -> None:
        self.assertEqual(CLI_MANAGED_EXTENSIONS, frozenset({".json", ".txt"}))

    def test_is_direct_write_path_classifies_extensions(self) -> None:
        cli_paths = [
            "workspace/plans/p/plan.json",
            "workspace/orchestrations/orch_001/launches/x.reply.txt",
            "workspace/orchestrations/orch_001/agents/x/dialogs/agent.summary.TXT",
            "workspace/plans/p/derived_contract.JSON",
        ]
        direct_paths = [
            "workspace/plans/p/case.resolved.yaml",
            "workspace/plans/p/algorithm.summary.md",
            "workspace/pipelines/p/generate/g/src/main.f90",
            "workspace/pipelines/p/generate/g/src/lib.cpp",
            "workspace/pipelines/p/generate/g/include/header.hpp",
        ]
        for path in cli_paths:
            self.assertFalse(_is_direct_write_path(path), msg=path)
        for path in direct_paths:
            self.assertTrue(_is_direct_write_path(path), msg=path)

    def test_is_direct_write_path_rejects_empty(self) -> None:
        self.assertFalse(_is_direct_write_path(""))

    def test_allowed_file_tool_paths_auto_derive_excludes_cli_extensions(self) -> None:
        allowed_output_paths = [
            "workspace/plans/p/case.resolved.yaml",
            "workspace/plans/p/algorithm.resolved.yaml",
            "workspace/plans/p/algorithm.summary.md",
            "workspace/plans/p/derived_contract.json",
            "workspace/plans/p/plan_meta.json",
            "workspace/pipelines/p/generate/g/src/main.f90",
        ]
        derived = _allowed_file_tool_paths_for_launch(
            request_payload={},
            allowed_output_paths=allowed_output_paths,
        )
        self.assertEqual(
            derived,
            sorted(
                [
                    "workspace/plans/p/case.resolved.yaml",
                    "workspace/plans/p/algorithm.resolved.yaml",
                    "workspace/plans/p/algorithm.summary.md",
                    "workspace/pipelines/p/generate/g/src/main.f90",
                ]
            ),
        )

    def test_allowed_file_tool_paths_auto_derive_returns_empty_when_all_cli(self) -> None:
        derived = _allowed_file_tool_paths_for_launch(
            request_payload={},
            allowed_output_paths=[
                "workspace/plans/p/derived_contract.json",
                "workspace/plans/p/plan_meta.json",
            ],
        )
        self.assertEqual(derived, [])

    def test_allowed_file_tool_paths_explicit_subset_validation(self) -> None:
        allowed_output_paths = [
            "workspace/plans/p/case.resolved.yaml",
            "workspace/plans/p/derived_contract.json",
        ]
        explicit = _allowed_file_tool_paths_for_launch(
            request_payload={
                "allowed_file_tool_paths": [
                    "workspace/plans/p/case.resolved.yaml",
                ]
            },
            allowed_output_paths=allowed_output_paths,
        )
        self.assertEqual(explicit, ["workspace/plans/p/case.resolved.yaml"])

    def test_allowed_file_tool_paths_explicit_rejects_cli_managed_extensions(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not include CLI-managed extensions"):
            _allowed_file_tool_paths_for_launch(
                request_payload={
                    "allowed_file_tool_paths": [
                        "workspace/plans/p/derived_contract.json",
                    ]
                },
                allowed_output_paths=[
                    "workspace/plans/p/derived_contract.json",
                    "workspace/plans/p/case.resolved.yaml",
                ],
            )

    def test_allowed_file_tool_paths_explicit_rejects_path_outside_outputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be included in allowed_output_paths"):
            _allowed_file_tool_paths_for_launch(
                request_payload={
                    "allowed_file_tool_paths": [
                        "workspace/plans/p/extra.yaml",
                    ]
                },
                allowed_output_paths=[
                    "workspace/plans/p/case.resolved.yaml",
                ],
            )

    def test_allowed_file_tool_paths_explicit_empty_returns_empty(self) -> None:
        derived = _allowed_file_tool_paths_for_launch(
            request_payload={"allowed_file_tool_paths": []},
            allowed_output_paths=[
                "workspace/plans/p/case.resolved.yaml",
            ],
        )
        self.assertEqual(derived, [])

    def test_allowed_file_tool_paths_auto_derive_excludes_directory_entries(self) -> None:
        """Directory entries (trailing /) must not appear in auto-derived allowed_file_tool_paths.

        Regression: _normalize_rel_posix strips '/', making 'src/' become 'src' which
        passes _is_direct_write_path (no extension → True). This leaked directory tokens
        into the manifest, enabling a prefix-match bypass of extension policy at terminal.
        """
        src_dir = "workspace/pipelines/p/generate/gen_001/src/"
        derived = _allowed_file_tool_paths_for_launch(
            request_payload={},
            allowed_output_paths=[
                src_dir,
                "workspace/pipelines/p/lineage.json",  # CLI-managed: also excluded
            ],
        )
        self.assertNotIn("workspace/pipelines/p/generate/gen_001/src", derived)
        self.assertNotIn("workspace/pipelines/p/lineage.json", derived)
        self.assertEqual(derived, [])


class TerminalUnauthorizedWriteDirectWriteTests(unittest.TestCase):
    """`_validate_actual_write_paths` が direct write extension の path を許容する確認。"""

    def _setup_step_run_state(
        self,
        repo_root: Path,
        *,
        orchestration_id: str,
        agent_run_id: str,
        write_roots: list[str],
        allowed_output_paths: list[str],
        allowed_file_tool_paths: list[str],
    ) -> None:
        from tools.orchestration_runtime import (
            _capabilities_dir,
            _write_allowed_output_manifest,
            _write_run_write_baseline,
        )

        init_orchestration(repo_root=repo_root, orchestration_id=orchestration_id)
        cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{agent_run_id}.json"
        cap_path.parent.mkdir(parents=True, exist_ok=True)
        cap_path.write_text(
            json.dumps(
                {
                    "orchestration_id": orchestration_id,
                    "agent_run_id": agent_run_id,
                    "write_roots": write_roots,
                }
            ),
            encoding="utf-8",
        )
        _write_allowed_output_manifest(
            repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=agent_run_id,
            allowed_output_paths=allowed_output_paths,
            allowed_file_tool_paths=allowed_file_tool_paths,
        )
        _write_run_write_baseline(repo_root, orchestration_id, agent_run_id=agent_run_id)

    def test_step_terminal_accepts_direct_write_yaml_without_gate(self) -> None:
        from tools.orchestration_runtime import _validate_actual_write_paths

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_term_dw_001"
            run_id = "step_run_term_dw_001"
            yaml_rel = "workspace/plans/p/case.resolved.yaml"
            self._setup_step_run_state(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                write_roots=["workspace/plans/"],
                allowed_output_paths=[yaml_rel],
                allowed_file_tool_paths=[yaml_rel],
            )
            yaml_path = repo_root / yaml_rel
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            yaml_path.write_text("case: ok\n", encoding="utf-8")

            _validate_actual_write_paths(
                repo_root,
                orch,
                {
                    "agent_run_id": run_id,
                    "agent_role": "step",
                    "status": "pass",
                    "output_refs": [yaml_rel],
                },
            )

    def test_step_terminal_rejects_direct_write_yaml_when_not_in_manifest(self) -> None:
        from tools.orchestration_runtime import _validate_actual_write_paths

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_term_dw_002"
            run_id = "step_run_term_dw_002"
            yaml_rel = "workspace/plans/p/case.resolved.yaml"
            self._setup_step_run_state(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                write_roots=["workspace/plans/"],
                allowed_output_paths=[yaml_rel],
                allowed_file_tool_paths=[],
            )
            yaml_path = repo_root / yaml_rel
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            yaml_path.write_text("case: ok\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                _validate_actual_write_paths(
                    repo_root,
                    orch,
                    {
                        "agent_run_id": run_id,
                        "agent_role": "step",
                        "status": "pass",
                        "output_refs": [yaml_rel],
                    },
                )

    def test_step_terminal_uses_cumulative_gate_changed_paths_across_multiple_apply_patch_calls(self) -> None:
        from tools.orchestration_runtime import (
            _validate_actual_write_paths,
            _write_apply_patch_gate_evidence,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_term_dw_003"
            run_id = "step_run_term_dw_003"
            a_rel = "workspace/pipelines/p/build/build_001/A.json"
            b_rel = "workspace/pipelines/p/build/build_001/B.json"
            self._setup_step_run_state(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                write_roots=["workspace/pipelines/p/build/"],
                allowed_output_paths=[a_rel, b_rel],
                allowed_file_tool_paths=[],
            )
            a_path = repo_root / a_rel
            b_path = repo_root / b_rel
            a_path.parent.mkdir(parents=True, exist_ok=True)
            a_path.write_text('{"a": 1}\n', encoding="utf-8")
            b_path.write_text('{"b": 1}\n', encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                actor_role="step",
                changed_paths=[a_rel, b_rel],
                result_payload={"allowed": True, "checked_paths": [a_rel, b_rel]},
            )
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                actor_role="step",
                changed_paths=[a_rel],
                result_payload={"allowed": True, "checked_paths": [a_rel]},
            )

            _validate_actual_write_paths(
                repo_root,
                orch,
                {
                    "agent_run_id": run_id,
                    "agent_role": "step",
                    "status": "pass",
                    "output_refs": [a_rel, b_rel],
                },
            )

    def test_step_terminal_violation_includes_missing_from_gate_changed_paths(self) -> None:
        from tools.orchestration_runtime import _validate_actual_write_paths

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_term_dw_004"
            run_id = "step_run_term_dw_004"
            allowed_rel = "workspace/pipelines/p/build/build_001/A.json"
            unauthorized_rel = "workspace/pipelines/p/build/build_001/C.json"
            self._setup_step_run_state(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                write_roots=["workspace/pipelines/p/build/"],
                allowed_output_paths=[allowed_rel],
                allowed_file_tool_paths=[],
            )
            allowed_path = repo_root / allowed_rel
            unauthorized_path = repo_root / unauthorized_rel
            allowed_path.parent.mkdir(parents=True, exist_ok=True)
            allowed_path.write_text('{"a": 1}\n', encoding="utf-8")
            unauthorized_path.write_text('{"c": 1}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                _validate_actual_write_paths(
                    repo_root,
                    orch,
                    {
                        "agent_run_id": run_id,
                        "agent_role": "step",
                        "status": "pass",
                        "output_refs": [allowed_rel],
                    },
                )
            violation_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / orch
                / "violations"
                / f"{run_id}.unauthorized_write_violation.json"
            )
            violation = json.loads(violation_path.read_text(encoding="utf-8"))
            self.assertEqual(
                violation.get("missing_from_gate_changed_paths"),
                [allowed_rel, unauthorized_rel],
            )

    def test_step_terminal_falls_back_to_gate_file_when_cumulative_store_is_invalid_json(self) -> None:
        from tools.orchestration_runtime import (
            _gate_changed_paths_store_path,
            _validate_actual_write_paths,
            _write_apply_patch_gate_evidence,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_term_dw_005"
            run_id = "step_run_term_dw_005"
            out_rel = "workspace/pipelines/p/build/build_001/A.json"
            self._setup_step_run_state(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                write_roots=["workspace/pipelines/p/build/"],
                allowed_output_paths=[out_rel],
                allowed_file_tool_paths=[],
            )
            out_path = repo_root / out_rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text('{"a": 1}\n', encoding="utf-8")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                actor_role="step",
                changed_paths=[out_rel],
                result_payload={"allowed": True, "checked_paths": [out_rel]},
            )
            # Corrupt cumulative store to ensure loader falls back to gate file.
            _gate_changed_paths_store_path(
                repo_root,
                orch,
                agent_run_id=run_id,
            ).write_text("{invalid-json\n", encoding="utf-8")

            _validate_actual_write_paths(
                repo_root,
                orch,
                {
                    "agent_run_id": run_id,
                    "agent_role": "step",
                    "status": "pass",
                    "output_refs": [out_rel],
                },
            )

    def test_step_terminal_rejects_mod_file_without_gate_provenance(self) -> None:
        """Compiler byproducts (.mod) under directory allowlist are unauthorized without gate provenance.

        Agents must clean up build artefacts before record-agent-run to prevent unaudited binary injection.
        """
        from tools.orchestration_runtime import _validate_actual_write_paths

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_term_dw_007"
            run_id = "step_run_term_dw_007"
            src_dir = f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src/"
            self._setup_step_run_state(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                write_roots=[f"{_FIX_PIPE_REF}/generate/"],
                allowed_output_paths=[src_dir],
                allowed_file_tool_paths=[],
            )
            byproduct = repo_root / _FIX_PIPE_REF / "generate" / "gen_20260508_001" / "src" / "flux.mod"
            byproduct.parent.mkdir(parents=True, exist_ok=True)
            byproduct.write_text("MODULE flux\nEND MODULE\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                _validate_actual_write_paths(
                    repo_root,
                    orch,
                    {
                        "agent_run_id": run_id,
                        "agent_role": "step",
                        "status": "pass",
                        "output_refs": [],
                    },
                )

    def _setup_src_dir_state(
        self,
        repo_root: Path,
        *,
        orch: str,
        run_id: str,
        src_files: dict[str, str],
    ) -> None:
        src_dir = f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src/"
        self._setup_step_run_state(
            repo_root,
            orchestration_id=orch,
            agent_run_id=run_id,
            write_roots=[f"{_FIX_PIPE_REF}/generate/"],
            allowed_output_paths=[src_dir],
            allowed_file_tool_paths=[],
        )
        base = repo_root / _FIX_PIPE_REF / "generate" / "gen_20260508_001" / "src"
        base.mkdir(parents=True, exist_ok=True)
        for name, content in src_files.items():
            (base / name).write_text(content, encoding="utf-8")

    def _run_validate(self, repo_root: Path, orch: str, run_id: str) -> None:
        from tools.orchestration_runtime import _validate_actual_write_paths
        _validate_actual_write_paths(
            repo_root,
            orch,
            {"agent_run_id": run_id, "agent_role": "step", "status": "pass", "output_refs": []},
        )

    def test_step_terminal_rejects_script_under_directory_entry(self) -> None:
        """Shell scripts under directory allowlist are unauthorized (not in allowlist)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_src_dir_state(repo_root, orch="orch_dw_bl_001", run_id="run_dw_bl_001",
                                      src_files={"exploit.sh": "#!/bin/sh\nrm -rf /\n"})
            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                self._run_validate(repo_root, "orch_dw_bl_001", "run_dw_bl_001")

    def test_step_terminal_rejects_shared_lib_under_directory_entry(self) -> None:
        """.so shared libraries are not a generate-phase source byproduct."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_src_dir_state(repo_root, orch="orch_dw_bl_002", run_id="run_dw_bl_002",
                                      src_files={"libevil.so": "\x7fELF"})
            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                self._run_validate(repo_root, "orch_dw_bl_002", "run_dw_bl_002")

    def test_step_terminal_rejects_unknown_extension_under_directory_entry(self) -> None:
        """Files with unknown/unlisted extensions are fail-closed."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_src_dir_state(repo_root, orch="orch_dw_bl_003", run_id="run_dw_bl_003",
                                      src_files={"payload.whl": "PK\x03\x04"})
            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                self._run_validate(repo_root, "orch_dw_bl_003", "run_dw_bl_003")

    def test_step_terminal_rejects_source_file_without_gate_provenance(self) -> None:
        """A source file (.f90) written outside guarded-apply-patch must fail terminal validation."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # Write the source file directly (not via guarded-apply-patch → not in gate_changed_paths)
            self._setup_src_dir_state(repo_root, orch="orch_dw_gate_001", run_id="run_dw_gate_001",
                                      src_files={"flux.f90": "MODULE flux\nEND MODULE\n"})
            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                self._run_validate(repo_root, "orch_dw_gate_001", "run_dw_gate_001")

    def test_step_terminal_rejects_compiler_byproduct_without_gate_provenance(self) -> None:
        """Compiler byproducts (.mod, .o, .a) are unauthorized without gate provenance.

        Agents must clean up compiler artefacts before record-agent-run to prevent
        unaudited binary injection into the pipeline.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_src_dir_state(repo_root, orch="orch_dw_gate_002", run_id="run_dw_gate_002",
                                      src_files={"flux.mod": "MODULE flux\nEND MODULE\n",
                                                 "flux.o": "\x7fELF"})
            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                self._run_validate(repo_root, "orch_dw_gate_002", "run_dw_gate_002")

    def test_step_terminal_rejects_makefile_under_directory_entry(self) -> None:
        """Makefile is a build-control file and requires explicit file pin — not in extensionless allowlist."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_src_dir_state(repo_root, orch="orch_dw_bl_004", run_id="run_dw_bl_004",
                                      src_files={"Makefile": "all:\n\tgfortran main.f90\n"})
            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                self._run_validate(repo_root, "orch_dw_bl_004", "run_dw_bl_004")

    def test_step_terminal_rejects_unknown_extensionless_file_under_directory_entry(self) -> None:
        """An extensionless file not in _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_src_dir_state(repo_root, orch="orch_dw_bl_005", run_id="run_dw_bl_005",
                                      src_files={"myexe": "#!/bin/bash\necho hi\n"})
            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                self._run_validate(repo_root, "orch_dw_bl_005", "run_dw_bl_005")

    def test_step_terminal_rejects_cmake_under_directory_entry(self) -> None:
        """Build control file (.cmake) requires explicit file pin — can inject commands."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_src_dir_state(repo_root, orch="orch_dw_bl_006", run_id="run_dw_bl_006",
                                      src_files={"CMakeLists.txt": "cmake_minimum_required(VERSION 3.0)\n"})
            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                self._run_validate(repo_root, "orch_dw_bl_006", "run_dw_bl_006")

    def test_step_terminal_rejects_nml_under_directory_entry(self) -> None:
        """Namelist file (.nml) requires explicit file pin."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_src_dir_state(repo_root, orch="orch_dw_bl_007", run_id="run_dw_bl_007",
                                      src_files={"params.nml": "&input\n  dt=0.1\n/\n"})
            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                self._run_validate(repo_root, "orch_dw_bl_007", "run_dw_bl_007")

    def test_step_terminal_rejects_toml_under_directory_entry(self) -> None:
        """Build config file (.toml) requires explicit file pin."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_src_dir_state(repo_root, orch="orch_dw_bl_008", run_id="run_dw_bl_008",
                                      src_files={"build.toml": "[build]\nmode = 'release'\n"})
            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                self._run_validate(repo_root, "orch_dw_bl_008", "run_dw_bl_008")

    def test_step_terminal_rejects_json_even_if_directory_path_leaked_into_manifest_file_tool_paths(self) -> None:
        """Regression: old auto-derive bug put directory tokens (no trailing /) into
        allowed_file_tool_paths manifest, then prefix-match in exact_declared_paths bypassed
        extension policy — any file under src/ would pass. Verify terminal rejects it."""
        from tools.orchestration_runtime import _validate_actual_write_paths

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_dw_reg_001"
            run_id = "run_dw_reg_001"
            src_dir_entry = f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src/"
            # Simulate the buggy manifest: directory path WITHOUT trailing slash in allowed_file_tool_paths
            self._setup_step_run_state(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                write_roots=[f"{_FIX_PIPE_REF}/generate/"],
                allowed_output_paths=[src_dir_entry],
                # Inject the buggy token directly — no trailing slash, looks like a path prefix
                allowed_file_tool_paths=[f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src"],
            )
            forbidden = repo_root / _FIX_PIPE_REF / "generate" / "gen_20260508_001" / "src" / "config.json"
            forbidden.parent.mkdir(parents=True, exist_ok=True)
            forbidden.write_text('{"key": "value"}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                _validate_actual_write_paths(
                    repo_root, orch,
                    {"agent_run_id": run_id, "agent_role": "step", "status": "pass", "output_refs": []},
                )

    def test_step_terminal_rejects_sh_even_if_directory_path_leaked_into_manifest_file_tool_paths(self) -> None:
        """Regression: same bypass as above but for shell scripts."""
        from tools.orchestration_runtime import _validate_actual_write_paths

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_dw_reg_002"
            run_id = "run_dw_reg_002"
            src_dir_entry = f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src/"
            self._setup_step_run_state(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                write_roots=[f"{_FIX_PIPE_REF}/generate/"],
                allowed_output_paths=[src_dir_entry],
                allowed_file_tool_paths=[f"{_FIX_PIPE_REF}/generate/gen_20260508_001/src"],
            )
            forbidden = repo_root / _FIX_PIPE_REF / "generate" / "gen_20260508_001" / "src" / "exploit.sh"
            forbidden.parent.mkdir(parents=True, exist_ok=True)
            forbidden.write_text("#!/bin/sh\nrm -rf /\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unauthorized write paths"):
                _validate_actual_write_paths(
                    repo_root, orch,
                    {"agent_run_id": run_id, "agent_role": "step", "status": "pass", "output_refs": []},
                )

    def test_step_terminal_accepts_write_with_directory_write_root(self) -> None:
        """Directory write_roots must have a trailing slash."""
        from tools.orchestration_runtime import _validate_actual_write_paths

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_term_dw_006"
            run_id = "step_run_term_dw_006"
            yaml_rel = "workspace/plans/p/case.resolved.yaml"
            self._setup_step_run_state(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                write_roots=["workspace/plans/"],
                allowed_output_paths=[yaml_rel],
                allowed_file_tool_paths=[yaml_rel],
            )
            yaml_path = repo_root / yaml_rel
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            yaml_path.write_text("case: ok\n", encoding="utf-8")

            _validate_actual_write_paths(
                repo_root,
                orch,
                {
                    "agent_run_id": run_id,
                    "agent_role": "step",
                    "status": "pass",
                    "output_refs": [yaml_rel],
                },
            )


class LoadWriteRootsFromCapTests(unittest.TestCase):
    """Tests for _load_write_roots_from_cap normalization and rejection rules."""

    def setUp(self) -> None:
        from tools.orchestration_runtime import _load_write_roots_from_cap
        self._load = _load_write_roots_from_cap

    def test_trailing_slash_entry_normalized_as_directory(self) -> None:
        result = self._load(["workspace/plans/"])
        self.assertEqual(result, ["workspace/plans/"])

    def test_extension_bearing_entry_kept_as_file_pin(self) -> None:
        result = self._load(["workspace/pipelines/a__b__1.0/lineage.json"])
        self.assertEqual(result, ["workspace/pipelines/a__b__1.0/lineage.json"])

    def test_extensionless_no_slash_entry_accepted_as_file_pin(self) -> None:
        """Extensionless entries like Makefile or LICENSE must be accepted as file pins."""
        result = self._load(["workspace/pipelines/a__b__1.0/pipe_001/src/Makefile"])
        self.assertEqual(result, ["workspace/pipelines/a__b__1.0/pipe_001/src/Makefile"])

    def test_multiple_extensionless_file_pins_accepted(self) -> None:
        """Multiple extensionless file pins are all kept in declaration order."""
        result = self._load([
            "workspace/pipelines/a__b__1.0/pipe_001/src/Makefile",
            "workspace/pipelines/a__b__1.0/pipe_001/src/LICENSE",
            "workspace/pipelines/a__b__1.0/pipe_001/generate/",
        ])
        self.assertEqual(result, [
            "workspace/pipelines/a__b__1.0/pipe_001/src/Makefile",
            "workspace/pipelines/a__b__1.0/pipe_001/src/LICENSE",
            "workspace/pipelines/a__b__1.0/pipe_001/generate/",
        ])

    def test_empty_and_whitespace_entries_skipped(self) -> None:
        result = self._load(["", "  ", None, 42, "workspace/plans/"])  # type: ignore[list-item]
        self.assertEqual(result, ["workspace/plans/"])

    def test_non_list_input_returns_empty(self) -> None:
        self.assertEqual(self._load(None), [])
        self.assertEqual(self._load("workspace/plans/"), [])


class ApplyPatchGateCoverageExtensionTests(unittest.TestCase):
    """`_validate_apply_patch_gate_coverage` の extension 別 gate 要件確認。"""

    def _make_payload(
        self,
        *,
        agent_role: str,
        agent_run_id: str,
        output_refs: list[str],
    ) -> dict[str, object]:
        return {
            "agent_role": agent_role,
            "agent_run_id": agent_run_id,
            "output_refs": output_refs,
        }

    def test_step_pass_without_gate_for_direct_write_only_outputs(self) -> None:
        from tools.orchestration_runtime import _validate_apply_patch_gate_coverage

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_ext_001")
            payload = self._make_payload(
                agent_role="step",
                agent_run_id="step_run_ext_001",
                output_refs=[
                    "workspace/plans/p/case.resolved.yaml",
                    "workspace/plans/p/algorithm.summary.md",
                    "workspace/pipelines/p/generate/g/src/main.f90",
                ],
            )
            _validate_apply_patch_gate_coverage(repo_root, "orch_ext_001", payload)

    def test_step_pass_requires_gate_for_json_output(self) -> None:
        from tools.orchestration_runtime import _validate_apply_patch_gate_coverage

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_ext_002")
            payload = self._make_payload(
                agent_role="step",
                agent_run_id="step_run_ext_002",
                output_refs=[
                    "workspace/plans/p/case.resolved.yaml",
                    "workspace/plans/p/derived_contract.json",
                ],
            )
            with self.assertRaisesRegex(
                ValueError, "apply_patch_writes gate evidence"
            ):
                _validate_apply_patch_gate_coverage(repo_root, "orch_ext_002", payload)

    def test_step_pass_with_gate_covers_only_cli_extension_refs(self) -> None:
        from tools.orchestration_runtime import _validate_apply_patch_gate_coverage

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_ext_003")
            _write_apply_patch_gate_evidence(
                repo_root,
                orchestration_id="orch_ext_003",
                agent_run_id="step_run_ext_003",
                actor_role="step",
                changed_paths=["workspace/plans/p/derived_contract.json"],
            )
            payload = self._make_payload(
                agent_role="step",
                agent_run_id="step_run_ext_003",
                output_refs=[
                    "workspace/plans/p/case.resolved.yaml",
                    "workspace/plans/p/algorithm.summary.md",
                    "workspace/plans/p/derived_contract.json",
                ],
            )
            _validate_apply_patch_gate_coverage(repo_root, "orch_ext_003", payload)


class BwrapProfileFilePinTests(unittest.TestCase):
    """build_bwrap_profile + render_bwrap_command: file-pin write_roots must get a bind mount."""

    def _write_cap_and_manifest(
        self,
        repo_root: Path,
        *,
        orchestration_id: str,
        agent_run_id: str,
        write_roots: list[str],
    ) -> None:
        from tools.orchestration_runtime import (
            _capabilities_dir,
            _read_manifests_dir,
            _ensure_orchestration_audit_dirs,
        )
        _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
        cap_dir = _capabilities_dir(repo_root, orchestration_id)
        cap_dir.mkdir(parents=True, exist_ok=True)
        cap_path = cap_dir / f"{agent_run_id}.json"
        cap_path.write_text(
            json.dumps({"agent_run_id": agent_run_id, "write_roots": write_roots}),
            encoding="utf-8",
        )
        rm_dir = _read_manifests_dir(repo_root, orchestration_id)
        rm_dir.mkdir(parents=True, exist_ok=True)
        rm_path = rm_dir / f"{agent_run_id}.json"
        rm_path.write_text(
            json.dumps({"agent_run_id": agent_run_id, "allowed_read_roots": ["workspace/"]}),
            encoding="utf-8",
        )

    def test_file_pin_write_root_pre_creates_target_file(self) -> None:
        """build_bwrap_profile must touch a file-pin target so render_bwrap_command can bind it."""
        from tools.orchestration_runtime import build_bwrap_profile

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bwrap_fp_001"
            run_id = "run_bwrap_fp_001"
            pin = "workspace/pipelines/a__b__1.0/lineage.json"
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=[pin],
            )

            profile = build_bwrap_profile(
                repo_root=repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                backend_command="python3 agent.py",
            )

            pin_path = repo_root / pin
            self.assertTrue(pin_path.exists(), "file-pin target must be pre-created for bwrap file-level bind")
            self.assertIn(pin, profile["write_roots"])

    def test_file_pin_write_root_appears_in_render_bwrap_bind(self) -> None:
        """render_bwrap_command must emit --bind for the file-pin itself, not its parent directory."""
        from tools.orchestration_runtime import build_bwrap_profile, render_bwrap_command

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bwrap_fp_002"
            run_id = "run_bwrap_fp_002"
            pin = "workspace/pipelines/a__b__1.0/lineage.json"
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=[pin],
            )
            profile = build_bwrap_profile(
                repo_root=repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                backend_command="python3 agent.py",
            )
            cmd = render_bwrap_command(profile=profile, command_argv=["python3", "agent.py"])
            pin_abs = str((repo_root / pin).resolve())
            parent_abs = str((repo_root / pin).resolve().parent)
            bind_targets = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--bind"]
            self.assertIn(pin_abs, bind_targets, "file-pin target must appear in bwrap --bind list")
            self.assertNotIn(parent_abs, bind_targets, "parent directory must NOT be bound — that would grant access to sibling files")

    def test_dotted_directory_write_root_not_misclassified_as_file_pin(self) -> None:
        """A directory write_root with a dotted name (e.g. v1.0/) must not be treated as a file pin."""
        from tools.orchestration_runtime import build_bwrap_profile

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bwrap_fp_003"
            run_id = "run_bwrap_fp_003"
            dotted_dir = "workspace/plans/v1.0/"
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=[dotted_dir],
            )
            build_bwrap_profile(
                repo_root=repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                backend_command="python3 agent.py",
            )
            # Must create the directory itself, not just its parent
            dir_path = repo_root / "workspace" / "plans" / "v1.0"
            self.assertTrue(dir_path.is_dir(), "dotted directory write_root must be created as directory")

    def _save_profile(self, repo_root: Path, orchestration_id: str, agent_run_id: str, profile: dict) -> None:
        """Helper: save bwrap profile to disk so _cleanup_empty_file_pin_stubs can find it."""
        import json as _json
        from tools.orchestration_runtime import _sandbox_profiles_dir
        profiles_dir = _sandbox_profiles_dir(repo_root, orchestration_id)
        profiles_dir.mkdir(parents=True, exist_ok=True)
        (profiles_dir / f"{agent_run_id}.json").write_text(
            _json.dumps(profile), encoding="utf-8"
        )

    def _seed_launch_request(self, repo_root: Path, orchestration_id: str, arid: str) -> None:
        """Helper: create launches/<arid>.request.json so that
        _cleanup_agent_tmp_root's Adv-5 ownership guard accepts the call."""
        launches_dir = repo_root / "workspace" / "orchestrations" / orchestration_id / "launches"
        launches_dir.mkdir(parents=True, exist_ok=True)
        (launches_dir / f"{arid}.request.json").write_text(
            json.dumps({"agent_run_id": arid}), encoding="utf-8"
        )

    def test_cleanup_agent_tmp_root_removes_per_agent_tmp_dir(self) -> None:
        """Fix 4: _cleanup_agent_tmp_root must rmtree workspace/tmp/<arid>/
        when the calling orchestration owns the launch record."""
        from tools.orchestration_runtime import _cleanup_agent_tmp_root

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "abc123-tmp-cleanup-1"
            self._seed_launch_request(repo_root, "orch_x", arid)
            tmp_dir = repo_root / "workspace" / "tmp" / arid
            (tmp_dir / "sub").mkdir(parents=True, exist_ok=True)
            (tmp_dir / "run_record_launch.py").write_text("print('x')\n", encoding="utf-8")
            (tmp_dir / "sub" / "patch.txt").write_text("data\n", encoding="utf-8")
            self.assertTrue(tmp_dir.exists())

            _cleanup_agent_tmp_root(repo_root, "orch_x", agent_run_id=arid)
            self.assertFalse(tmp_dir.exists(), "agent tmp dir must be removed")

    def test_cleanup_agent_tmp_root_refuses_when_not_owner(self) -> None:
        """Adv-5: refuse to delete when the calling orchestration has no launch
        record for arid. Two orchestrations could collide on the same arid (the
        tmp namespace is flat); the non-owner must not be able to wipe the
        owner's live scratch."""
        from tools.orchestration_runtime import _cleanup_agent_tmp_root

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "shared-arid-cross-orch"
            # Owner = orch_owner; intruder = orch_intruder
            self._seed_launch_request(repo_root, "orch_owner", arid)
            tmp_dir = repo_root / "workspace" / "tmp" / arid
            tmp_dir.mkdir(parents=True, exist_ok=True)
            sentinel = tmp_dir / "live_data.txt"
            sentinel.write_text("owner data\n", encoding="utf-8")

            # Intruder orchestration — no launch record for this arid.
            _cleanup_agent_tmp_root(repo_root, "orch_intruder", agent_run_id=arid)
            self.assertTrue(
                sentinel.exists(),
                "non-owner orchestration must not wipe the owner's tmp scratch",
            )
            self.assertTrue(tmp_dir.exists())

    def test_cleanup_agent_tmp_root_no_op_when_dir_missing(self) -> None:
        """Idempotent: missing directory is not an error."""
        from tools.orchestration_runtime import _cleanup_agent_tmp_root

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_launch_request(repo_root, "orch_x", "never-existed")
            _cleanup_agent_tmp_root(repo_root, "orch_x", agent_run_id="never-existed")

    def test_cleanup_agent_tmp_root_rejects_path_traversal(self) -> None:
        """Defensive: reject agent_run_id values containing path separators or '..'."""
        from tools.orchestration_runtime import _cleanup_agent_tmp_root

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            sentinel = repo_root / "workspace" / "tmp" / "abc"
            sentinel.mkdir(parents=True, exist_ok=True)
            (sentinel / "keep.txt").write_text("keep\n", encoding="utf-8")
            self._seed_launch_request(repo_root, "orch_x", "abc")
            # Each of these must be a no-op (no exception, no deletion).
            for bad in ("../abc", "abc/sub", "..", ".", ""):
                _cleanup_agent_tmp_root(repo_root, "orch_x", agent_run_id=bad)
            self.assertTrue((sentinel / "keep.txt").exists(),
                            "valid sibling tmp dir must not be touched by traversal attempts")

    def test_cleanup_agent_tmp_root_refuses_on_cross_orch_arid_collision(self) -> None:
        """Adv-11: when two orchestrations both have launch records for the
        same arid, the flat workspace/tmp/<arid>/ namespace cannot
        disambiguate ownership. Cleanup must refuse to delete to avoid wiping
        the colliding orchestration's live scratch.
        """
        from tools.orchestration_runtime import _cleanup_agent_tmp_root

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            shared_arid = "collision-arid-001"
            self._seed_launch_request(repo_root, "orch_alpha", shared_arid)
            self._seed_launch_request(repo_root, "orch_beta", shared_arid)
            tmp_dir = repo_root / "workspace" / "tmp" / shared_arid
            tmp_dir.mkdir(parents=True, exist_ok=True)
            sentinel = tmp_dir / "shared.txt"
            sentinel.write_text("shared\n", encoding="utf-8")

            _cleanup_agent_tmp_root(repo_root, "orch_alpha", agent_run_id=shared_arid)
            self.assertTrue(
                sentinel.exists(),
                "Cross-orch collision must prevent deletion (orch_alpha tries to clean shared dir)",
            )
            _cleanup_agent_tmp_root(repo_root, "orch_beta", agent_run_id=shared_arid)
            self.assertTrue(
                sentinel.exists(),
                "Cross-orch collision must prevent deletion (orch_beta also tries to clean shared dir)",
            )

    def test_cleanup_agent_tmp_root_refuses_on_orch_meta_vs_launch_collision(self) -> None:
        """Adv-11 + Adv-9: collision can also be between one orch's
        orchestration_agent_run_id and another orch's substep launch arid."""
        from tools.orchestration_runtime import _cleanup_agent_tmp_root

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            shared_arid = "orch-vs-substep-collision"
            # Orch X: claims shared_arid as its orchestration_agent_run_id.
            orch_x = repo_root / "workspace" / "orchestrations" / "orch_X"
            orch_x.mkdir(parents=True, exist_ok=True)
            (orch_x / "orchestration_meta.json").write_text(
                json.dumps({
                    "orchestration_id": "orch_X",
                    "orchestration_agent_run_id": shared_arid,
                }),
                encoding="utf-8",
            )
            # Orch Y: launched shared_arid as a child agent.
            self._seed_launch_request(repo_root, "orch_Y", shared_arid)
            tmp_dir = repo_root / "workspace" / "tmp" / shared_arid
            tmp_dir.mkdir(parents=True, exist_ok=True)
            sentinel = tmp_dir / "shared.txt"
            sentinel.write_text("shared\n", encoding="utf-8")

            _cleanup_agent_tmp_root(repo_root, "orch_Y", agent_run_id=shared_arid)
            self.assertTrue(
                sentinel.exists(),
                "Collision between orchestration_agent_run_id and substep launch must prevent deletion",
            )

    def test_cleanup_agent_tmp_root_accepts_orchestration_meta_proof(self) -> None:
        """Adv-7: orchestration agents are not 'launched' via record-launch and
        so have no .request.json. Their identity is pinned in
        orchestration_meta.json#orchestration_agent_run_id; that field is
        accepted as ownership proof for cleanup."""
        from tools.orchestration_runtime import _cleanup_agent_tmp_root

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_arid = "orch-agent-run-id-001"
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_meta"
            orch_dir.mkdir(parents=True, exist_ok=True)
            (orch_dir / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_agent_run_id": orch_arid}), encoding="utf-8",
            )
            tmp_dir = repo_root / "workspace" / "tmp" / orch_arid
            tmp_dir.mkdir(parents=True, exist_ok=True)
            (tmp_dir / "scratch.txt").write_text("ok\n", encoding="utf-8")

            _cleanup_agent_tmp_root(repo_root, "orch_meta", agent_run_id=orch_arid)
            self.assertFalse(
                tmp_dir.exists(),
                "orchestration role tmp dir must be cleaned when meta proves ownership",
            )

    def test_cleanup_agent_tmp_root_does_not_follow_symlink(self) -> None:
        """Symlinks at workspace/tmp/<arid>/ must not be followed (refuse to delete)."""
        import os
        from tools.orchestration_runtime import _cleanup_agent_tmp_root

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            external = repo_root / "external_real_dir"
            external.mkdir()
            (external / "important.txt").write_text("do not delete\n", encoding="utf-8")
            workspace_tmp = repo_root / "workspace" / "tmp"
            workspace_tmp.mkdir(parents=True, exist_ok=True)
            link_path = workspace_tmp / "link-arid"
            try:
                os.symlink(external, link_path)
            except OSError:
                self.skipTest("symlink not supported on this filesystem")
            self._seed_launch_request(repo_root, "orch_x", "link-arid")
            _cleanup_agent_tmp_root(repo_root, "orch_x", agent_run_id="link-arid")
            self.assertTrue(
                (external / "important.txt").exists(),
                "symlink target must not be touched",
            )

    def test_file_pin_stub_cleaned_up_when_agent_never_writes(self) -> None:
        """_cleanup_empty_file_pin_stubs must delete empty stubs if the agent never wrote to the pin."""
        from tools.orchestration_runtime import build_bwrap_profile, _cleanup_empty_file_pin_stubs

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bwrap_fp_005"
            run_id = "run_bwrap_fp_005"
            pin = "workspace/pipelines/a__b__1.0/pipe_001/lineage.json"
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=[pin],
            )
            # Profile build pre-creates the stub; profile is saved so cleanup can read it.
            profile = build_bwrap_profile(
                repo_root=repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                backend_command="python3 agent.py",
            )
            self._save_profile(repo_root, orch, run_id, profile)
            pin_path = repo_root / pin
            self.assertTrue(pin_path.exists(), "stub must exist after build_bwrap_profile")
            self.assertEqual(pin_path.stat().st_size, 0, "stub must be empty")

            # Simulate terminal cleanup: agent terminated without writing to the pin.
            _cleanup_empty_file_pin_stubs(repo_root, orch, agent_run_id=run_id)
            self.assertFalse(
                pin_path.exists(),
                "empty stub must be removed by _cleanup_empty_file_pin_stubs when agent never wrote",
            )

    def test_file_pin_stub_preserved_when_agent_wrote_content(self) -> None:
        """_cleanup_empty_file_pin_stubs must not delete a pin file that the agent wrote content to."""
        from tools.orchestration_runtime import (
            build_bwrap_profile, _cleanup_empty_file_pin_stubs,
            _gate_changed_paths_store_path, _ensure_orchestration_audit_dirs,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bwrap_fp_006"
            run_id = "run_bwrap_fp_006"
            pin = "workspace/pipelines/a__b__1.0/pipe_001/lineage.json"
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=[pin],
            )
            profile = build_bwrap_profile(
                repo_root=repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                backend_command="python3 agent.py",
            )
            self._save_profile(repo_root, orch, run_id, profile)
            pin_path = repo_root / pin
            # Simulate agent writing content to the pin.
            pin_path.write_text('{"node_key":"a"}', encoding="utf-8")
            # Simulate gate_changed_paths recording the write.
            _ensure_orchestration_audit_dirs(repo_root, orch)
            gcp_path = _gate_changed_paths_store_path(repo_root, orch, agent_run_id=run_id)
            gcp_path.parent.mkdir(parents=True, exist_ok=True)
            import json as _json
            gcp_path.write_text(
                _json.dumps({"gate_changed_paths": [pin]}), encoding="utf-8"
            )
            _cleanup_empty_file_pin_stubs(repo_root, orch, agent_run_id=run_id)
            self.assertTrue(
                pin_path.exists(),
                "non-empty pin file must NOT be removed by cleanup",
            )

    def test_pre_existing_empty_file_pin_not_deleted_by_cleanup(self) -> None:
        """A zero-byte file that existed BEFORE build_bwrap_profile must survive _cleanup_empty_file_pin_stubs."""
        from tools.orchestration_runtime import build_bwrap_profile, _cleanup_empty_file_pin_stubs

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bwrap_fp_008"
            run_id = "run_bwrap_fp_008"
            pin = "workspace/pipelines/a__b__1.0/pipe_001/lineage.json"
            # Pre-create the file BEFORE build_bwrap_profile (simulates pre-existing file).
            pin_path = repo_root / pin
            pin_path.parent.mkdir(parents=True, exist_ok=True)
            pin_path.write_bytes(b"")  # zero bytes, pre-existing
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=[pin],
            )
            # build_bwrap_profile must NOT add it to created_file_pin_stubs (file already existed).
            profile = build_bwrap_profile(
                repo_root=repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                backend_command="python3 agent.py",
            )
            stub_paths = [
                entry["path"]
                for entry in profile.get("created_file_pin_stubs", [])
                if isinstance(entry, dict)
            ]
            self.assertNotIn(
                "workspace/pipelines/a__b__1.0/pipe_001/lineage.json",
                stub_paths,
                "pre-existing file must NOT be recorded as a stub",
            )
            # Cleanup must leave the pre-existing file untouched.
            _cleanup_empty_file_pin_stubs(repo_root, orch, agent_run_id=run_id)
            self.assertTrue(
                pin_path.exists(),
                "pre-existing empty file must NOT be deleted by cleanup",
            )

    def test_file_pin_stub_preserved_when_subprocess_writes_zero_bytes(self) -> None:
        """A subprocess that writes zero bytes to a pinned file updates the mtime.
        _cleanup_empty_file_pin_stubs must NOT delete a stub whose mtime has changed,
        even if the file is still zero bytes — the subprocess output is legitimate."""
        from tools.orchestration_runtime import build_bwrap_profile, _cleanup_empty_file_pin_stubs
        import time

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bwrap_fp_009"
            run_id = "run_bwrap_fp_009"
            pin = "workspace/pipelines/a__b__1.0/pipe_001/lineage.json"
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=[pin],
            )
            profile = build_bwrap_profile(
                repo_root=repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                backend_command="python3 agent.py",
            )
            self._save_profile(repo_root, orch, run_id, profile)
            pin_path = repo_root / pin
            self.assertTrue(pin_path.exists())
            self.assertEqual(pin_path.stat().st_size, 0)

            # Simulate a subprocess writing zero bytes — this updates mtime even though
            # file size stays at zero.
            time.sleep(0.01)  # ensure mtime advances
            pin_path.write_bytes(b"")  # zero-byte write updates mtime

            # Cleanup must preserve the file because mtime changed since our touch().
            _cleanup_empty_file_pin_stubs(repo_root, orch, agent_run_id=run_id)
            self.assertTrue(
                pin_path.exists(),
                "zero-byte file written by subprocess (mtime changed) must NOT be deleted",
            )

    def test_generate_step_bwrap_does_not_bind_pipeline_root_as_writable(self) -> None:
        """bwrap profile for generate step must NOT make pipeline_ref/ writable — sibling dirs stay read-only."""
        from tools.orchestration_runtime import build_bwrap_profile, render_bwrap_command

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bwrap_fp_007"
            run_id = "run_bwrap_fp_007"
            pipeline_root = "workspace/pipelines/a__b__1.0/pipe_001"
            # generate step write_roots: generate/ dir + lineage.json file pin
            write_roots = [
                f"{pipeline_root}/generate/",
                f"{pipeline_root}/lineage.json",
            ]
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=write_roots,
            )
            profile = build_bwrap_profile(
                repo_root=repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                backend_command="python3 agent.py",
            )
            cmd = render_bwrap_command(profile=profile, command_argv=["python3", "agent.py"])
            pipeline_abs = str((repo_root / pipeline_root).resolve())
            bind_targets = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--bind"]
            self.assertNotIn(
                pipeline_abs, bind_targets,
                "pipeline_ref/ directory must NOT appear in bwrap --bind — that would make build/execute writable",
            )

    def test_extensionless_no_slash_entry_treated_as_file_pin(self) -> None:
        """An extensionless no-slash write_root entry (e.g. Makefile) is treated as a file pin.

        The trailing-slash convention is sufficient to distinguish directories from file pins;
        no extension is required for file pins.
        """
        from tools.orchestration_runtime import build_bwrap_profile

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bwrap_fp_004"
            run_id = "run_bwrap_fp_004"
            makefile_pin = "workspace/pipelines/a__b__1.0/pipe_001/src/Makefile"
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=[makefile_pin],
            )
            # Must succeed — extensionless file pin is valid
            profile = build_bwrap_profile(
                repo_root=repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                backend_command="python3 agent.py",
            )
            # The file pin must be pre-created as a stub
            pin_path = repo_root / makefile_pin
            self.assertTrue(pin_path.exists(), "extensionless file pin must be pre-created as stub")
            self.assertTrue(pin_path.is_file(), "extensionless file pin stub must be a regular file")

    def test_symlinked_directory_write_root_raises_before_mkdir(self) -> None:
        """A write_root that resolves outside repo_root via symlink must be rejected before mkdir."""
        import os
        from tools.orchestration_runtime import build_bwrap_profile

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            repo_root = Path(tmp)
            orch = "orch_bwrap_sl_001"
            run_id = "run_bwrap_sl_001"
            # Create a symlink inside the repo that points outside
            link_parent = repo_root / "workspace" / "plans"
            link_parent.mkdir(parents=True, exist_ok=True)
            link_path = link_parent / "escape"
            os.symlink(outside, str(link_path))
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=["workspace/plans/escape/"],
            )
            with self.assertRaises(ValueError) as ctx:
                build_bwrap_profile(
                    repo_root=repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    backend_command="python3 agent.py",
                )
            self.assertIn("resolves outside repo_root", str(ctx.exception))

    def test_symlinked_file_pin_write_root_raises_before_touch(self) -> None:
        """A file-pin write_root that resolves outside repo_root via symlink must be rejected."""
        import os
        from tools.orchestration_runtime import build_bwrap_profile

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            repo_root = Path(tmp)
            orch = "orch_bwrap_sl_002"
            run_id = "run_bwrap_sl_002"
            link_parent = repo_root / "workspace" / "pipelines" / "a"
            link_parent.mkdir(parents=True, exist_ok=True)
            # Symlink the parent directory so pin resolution escapes
            link_path = link_parent / "escape"
            os.symlink(outside, str(link_path))
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=["workspace/pipelines/a/escape/lineage.json"],
            )
            with self.assertRaises(ValueError) as ctx:
                build_bwrap_profile(
                    repo_root=repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    backend_command="python3 agent.py",
                )
            self.assertIn("resolves outside repo_root", str(ctx.exception))

    def test_file_pin_write_root_raises_if_path_is_existing_directory(self) -> None:
        """build_bwrap_profile must reject a file pin if the path already exists as a directory."""
        from tools.orchestration_runtime import build_bwrap_profile

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bwrap_type_001"
            run_id = "run_bwrap_type_001"
            pin = "workspace/pipelines/a__b__1.0/lineage.json"
            # Pre-create the pin path as a directory (not a file).
            (repo_root / pin).mkdir(parents=True, exist_ok=True)
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=[pin],
            )
            with self.assertRaises(ValueError) as ctx:
                build_bwrap_profile(
                    repo_root=repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    backend_command="python3 agent.py",
                )
            self.assertIn("resolves to a directory", str(ctx.exception))

    def test_file_pin_write_root_raises_if_path_is_symlink_inside_repo(self) -> None:
        """build_bwrap_profile must reject a file pin that is a symlink even within the repo."""
        import os
        from tools.orchestration_runtime import build_bwrap_profile

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_bwrap_type_002"
            run_id = "run_bwrap_type_002"
            pin = "workspace/pipelines/a__b__1.0/lineage.json"
            pin_path = repo_root / pin
            pin_path.parent.mkdir(parents=True, exist_ok=True)
            # Create a real file and a symlink pointing to it.
            real_file = pin_path.parent / "lineage_real.json"
            real_file.write_text("{}", encoding="utf-8")
            os.symlink(str(real_file), str(pin_path))
            self._write_cap_and_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                write_roots=[pin],
            )
            with self.assertRaises(ValueError) as ctx:
                build_bwrap_profile(
                    repo_root=repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    backend_command="python3 agent.py",
                )
            self.assertIn("symlink", str(ctx.exception))

    def test_render_bwrap_raises_if_file_pin_is_directory_at_render_time(self) -> None:
        """render_bwrap_command must raise if a file pin became a directory between build and render."""
        from tools.orchestration_runtime import render_bwrap_command

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            pin_rel = "workspace/pipelines/a__b__1.0/lineage.json"
            pin_abs = repo_root / pin_rel
            # Place a directory at the pin path (simulating a race or misconfiguration).
            pin_abs.mkdir(parents=True, exist_ok=True)
            ws_tmp = repo_root / "workspace" / "tmp" / "run_rt_001"
            ws_tmp.mkdir(parents=True, exist_ok=True)
            profile = {
                "repo_root": str(repo_root),
                "tmp_dir": str(ws_tmp),
                "workspace_tmp_rw_abs": str(ws_tmp),
                "write_roots": [pin_rel],
                "read_roots": [],
                "runtime_ro_bind_paths": [],
            }
            with self.assertRaises(ValueError) as ctx:
                render_bwrap_command(profile=profile, command_argv=["python3", "agent.py"])
            self.assertIn("non-file", str(ctx.exception))

    def test_render_bwrap_raises_if_file_pin_is_symlink_at_render_time(self) -> None:
        """render_bwrap_command must raise if a file pin is a symlink at render time."""
        import os
        from tools.orchestration_runtime import render_bwrap_command

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            pin_rel = "workspace/pipelines/a__b__1.0/lineage.json"
            pin_abs = repo_root / pin_rel
            pin_abs.parent.mkdir(parents=True, exist_ok=True)
            real = pin_abs.parent / "real.json"
            real.write_text("{}", encoding="utf-8")
            os.symlink(str(real), str(pin_abs))
            ws_tmp = repo_root / "workspace" / "tmp" / "run_rt_002"
            ws_tmp.mkdir(parents=True, exist_ok=True)
            profile = {
                "repo_root": str(repo_root),
                "tmp_dir": str(ws_tmp),
                "workspace_tmp_rw_abs": str(ws_tmp),
                "write_roots": [pin_rel],
                "read_roots": [],
                "runtime_ro_bind_paths": [],
            }
            with self.assertRaises(ValueError) as ctx:
                render_bwrap_command(profile=profile, command_argv=["python3", "agent.py"])
            self.assertIn("symlink", str(ctx.exception))


class PreWriteManifestExtensionPolicyTests(unittest.TestCase):
    """_validate_paths_against_allowed_output_manifest must enforce extension policy pre-mutation."""

    def _write_manifest(
        self,
        repo_root: Path,
        *,
        orchestration_id: str,
        agent_run_id: str,
        allowed_output_paths: list[str],
    ) -> None:
        from tools.orchestration_runtime import (
            _output_manifests_dir,
            _ensure_orchestration_audit_dirs,
        )
        _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
        m_dir = _output_manifests_dir(repo_root, orchestration_id)
        m_dir.mkdir(parents=True, exist_ok=True)
        (m_dir / f"{agent_run_id}.json").write_text(
            json.dumps({"allowed_output_paths": allowed_output_paths, "allowed_tmp_root": ""}),
            encoding="utf-8",
        )

    def test_pre_write_allows_known_extension_under_directory_entry(self) -> None:
        from tools.orchestration_runtime import _validate_paths_against_allowed_output_manifest

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_pw_ext_001"
            run_id = "run_pw_ext_001"
            self._write_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                allowed_output_paths=["workspace/pipelines/a/generate/g_001/src/"],
            )
            # Must not raise
            _validate_paths_against_allowed_output_manifest(
                repo_root,
                orchestration_id=orch,
                agent_run_id=run_id,
                paths=["workspace/pipelines/a/generate/g_001/src/flux.f90"],
            )

    def test_pre_write_rejects_makefile_under_directory_entry(self) -> None:
        """Makefile requires explicit file pin — not in extensionless allowlist."""
        from tools.orchestration_runtime import _validate_paths_against_allowed_output_manifest

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_pw_ext_002"
            run_id = "run_pw_ext_002"
            self._write_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                allowed_output_paths=["workspace/pipelines/a/generate/g_001/src/"],
            )
            with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                _validate_paths_against_allowed_output_manifest(
                    repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    paths=["workspace/pipelines/a/generate/g_001/src/Makefile"],
                )

    def test_pre_write_rejects_script_under_directory_entry(self) -> None:
        from tools.orchestration_runtime import _validate_paths_against_allowed_output_manifest

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_pw_ext_003"
            run_id = "run_pw_ext_003"
            self._write_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                allowed_output_paths=["workspace/pipelines/a/generate/g_001/src/"],
            )
            with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                _validate_paths_against_allowed_output_manifest(
                    repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    paths=["workspace/pipelines/a/generate/g_001/src/exploit.sh"],
                )

    def test_pre_write_rejects_unknown_extensionless_under_directory_entry(self) -> None:
        from tools.orchestration_runtime import _validate_paths_against_allowed_output_manifest

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_pw_ext_004"
            run_id = "run_pw_ext_004"
            self._write_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                allowed_output_paths=["workspace/pipelines/a/generate/g_001/src/"],
            )
            with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                _validate_paths_against_allowed_output_manifest(
                    repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    paths=["workspace/pipelines/a/generate/g_001/src/myexe"],
                )

    def test_pre_write_rejects_json_under_directory_entry(self) -> None:
        """Structured data (.json) under a directory entry requires an explicit file pin."""
        from tools.orchestration_runtime import _validate_paths_against_allowed_output_manifest

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_pw_ext_005"
            run_id = "run_pw_ext_005"
            self._write_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                allowed_output_paths=["workspace/pipelines/a/generate/g_001/src/"],
            )
            with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                _validate_paths_against_allowed_output_manifest(
                    repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    paths=["workspace/pipelines/a/generate/g_001/src/results.json"],
                )

    def test_pre_write_rejects_yaml_under_directory_entry(self) -> None:
        """Structured data (.yaml) under a directory entry requires an explicit file pin."""
        from tools.orchestration_runtime import _validate_paths_against_allowed_output_manifest

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_pw_ext_006"
            run_id = "run_pw_ext_006"
            self._write_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                allowed_output_paths=["workspace/pipelines/a/generate/g_001/src/"],
            )
            with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                _validate_paths_against_allowed_output_manifest(
                    repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    paths=["workspace/pipelines/a/generate/g_001/src/config.yaml"],
                )

    def test_pre_write_rejects_cmake_under_directory_entry(self) -> None:
        """Build control file (.cmake) under a directory entry requires an explicit file pin."""
        from tools.orchestration_runtime import _validate_paths_against_allowed_output_manifest

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_pw_ext_007"
            run_id = "run_pw_ext_007"
            self._write_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                allowed_output_paths=["workspace/pipelines/a/generate/g_001/src/"],
            )
            with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                _validate_paths_against_allowed_output_manifest(
                    repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    paths=["workspace/pipelines/a/generate/g_001/src/CMakeLists.txt"],
                )

    def test_pre_write_rejects_nml_under_directory_entry(self) -> None:
        """Namelist file (.nml) under a directory entry requires an explicit file pin."""
        from tools.orchestration_runtime import _validate_paths_against_allowed_output_manifest

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_pw_ext_008"
            run_id = "run_pw_ext_008"
            self._write_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                allowed_output_paths=["workspace/pipelines/a/generate/g_001/src/"],
            )
            with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                _validate_paths_against_allowed_output_manifest(
                    repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    paths=["workspace/pipelines/a/generate/g_001/src/params.nml"],
                )

    def test_pre_write_rejects_object_file_under_directory_entry(self) -> None:
        """Compiler byproduct (.o) is subprocess output — Edit/Write must be rejected pre-mutation."""
        from tools.orchestration_runtime import _validate_paths_against_allowed_output_manifest

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_pw_ext_009"
            run_id = "run_pw_ext_009"
            self._write_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                allowed_output_paths=["workspace/pipelines/a/generate/g_001/src/"],
            )
            with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                _validate_paths_against_allowed_output_manifest(
                    repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    paths=["workspace/pipelines/a/generate/g_001/src/flux.o"],
                )

    def test_pre_write_rejects_module_file_under_directory_entry(self) -> None:
        """Compiler byproduct (.mod) is subprocess output — Edit/Write must be rejected pre-mutation."""
        from tools.orchestration_runtime import _validate_paths_against_allowed_output_manifest

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_pw_ext_010"
            run_id = "run_pw_ext_010"
            self._write_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                allowed_output_paths=["workspace/pipelines/a/generate/g_001/src/"],
            )
            with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                _validate_paths_against_allowed_output_manifest(
                    repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    paths=["workspace/pipelines/a/generate/g_001/src/flux.mod"],
                )

    def test_pre_write_rejects_archive_file_under_directory_entry(self) -> None:
        """Compiler byproduct (.a) is subprocess output — Edit/Write must be rejected pre-mutation."""
        from tools.orchestration_runtime import _validate_paths_against_allowed_output_manifest

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_pw_ext_011"
            run_id = "run_pw_ext_011"
            self._write_manifest(
                repo_root, orchestration_id=orch, agent_run_id=run_id,
                allowed_output_paths=["workspace/pipelines/a/generate/g_001/src/"],
            )
            with self.assertRaisesRegex(ValueError, "allowed_output_paths manifest violation"):
                _validate_paths_against_allowed_output_manifest(
                    repo_root,
                    orchestration_id=orch,
                    agent_run_id=run_id,
                    paths=["workspace/pipelines/a/generate/g_001/src/libflux.a"],
                )


class RecordTimeoutTests(unittest.TestCase):
    """Fix 5: record-timeout is the canonical recovery for child Agent stream timeouts."""

    def _setup_substep_launch(self, repo_root: Path) -> str:
        """Initialise an orchestration with a substep launched and return the substep agent_run_id."""
        init_orchestration(
            repo_root=repo_root,
            orchestration_id="orch_to_001",
            spec_ref="spec/problem/shallow_water2d/controlled_spec.md",
            source_dependency_ref="spec/problem/shallow_water2d/deps.yaml",
        )
        write_preflight(
            repo_root=repo_root,
            orchestration_id="orch_to_001",
            payload={
                "status": "pass",
                "backend": "claude",
                "sandbox_runtime": "bwrap",
                "sandbox_enforced": True,
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
                "feature_states": {"multi_agent": True, "codex_hooks": True},
                "checks": [
                    {"name": "multi_agent_enabled", "pass": True},
                    {"name": "codex_hooks_enabled", "pass": True},
                    {"name": "codex_home_writable", "pass": True},
                    {"name": "sandbox_bwrap_available", "pass": True},
                    {"name": "sandbox_bwrap_userns", "pass": True},
                ],
            },
        )
        substep_arid = "substep_run_to_001"
        record_launch(
            repo_root=repo_root,
            orchestration_id="orch_to_001",
            parent_agent_run_id="orch_run_to_001",
            child_agent_run_id=substep_arid,
            request_payload={
                "agent_run_id": substep_arid,
                "agent_role": "substep",
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "generate",
                "substep": "generate",
                "orchestration_id": "orch_to_001",
                "parent_agent_run_id": "orch_run_to_001",
                "plan_ref": _FIX_PLAN_REF,
                "pipeline_ref": _FIX_PIPE_REF,
                "dependency_ref": _FIX_PLAN_REF,
                "skill_name": "workflow-generate-generate",
                "skill_ref": "skills/workflow-generate-generate/SKILL.md",
                "skill_must_read_refs": _fixture_skill_must_read_refs_substep(
                    "generate", "generate"
                ),
                "allowed_output_paths": [
                    f"{_FIX_PIPE_REF}/generate/gen_001/generate_meta.json",
                ],
                "launch_prompt_full": render_launch_prompt_text({
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "generate",
                    "substep": "generate",
                    "agent_run_id": substep_arid,
                    "orchestration_id": "orch_to_001",
                    "parent_agent_run_id": "orch_run_to_001",
                    "workflow_mode": "dev",
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_PLAN_REF,
                    "skill_name": "workflow-generate-generate",
                    "skill_ref": "skills/workflow-generate-generate/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_substep(
                        "generate", "generate"
                    ),
                    "issue_severity": "none",
                    "repair_strategy": "none",
                    "repair_target_agent_run_id": "none",
                    "repair_reason": "none",
                }),
            },
            response_payload={
                "agent_run_id": substep_arid,
                "backend": "claude",
                "started_at": "2026-05-09T08:00:00Z",
                **_spawn_response_payload(substep_arid),
            },
        )
        return substep_arid

    def _deactivate(self, repo_root: Path, arid: str) -> None:
        """Helper: record child return ack (Adv-20/Adv-30) and clear the
        active marker so Adv-14 guard accepts the subsequent record-timeout
        call. Reads the per-arid parent_return_token issued by record-launch."""
        from tools.orchestration_runtime import (
            record_child_return, deactivate_child_agent,
            _parent_return_token_path,
        )
        token = _parent_return_token_path(
            repo_root, "orch_to_001", arid
        ).read_text(encoding="utf-8").strip()
        record_child_return(
            repo_root=repo_root,
            orchestration_id="orch_to_001",
            agent_run_id=arid,
            return_token=token,
        )
        deactivate_child_agent(
            repo_root=repo_root,
            orchestration_id="orch_to_001",
            child_run_id=arid,
        )

    def test_record_timeout_appends_terminal_entry_and_cleans_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            tmp_dir = repo_root / "workspace" / "tmp" / arid
            tmp_dir.mkdir(parents=True, exist_ok=True)
            (tmp_dir / "scratch.py").write_text("print('x')\n", encoding="utf-8")
            (tmp_dir / "request.json").write_text("{}\n", encoding="utf-8")
            self.assertTrue(tmp_dir.exists())

            self._deactivate(repo_root, arid)
            result = record_timeout(
                repo_root=repo_root,
                orchestration_id="orch_to_001",
                agent_run_id=arid,
                reason="API stream idle timeout after 600s",
            )
            self.assertEqual(result.get("status"), "timeout")
            self.assertEqual(result.get("agent_run_id"), arid)
            self.assertEqual(result.get("timeout_reason"), "API stream idle timeout after 600s")

            runs_path = repo_root / "workspace" / "orchestrations" / "orch_to_001" / "agent_runs.jsonl"
            entries = [json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            timeout_entry = next((e for e in entries if e.get("agent_run_id") == arid), None)
            self.assertIsNotNone(timeout_entry)
            self.assertEqual(timeout_entry["status"], "timeout")
            self.assertIn("finished_at", timeout_entry)
            self.assertEqual(timeout_entry["agent_role"], "substep")
            self.assertEqual(timeout_entry["timeout_reason"], "API stream idle timeout after 600s")
            self.assertFalse(tmp_dir.exists(),
                             "workspace/tmp/<arid>/ must be cleaned by record-timeout")  # noqa: E501

    def test_record_timeout_rejects_missing_launch_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_to_002")
            with self.assertRaisesRegex(ValueError, "cannot find launch request"):
                record_timeout(
                    repo_root=repo_root,
                    orchestration_id="orch_to_002",
                    agent_run_id="never-launched",
                    reason="x",
                )

    def test_record_timeout_requires_non_empty_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "non-empty --reason"):
                record_timeout(
                    repo_root=repo_root,
                    orchestration_id="orch_to_003",
                    agent_run_id="any",
                    reason="   ",
                )

    def test_record_timeout_preserves_launched_context_id_and_parent(self) -> None:
        """Adv-3: context_id (per-run isolation context, distinct from
        agent_session_id under Codex's repair_strategy=reuse) and
        parent_agent_run_id must be carried over from the launch artifacts /
        session_run_index, not synthesised from agent_session_id."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)

            # Simulate a backend where context_id was explicitly recorded at
            # launch time as a separate identifier from agent_session_id
            # (e.g. Codex distinguishes session and per-run isolated context).
            # agent_session_id stays bound to the launch response (record-agent-run
            # rejects mismatch) but context_id can diverge.
            from tools.orchestration_runtime import (
                _read_session_run_index,
                _session_run_index_path,
            )
            doc = _read_session_run_index(repo_root, "orch_to_001")
            for entry in doc.get("entries", []):
                if entry.get("agent_run_id") == arid:
                    entry["context_id"] = "explicit-isolated-context-id-XYZ"
            (_session_run_index_path(repo_root, "orch_to_001")).write_text(
                json.dumps(doc), encoding="utf-8"
            )

            self._deactivate(repo_root, arid)
            result = record_timeout(
                repo_root=repo_root,
                orchestration_id="orch_to_001",
                agent_run_id=arid,
                reason="stream idle timeout",
            )
            self.assertEqual(result.get("context_id"), "explicit-isolated-context-id-XYZ",
                             "context_id from session_run_index must be preserved, NOT replaced by agent_session_id")
            self.assertEqual(result.get("parent_agent_run_id"), "orch_run_to_001",
                             "parent_agent_run_id from launch request must be preserved")

            # Verify the persisted entry on disk also has the right identity.
            runs_path = repo_root / "workspace" / "orchestrations" / "orch_to_001" / "agent_runs.jsonl"
            entries = [
                json.loads(line)
                for line in runs_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            timeout_entry = next((e for e in entries if e.get("agent_run_id") == arid), None)
            self.assertIsNotNone(timeout_entry)
            self.assertEqual(timeout_entry["context_id"], "explicit-isolated-context-id-XYZ")
            self.assertEqual(timeout_entry["parent_agent_run_id"], "orch_run_to_001")

    def test_record_agent_run_terminal_routes_through_ownership_guard(self) -> None:
        """Adv-7 end-to-end: the cleanup at end of record_agent_run must go
        through _cleanup_agent_tmp_root (with symlink refusal etc.), NOT a raw
        unconditional shutil.rmtree.

        Verified by setting up a symlinked tmp dir at workspace/tmp/<arid>/
        pointing OUTSIDE the repo. The helper refuses to follow symlinks. If
        the legacy raw rmtree were still in place (or were called instead of
        the helper), the symlink target's contents would be deleted.
        """
        import os
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as ext_tmp:
            repo_root = Path(repo_tmp)
            arid = self._setup_substep_launch(repo_root)
            workspace_tmp = repo_root / "workspace" / "tmp"
            workspace_tmp.mkdir(parents=True, exist_ok=True)
            # external dir lives OUTSIDE repo_root so that record-timeout's
            # workspace-diff check does not flag it as an unauthorized write.
            external = Path(ext_tmp) / "external_real_dir"
            external.mkdir()
            sentinel = external / "must_not_delete.txt"
            sentinel.write_text("preserve\n", encoding="utf-8")
            link_path = workspace_tmp / arid
            # record_launch pre-creates workspace/tmp/<arid>/ as a real dir
            # (see build_bwrap_profile). Replace it with a symlink so cleanup
            # is exercised against a symlink target.
            if link_path.is_dir() and not link_path.is_symlink():
                import shutil as _sh
                _sh.rmtree(link_path)
            try:
                os.symlink(external, link_path)
            except OSError:
                self.skipTest("symlink not supported on this filesystem")

            self._deactivate(repo_root, arid)
            record_timeout(
                repo_root=repo_root,
                orchestration_id="orch_to_001",
                agent_run_id=arid,
                reason="stream idle timeout",
            )

            self.assertTrue(
                sentinel.exists(),
                "symlink target contents must NOT be deleted — proves cleanup "
                "routed through _cleanup_agent_tmp_root (which refuses symlinks) "
                "rather than the legacy raw shutil.rmtree.",
            )

    def test_runs_jsonl_exclusive_lock_helper_is_cross_process_exclusive(self) -> None:
        """NEW-L1: Verify that the production wrapper
        `_runs_jsonl_exclusive_lock` (not `fcntl.flock` directly) honors
        cross-process exclusion. This catches regressions where a refactor
        opens the wrong lock path or releases early."""
        import multiprocessing as _mp
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_id = "orch_xprocess_helper"
            (repo_root / "workspace" / "orchestrations" / orch_id).mkdir(parents=True, exist_ok=True)
            barrier = _mp.Barrier(2)
            results = _mp.Queue()

            def _worker(rr: str, oid: str, b, q):
                import sys as _sys
                _sys.path.insert(0, "/home/seiya/work/met-dsl")
                from tools.orchestration_runtime import _runs_jsonl_exclusive_lock
                import time as _time
                from pathlib import Path as _P
                b.wait(timeout=10)
                # Try to acquire the helper non-blocking by racing both at
                # the same instant. The runtime helper uses LOCK_EX
                # (blocking), so we time-box one worker holding the lock
                # and assert the other observes ordered serialization.
                acquired_at = _time.monotonic()
                with _runs_jsonl_exclusive_lock(_P(rr), oid):
                    enter = _time.monotonic()
                    _time.sleep(0.3)
                    exit_t = _time.monotonic()
                q.put((enter - acquired_at, exit_t - enter))

            ctx = _mp.get_context("fork")
            p1 = ctx.Process(target=_worker, args=(str(repo_root), orch_id, barrier, results))
            p2 = ctx.Process(target=_worker, args=(str(repo_root), orch_id, barrier, results))
            p1.start(); p2.start()
            p1.join(timeout=15); p2.join(timeout=15)
            self.assertFalse(p1.is_alive() or p2.is_alive(), "workers must complete")
            outcomes = []
            while not results.empty():
                outcomes.append(results.get_nowait())
            outcomes.sort()
            # First acquirer enters near 0; second waits ~0.3s for first.
            # Both hold for ~0.3s. If the helper failed to serialize, both
            # would enter near 0.
            self.assertEqual(len(outcomes), 2)
            wait_a, hold_a = outcomes[0]
            wait_b, hold_b = outcomes[1]
            self.assertLess(wait_a, 0.15,
                            f"first acquirer should enter quickly (got {wait_a}s)")
            self.assertGreater(wait_b, 0.15,
                               f"second acquirer must wait for first to release "
                               f"(got {wait_b}s — helper likely not serializing)")

    def test_runs_jsonl_lock_is_cross_process_exclusive(self) -> None:
        """L5: the fcntl LOCK_EX on agent_runs.jsonl.lock is honored across
        OS processes (not just same-process semantics). Two child processes
        that both attempt to acquire LOCK_EX | LOCK_NB on the same lock
        file must observe contention — exactly one acquires at a time.
        Validates that the Adv-24 serialization story holds for real
        concurrent finalizers, not just intra-process locking."""
        import multiprocessing as _mp
        from tools.orchestration_runtime import _runs_jsonl_lock_path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_id = "orch_xprocess"
            (repo_root / "workspace" / "orchestrations" / orch_id).mkdir(parents=True, exist_ok=True)
            lock_path = _runs_jsonl_lock_path(repo_root, orch_id)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.touch()

            barrier = _mp.Barrier(2)
            results = _mp.Queue()

            def _worker(lp: str, b, q):
                import os as _os
                import fcntl as _fcntl
                fd = _os.open(lp, _os.O_WRONLY)
                try:
                    b.wait(timeout=10)
                    try:
                        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                        # Hold briefly then release.
                        import time as _time
                        _time.sleep(0.2)
                        try:
                            _fcntl.flock(fd, _fcntl.LOCK_UN)
                        except OSError:
                            pass
                        q.put("acquired")
                    except OSError:
                        q.put("contended")
                finally:
                    _os.close(fd)

            ctx = _mp.get_context("fork")
            p1 = ctx.Process(target=_worker, args=(str(lock_path), barrier, results))
            p2 = ctx.Process(target=_worker, args=(str(lock_path), barrier, results))
            p1.start(); p2.start()
            p1.join(timeout=15); p2.join(timeout=15)
            self.assertFalse(p1.is_alive() or p2.is_alive(), "workers must complete")
            outcomes = []
            while not results.empty():
                outcomes.append(results.get_nowait())
            outcomes.sort()
            # Exactly one acquires; one observes contention. Serialization holds.
            self.assertEqual(outcomes, ["acquired", "contended"],
                             f"cross-process LOCK_EX must serialize: got {outcomes}")

    def test_read_existing_run_ids_tolerates_partial_tail_when_writer_active(self) -> None:
        """Adv-21+Adv-40: while a writer holds the runs lock, a truncated
        trailing line is treated as in-flight append and tolerated.
        Exercised directly on `_read_existing_run_ids` (not record_timeout)
        because record_agent_run's own LOCK_EX would deadlock against a
        same-process holder."""
        import os as _os
        import fcntl as _fcntl
        from tools.orchestration_runtime import _read_existing_run_ids
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_dir = repo_root / "workspace" / "orchestrations" / "lockheld"
            orch_dir.mkdir(parents=True, exist_ok=True)
            runs_path = orch_dir / "agent_runs.jsonl"
            lock_path = orch_dir / "agent_runs.jsonl.lock"
            runs_path.write_text(
                json.dumps({"agent_run_id": "ok", "status": "pass"}) + "\n"
                + '{"agent_run_id": "in-flight", "status": "passi',
                encoding="utf-8",
            )

            fd = _os.open(str(lock_path), _os.O_WRONLY | _os.O_CREAT, 0o600)
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX)
                # Lock held → reader must tolerate the partial trailing line.
                ids = _read_existing_run_ids(runs_path)
                self.assertEqual(ids, {"ok"})
            finally:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
                finally:
                    _os.close(fd)

    def test_read_existing_run_ids_self_locked_raises_on_durable_corruption(self) -> None:
        """NEW-H1: when the caller already holds the runs lock, the
        writer-active probe would self-contend and falsely report 'in
        flight'. The caller_holds_lock=True flag forces every malformed
        line to surface as durable corruption — even though the lock IS
        held (by the caller themselves)."""
        from tools.orchestration_runtime import _read_existing_run_ids
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            runs = repo_root / "workspace" / "orchestrations" / "x" / "agent_runs.jsonl"
            runs.parent.mkdir(parents=True, exist_ok=True)
            lock_path = runs.parent / "agent_runs.jsonl.lock"
            lock_path.touch()
            runs.write_text(
                json.dumps({"agent_run_id": "a", "status": "pass"}) + "\n"
                + '{"agent_run_id": "broken-tail',
                encoding="utf-8",
            )

            # Without the flag (and with lock file present), the writer-active
            # probe in another process scenario would see no contention if
            # we don't hold the lock — but the spec requires `caller_holds_lock`
            # to force-raise. Here we simulate the locked re-check from
            # inside the lock holder, which must surface durable corruption.
            with self.assertRaisesRegex(RuntimeError, "no active writer detected"):
                _read_existing_run_ids(runs, caller_holds_lock=True)

    def test_read_existing_run_ids_raises_on_durable_corruption_when_no_writer(self) -> None:
        """Adv-40: a malformed trailing line WITHOUT an active writer is
        durable corruption — must surface as RuntimeError so the caller
        can quarantine the ledger rather than silently appending a
        duplicate terminal entry on top of corrupted state."""
        from tools.orchestration_runtime import _read_existing_run_ids
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            runs = repo_root / "workspace" / "orchestrations" / "x" / "agent_runs.jsonl"
            runs.parent.mkdir(parents=True, exist_ok=True)
            runs.write_text(
                json.dumps({"agent_run_id": "a", "status": "pass"}) + "\n"
                + '{"agent_run_id": "broken-tail',  # truncated
                encoding="utf-8",
            )
            # No lock file → no writer → tail is durable corruption.
            with self.assertRaisesRegex(RuntimeError, "no active writer detected"):
                _read_existing_run_ids(runs)

    def test_write_json_is_atomic_no_partial_observation(self) -> None:
        """Adv-27: _write_json must use temp-file + rename so concurrent
        readers cannot observe an empty / truncated state. Verified by
        snapshotting the file's content and existence at every write step
        via a patched os.replace that takes a pre-rename snapshot."""
        from tools.orchestration_runtime import _write_json
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "subdir" / "meta.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"version": 1}), encoding="utf-8")

            observed_during_replace: list[str] = []

            real_replace = os.replace

            def _spying_replace(src, dst):
                # At this moment src is the temp file with full new content;
                # dst (the canonical path) still holds the OLD content.
                # A reader landing here must NOT see empty/partial state.
                try:
                    observed_during_replace.append(Path(dst).read_text(encoding="utf-8"))
                except OSError:
                    observed_during_replace.append("__OSERROR__")
                return real_replace(src, dst)

            from unittest.mock import patch as _mp
            with _mp("tools.orchestration_runtime.os.replace", side_effect=_spying_replace):
                _write_json(target, {"version": 2, "deep": {"k": "v" * 1000}})

            self.assertEqual(len(observed_during_replace), 1)
            mid_write_view = observed_during_replace[0]
            self.assertEqual(
                mid_write_view, json.dumps({"version": 1}),
                "concurrent reader must observe the OLD complete content, never empty/partial",
            )
            # Final state is the new content.
            final = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(final["version"], 2)

    def test_record_agent_run_running_status_does_not_wipe_orch_tmp(self) -> None:
        """Adv-25: record_agent_run for status='running' (init_orchestration's
        seed entry pattern) must NOT wipe workspace/tmp/<orch_arid>/.
        Cleanup is only legitimate for terminal-status entries."""
        from tools.orchestration_runtime import (
            init_orchestration, record_agent_run,
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_running_seed")
            meta_path = (
                repo_root / "workspace" / "orchestrations" / "orch_running_seed"
                / "orchestration_meta.json"
            )
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            orch_arid = meta["orchestration_agent_run_id"]
            tmp_dir = repo_root / "workspace" / "tmp" / orch_arid
            self.assertTrue(tmp_dir.is_dir(), "init_orchestration must create the orch tmp dir")
            # Drop a sanctioned helper into the orch tmp; subsequent
            # record_agent_run(status='running') must not delete it.
            sentinel = tmp_dir / "subsequent_helper.py"
            sentinel.write_text("print('helper')\n", encoding="utf-8")

            # init_orchestration already writes one running entry; record a
            # second non-terminal entry to exercise the cleanup-gate path.
            # (Use a fresh arid to avoid the duplicate guard.)
            record_agent_run(
                repo_root=repo_root,
                orchestration_id="orch_running_seed",
                payload={
                    "agent_run_id": "second-running-arid",
                    "agent_role": "orchestration",
                    "agent_backend": "claude",
                    "status": "running",
                    "started_at": "2026-05-09T10:00:00Z",
                    "result_summary": "ongoing",
                },
            )
            self.assertTrue(
                sentinel.exists(),
                "non-terminal record_agent_run must NOT clean orch tmp scratch",
            )
            self.assertTrue(tmp_dir.is_dir())

    def _make_launch_request(self, repo_root: Path, orch_id: str, arid: str) -> None:
        d = repo_root / "workspace" / "orchestrations" / orch_id / "launches"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{arid}.request.json").write_text(
            json.dumps({"agent_run_id": arid}), encoding="utf-8"
        )

    def test_cleanup_committed_marker_not_written_when_cleanup_refused(self) -> None:
        """Adv-36: ownership/collision refusal must NOT yield a committed
        marker. The Adv-35 invariant requires committed = "actual cleanup
        succeeded (or already absent)" — a refused cleanup leaving leftover
        scratch must keep validator exemption alive."""
        from tools.orchestration_runtime import (
            _cleanup_agent_tmp_root, _cleanup_committed_marker_path,
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "intruder-arid"
            # Owner orch has the launch record; intruder does NOT.
            self._make_launch_request(repo_root, "orch_owner", arid)
            tmp_dir = repo_root / "workspace" / "tmp" / arid
            tmp_dir.mkdir(parents=True, exist_ok=True)
            (tmp_dir / "owner_data.txt").write_text("preserve\n", encoding="utf-8")

            # Caller is intruder — cleanup must REFUSE and return False.
            result = _cleanup_agent_tmp_root(
                repo_root, "orch_intruder", agent_run_id=arid,
            )
            self.assertFalse(result, "intruder cleanup must return False")
            self.assertTrue(tmp_dir.exists(), "owner data must survive")
            self.assertFalse(
                _cleanup_committed_marker_path(repo_root, "orch_intruder", arid).is_file(),
                "no committed marker should exist for refused cleanup",
            )

    def test_cleanup_returns_true_when_tmp_already_absent(self) -> None:
        """Adv-36 corollary: if the tmp dir was already absent (e.g. cleaned
        by a prior run), the function returns True so the committed marker
        can be written — there is nothing to clean."""
        from tools.orchestration_runtime import _cleanup_agent_tmp_root
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "already-clean-arid"
            self._make_launch_request(repo_root, "orch_x", arid)
            # Note: no workspace/tmp/<arid>/ created.
            result = _cleanup_agent_tmp_root(
                repo_root, "orch_x", agent_run_id=arid,
            )
            self.assertTrue(result, "absent tmp must return True (cleanup trivially complete)")

    def test_record_agent_run_success_path_does_not_pre_clean_tmp(self) -> None:
        """NEW-M2: cleanup must NOT happen during _validate_terminal_run_payload's
        success path. A crash between the early cleanup and the durable
        runs.jsonl append would leave the run with neither scratch nor
        terminal entry — defeating Adv-35 two-phase commit."""
        from unittest.mock import patch as _mp
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            self._deactivate(repo_root, arid)
            tmp_dir = repo_root / "workspace" / "tmp" / arid
            tmp_dir.mkdir(parents=True, exist_ok=True)
            (tmp_dir / "scratch.py").write_text("print('s')\n", encoding="utf-8")

            tmp_present_at_lock_release: list[bool] = []

            from tools import orchestration_runtime as _rt
            real_validate = _rt._validate_terminal_run_payload

            def _spying_validate(repo_root, orch_id, payload, *, caller_holds_lock=False):
                # Run the real validator (success path) and snapshot tmp
                # presence immediately after — before the runs.jsonl append.
                real_validate(
                    repo_root, orch_id, payload, caller_holds_lock=caller_holds_lock
                )
                tmp_present_at_lock_release.append(tmp_dir.exists())

            with _mp.object(_rt, "_validate_terminal_run_payload", _spying_validate):
                record_timeout(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    agent_run_id=arid,
                    reason="NEW-M2 deferred-cleanup test",
                )
            self.assertTrue(tmp_present_at_lock_release,
                            "spy must have observed validation completion")
            self.assertTrue(
                tmp_present_at_lock_release[-1],
                "tmp dir must STILL EXIST after _validate_terminal_run_payload — "
                "cleanup is deferred to post-append per NEW-M2",
            )
            # Final state: cleanup ran AFTER the locked append → tmp gone.
            self.assertFalse(tmp_dir.exists())

    def test_record_agent_run_partial_failure_preserves_tmp_for_recovery(self) -> None:
        """Adv-35 partial-failure invariant: if the terminal append succeeds
        but cleanup fails (or process dies between append and cleanup), the
        validator MUST keep workspace/tmp/<arid>/ exempt until the
        cleanup_committed marker is eventually written. Otherwise scratch
        needed for forensics / retry would be silently flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            self._deactivate(repo_root, arid)
            tmp_dir = repo_root / "workspace" / "tmp" / arid
            tmp_dir.mkdir(parents=True, exist_ok=True)
            recovery_helper = tmp_dir / "recovery.py"
            recovery_helper.write_text("print('preserve me')\n", encoding="utf-8")

            # Manually simulate the exact partial-failure window: terminal
            # entry written, cleanup NOT performed, no committed marker.
            runs_path = (
                repo_root / "workspace" / "orchestrations" / "orch_to_001"
                / "agent_runs.jsonl"
            )
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "agent_run_id": arid,
                    "agent_role": "substep",
                    "agent_backend": "claude",
                    "status": "timeout",
                    "started_at": "2026-05-09T08:00:00Z",
                    "finished_at": "2026-05-09T09:00:00Z",
                }) + "\n")
            # Crucially: no cleanup, no commit marker, tmp dir intact.
            self.assertTrue(recovery_helper.exists())

            # validate_workspace_root must NOT flag the recovery helper.
            from tools.validate_workspace_root import validate as _validate
            violations, _ = _validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(str(recovery_helper) in v for v in violations),
                f"Adv-35: cleanup_committed missing → exemption stays alive: {violations}",
            )

    def test_record_agent_run_terminal_writes_cleanup_committed_marker(self) -> None:
        """Adv-35 invariant: a successful terminal record_agent_run must
        produce all three durable artifacts:
          (1) terminal entry in agent_runs.jsonl,
          (2) workspace/tmp/<arid>/ cleaned,
          (3) cleanup_committed/<arid>.json marker written.
        The validator's exemption-revoke decision depends on (1)+(3); the
        marker's absence keeps exemption alive as cleanup-pending."""
        from tools.orchestration_runtime import _cleanup_committed_marker_path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            self._deactivate(repo_root, arid)
            tmp_dir = repo_root / "workspace" / "tmp" / arid
            tmp_dir.mkdir(parents=True, exist_ok=True)
            (tmp_dir / "scratch.py").write_text("print('s')\n", encoding="utf-8")

            record_timeout(
                repo_root=repo_root,
                orchestration_id="orch_to_001",
                agent_run_id=arid,
                reason="two-phase invariant test",
            )

            runs_path = (
                repo_root / "workspace" / "orchestrations" / "orch_to_001"
                / "agent_runs.jsonl"
            )
            entries = [
                json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            arid_entries = [e for e in entries if e.get("agent_run_id") == arid]
            self.assertEqual(len(arid_entries), 1, "exactly one terminal entry expected")
            self.assertEqual(arid_entries[0]["status"], "timeout")
            self.assertFalse(tmp_dir.exists(), "tmp dir must be cleaned")
            committed = _cleanup_committed_marker_path(repo_root, "orch_to_001", arid)
            self.assertTrue(
                committed.is_file(),
                "cleanup_committed marker must be written after cleanup",
            )

    def test_session_id_helper_failure_records_specific_fail_reason(self) -> None:
        """L-NEW-2: M-FOURTH-1 introduced a pre-set sandbox_fail_reason
        before _validate_response_agent_session_id so the helper's
        ValueError records as 'launch_response_session_id_invalid' rather
        than the generic 'terminal_payload_validation_error'. This regression
        test pins that behavior."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            # Corrupt the launch response so _validate_response_agent_session_id
            # raises (placeholder token).
            resp_path = (
                repo_root / "workspace" / "orchestrations" / "orch_to_001"
                / "launches" / f"{arid}.response.json"
            )
            resp = json.loads(resp_path.read_text(encoding="utf-8"))
            resp["agent_session_id"] = "<placeholder>"
            resp_path.write_text(json.dumps(resp), encoding="utf-8")

            with self.assertRaises(ValueError):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    payload={
                        "agent_run_id": arid,
                        "agent_role": "substep",
                        "agent_backend": "claude",
                        "status": "pass",
                        "started_at": "2026-05-09T08:00:00Z",
                        "finished_at": "2026-05-09T09:00:00Z",
                        "agent_session_id": arid,
                        "context_id": arid,
                        "context_isolated": True,
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "generate",
                        "substep": "generate",
                        "result_summary": "should fail session_id check",
                        "output_refs": [
                            "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                            "shallow-water2d_20260415_001/generate/gen_001/"
                            "generate_meta.json"
                        ],
                    },
                )
            invalid_path = (
                repo_root / "workspace" / "orchestrations" / "orch_to_001"
                / "agent_runs_invalid.jsonl"
            )
            entries = [
                json.loads(line) for line in invalid_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            arid_entries = [e for e in entries if e.get("agent_run_id") == arid]
            self.assertEqual(len(arid_entries), 1)
            self.assertEqual(
                arid_entries[0].get("fail_reason"), "launch_response_session_id_invalid",
                f"specific session_id reason expected, got {arid_entries[0]}",
            )

    def test_sandbox_violation_records_specific_fail_reason(self) -> None:
        """L-NEW-1: when record_agent_run raises due to a sandbox violation
        (sandbox_runtime != bwrap, etc.), the agent_runs_invalid.jsonl entry
        must record the specific reason rather than a generic
        'terminal_payload_validation_error'."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            # Fixture sets sandbox_runtime=bwrap; corrupt it to trigger the
            # sandbox_runtime_not_bwrap branch.
            resp_path = (
                repo_root / "workspace" / "orchestrations" / "orch_to_001"
                / "launches" / f"{arid}.response.json"
            )
            resp = json.loads(resp_path.read_text(encoding="utf-8"))
            resp["sandbox_runtime"] = "wrong-runtime"
            resp_path.write_text(json.dumps(resp), encoding="utf-8")

            with self.assertRaises(ValueError):
                record_agent_run(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    payload={
                        "agent_run_id": arid,
                        "agent_role": "substep",
                        "agent_backend": "claude",
                        "status": "pass",
                        "started_at": "2026-05-09T08:00:00Z",
                        "finished_at": "2026-05-09T09:00:00Z",
                        "agent_session_id": arid,
                        "context_id": arid,
                        "context_isolated": True,
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "generate",
                        "substep": "generate",
                        "result_summary": "should fail sandbox check",
                        "output_refs": [
                            "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                            "shallow-water2d_20260415_001/generate/gen_001/"
                            "generate_meta.json"
                        ],
                    },
                )
            invalid_path = (
                repo_root / "workspace" / "orchestrations" / "orch_to_001"
                / "agent_runs_invalid.jsonl"
            )
            self.assertTrue(invalid_path.is_file())
            entries = [
                json.loads(line) for line in invalid_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            arid_entries = [e for e in entries if e.get("agent_run_id") == arid]
            self.assertEqual(len(arid_entries), 1)
            self.assertEqual(
                arid_entries[0].get("fail_reason"), "sandbox_runtime_not_bwrap",
                f"specific sandbox reason expected, got {arid_entries[0]}",
            )
            self.assertIn("fail_message", arid_entries[0])

    def test_record_agent_run_locked_recheck_catches_concurrent_duplicate(self) -> None:
        """Adv-24: under fcntl serialization, even if the unlocked early
        duplicate check misses an in-flight write, the locked re-check
        immediately before the append must catch it and raise — preventing
        two terminal entries for the same arid."""
        from tools.orchestration_runtime import (
            _runs_jsonl_exclusive_lock, record_agent_run,
            _read_existing_run_ids,
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            self._deactivate(repo_root, arid)

            # Pre-seed agent_runs.jsonl with a terminal entry written DIRECTLY
            # (bypassing record_agent_run's early check) to simulate the case
            # where another process appended between the unlocked early read
            # and the locked re-check. The locked re-check must observe it.
            runs_path = (
                repo_root / "workspace" / "orchestrations" / "orch_to_001"
                / "agent_runs.jsonl"
            )

            # Hijack the early check to return an empty set (simulating the
            # race where the early read landed before the in-flight write).
            from unittest.mock import patch as _mp
            real_read = _read_existing_run_ids
            call_count = {"n": 0}

            def _patched(path, *, caller_holds_lock: bool = False):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return set()  # early check sees nothing
                # Locked re-check: insert the racing entry mid-flight
                with runs_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"agent_run_id": arid, "status": "pass"}) + "\n")
                return real_read(path, caller_holds_lock=caller_holds_lock)

            with _mp("tools.orchestration_runtime._read_existing_run_ids", side_effect=_patched):
                with self.assertRaisesRegex(ValueError, "race detected on locked re-check"):
                    record_agent_run(
                        repo_root=repo_root,
                        orchestration_id="orch_to_001",
                        payload={
                            "agent_run_id": arid,
                            "agent_role": "substep",
                            "agent_backend": "claude",
                            "status": "timeout",
                            "started_at": "2026-05-09T08:00:00Z",
                            "finished_at": "2026-05-09T09:00:00Z",
                            "agent_session_id": arid,
                            "context_id": arid,
                            "context_isolated": True,
                            "node_key": "problem/shallow_water2d@0.3.0",
                            "step": "generate",
                            "substep": "generate",
                            "result_summary": "timeout: race",
                        },
                    )
            # Verify only the racing-winner entry exists; race-loser did not append.
            entries = [
                json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            arid_entries = [e for e in entries if e.get("agent_run_id") == arid]
            self.assertEqual(len(arid_entries), 1,
                             f"locked re-check must prevent duplicate append; got {arid_entries}")
            self.assertEqual(arid_entries[0]["status"], "pass",
                             "the racing-winner's entry is preserved; the timeout race-loser is rejected")

    def test_record_existing_run_ids_raises_on_durable_corruption(self) -> None:
        """Adv-21: a malformed NON-TRAILING line is durable corruption and
        must raise a controlled RuntimeError (not silently skipped)."""
        from tools.orchestration_runtime import _read_existing_run_ids
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            runs = repo_root / "workspace" / "orchestrations" / "x" / "agent_runs.jsonl"
            runs.parent.mkdir(parents=True, exist_ok=True)
            runs.write_text(
                json.dumps({"agent_run_id": "a", "status": "pass"}) + "\n"
                + 'this line is broken\n'
                + json.dumps({"agent_run_id": "b", "status": "pass"}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "malformed non-trailing JSON"):
                _read_existing_run_ids(runs)

    def test_record_timeout_refuses_when_arid_already_in_agent_runs_jsonl(self) -> None:
        """Adv-14: a duplicate timeout call (e.g. retry / delayed callback)
        must not fabricate a second terminal record nor wipe scratch — refuse
        if any entry for arid already exists in agent_runs.jsonl."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            # Pre-seed the runs ledger with an arbitrary entry for the same arid
            # (the actual content doesn't matter — duplicate detection is on id).
            runs_path = (
                repo_root / "workspace" / "orchestrations" / "orch_to_001"
                / "agent_runs.jsonl"
            )
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"agent_run_id": arid, "status": "running"}) + "\n")

            with self.assertRaisesRegex(ValueError, "already has a durable entry"):
                record_timeout(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    agent_run_id=arid,
                    reason="duplicate timer",
                )

    def test_record_timeout_refuses_when_session_run_index_status_not_running(self) -> None:
        """Adv-14: refuse if session_run_index already shows the run as
        terminal — another finalization path won the race."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            from tools.orchestration_runtime import (
                _read_session_run_index, _session_run_index_path,
            )
            doc = _read_session_run_index(repo_root, "orch_to_001")
            for entry in doc.get("entries", []):
                if entry.get("agent_run_id") == arid:
                    entry["status"] = "pass"  # simulate prior terminal record
            (_session_run_index_path(repo_root, "orch_to_001")).write_text(
                json.dumps(doc), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "session_run_index.*not 'running'"):
                record_timeout(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    agent_run_id=arid,
                    reason="late callback",
                )

    def test_record_timeout_refuses_when_active_child_marker_still_set(self) -> None:
        """Adv-14 + Adv-16: refuse if backend-neutral per-arid marker
        active_children/<arid>.txt still exists. The orchestration must call
        deactivate-child first (signaling that the Agent tool actually
        returned) before record-timeout can run, regardless of backend."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            from tools.orchestration_runtime import (
                _active_child_agent_run_id_path,
                _active_child_marker_path,
            )
            # The fixture (claude backend) produces both markers.
            self.assertTrue(_active_child_agent_run_id_path(repo_root, "orch_to_001").is_file())
            self.assertTrue(_active_child_marker_path(repo_root, "orch_to_001", arid).is_file())

            with self.assertRaisesRegex(ValueError, "active-child marker .* still exists"):
                record_timeout(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    agent_run_id=arid,
                    reason="early fire",
                )

    def test_record_timeout_refuses_for_codex_backend_when_marker_present(self) -> None:
        """Adv-16: backend-neutral guard. Codex/Cursor never had the legacy
        single-file Claude marker, so the per-arid marker is the only
        liveness handshake protecting them from delayed/duplicated timeouts.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            # Force the launch response into a non-Claude backend so the
            # legacy single-file Claude marker is bypassed and only the new
            # per-arid marker remains as protection.
            resp_path = (
                repo_root / "workspace" / "orchestrations" / "orch_to_001"
                / "launches" / f"{arid}.response.json"
            )
            resp = json.loads(resp_path.read_text(encoding="utf-8"))
            resp["backend"] = "codex"
            resp_path.write_text(json.dumps(resp), encoding="utf-8")
            from tools.orchestration_runtime import (
                _active_child_agent_run_id_path,
                _active_child_marker_path,
            )
            # Remove Claude legacy marker so the per-arid marker is the sole
            # remaining defense (mirrors how a real Codex run would look —
            # record_launch never writes it for non-Claude backends).
            _active_child_agent_run_id_path(repo_root, "orch_to_001").unlink(missing_ok=True)
            self.assertTrue(_active_child_marker_path(repo_root, "orch_to_001", arid).is_file(),
                            "per-arid marker must exist for the protection to be testable")

            with self.assertRaisesRegex(ValueError, "active-child marker .* still exists"):
                record_timeout(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    agent_run_id=arid,
                    reason="codex delayed callback",
                )

    def test_deactivate_child_validates_before_unlinking_per_arid_marker(self) -> None:
        """Adv-18: a stray deactivate-child for a non-active arid must NOT
        clear the per-arid marker. Otherwise it could unlock record-timeout
        for a still-running child by removing its liveness guard."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            real_arid = self._setup_substep_launch(repo_root)
            # Mismatched deactivate (Claude single-marker mismatch) must
            # raise BEFORE clearing per-arid marker.
            from tools.orchestration_runtime import (
                deactivate_child_agent, _active_child_marker_path,
            )
            real_marker = _active_child_marker_path(repo_root, "orch_to_001", real_arid)
            self.assertTrue(real_marker.is_file())
            with self.assertRaisesRegex(ValueError, "active child run mismatch"):
                deactivate_child_agent(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    child_run_id="other-arid-typo",
                )
            # The legitimate live arid's marker survives the failed call.
            self.assertTrue(
                real_marker.is_file(),
                "stray deactivate-child must NOT remove the live child's marker",
            )
            # And record-timeout for the live arid is still blocked.
            with self.assertRaisesRegex(ValueError, "active-child marker .* still exists"):
                record_timeout(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    agent_run_id=real_arid,
                    reason="should be blocked",
                )

    def test_deactivate_child_idempotent_when_no_markers(self) -> None:
        """Adv-18: a second deactivate-child after both markers gone returns
        already_inactive=True without raising."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            from tools.orchestration_runtime import (
                deactivate_child_agent, record_child_return,
                _parent_return_token_path,
            )
            token = _parent_return_token_path(
                repo_root, "orch_to_001", arid
            ).read_text(encoding="utf-8").strip()
            record_child_return(
                repo_root=repo_root, orchestration_id="orch_to_001",
                agent_run_id=arid, return_token=token,
            )
            r1 = deactivate_child_agent(
                repo_root=repo_root, orchestration_id="orch_to_001", child_run_id=arid,
            )
            self.assertFalse(r1.get("already_inactive"))
            r2 = deactivate_child_agent(
                repo_root=repo_root, orchestration_id="orch_to_001", child_run_id=arid,
            )
            self.assertTrue(r2.get("already_inactive"))

    def test_deactivate_child_refuses_without_record_child_return(self) -> None:
        """Adv-20: deactivate-child must require an explicit record-child-return
        ack first. Without it, a misrouted deactivate-child cannot remove the
        per-arid active marker that protects record-timeout."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            from tools.orchestration_runtime import (
                deactivate_child_agent, _active_child_marker_path,
            )
            with self.assertRaisesRegex(ValueError, "child_returns/.* is missing"):
                deactivate_child_agent(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    child_run_id=arid,
                )
            # Marker survives the failed call.
            self.assertTrue(_active_child_marker_path(repo_root, "orch_to_001", arid).is_file())

    def test_record_child_return_refuses_for_unlaunched_arid(self) -> None:
        """Adv-20: record-child-return must prove the arid was actually
        launched (active_children marker exists). Otherwise a misrouted call
        could pre-issue an ack for an arbitrary arid and unlock a future
        deactivate-child for it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._setup_substep_launch(repo_root)
            from tools.orchestration_runtime import record_child_return
            with self.assertRaisesRegex(ValueError, "no active_children/.* marker"):
                record_child_return(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    agent_run_id="never-launched-arid",
                    return_token="any-token",
                )

    def test_record_child_return_refuses_with_wrong_token(self) -> None:
        """Adv-30: a caller that does not know the per-arid parent return
        token (stored at launches/<arid>.parent_return_token) must NOT be
        able to forge an ack."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            from tools.orchestration_runtime import record_child_return
            with self.assertRaisesRegex(ValueError, "return_token does not match"):
                record_child_return(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    agent_run_id=arid,
                    return_token="forged-token-attacker-guessed",
                )

    def test_deactivate_child_rejects_tampered_ack_file(self) -> None:
        """Adv-30: even if the ack file exists, deactivate-child must verify
        the embedded token matches the parent_return_token. A tampered ack
        (without the secret token) cannot pass."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            from tools.orchestration_runtime import (
                _child_return_marker_path, deactivate_child_agent,
            )
            # Manually fabricate an ack file with NO valid token.
            ack = _child_return_marker_path(repo_root, "orch_to_001", arid)
            ack.parent.mkdir(parents=True, exist_ok=True)
            ack.write_text(
                json.dumps({"agent_run_id": arid, "return_token": "fake"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "valid parent return token"):
                deactivate_child_agent(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    child_run_id=arid,
                )

    def test_deactivate_child_refuses_when_parent_return_token_missing(self) -> None:
        """Adv-30 token-missing invariant: if the ack file exists but
        launches/<arid>.parent_return_token is absent, deactivate-child must
        refuse to clear liveness markers. record_child_return requires both
        files together, so the token's absence indicates corruption or
        tampering — silently bypassing token verification would let an
        attacker forge an ack by deleting the token first."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            from tools.orchestration_runtime import (
                _child_return_marker_path, _parent_return_token_path,
                deactivate_child_agent, record_child_return,
            )
            # Legitimate record_child_return path first (uses real token).
            token_path = _parent_return_token_path(
                repo_root, "orch_to_001", arid
            )
            real_token = token_path.read_text(encoding="utf-8").strip()
            record_child_return(
                repo_root=repo_root,
                orchestration_id="orch_to_001",
                agent_run_id=arid,
                return_token=real_token,
            )
            ack = _child_return_marker_path(repo_root, "orch_to_001", arid)
            self.assertTrue(ack.is_file())
            # Now simulate corruption: delete the parent_return_token sidecar
            # while the ack still contains a (formerly valid) token.
            token_path.unlink()
            self.assertFalse(token_path.is_file())
            with self.assertRaisesRegex(
                ValueError, "parent_return_token is missing"
            ):
                deactivate_child_agent(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    child_run_id=arid,
                )
            # Markers must remain so an operator can investigate / re-issue.
            self.assertTrue(ack.is_file())

    def test_forced_record_timeout_preserves_markers_when_record_agent_run_fails(self) -> None:
        """Adv-37: in the forced bypass path, markers must NOT be cleared
        until record_agent_run successfully commits the terminal entry.
        Otherwise a record_agent_run failure leaves the run without markers
        AND without terminal record — wedged forever, no retry possible."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            from tools.orchestration_runtime import (
                _active_child_marker_path, _active_child_agent_run_id_path,
            )
            self.assertTrue(_active_child_marker_path(repo_root, "orch_to_001", arid).is_file())
            self.assertTrue(_active_child_agent_run_id_path(repo_root, "orch_to_001").is_file())

            # Inject a duplicate entry into agent_runs.jsonl so record_agent_run's
            # locked re-check raises ValueError mid-flow.
            runs_path = (
                repo_root / "workspace" / "orchestrations" / "orch_to_001"
                / "agent_runs.jsonl"
            )
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"agent_run_id": arid, "status": "running"}) + "\n")

            with self.assertRaises(ValueError):
                record_timeout(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    agent_run_id=arid,
                    reason="forced timeout that will fail mid-commit",
                    force_reason="operator override",
                )
            # Markers MUST survive the failed call so retry/recovery is possible.
            self.assertTrue(
                _active_child_marker_path(repo_root, "orch_to_001", arid).is_file(),
                "active_children marker must NOT be unlinked before terminal commit",
            )
            self.assertTrue(
                _active_child_agent_run_id_path(repo_root, "orch_to_001").is_file(),
                "legacy Claude marker must NOT be unlinked before terminal commit",
            )

    def test_record_timeout_force_reason_bypasses_marker_for_wedged_child(self) -> None:
        """Adv-26: when a child wedges before parent observes any return,
        record-child-return is unreachable and the normal flow deadlocks. The
        --force-reason escape clears the markers and finalizes the run, with
        forced=True + forced_reason recorded in the payload for audit."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            # Intentionally do NOT call _deactivate — simulate a wedged child
            # where deactivate-child is unreachable.
            from tools.orchestration_runtime import (
                _active_child_marker_path, _active_child_agent_run_id_path,
            )
            self.assertTrue(_active_child_marker_path(repo_root, "orch_to_001", arid).is_file())

            # Without --force-reason: blocked.
            with self.assertRaisesRegex(ValueError, "active-child marker"):
                record_timeout(
                    repo_root=repo_root,
                    orchestration_id="orch_to_001",
                    agent_run_id=arid,
                    reason="wedged child",
                )

            # With --force-reason: succeeds.
            result = record_timeout(
                repo_root=repo_root,
                orchestration_id="orch_to_001",
                agent_run_id=arid,
                reason="API stream wedged after 30 minutes",
                force_reason="operator override: child PID killed externally",
            )
            self.assertEqual(result.get("status"), "timeout")
            self.assertTrue(result.get("forced"))
            self.assertIn("operator override", result.get("forced_reason", ""))
            self.assertIn("FORCED", result.get("timeout_reason", ""))
            # Markers cleared by the forced bypass.
            self.assertFalse(_active_child_marker_path(repo_root, "orch_to_001", arid).is_file())
            self.assertFalse(_active_child_agent_run_id_path(repo_root, "orch_to_001").is_file())

    def test_record_timeout_succeeds_after_deactivate_child(self) -> None:
        """Adv-14 happy path: record-child-return + deactivate-child clears
        the marker; then record-timeout proceeds and finalizes the run."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            from tools.orchestration_runtime import (
                deactivate_child_agent, record_child_return,
                _parent_return_token_path,
            )
            token = _parent_return_token_path(
                repo_root, "orch_to_001", arid
            ).read_text(encoding="utf-8").strip()
            record_child_return(
                repo_root=repo_root,
                orchestration_id="orch_to_001",
                agent_run_id=arid,
                return_token=token,
            )
            deactivate_child_agent(
                repo_root=repo_root,
                orchestration_id="orch_to_001",
                child_run_id=arid,
            )
            result = record_timeout(
                repo_root=repo_root,
                orchestration_id="orch_to_001",
                agent_run_id=arid,
                reason="legitimate timeout after deactivate",
            )
            self.assertEqual(result.get("status"), "timeout")

    def test_record_timeout_cli_dispatch(self) -> None:
        """The `record-timeout` subcommand is wired into main()."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = self._setup_substep_launch(repo_root)
            self._deactivate(repo_root, arid)
            argv = [
                "record-timeout",
                "--repo-root", str(repo_root),
                "--orchestration-id", "orch_to_001",
                "--agent-run-id", arid,
                "--reason", "stream idle timeout",
            ]
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = main(argv)
            self.assertEqual(rc, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload.get("status"), "timeout")
            self.assertEqual(payload.get("agent_run_id"), arid)


if __name__ == "__main__":
    unittest.main()
