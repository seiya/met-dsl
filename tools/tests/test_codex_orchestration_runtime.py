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
    init_orchestration,
    parse_feature_list,
    probe_codex_cli,
    record_agent_run,
    record_launch,
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
plan_ref: workspace/plans/problem__shallow_water2d__0.3.0/plan_001
pipeline_ref: workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001
dependency_ref: workspace/plans/problem__shallow_water2d__0.3.0/plan_001/dependency.resolved.yaml
skill_name: workflow-{step}
skill_ref: skills/workflow-{step}/SKILL.md
skill_must_read_refs: docs/WORKFLOW.md,docs/ORCHESTRATION.md
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
plan_ref: workspace/plans/problem__shallow_water2d__0.3.0/plan_001
pipeline_ref: workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001
dependency_ref: workspace/plans/problem__shallow_water2d__0.3.0/plan_001/dependency.resolved.yaml
skill_name: workflow-{step}-{substep}
skill_ref: skills/workflow-{step}-{substep}/SKILL.md
skill_must_read_refs: docs/WORKFLOW.md,docs/ORCHESTRATION.md
issue_severity: none
repair_strategy: none
repair_target_agent_run_id: none
repair_reason: none

必須要件:
- 契約された substep を完了すること。
"""


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

    def test_writes_orchestration_artifacts_in_canonical_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            init_orchestration(
                repo_root=repo_root,
                orchestration_id="orch_001",
                spec_ref="spec/problem/shallow_water2d/controlled_spec.md",
                dependency_ref="workspace/plans/problem__shallow_water2d__0.3.0/plan_001/dependency.resolved.yaml",
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001/dependency.resolved.yaml",
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
                    "agent_session_id": "sess_substep_plan_generate_001",
                    "accepted": True,
                    "launch_reply": "accepted: sess_substep_plan_generate_001",
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001/dependency.resolved.yaml",
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
                    "agent_session_id": "sess_step_build_001",
                    "accepted": True,
                    "launch_reply": "accepted: sess_step_build_001",
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
                        "workspace/plans/problem__shallow_water2d__0.3.0/plan_001/case.resolved.yaml"
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
                        "workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001/build/build_001/bin/simulate"
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
                        "workspace/plans/problem__shallow_water2d__0.3.0/plan_001/case.resolved.yaml"
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
                        "workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001/build/build_001/bin/simulate"
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001/dependency.resolved.yaml",
                    "skill_name": "workflow-plan-generate",
                    "skill_ref": "skills/workflow-plan-generate/SKILL.md",
                    "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
                    "launch_prompt": "short summary",
                    "prompt": full_prompt,
                },
                response_payload={
                    "agent_session_id": "sess_substep_plan_generate_001",
                    "launch_reply": "accepted",
                },
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001/dependency.resolved.yaml",
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
                response_payload={
                    "agent_session_id": "sess_substep_plan_generate_001",
                    "launch_reply": "accepted",
                },
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001/dependency.resolved.yaml",
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
                response_payload={
                    "agent_session_id": "sess_substep_plan_generate_001",
                    "launch_reply": "accepted",
                },
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
                        "launch_prompt_full": "Build step for node problem/shallow_water2d@0.3.0",
                    },
                    response_payload={"launch_reply": "accepted"},
                )

    def test_rejects_verify_launch_without_required_resolved_artifacts(self) -> None:
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
            with self.assertRaisesRegex(ValueError, "missing required verify inputs"):
                record_launch(
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
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001/dependency.resolved.yaml",
                        "skill_name": "workflow-generate-verify",
                        "skill_ref": "skills/workflow-generate-verify/SKILL.md",
                        "skill_must_read_refs": "docs/WORKFLOW.md,workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001/generate/gen_001/generate_meta.json",
                        "launch_prompt_full": _substep_launch_prompt(
                            "problem/shallow_water2d@0.3.0",
                            "Generate",
                            "verify",
                            "substep_run_generate_verify_001",
                        ),
                    },
                    response_payload={"launch_reply": "accepted"},
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001/dependency.resolved.yaml",
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
                    "agent_session_id": "sess_substep_plan_generate_001",
                    "launch_reply": "accepted",
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
                    "output_refs": ["workspace/plans/problem__shallow_water2d__0.3.0/plan_001/impl.resolved.yaml"],
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
                            "workspace/plans/problem__shallow_water2d__0.3.0/plan_001/case.resolved.yaml"
                        ],
                        "failed_substeps": [],
                        "substep_agent_run_ids": ["substep_run_plan_generate_001"],
                    },
                )

    def test_record_agent_run_writes_explicit_summary_when_provided(self) -> None:
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
            self.assertEqual(
                summary_path.read_text(encoding="utf-8").strip(),
                "compile diagnostics show missing dependency metadata",
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
                    response_payload={"accepted": True},
                )

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
                        "output_refs": ["workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001/build/build_001/bin/simulate"],
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
            with patch("tools.codex_orchestration_runtime.probe_codex_cli") as probe_mock:
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
                    request_payload={"launch_prompt": "do task"},
                    response_payload={"launch_reply": "accepted"},
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
                "tools.codex_orchestration_runtime.probe_codex_cli",
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
                        "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001",
                        "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001",
                        "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001/dependency.resolved.yaml",
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
                        "agent_session_id": "sess_step_build_001",
                        "launch_reply": "accepted",
                    },
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
                        "output_refs": ["workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001/build/build_001/bin/simulate"],
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
                            "workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001/build/build_001/bin/simulate"
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
                    "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001",
                    "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/pipeline_001",
                    "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_001/dependency.resolved.yaml",
                    "skill_name": "workflow-build",
                    "skill_ref": "skills/workflow-build/SKILL.md",
                    "skill_must_read_refs": "docs/WORKFLOW.md,docs/ORCHESTRATION.md",
                    "launch_prompt_full": _step_launch_prompt(
                        "problem/shallow_water2d@0.3.0",
                        "build",
                        "step_run_build_001",
                    ),
                },
                response_payload={"launch_reply": "accepted"},
            )
            with self.assertRaisesRegex(RuntimeError, "child_agent_run_id missing from agent_runs.jsonl"):
                update_orchestration_status(
                    repo_root=repo_root,
                    orchestration_id="orch_001",
                    status="pass",
                )


if __name__ == "__main__":
    unittest.main()
