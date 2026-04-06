#!/usr/bin/env python3
"""Regression tests for Codex orchestration runtime helpers."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.codex_orchestration_runtime import (
    build_launch_prompt_text,
    init_orchestration,
    parse_feature_list,
    probe_execution_platform,
    prepare_launch_request_payload,
    probe_codex_cli,
    record_agent_run,
    record_launch,
    render_launch_prompt_text,
    update_orchestration_status,
    write_preflight,
    write_step_result,
)


def _step_launch_prompt(node_key: str, step: str, agent_run_id: str) -> str:
    return f"""あなたは step agent である。
対象 node_key: {node_key}
対象 step: {step}
orchestration_id: orch_001
agent_run_id: {agent_run_id}
parent_agent_run_id: orch_run_001
plan_ref: workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001
pipeline_ref: workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001
dependency_ref: workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml
skill_name: workflow-{step}
skill_ref: skills/workflow-{step}/SKILL.md
skill_must_read_refs: skills/workflow-{step}/SKILL.md,docs/WORKFLOW.md,docs/ORCHESTRATION.md
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
plan_ref: workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001
pipeline_ref: workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001
dependency_ref: workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml
skill_name: workflow-{step}-{substep}
skill_ref: skills/workflow-{step}-{substep}/SKILL.md
skill_must_read_refs: skills/workflow-{step}-{substep}/SKILL.md,docs/WORKFLOW.md,docs/ORCHESTRATION.md
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
                return _FakeCompletedProcess(0, stdout="claude 1.0.0\n")
            if args[0] == "claude" and args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(0, stdout="multi_agent experimental false\n")
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

    def test_prepare_launch_request_payload_fills_verify_defaults(self) -> None:
        payload = prepare_launch_request_payload(
            {
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "plan",
                "substep": "verify",
                "orchestration_id": "orch_001",
                "agent_run_id": "substep_run_plan_verify_001",
                "parent_agent_run_id": "orch_run_001",
                "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
            }
        )
        self.assertEqual(payload["skill_name"], "workflow-plan-verify")
        self.assertEqual(payload["skill_ref"], "skills/workflow-plan-verify/SKILL.md")
        self.assertEqual(payload["issue_severity"], "none")
        self.assertIn("docs/WORKFLOW.md", payload["skill_must_read_refs"])
        self.assertIn("docs/ORCHESTRATION.md", payload["skill_must_read_refs"])
        self.assertIn("skills/workflow-plan-verify/SKILL.md", payload["skill_must_read_refs"])
        self.assertIn(
            "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/derived_contract.json",
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
                "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                "skill_name": "workflow-build",
                "skill_ref": "skills/workflow-build/SKILL.md",
                "skill_must_read_refs": "skills/workflow-build/SKILL.md,docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                dependency_ref="workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                    "agent_backend": "openai_responses",
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
                        "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/case.resolved.yaml"
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
                    "agent_backend": "openai_responses",
                    "agent_model": "gpt-5-codex",
                    "context_id": "ctx_step_build_001",
                    "agent_session_id": "sess_step_build_001",
                    "started_at": "2026-03-11T00:00:20Z",
                    "finished_at": "2026-03-11T00:01:10Z",
                    "output_refs": [
                        "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001/build/build_001/bin/simulate"
                    ],
                },
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
                        "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/case.resolved.yaml"
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
                    "required_outputs": [
                        "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001/build/build_001/bin/simulate"
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                        "skill_name": "workflow-build",
                        "skill_ref": "skills/workflow-build/SKILL.md",
                        "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                    "generation_id": "gen_001",
                    "skill_name": "workflow-generate-verify",
                    "skill_ref": "skills/workflow-generate-verify/SKILL.md",
                    "skill_must_read_refs": "docs/WORKFLOW.md,workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001/generate/gen_001/generate_meta.json",
                },
                response_payload=_spawn_response_payload("sess_substep_run_generate_verify_001"),
            )
            request_payload = json.loads(
                (repo_root / launch_refs["launch_request_ref"]).read_text(encoding="utf-8")
            )
            self.assertIn(
                "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/case.resolved.yaml",
                request_payload["skill_must_read_refs"],
            )
            self.assertIn(
                "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/derived_contract.json",
                request_payload["skill_must_read_refs"],
            )
            self.assertIn(
                "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001/lineage.json",
                request_payload["skill_must_read_refs"],
            )
            self.assertIn(
                "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001/generate/gen_001/generate_meta.json",
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
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                        "skill_name": "workflow-plan-generate",
                        "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                        "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                "problem__shallow_water2d__0.3.0_pl001/generate/gen_001/generate_meta.json"
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
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                        "pipeline_ref": bad_pipeline,
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                        "skill_name": "workflow-generate-generate",
                        "skill_ref": "skills/workflow-generate-generate/SKILL.md",
                        "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                        "skill_name": "workflow-generate-verify",
                        "skill_ref": "skills/workflow-generate-verify/SKILL.md",
                        "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                },
                response_payload=_spawn_response_payload("sess_substep_run_plan_verify_001"),
            )
            request_path = repo_root / launch_refs["launch_request_ref"]
            prompt_path = repo_root / launch_refs["launch_prompt_ref"]
            request_payload = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request_payload["skill_name"], "workflow-plan-verify")
            self.assertEqual(request_payload["skill_ref"], "skills/workflow-plan-verify/SKILL.md")
            self.assertIn(
                "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/derived_contract.json",
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
            payload = {
                "node_key": "problem/shallow_water2d@0.3.0",
                "step": "plan",
                "substep": "verify",
                "orchestration_id": "orch_001",
                "agent_run_id": "substep_run_plan_verify_001",
                "parent_agent_run_id": "orch_run_001",
                "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                "skill_name": "workflow-plan-verify",
                "skill_ref": "skills/workflow-plan-verify/SKILL.md",
                "skill_must_read_refs": ",".join(
                    [
                        "docs/WORKFLOW.md",
                        "docs/ORCHESTRATION.md",
                        "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/case.resolved.yaml",
                        "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/algorithm.resolved.yaml",
                        "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/impl.resolved.yaml",
                        "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                        "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/derived_contract.json",
                    ]
                ),
                "issue_severity": "none",
                "repair_strategy": "none",
                "repair_target_agent_run_id": "none",
                "repair_reason": "none",
            }
            prompt = build_launch_prompt_text(payload).replace(
                "skill_name: workflow-plan-verify",
                "skill_name: workflow-plan-generate",
            ) + "\n\n必須要件:\n- 契約された substep を完了すること。\n"
            with self.assertRaisesRegex(ValueError, "template field values"):
                record_launch(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    parent_agent_run_id="orch_run_001",
                    child_agent_run_id="substep_run_plan_verify_001",
                    request_payload={**payload, "launch_prompt_full": prompt},
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                    "agent_backend": "openai_responses",
                    "agent_model": "gpt-5-codex",
                    "context_id": "ctx_substep_plan_generate_001",
                    "agent_session_id": "sess_substep_plan_generate_001",
                    "output_refs": ["workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/impl.resolved.yaml"],
                },
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
                            "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/case.resolved.yaml"
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
                    "agent_backend": "openai_responses",
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
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                        "skill_name": "workflow-build",
                        "skill_ref": "skills/workflow-build/SKILL.md",
                        "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                        "agent_backend": "openai_responses",
                        "agent_model": "gpt-5-codex",
                        "context_id": "ctx_step_build_001",
                        "agent_session_id": "sess_step_build_999",
                        "output_refs": [
                            "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001/build/build_001/bin/simulate"
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
                "agent_backend": "openai_responses",
                "agent_model": "gpt-5-codex",
                "context_id": "ctx_orch_run_001",
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
                        "agent_backend": "openai_responses",
                        "agent_model": "gpt-5-codex",
                        "context_id": "ctx_step_plan_001",
                        "agent_session_id": "sess_step_plan_001",
                        "output_refs": ["workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001/build/build_001/bin/simulate"],
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
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                        "skill_name": "workflow-plan-generate",
                        "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                        "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                        "skill_name": "workflow-build",
                        "skill_ref": "skills/workflow-build/SKILL.md",
                        "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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
                        "agent_backend": "openai_responses",
                        "agent_model": "gpt-5-codex",
                        "context_id": "ctx_step_build_001",
                        "agent_session_id": "sess_step_build_001",
                        "output_refs": ["workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001/build/build_001/bin/simulate"],
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
                        "required_outputs": [
                            "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001/build/build_001/bin/simulate"
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_pl001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_plan001/dependency.resolved.yaml",
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
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


if __name__ == "__main__":
    unittest.main()
