#!/usr/bin/env python3
"""Regression tests for Codex orchestration runtime helpers."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mcp_servers.build_runtime_server import tool_compile_project

from tools.codex_orchestration_runtime import (
    TERMINAL_STATUSES,
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
workflow_mode: dev
plan_ref: {_FIX_PLAN_REF}
pipeline_ref: {_FIX_PIPE_REF}
dependency_ref: {_FIX_DEP_REF}
skill_name: workflow-{step}
skill_ref: skills/workflow-{step}/SKILL.md
skill_must_read_refs: {_fixture_skill_must_read_refs_step(step)}
issue_severity: none
repair_strategy: none
repair_target_agent_run_id: none
repair_reason: none

必須要件:
- あなたは phase artifacts を直接生成する担当である。
- この step は標準 substep を持たない phase である。自身で step 契約を完了させること。
- 起動直後に `skill_ref` を読み、`skill_must_read_refs` と矛盾しない契約で実行すること。
- 要求定義と判定規則は `docs/` と `spec/` と `skill_must_read_refs` に含まれる当該試行 artifact だけから解釈すること。`tools/` 配下の実装、検証 `script`、test code、validator code から rule を抽出してはならない。
- `orchestration-read` は `python3 tools/codex_orchestration_runtime.py run-gate --gate orchestration_read --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '{{"read_path":"..."}}'` を唯一の経路として実行し、`orchestration-read` 直呼びを禁止する。
- `apply_patch` は `python3 tools/codex_orchestration_runtime.py guarded-apply-patch --repo-root <repo_root> --orchestration-id <orchestration_id> --actor-role step --agent-run-id <agent_run_id> --paths-json '["..."]' --patch-text '<patch_text>' --capability-token <capability_token>` を唯一の経路として実行し、拒否時は編集を停止すること。
- 書き込みは `apply_patch_writes` gate を通過した `guarded-apply-patch` 経由に限定し、shell redirection・直接 `write_text`・任意コマンドによる file write を禁止する。
- `skill_name` と `skill_ref` が未指定の場合は fail で停止すること。
- 入力不足時は推測補完せず fail で停止すること。
- `workflow_mode=dev` の場合、verify 系判定で `issue_severity=major|critical` を検出した時点で fail 停止すること。
- `workflow_mode=dev` で fail した場合、`failure_analysis.json` 生成に必要な根拠（失敗理由、関連 output_refs、主要ログ要約）を返答へ含めること。
- `Plan` の場合、直下依存 `node` の `direct dependency plan readiness` を満たさない限り開始してはならない。
- `Generate` / `Build` / `Execute` / `Judge` の場合、直下依存 `node` の `direct dependency execution readiness` を満たさない限り開始してはならない。
- 直下依存 `node` が未完了でも、依存先 code を自身の `src/` へ内包して代替してはならない。
- 完了後は required_outputs と failed_substeps と substep_agent_run_ids を親へ返すこと。
- 完了返答には `launch_reply` として、実施内容と判定結果を平文で含めること。"""


def _substep_launch_prompt(node_key: str, step: str, substep: str, agent_run_id: str) -> str:
    return f"""あなたは substep agent である。
対象 node_key: {node_key}
対象 step: {step}
対象 substep: {substep}
orchestration_id: orch_001
agent_run_id: {agent_run_id}
parent_agent_run_id: orch_run_001
workflow_mode: dev
plan_ref: {_FIX_PLAN_REF}
pipeline_ref: {_FIX_PIPE_REF}
dependency_ref: {_FIX_DEP_REF}
skill_name: workflow-{step}-{substep}
skill_ref: skills/workflow-{step}-{substep}/SKILL.md
skill_must_read_refs: {_fixture_skill_must_read_refs_substep(step, substep)}
issue_severity: none
repair_strategy: none
repair_target_agent_run_id: none
repair_reason: none

必須要件:
- 契約された入力だけを読むこと。
- 契約された artifacts だけを書くこと。
- expected output と保存先を守ること。
- 起動直後に `skill_ref` を読み、`skill_must_read_refs` と矛盾しない契約で実行すること。
- 要求定義と判定規則は `docs/` と `spec/` と `skill_must_read_refs` に含まれる当該試行 artifact だけから解釈すること。`tools/` 配下の実装、検証 `script`、test code、validator code から rule を抽出してはならない。
- `orchestration-read` は `python3 tools/codex_orchestration_runtime.py run-gate --gate orchestration_read --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '{{"read_path":"..."}}'` を唯一の経路として実行し、`orchestration-read` 直呼びを禁止する。
- `apply_patch` は `python3 tools/codex_orchestration_runtime.py guarded-apply-patch --repo-root <repo_root> --orchestration-id <orchestration_id> --actor-role substep --agent-run-id <agent_run_id> --paths-json '["..."]' --patch-text '<patch_text>' --capability-token <capability_token>` を唯一の経路として実行し、拒否時は編集を停止すること。
- 書き込みは `apply_patch_writes` gate を通過した `guarded-apply-patch` 経由に限定し、shell redirection・直接 `write_text`・任意コマンドによる file write を禁止する。
- `skill_name` と `skill_ref` が未指定の場合は fail で停止すること。
- 入力不足時は推測補完せず fail で停止すること。
- `workflow_mode=dev` の場合、verify 系判定で `issue_severity=major|critical` を検出した時点で fail 停止すること。
- `workflow_mode=dev` で fail した場合、`failure_analysis.json` 生成に必要な根拠（失敗理由、関連 output_refs、主要ログ要約）を返答へ含めること。
- `Plan` の substep は、直下依存 `node` の `direct dependency plan readiness` を満たさない限り開始してはならない。
- `Generate` / `Build` / `Execute` / `Judge` の substep は、直下依存 `node` の `direct dependency execution readiness` を満たさない限り開始してはならない。
- 直下依存 `node` が未完了でも、依存先 code を対象 `node` の `src/` へ内包して代替してはならない。
- `repair_strategy=reuse` の場合は、`repair_target_agent_run_id` の出力との差分修正に限定すること。
- `repair_strategy=restart` の場合は、過去出力を流用せず契約入力から再生成すること。
- 完了時は artifact 参照と status を `orchestration agent` へ返すこと。
- 完了返答には `launch_reply` として、実施内容と判定結果を平文で含めること。"""


def _spawn_response_payload(session_id: str) -> dict[str, object]:
    return {
        "agent_session_id": session_id,
        "accepted": True,
        "launch_reply": f"accepted: {session_id}",
    }


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
        self._old_live_preflight = os.environ.get("CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT")
        os.environ["CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT"] = "0"

    def tearDown(self) -> None:
        if self._old_live_preflight is None:
            os.environ.pop("CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT", None)
        else:
            os.environ["CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT"] = self._old_live_preflight

    def test_terminal_statuses_do_not_include_fail_closed(self) -> None:
        self.assertEqual(TERMINAL_STATUSES, {"pass", "fail", "blocked", "timeout", "cancel"})

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
        self.assertIn("guarded-apply-patch", prompt)

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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
            self.assertEqual(prompt_path.read_text(encoding="utf-8"), f"{full_prompt}\n")

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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                )
                + "\n",
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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
            prompt = render_launch_prompt_text(prepared).replace(
                "- 書き込みは `apply_patch_writes` gate を通過した `guarded-apply-patch` 経由に限定し、shell redirection・直接 `write_text`・任意コマンドによる file write を禁止する。\n",
                "",
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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
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

    def test_record_agent_run_accepts_step_terminal_when_gate_changed_paths_uses_directory_prefix(self) -> None:
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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "plan_ref": _FIX_PLAN_REF,
                    "pipeline_ref": _FIX_PIPE_REF,
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": _fixture_skill_must_read_refs_step("build"),
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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

    def test_record_agent_run_rejects_tune_substep_terminal_write_outside_tune_root(self) -> None:
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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
            launch_response_path.write_text(
                json.dumps(
                    {
                        "agent_run_id": "substep_run_tune_001",
                        **_spawn_response_payload("sess_substep_tune_001"),
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                        "feature_states": {"multi_agent": True},
                        "checks": [
                            {"name": "multi_agent_enabled", "pass": True},
                            {"name": "codex_hooks_enabled", "pass": True},
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
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                        "feature_states": {"multi_agent": True, "codex_hooks": True},
                        "checks": [
                            {"name": "multi_agent_enabled", "pass": True},
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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
                },
            )
            os.environ["CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT"] = "1"
            with patch("tools.codex_orchestration_runtime.probe_execution_platform") as probe_mock:
                probe_mock.return_value = {
                    "checked_at": "2026-04-15T12:00:00Z",
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                "feature_states": {"multi_agent": True, "codex_hooks": True},
                "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
                "feature_states": {"multi_agent": True, "codex_hooks": True},
                "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                "feature_states": {"multi_agent": True, "codex_hooks": True},
                "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
        "can_launch_step_agents": True,
        "can_launch_substep_agents": True,
        "session_policy": {
            "allow_step_agent_launch": True,
            "allow_substep_agent_launch": True,
        },
        "session_policy_launchable": True,
        "feature_states": {"multi_agent": True, "codex_hooks": True},
        "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
    }
    base.update(extra)
    return base


class OrchestrationMetaAndJudgeHookTests(unittest.TestCase):
    def test_write_preflight_persists_parallel_nodes_meta_without_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            orch = "preflight_only_orch"
            with patch.dict(os.environ, {"CODEX_ALLOW_PARALLEL_NODES": "1"}):
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
            with patch.dict(os.environ, {"CODEX_ALLOW_PARALLEL_NODES": "true"}):
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

    def test_pre_orchestration_start_logs_persisted_parallel_nodes_explicit(self) -> None:
        """setdefault で保持した値と hook 返却・ログ用 detail が一致すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            orch = "orch_parallel_audit"
            with patch.dict(os.environ, {"CODEX_ALLOW_PARALLEL_NODES": "1"}):
                out1 = pre_orchestration_start(repo, orch, event="init")
            self.assertTrue(out1["parallel_nodes_explicit"])
            meta_path = repo / "workspace" / "orchestrations" / orch / "orchestration_meta.json"
            meta1 = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertTrue(meta1.get("parallel_nodes_explicit"))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CODEX_ALLOW_PARALLEL_NODES", None)
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
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
                    "apply_patch_writes",
                ],
            )
            cap_path = orch / "capabilities" / "substep_p1_001.json"
            self.assertTrue(cap_path.exists())
            cap = json.loads(cap_path.read_text(encoding="utf-8"))
            self.assertEqual(cap.get("agent_run_id"), "substep_p1_001")
            self.assertEqual(cap.get("step"), "plan")
            self.assertTrue(isinstance(cap.get("capability_token"), str) and cap["capability_token"])
            self.assertIn(_FIX_PLAN_REF.rstrip("/") + "/", cap.get("write_roots", []))
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
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

    def test_phase1_resume_missing_phase_state_infers_preflight_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="orch_p1m")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="orch_p1m",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "dependency_ref": _FIX_DEP_REF,
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "",
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "session_policy": {"allow_substep_agent_launch": False},
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                        "feature_states": {"multi_agent": True, "codex_hooks": True},
                        "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "session_policy": {
                        "allow_step_agent_launch": True,
                        "allow_substep_agent_launch": True,
                    },
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "session_policy": {"allow_substep_agent_launch": False},
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(repo_root=repo_root, orchestration_id="g3")
            write_preflight(
                repo_root=repo_root,
                orchestration_id="g3",
                payload={
                    "status": "pass",
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
                },
            )
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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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
                        "agent_run_id": "plan_sub_1",
                        "node_key": "problem/shallow_water2d@0.3.0",
                        "step": "plan",
                        "substep": "generate",
                        "orchestration_id": "g6",
                        "parent_agent_run_id": "orch_g6",
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
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
                "feature_states": {"multi_agent": True, "codex_hooks": True},
                "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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

    def test_run_gate_apply_patch_writes_uses_inline_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            out = run_gate(
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
            self.assertEqual(out.get("violations"), [])
            result = out.get("result", {})
            self.assertTrue(result.get("allowed"))
            self.assertEqual(
                result.get("checked_paths"),
                [f"{_FIX_PIPE_REF}/build/new_artifact.json"],
            )

    def test_guarded_apply_patch_calls_git_apply_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            patch_text = "\n".join(
                [
                    "diff --git a/workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/new_artifact.json b/workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/new_artifact.json",
                    "--- a/workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/new_artifact.json",
                    "+++ b/workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/build/new_artifact.json",
                    "@@ -0,0 +1 @@",
                    "+{\"ok\": true}",
                    "",
                ]
            )
            with patch("tools.codex_orchestration_runtime.subprocess.run") as run_mock:
                run_mock.return_value = _FakeCompletedProcess(returncode=0, stdout="", stderr="")
                out = guarded_apply_patch(
                    repo_root,
                    orchestration_id="rg1",
                    actor_role="step",
                    agent_run_id="build_child_rg1",
                    changed_paths=[f"{_FIX_PIPE_REF}/build/new_artifact.json"],
                    patch_text=patch_text,
                    capability_token=token,
                )
            self.assertTrue(out.get("applied"))
            self.assertEqual(
                out.get("gate_result_ref"),
                "workspace/orchestrations/rg1/gates/build_child_rg1/apply_patch_writes.json",
            )
            self.assertEqual(run_mock.call_count, 1)

    def test_guarded_apply_patch_rejects_patch_outside_declared_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            patch_text = "\n".join(
                [
                    "diff --git a/workspace/orchestrations/rg1/orchestration_meta.json b/workspace/orchestrations/rg1/orchestration_meta.json",
                    "--- a/workspace/orchestrations/rg1/orchestration_meta.json",
                    "+++ b/workspace/orchestrations/rg1/orchestration_meta.json",
                    "@@ -1 +1 @@",
                    "-{}",
                    "+{\"status\": \"running\"}",
                    "",
                ]
            )
            with patch("tools.codex_orchestration_runtime.subprocess.run") as run_mock:
                with self.assertRaisesRegex(RuntimeError, "not covered by changed_paths"):
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

    def test_main_guarded_apply_patch_returns_nonzero_on_gate_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token = self._setup_run_gate_fixture(repo_root)
            patch_text = "\n".join(
                [
                    "diff --git a/workspace/orchestrations/rg1/orchestration_meta.json b/workspace/orchestrations/rg1/orchestration_meta.json",
                    "--- a/workspace/orchestrations/rg1/orchestration_meta.json",
                    "+++ b/workspace/orchestrations/rg1/orchestration_meta.json",
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
            self.assertIn("not covered by changed_paths", err.getvalue())

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
                    "can_launch_step_agents": True,
                    "can_launch_substep_agents": True,
                    "feature_states": {"multi_agent": True, "codex_hooks": True},
                    "checks": [{"name": "multi_agent_enabled", "pass": True}, {"name": "codex_hooks_enabled", "pass": True}],
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


if __name__ == "__main__":
    unittest.main()
