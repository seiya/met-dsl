#!/usr/bin/env python3
"""Regression tests for pipeline semantic validation anti-cheat rules."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.validate_pipeline_semantics import (
    _validate_generate_lint_command_logs,
    _validate_generate_meta_json_files,
    validate,
    validate_plan_stage,
    validate_post_build_stage,
    validate_post_generate_stage,
)


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_STEP_PHASE_PATH = {
    "plan": "docs/workflow/phases/phase_01_plan.md",
    "generate": "docs/workflow/phases/phase_02_generate.md",
    "build": "docs/workflow/phases/phase_03_build.md",
    "execute": "docs/workflow/phases/phase_04_execute.md",
    "judge": "docs/workflow/phases/phase_05_judge.md",
}


def _step_prompt_fixture(orchestration_id: str, node_key: str, step: str, run_id: str) -> str:
    phase_path = _STEP_PHASE_PATH.get(step, "docs/workflow/phases/phase_01_plan.md")
    refs = (
        f"skills/workflow-{step}/SKILL.md,"
        f"docs/workflow/WORKFLOW_CORE.md,docs/ORCHESTRATION.md,{phase_path}"
    )
    return f"""あなたは step agent である。
対象 node_key: {node_key}
対象 step: {step}
orchestration_id: {orchestration_id}
agent_run_id: {run_id}
parent_agent_run_id: orch_run_001
plan_ref: workspace/plans/problem__shallow_water2d__0.3.0/plan_test
pipeline_ref: workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_test_pipeline
dependency_ref: workspace/plans/problem__shallow_water2d__0.3.0/plan_test/dependency.resolved.yaml
skill_name: workflow-{step}
skill_ref: skills/workflow-{step}/SKILL.md
skill_must_read_refs: {refs}
必須要件:
- 契約を完了すること。
"""


def _substep_prompt_fixture(
    orchestration_id: str,
    node_key: str,
    step: str,
    substep: str,
    run_id: str,
) -> str:
    phase_path = _STEP_PHASE_PATH.get(step, "docs/workflow/phases/phase_01_plan.md")
    refs = (
        f"skills/workflow-{step}-{substep}/SKILL.md,"
        f"docs/workflow/WORKFLOW_CORE.md,docs/ORCHESTRATION.md,{phase_path}"
    )
    return f"""あなたは substep agent である。
対象 node_key: {node_key}
対象 step: {step}
対象 substep: {substep}
orchestration_id: {orchestration_id}
agent_run_id: {run_id}
parent_agent_run_id: orch_run_001
plan_ref: workspace/plans/problem__shallow_water2d__0.3.0/plan_test
pipeline_ref: workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_test_pipeline
dependency_ref: workspace/plans/problem__shallow_water2d__0.3.0/plan_test/dependency.resolved.yaml
skill_name: workflow-{step}-{substep}
skill_ref: skills/workflow-{step}-{substep}/SKILL.md
skill_must_read_refs: {refs}
必須要件:
- 契約を完了すること。
"""


def _spawn_response_payload(session_id: str, launch_reply: str) -> dict[str, object]:
    return {
        "accepted": True,
        "agent_session_id": session_id,
        "launch_reply_ref": "",
        "launch_reply": launch_reply,
    }


def _create_minimal_execution_tree(
    repo_root: Path,
    *,
    dep_spec_id: str,
    model_text: str,
    runner_text: str,
    run_command: list[str],
    extra_sources: dict[str, str] | None = None,
    makefile_text: str | None = None,
    algorithm_contract: dict[str, object] | None = None,
    derived_contract: dict[str, object] | None = None,
    dependency_resolved: dict[str, object] | None = None,
    impl_resolved: dict[str, object] | None = None,
    metrics_basis: object | None = None,
) -> None:
    workspace = repo_root / "workspace"
    node_safe = "problem__shallow_water2d__0.3.0"
    pipeline_id = "problem__shallow_water2d__0.3.0_test_pipeline"
    exec_id = "exe_test_001"

    pipeline_dir = workspace / "pipelines" / node_safe / pipeline_id
    node_dir = pipeline_dir / "execute" / exec_id / "problem" / "shallow_water2d"
    raw_dir = node_dir / "raw"
    snapshots_dir = raw_dir / "state_snapshots"
    src_dir = pipeline_dir / "generate" / "gen_test_001" / "src"
    log_path = node_dir / "run_commands.jsonl"

    _write_json(
        pipeline_dir / "lineage.json",
        {
            "node_key": "problem/shallow_water2d@0.3.0",
            "pipeline_id": pipeline_id,
            "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_test",
            "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_test/dependency.resolved.yaml",
        },
    )
    lint_command_id = "lint_cmd_fixture_001"
    rel_lint_log = (
        f"workspace/pipelines/{node_safe}/{pipeline_id}/generate/gen_test_001/src/mcp_command_log.jsonl"
    )
    if dependency_resolved is None:
        dependency_resolved = {
            "node_key": "problem/shallow_water2d@0.3.0",
            "direct_deps": [f"component/{dep_spec_id}@0.1.0"],
            "transitive_deps": [f"component/{dep_spec_id}@0.1.0"],
            "topo_level": 1,
        }
    _write_json(
        workspace / "plans" / "problem__shallow_water2d__0.3.0" / "plan_test" / "dependency.resolved.yaml",
        dependency_resolved,
    )
    if algorithm_contract is None:
        algorithm_contract = {
            "algorithm_id": "shallow_water2d_test_algorithm",
            "execution_mode": "sequence",
            "steps": [
                {
                    "step_id": "compute_flux",
                    "step_kind": "flux_compute",
                    "operation_ref": f"{dep_spec_id}__compute_flux",
                    "inputs": ["h", "hu", "hv"],
                    "outputs": ["h", "hu", "hv"],
                }
            ],
            "ordering": [],
            "control_condition": [],
            "iteration_contract": {"kind": "none"},
            "update_semantics": {"mode": "in_place"},
            "temporaries": [],
            "derived_field_rules": [],
            "invariants": [],
            "splitting_policy": {"kind": "none"},
            "state_contract": {
                "state_variables": [
                    {"name": "h", "shape_expr": "[2,2]"},
                    {"name": "hu", "shape_expr": "[2,2]"},
                    {"name": "hv", "shape_expr": "[2,2]"},
                ],
                "required_update_paths": ["h", "hu", "hv"],
                "diagnostics_from_state": True,
                "fallback_policy": "fail_closed",
            },
        }
    _write_json(
        workspace / "plans" / "problem__shallow_water2d__0.3.0" / "plan_test" / "algorithm.resolved.yaml",
        algorithm_contract,
    )
    if derived_contract is None:
        derived_contract = {
            "io_contract": {
                "inputs": [
                    {
                        "name": "case_resolved",
                        "source": "case.resolved.yaml",
                    }
                ],
                "outputs": [
                    {
                        "name": "metric",
                        "shape_expr": "scalar",
                        "evidence_ref": "raw/metrics_basis.json",
                        "raw_variables": ["h", "hu", "hv", "time"],
                    }
                ],
            },
            "semantic_dependency": {"required_sources": []},
            "raw_requirements": {
                "required_evidence": [
                    {"artifact": "metrics_basis.json", "required": True},
                    {"artifact": "execution_trace.json", "required": True},
                    {
                        "artifact": "state_snapshots",
                        "required": True,
                        "min_samples": 1,
                        "schema": {
                            "variables": [
                                {"name": "h", "shape_expr": "[2,2]"},
                                {"name": "hu", "shape_expr": "[2,2]"},
                                {"name": "hv", "shape_expr": "[2,2]"},
                            ],
                            "time_variable": "time",
                            "time_shape_expr": "scalar",
                        },
                    },
                ]
            },
        }
    _write_json(
        workspace / "plans" / "problem__shallow_water2d__0.3.0" / "plan_test" / "derived_contract.json",
        derived_contract,
    )
    if impl_resolved is None:
        impl_resolved = {
            "target": {
                "class": "cpu",
                "backend": "fortran",
                "architecture": "x86_64",
            },
            "toolchain": {
                "language": "fortran",
                "standard": "f2008",
                "build_system": "make",
            },
            "selected": {
                "backend_key": "cpu/x86_64/fortran/make",
            },
            "abstract": {
                "parallelism": "none",
                "layout": "scalar_interfaces",
                "fusion": "none",
            },
            "backend_overrides": [],
        }
    _write_json(
        workspace / "plans" / "problem__shallow_water2d__0.3.0" / "plan_test" / "impl.resolved.yaml",
        impl_resolved,
    )

    _write_json(node_dir / "diagnostics.json", {"metric": 1.0})
    _write_json(
        node_dir / "perf.json",
        {
            "case_id": "case-001",
            "target": "cpu",
            "walltime_sec": 0.01,
            "steps": 1,
            "cells_updated": 4,
            "throughput_cells_per_sec": 400.0,
            "parallelism": {
                "mpi_ranks": 1,
                "threads_per_rank": 1,
                "gpu_devices": 0,
                "parallel_degree_total": 1,
            },
        },
    )
    if metrics_basis is None:
        metrics_basis = {"basis": 2.0}
    _write_json(raw_dir / "metrics_basis.json", metrics_basis)
    _write_json(raw_dir / "execution_trace.json", {"trace": ["step1", "step2"]})
    _write_json(
        snapshots_dir / "snapshot_schema.json",
        {
            "variables": [
                {"name": "h", "shape_expr": "[2,2]"},
                {"name": "hu", "shape_expr": "[2,2]"},
                {"name": "hv", "shape_expr": "[2,2]"},
            ],
            "time_variable": "time",
            "time_shape_expr": "scalar",
        },
    )
    _write_json(
        snapshots_dir / "snapshot000.json",
        {
            "h": [[1.0, 1.0], [1.0, 1.0]],
            "hu": [[0.0, 0.0], [0.0, 0.0]],
            "hv": [[0.0, 0.0], [0.0, 0.0]],
            "time": 0.0,
        },
    )
    _write_json(
        node_dir / "quality_check.json",
        {
            "status": "pass",
            "checks": {
                "verdict_available": True,
                "diagnostics_match": True,
                "verdict_match": True,
            },
        },
    )

    command_id = "cmd_run_001"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(
            {
                "command_id": command_id,
                "tool_name": "run_program",
                "command": run_command,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(
        node_dir / "trial_meta.json",
        {
            "generated_by_stage": "execute",
            "source_execution_id": exec_id,
            "source_command_ref": {
                "run_threads_1": {
                    "command_id": command_id,
                    "command_log_ref": f"workspace/{log_path.relative_to(workspace).as_posix()}",
                }
            },
            "runner_command": "./simulate",
            "process_trace_ref": f"workspace/{(raw_dir / 'execution_trace.json').relative_to(workspace).as_posix()}",
            "raw_artifact_refs": [
                f"workspace/{(raw_dir / 'metrics_basis.json').relative_to(workspace).as_posix()}",
                f"workspace/{(snapshots_dir / 'snapshot000.json').relative_to(workspace).as_posix()}",
            ],
        },
    )

    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "shallow_water2d_model.f90").write_text(model_text, encoding="utf-8")
    (src_dir / "shallow_water2d_runner.f90").write_text(runner_text, encoding="utf-8")
    if extra_sources:
        for filename, content in extra_sources.items():
            (src_dir / filename).write_text(content, encoding="utf-8")

    if makefile_text is None:
        makefile_text = """FC ?= gfortran
OBJS = shallow_water2d_model.o shallow_water2d_runner.o

simulate: $(OBJS)
\t$(FC) -o $@ $(OBJS)

shallow_water2d_model.o shallow_water2d_model.mod: shallow_water2d_model.f90
\t$(FC) -c $<

shallow_water2d_runner.o: shallow_water2d_runner.f90 shallow_water2d_model.mod
\t$(FC) -c $<
"""
    (src_dir / "Makefile").write_text(makefile_text, encoding="utf-8")

    (src_dir / "mcp_command_log.jsonl").write_text(
        json.dumps(
            {
                "command_id": lint_command_id,
                "tool_name": "run_linter",
                "command": ["fortitude", "check", "."],
                "ok": True,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(
        pipeline_dir / "generate" / "gen_test_001" / "generate_meta.json",
        {
            "attempt_count": 1,
            "verification_status": "pass",
            "last_fail_reason": "",
            "debug_mode": False,
            "context_isolated": True,
            "lint_command_ref": {
                "run_linter": [
                    {
                        "command_id": lint_command_id,
                        "command_log_ref": rel_lint_log,
                        "preset": "fortitude",
                    }
                ]
            },
        },
    )

    _write_json(
        node_dir / "semantic_review.json",
        {
            "review_method": "llm_semantic_review",
            "decision": "pass",
            "scope": {
                "model_ref": f"workspace/{(src_dir / 'shallow_water2d_model.f90').relative_to(workspace).as_posix()}",
                "runner_ref": f"workspace/{(src_dir / 'shallow_water2d_runner.f90').relative_to(workspace).as_posix()}",
                "raw_refs": [
                    f"workspace/{(raw_dir / 'metrics_basis.json').relative_to(workspace).as_posix()}",
                    f"workspace/{(raw_dir / 'execution_trace.json').relative_to(workspace).as_posix()}",
                ],
            },
            "findings": [],
        },
    )


def _create_minimal_orchestration_tree(
    repo_root: Path,
    *,
    node_safe: str = "problem__shallow_water2d__0.3.0",
    node_key: str = "problem/shallow_water2d@0.3.0",
    orchestration_id: str = "orch_test_001",
) -> None:
    orchestration_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    launches_root = orchestration_root / "launches"
    launches_root.mkdir(parents=True, exist_ok=True)
    _write_json(
        orchestration_root / "orchestration_meta.json",
        {
            "orchestration_id": orchestration_id,
            "status": "pass",
        },
    )
    _write_json(
        orchestration_root / "preflight.json",
        {
            "status": "pass",
            "can_launch_step_agents": True,
            "can_launch_substep_agents": True,
            "feature_states": {
                "multi_agent": True,
            },
            "checks": [
                {
                    "name": "multi_agent_enabled",
                    "pass": True,
                }
            ],
        },
    )
    step_ids = {
        step: f"step_run_{step}_001" for step in ("build", "execute", "judge")
    }
    substep_ids = {
        "plan": ["substep_run_plan_generate_001", "substep_run_plan_verify_001"],
        "generate": ["substep_run_generate_generate_001", "substep_run_generate_verify_001"],
    }
    graph_data = {"edges": []}
    for step in ("build", "execute", "judge"):
        graph_data["edges"].append(
            {
                "parent_agent_run_id": "orch_run_001",
                "child_agent_run_id": step_ids[step],
                "relation_type": "launch",
            }
        )
    for substeps in substep_ids.values():
        for substep_id in substeps:
            graph_data["edges"].append(
                {
                    "parent_agent_run_id": "orch_run_001",
                    "child_agent_run_id": substep_id,
                    "relation_type": "launch",
                }
            )
    _write_json(orchestration_root / "agent_graph.json", graph_data)

    agent_runs_path = orchestration_root / "agent_runs.jsonl"
    agent_runs_path.parent.mkdir(parents=True, exist_ok=True)
    run_items = [
        {
            "agent_run_id": "orch_run_001",
            "agent_role": "orchestration",
            "status": "pass",
            "started_at": "2026-03-01T00:00:00Z",
            "finished_at": "2026-03-01T00:10:00Z",
        }
    ]
    for step in ("build", "execute", "judge"):
        step_request_ref = f"workspace/orchestrations/{orchestration_id}/launches/{step_ids[step]}.request.json"
        step_response_ref = f"workspace/orchestrations/{orchestration_id}/launches/{step_ids[step]}.response.json"
        step_prompt_ref = f"workspace/orchestrations/{orchestration_id}/launches/{step_ids[step]}.prompt.txt"
        step_reply_ref = f"workspace/orchestrations/{orchestration_id}/launches/{step_ids[step]}.reply.txt"

        _write_json(
            launches_root / f"{step_ids[step]}.request.json",
            {
                "agent_run_id": step_ids[step],
                "role": "step",
                "step": step,
                "launch_prompt_ref": step_prompt_ref,
                "launch_prompt": f"run step {step}",
            },
        )
        _write_json(
            launches_root / f"{step_ids[step]}.response.json",
            {
                "agent_run_id": step_ids[step],
                **_spawn_response_payload(
                    f"sess_step_{step}",
                    f"accepted: sess_step_{step}",
                ),
                "launch_reply_ref": step_reply_ref,
            },
        )
        (launches_root / f"{step_ids[step]}.prompt.txt").write_text(
            _step_prompt_fixture(orchestration_id, node_key, step, step_ids[step]) + "\n",
            encoding="utf-8",
        )
        (launches_root / f"{step_ids[step]}.reply.txt").write_text(
            f"accepted: sess_step_{step}\n",
            encoding="utf-8",
        )
        step_agent_dir = orchestration_root / "agents" / step_ids[step] / "dialogs"
        step_agent_dir.mkdir(parents=True, exist_ok=True)
        step_agent_result_ref = f"workspace/orchestrations/{orchestration_id}/agents/{step_ids[step]}/dialogs/agent.result.json"
        step_agent_summary_ref = f"workspace/orchestrations/{orchestration_id}/agents/{step_ids[step]}/dialogs/agent.summary.txt"
        step_payload = {
            "agent_run_id": step_ids[step],
            "parent_agent_run_id": "orch_run_001",
            "agent_role": "step",
            "node_key": node_key,
            "step": step,
            "status": "pass",
            "agent_backend": "openai_responses",
            "agent_model": "gpt-5-codex",
            "context_id": f"ctx_step_{step}",
            "context_isolated": True,
            "agent_session_id": f"sess_step_{step}",
            "launch_request_ref": step_request_ref,
            "launch_response_ref": step_response_ref,
            "launch_prompt_ref": step_prompt_ref,
            "launch_reply_ref": step_reply_ref,
            "agent_result_ref": step_agent_result_ref,
            "agent_summary_ref": step_agent_summary_ref,
            "started_at": "2026-03-01T00:00:10Z",
            "finished_at": "2026-03-01T00:01:10Z",
        }
        _write_json(step_agent_dir / "agent.result.json", step_payload)
        _write_json(
            step_agent_dir / "child.response.json",
            {
                "agent_run_id": step_ids[step],
                **_spawn_response_payload(
                    f"sess_step_{step}",
                    f"accepted: sess_step_{step}",
                ),
                "launch_reply_ref": step_reply_ref,
            },
        )
        (step_agent_dir / "agent.summary.txt").write_text(
            f"agent_run_id: {step_ids[step]}\nstatus: pass\noutput_refs:\n- workspace/pipelines/{node_safe}/pipeline_step_{step}\n",
            encoding="utf-8",
        )
        run_items.append(step_payload)
    for step, substeps in substep_ids.items():
        for idx, substep_id in enumerate(substeps, start=1):
            substep_request_ref = f"workspace/orchestrations/{orchestration_id}/launches/{substep_id}.request.json"
            substep_response_ref = f"workspace/orchestrations/{orchestration_id}/launches/{substep_id}.response.json"
            substep_prompt_ref = f"workspace/orchestrations/{orchestration_id}/launches/{substep_id}.prompt.txt"
            substep_reply_ref = f"workspace/orchestrations/{orchestration_id}/launches/{substep_id}.reply.txt"

            _write_json(
                launches_root / f"{substep_id}.request.json",
                {
                    "agent_run_id": substep_id,
                    "role": "substep",
                    "step": step,
                    "launch_prompt_ref": substep_prompt_ref,
                    "launch_prompt": f"run substep {step} part {idx}",
                },
            )
            _write_json(
                launches_root / f"{substep_id}.response.json",
                {
                    "agent_run_id": substep_id,
                    **_spawn_response_payload(
                        f"sess_substep_{step}_{idx}",
                        f"accepted: sess_substep_{step}_{idx}",
                    ),
                    "launch_reply_ref": substep_reply_ref,
                },
            )
            (launches_root / f"{substep_id}.prompt.txt").write_text(
                _substep_prompt_fixture(
                    orchestration_id,
                    node_key,
                    step,
                    f"part_{idx}",
                    substep_id,
                )
                + "\n",
                encoding="utf-8",
            )
            (launches_root / f"{substep_id}.reply.txt").write_text(
                f"accepted: sess_substep_{step}_{idx}\n",
                encoding="utf-8",
            )

            substep_agent_dir = orchestration_root / "agents" / substep_id / "dialogs"
            substep_agent_dir.mkdir(parents=True, exist_ok=True)
            substep_agent_result_ref = f"workspace/orchestrations/{orchestration_id}/agents/{substep_id}/dialogs/agent.result.json"
            substep_agent_summary_ref = f"workspace/orchestrations/{orchestration_id}/agents/{substep_id}/dialogs/agent.summary.txt"
            substep_payload = {
                "agent_run_id": substep_id,
                "parent_agent_run_id": "orch_run_001",
                "agent_role": "substep",
                "node_key": node_key,
                "step": step,
                "substep": f"part_{idx}",
                "status": "pass",
                "agent_backend": "openai_responses",
                "agent_model": "gpt-5-codex",
                "context_id": f"ctx_substep_{step}_{idx}",
                "context_isolated": True,
                "agent_session_id": f"sess_substep_{step}_{idx}",
                "launch_request_ref": substep_request_ref,
                "launch_response_ref": substep_response_ref,
                "launch_prompt_ref": substep_prompt_ref,
                "launch_reply_ref": substep_reply_ref,
                "agent_result_ref": substep_agent_result_ref,
                "agent_summary_ref": substep_agent_summary_ref,
                "started_at": "2026-03-01T00:00:20Z",
                "finished_at": "2026-03-01T00:00:50Z",
            }
            _write_json(substep_agent_dir / "agent.result.json", substep_payload)
            _write_json(
                substep_agent_dir / "child.response.json",
                {
                    "agent_run_id": substep_id,
                    **_spawn_response_payload(
                        f"sess_substep_{step}_{idx}",
                        f"accepted: sess_substep_{step}_{idx}",
                    ),
                    "launch_reply_ref": substep_reply_ref,
                },
            )
            (substep_agent_dir / "agent.summary.txt").write_text(
                f"agent_run_id: {substep_id}\nstatus: pass\noutput_refs:\n- workspace/plans/{node_safe}/plan_{step}_{idx}\n",
                encoding="utf-8",
            )
            run_items.append(substep_payload)
    agent_runs_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in run_items) + "\n",
        encoding="utf-8",
    )

    for step in ("plan", "generate"):
        _write_json(
            orchestration_root
            / "steps"
            / node_safe
            / step
            / "orch_run_001"
            / "step_result.json",
            {
                "status": "pass",
                "required_outputs": [],
                "failed_substeps": [],
                "executor_agent_run_id": "orch_run_001",
                "substep_agent_run_ids": substep_ids[step],
            },
        )

    for step in ("build", "execute", "judge"):
        _write_json(
            orchestration_root
            / "steps"
            / node_safe
            / step
            / step_ids[step]
            / "step_result.json",
            {
                "status": "pass",
                "required_outputs": [],
                "failed_substeps": [],
                "executor_agent_run_id": step_ids[step],
                "substep_agent_run_ids": [],
            },
        )


class ValidatePipelineSemanticsTests(unittest.TestCase):
    def test_rejects_noncanonical_workspace_root_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            violations = validate(repo_root=repo_root, workspace_root="workspace/runs/sample")
            self.assertTrue(
                any("workspace_root must be exactly 'workspace'" in v for v in violations)
            )

    def test_ignores_empty_execution_node_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'diagnostics only'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )

            empty_node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_empty_pipeline"
                / "execute"
                / "exe_empty_001"
                / "problem"
                / "shallow_water2d"
            )
            empty_node_dir.mkdir(parents=True, exist_ok=True)

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertEqual([], violations)

    def test_detects_dependency_dummy_and_runner_output_and_missing_case_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
implicit none
contains
subroutine dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
  logical, intent(out) :: flag
  flag = .true.
end subroutine dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'verdict.json'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")

            self.assertTrue(
                any("dependency operation redefinition detected" in v for v in violations)
            )
            self.assertTrue(any("missing dependency module use" in v for v in violations))
            self.assertTrue(any("missing dependency operation call" in v for v in violations))
            self.assertTrue(any("forbidden runner output write detected" in v for v in violations))
            self.assertTrue(any("must include case.resolved.yaml" in v for v in violations))

    def test_passes_with_dependency_use_and_case_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'diagnostics only'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertEqual([], violations)

    def test_ignores_aux_model_file_for_dependency_usage_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'diagnostics only'
end program shallow_water2d_runner
"""
            aux_model_text = """module aux_model
implicit none
contains
subroutine aux_run(flag)
  logical, intent(out) :: flag
  flag = .true.
end subroutine aux_run
end module aux_model
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                extra_sources={"aux_model.f90": aux_model_text},
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertEqual([], violations)

    def test_detects_dependency_dag_incomplete_for_target_pipeline_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'diagnostics only'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                dependency_resolved={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "direct_deps": ["component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0"],
                    "transitive_deps": ["component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0"],
                    "topo_level": 1,
                    "resolved_at": "20260304T000000Z",
                    "all_nodes": [
                        {"node_key": "component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0"},
                        {"node_key": "problem/shallow_water2d@0.3.0"},
                    ],
                },
            )

            pipeline_root = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
            )

            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                pipeline_roots=[pipeline_root],
            )
            self.assertTrue(any("dependency DAG incomplete" in v for v in violations))
            self.assertTrue(
                any("component/dynamics_shallow_water_flux_2d_rusanov_p0" in v for v in violations)
            )
            self.assertTrue(any("node plans not issued" in v for v in violations))
            self.assertTrue(any("node pipelines not issued" in v for v in violations))

    def test_detects_problem_constant_model_and_runner_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(cfl_max, h_min, mass_drift_rel)
  real(8), intent(out) :: cfl_max, h_min, mass_drift_rel
  if (.true.) then
    cfl_max = 0.72d0
    h_min = 0.91d0
    mass_drift_rel = 1.0d-12
  else
    cfl_max = 1.5d0
    h_min = 0.01d0
    mass_drift_rel = 1.0d-3
  end if
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
real(8) :: cfl_max, h_min, mass_drift_rel
integer :: u
call shallow_water2d__step(cfl_max, h_min, mass_drift_rel)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
write(u,'(a)') '{"cfl":{"max":0.72},"extrema":{"h":{"min":0.91}},"mass_drift_rel":1.0e-12,"momx_drift_rel":1.0e-12,"momy_drift_rel":1.0e-12,"convergence_order":{"n32_to_n64":1.0}}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("literal-only assignments for all intent(out) vars" in v for v in violations)
            )
            self.assertTrue(
                any("diagnostics block does not reference model call arguments" in v for v in violations)
            )

    def test_detects_invalid_perf_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(metric)
  real(8), intent(out) :: metric
  metric = 1.0d0
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
real(8) :: metric
call shallow_water2d__step(metric)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            perf_path = (
                repo_root
                / "workspace/pipelines/problem__shallow_water2d__0.3.0/problem__shallow_water2d__0.3.0_test_pipeline/execute/exe_test_001/problem/shallow_water2d/perf.json"
            )
            perf_path.write_text('{"walltime_sec":.000002}\n', encoding="utf-8")

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(v == f"{perf_path}: invalid json" for v in violations)
            )

    def test_detects_unsafe_fortran_f0_perf_serialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(walltime_sec)
  real(8), intent(out) :: walltime_sec
  walltime_sec = 2.0d-6
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
real(8) :: walltime_sec
integer :: u
call shallow_water2d__step(walltime_sec)
open(newunit=u, file='perf.json', status='replace', action='write')
write(u,'(a,f0.6,a)') '{"walltime_sec":', walltime_sec, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("perf.json block uses Fortran F0 formatting" in v for v in violations)
            )

    def test_detects_metric_only_scalar_kernel_for_2d_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__simulate_metrics(nx, ny, m1, m2, m3, m4, m5, m6)
  integer, intent(in) :: nx, ny
  real(8), intent(out) :: m1, m2, m3, m4, m5, m6
  real(8) :: flux_indicator
  logical :: ok
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(ok)
  flux_indicator = dble(nx + ny)
  m1 = flux_indicator * 1.0d-6
  m2 = flux_indicator * 2.0d-6
  m3 = flux_indicator * 3.0d-6
  m4 = flux_indicator * 4.0d-6
  m5 = flux_indicator * 5.0d-6
  m6 = flux_indicator * 6.0d-6
end subroutine shallow_water2d__simulate_metrics
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'diagnostics only'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("metric-only scalar kernel" in v for v in violations)
            )

    def test_requires_algorithm_state_contract_for_2d_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                algorithm_contract={
                    "algorithm_id": "broken_algorithm",
                    "execution_mode": "sequence",
                    "steps": [
                        {
                            "step_id": "compute_flux",
                            "step_kind": "flux_compute",
                            "operation_ref": "dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux",
                            "inputs": ["h", "hu", "hv"],
                            "outputs": ["h", "hu", "hv"],
                        }
                    ],
                    "ordering": [],
                    "control_condition": [],
                    "iteration_contract": {"kind": "none"},
                    "update_semantics": {"mode": "in_place"},
                    "temporaries": [],
                    "derived_field_rules": [],
                    "invariants": [],
                    "splitting_policy": {"kind": "none"},
                },
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("state_contract must be object for multidimensional problem node" in v for v in violations)
            )

    def test_detects_makefile_missing_fortran_module_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            dep_spec_id = "dynamics_shallow_water_flux_2d_rusanov_p0"
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
logical :: flag
call solve(flag)
write(*,*) flag
end program shallow_water2d_runner
"""
            dep_model_text = """module dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
  logical, intent(out) :: flag
  flag = .true.
end subroutine dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux
end module dynamics_shallow_water_flux_2d_rusanov_p0_model
"""
            makefile_text = """FC ?= gfortran
OBJS = dynamics_shallow_water_flux_2d_rusanov_p0_model.o shallow_water2d_model.o shallow_water2d_runner.o

simulate: $(OBJS)
\t$(FC) -o $@ $(OBJS)

dynamics_shallow_water_flux_2d_rusanov_p0_model.o dynamics_shallow_water_flux_2d_rusanov_p0_model.mod: dynamics_shallow_water_flux_2d_rusanov_p0_model.f90
\t$(FC) -c $<

shallow_water2d_model.o shallow_water2d_model.mod: shallow_water2d_model.f90
\t$(FC) -c $<

shallow_water2d_runner.o: shallow_water2d_runner.f90 shallow_water2d_model.mod
\t$(FC) -c $<
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id=dep_spec_id,
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                extra_sources={
                    "dynamics_shallow_water_flux_2d_rusanov_p0_model.f90": dep_model_text
                },
                makefile_text=makefile_text,
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("missing prerequisite for used module" in v for v in violations)
            )

    def test_detects_missing_llm_semantic_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )

            review_path = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
                / "semantic_review.json"
            )
            review_path.unlink()

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(any("semantic_review.json: missing" in v for v in violations))

    def test_detects_dependency_call_outputs_not_used_in_out_dataflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(scale, metrics)
  real(8), intent(in) :: scale
  real(8), intent(out) :: metrics(2)
  real(8) :: dep_checks(3,3)
  integer :: dep_istat
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(scale, dep_checks(:,1), dep_istat)
  metrics(1) = 0.5d0
  metrics(2) = 1.0d0
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
real(8) :: metrics(2)
call solve(1.0d0, metrics)
write(*,*) metrics
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("does not propagate dependency operation outputs" in v for v in violations)
            )

    def test_state_snapshots_can_be_optional_by_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                derived_contract={
                    "io_contract": {
                        "inputs": [{"name": "case_resolved", "source": "case.resolved.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/metrics_basis.json",
                            }
                        ],
                    },
                    "semantic_dependency": {"required_sources": []},
                    "raw_requirements": {
                        "required_evidence": [
                            {"artifact": "metrics_basis.json", "required": True},
                            {"artifact": "execution_trace.json", "required": True},
                            {"artifact": "state_snapshots", "required": False},
                        ]
                    },
                },
            )

            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            trial_meta["raw_artifact_refs"] = [
                ref
                for ref in trial_meta["raw_artifact_refs"]
                if "state_snapshots" not in str(ref)
            ]
            _write_json(trial_meta_path, trial_meta)

            shutil.rmtree(node_dir / "raw" / "state_snapshots")

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(any("state_snapshots" in v for v in violations))

    def test_detects_invalid_io_contract_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                derived_contract={
                    "io_contract": {
                        "inputs": [{"name": "case_resolved", "source": "case.resolved.yaml"}],
                        "outputs": [],
                    },
                    "semantic_dependency": {"required_sources": []},
                    "raw_requirements": {
                        "required_evidence": [
                            {"artifact": "metrics_basis.json", "required": True},
                            {"artifact": "execution_trace.json", "required": True},
                        ]
                    },
                },
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("io_contract.outputs must be non-empty list" in v for v in violations)
            )

    def test_detects_forbidden_custom_quality_check_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )

            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            run_log_ref = trial_meta["source_command_ref"]["run_threads_1"]["command_log_ref"]
            trial_meta["source_command_ref"]["run_quality_checks"] = {
                "command_id": "cmd_quality_001",
                "command_log_ref": run_log_ref,
            }
            _write_json(trial_meta_path, trial_meta)

            run_log_path = repo_root / run_log_ref
            with run_log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "command_id": "cmd_quality_001",
                            "tool_name": "run_quality_checks",
                            "command": ["python3", "quality_check.py", ".", "."],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("run_quality_checks command_id=cmd_quality_001 uses forbidden executable" in v for v in violations)
            )

    def test_rejects_pytest_quality_check_for_fortran_make_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )

            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            src_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
                / "generate"
                / "gen_test_001"
                / "src"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            run_log_ref = trial_meta["source_command_ref"]["run_threads_1"]["command_log_ref"]
            trial_meta["source_command_ref"]["run_quality_checks"] = {
                "command_id": "cmd_quality_001",
                "command_log_ref": run_log_ref,
            }
            _write_json(trial_meta_path, trial_meta)

            run_log_path = repo_root / run_log_ref
            with run_log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "command_id": "cmd_quality_001",
                            "tool_name": "run_quality_checks",
                            "cwd": str(src_dir),
                            "command": ["pytest", "-q"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("must use make_test/make_check for toolchain.language=fortran and toolchain.build_system=make" in v for v in violations)
            )

    def test_rejects_make_quality_check_without_declared_test_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                makefile_text="""FC ?= gfortran
OBJS = shallow_water2d_model.o shallow_water2d_runner.o

simulate: $(OBJS)
\t$(FC) -o $@ $(OBJS)

shallow_water2d_model.o shallow_water2d_model.mod: shallow_water2d_model.f90
\t$(FC) -c $<

shallow_water2d_runner.o: shallow_water2d_runner.f90 shallow_water2d_model.mod
\t$(FC) -c $<
""",
            )

            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            src_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
                / "generate"
                / "gen_test_001"
                / "src"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            run_log_ref = trial_meta["source_command_ref"]["run_threads_1"]["command_log_ref"]
            trial_meta["source_command_ref"]["run_quality_checks"] = {
                "command_id": "cmd_quality_001",
                "command_log_ref": run_log_ref,
            }
            _write_json(trial_meta_path, trial_meta)

            run_log_path = repo_root / run_log_ref
            with run_log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "command_id": "cmd_quality_001",
                            "tool_name": "run_quality_checks",
                            "cwd": str(src_dir),
                            "command": ["make", "test"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Makefile: missing test target required by run_quality_checks command_id=cmd_quality_001" in v for v in violations)
            )

    def test_rejects_source_command_log_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )

            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            outside_log = repo_root / "mcp_command_log.jsonl"
            outside_log.write_text(
                json.dumps(
                    {
                        "command_id": "cmd_run_001",
                        "tool_name": "run_program",
                        "command": ["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            trial_meta["source_command_ref"]["run_threads_1"]["command_log_ref"] = "mcp_command_log.jsonl"
            _write_json(trial_meta_path, trial_meta)

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("command_log_ref/path must start with workspace/" in v for v in violations)
            )

    def test_validates_tests_md_and_per_test_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            tests_md = repo_root / "spec" / "problem" / "mock_domain" / "mock_family" / "mock_spec" / "tests.md"
            tests_md.parent.mkdir(parents=True, exist_ok=True)
            tests_md.write_text(
                "## 7. テスト定義\n"
                "### 7-1. `test_a`\n"
                "### 7-2. `test_b`\n",
                encoding="utf-8",
            )

            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                derived_contract={
                    "source": {
                        "tests": "spec/problem/mock_domain/mock_family/mock_spec/tests.md"
                    },
                    "io_contract": {
                        "inputs": [{"name": "case_resolved", "source": "case.resolved.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/metrics_basis.json",
                            }
                        ],
                    },
                    "semantic_dependency": {"required_sources": []},
                    "raw_requirements": {
                        "required_evidence": [
                            {"artifact": "metrics_basis.json", "required": True},
                            {"artifact": "execution_trace.json", "required": True},
                        ]
                    },
                },
            )

            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            _write_json(
                node_dir / "verdict.json",
                {
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "self_verdict": "pass",
                    "per_test": [
                        {"test_id": "test_a", "status": "pass"},
                    ],
                },
            )
            _write_json(
                node_dir / "summary.json",
                {
                    "self_summary": {"status": "pass"},
                    "dependency_summary": {
                        "total": 1,
                        "pass": 1,
                        "xfail": 0,
                        "fail": 0,
                        "blocked": 0,
                    },
                    "counts": {
                        "pass": 1,
                        "fail": 0,
                        "xfail": 0,
                        "skipped": 0,
                    },
                },
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(any("per_test missing test_id entries from tests.md" in v for v in violations))

    def test_requires_raw_variables_when_snapshot_required_and_evidence_is_non_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            tests_md = (
                repo_root
                / "spec"
                / "problem"
                / "dynamics"
                / "shallow_water"
                / "shallow_water2d"
                / "tests.md"
            )
            tests_md.parent.mkdir(parents=True, exist_ok=True)
            tests_md.write_text(
                "## 7. テスト定義\n"
                "### 7-1. `l1_refinement_mass_and_positivity`\n",
                encoding="utf-8",
            )

            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                derived_contract={
                    "source": {
                        "tests": "spec/problem/dynamics/shallow_water/shallow_water2d/tests.md"
                    },
                    "io_contract": {
                        "inputs": [{"name": "case_resolved", "source": "case.resolved.yaml"}],
                        "outputs": [
                            {"name": "metric_mass", "shape_expr": "scalar", "evidence_ref": "raw/metrics_basis.json"},
                        ],
                    },
                    "semantic_dependency": {"required_sources": []},
                    "raw_requirements": {
                        "required_evidence": [
                            {"artifact": "metrics_basis.json", "required": True},
                            {"artifact": "execution_trace.json", "required": True},
                            {
                                "artifact": "state_snapshots",
                                "required": True,
                                "min_samples": 1,
                                "schema": {
                                    "variables": [
                                        {"name": "h", "shape_expr": "[2,2]"},
                                        {"name": "hu", "shape_expr": "[2,2]"},
                                        {"name": "hv", "shape_expr": "[2,2]"},
                                    ],
                                    "time_variable": "time",
                                    "time_shape_expr": "scalar",
                                },
                            },
                        ]
                    },
                    "test_evidence_requirements": [
                        {
                            "test_id": "l1_refinement_mass_and_positivity",
                            "required_raw_variables": ["h", "hu", "hv", "time"],
                        }
                    ],
                },
            )

            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            _write_json(
                node_dir / "verdict.json",
                {
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "self_verdict": "pass",
                    "per_test": [
                        {"test_id": "l1_refinement_mass_and_positivity", "status": "pass"},
                    ],
                },
            )
            _write_json(
                node_dir / "summary.json",
                {
                    "self_summary": {"status": "pass"},
                    "dependency_summary": {
                        "total": 1,
                        "pass": 1,
                        "xfail": 0,
                        "fail": 0,
                        "blocked": 0,
                    },
                    "counts": {
                        "pass": 1,
                        "fail": 0,
                        "xfail": 0,
                        "skipped": 0,
                        "blocked": 0,
                    },
                },
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(".raw_variables must be non-empty list when evidence_ref is non-snapshot and state_snapshots is required" in v for v in violations)
            )

    def test_detects_missing_test_evidence_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            tests_md = (
                repo_root / "spec" / "problem" / "mock_domain" / "mock_family" / "mock_spec" / "tests.md"
            )
            tests_md.parent.mkdir(parents=True, exist_ok=True)
            tests_md.write_text(
                "## 7. テスト定義\n"
                "### 7-1. `test_a`\n"
                "### 7-2. `test_b`\n",
                encoding="utf-8",
            )
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                derived_contract={
                    "source": {"tests": "spec/problem/mock_domain/mock_family/mock_spec/tests.md"},
                    "io_contract": {
                        "inputs": [{"name": "case_resolved", "source": "case.resolved.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/metrics_basis.json",
                                "raw_variables": ["h", "hu", "hv", "time"],
                            }
                        ],
                    },
                    "semantic_dependency": {"required_sources": []},
                    "raw_requirements": {
                        "required_evidence": [
                            {"artifact": "metrics_basis.json", "required": True},
                            {"artifact": "execution_trace.json", "required": True},
                            {
                                "artifact": "state_snapshots",
                                "required": True,
                                "min_samples": 1,
                                "schema": {
                                    "variables": [
                                        {"name": "h", "shape_expr": "[2,2]"},
                                        {"name": "hu", "shape_expr": "[2,2]"},
                                        {"name": "hv", "shape_expr": "[2,2]"},
                                    ],
                                    "time_variable": "time",
                                    "time_shape_expr": "scalar",
                                },
                            },
                        ]
                    },
                },
            )
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("test_evidence_requirements must be non-empty list" in v for v in violations)
            )

    def test_detects_metrics_basis_without_per_test_evidence_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            tests_path = (
                repo_root
                / "spec"
                / "problem"
                / "mock_domain"
                / "mock_family"
                / "mock_spec"
                / "tests.md"
            )
            tests_path.parent.mkdir(parents=True, exist_ok=True)
            tests_path.write_text(
                "## 7. テスト定義\n"
                "### 7-1. `test_a`\n"
                "### 7-2. `test_b`\n",
                encoding="utf-8",
            )
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                derived_contract={
                    "source": {"tests": "spec/problem/mock_domain/mock_family/mock_spec/tests.md"},
                    "io_contract": {
                        "inputs": [{"name": "case_resolved", "source": "case.resolved.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/metrics_basis.json",
                                "raw_variables": ["h", "time"],
                            }
                        ],
                    },
                    "semantic_dependency": {"required_sources": []},
                    "raw_requirements": {
                        "required_evidence": [
                            {"artifact": "metrics_basis.json", "required": True},
                            {"artifact": "execution_trace.json", "required": True},
                            {
                                "artifact": "state_snapshots",
                                "required": True,
                                "min_samples": 1,
                                "schema": {
                                    "variables": [
                                        {"name": "h", "shape_expr": "[2,2]"}
                                    ],
                                    "time_variable": "time",
                                    "time_shape_expr": "scalar",
                                },
                            },
                        ]
                    },
                    "test_evidence_requirements": [
                        {
                            "test_id": "test_a",
                            "required_raw_variables": ["h", "time"],
                        },
                        {
                            "test_id": "test_b",
                            "required_raw_variables": ["h", "time"],
                        },
                    ],
                },
                metrics_basis={
                    "wave_speed_x": -1.0,
                    "wave_speed_y": -1.0,
                },
            )
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("metrics_basis.json: must contain per_test list or tests object" in v for v in violations)
            )

    def test_detects_metrics_basis_missing_required_variable_in_per_test_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            tests_path = (
                repo_root
                / "spec"
                / "problem"
                / "mock_domain"
                / "mock_family"
                / "mock_spec"
                / "tests.md"
            )
            tests_path.parent.mkdir(parents=True, exist_ok=True)
            tests_path.write_text(
                "## 7. テスト定義\n"
                "### 7-1. `test_a`\n",
                encoding="utf-8",
            )
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                derived_contract={
                    "source": {"tests": "spec/problem/mock_domain/mock_family/mock_spec/tests.md"},
                    "io_contract": {
                        "inputs": [{"name": "case_resolved", "source": "case.resolved.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/metrics_basis.json",
                                "raw_variables": ["h", "time"],
                            }
                        ],
                    },
                    "semantic_dependency": {"required_sources": []},
                    "raw_requirements": {
                        "required_evidence": [
                            {"artifact": "metrics_basis.json", "required": True},
                            {"artifact": "execution_trace.json", "required": True},
                            {
                                "artifact": "state_snapshots",
                                "required": True,
                                "min_samples": 1,
                                "schema": {
                                    "variables": [
                                        {"name": "h", "shape_expr": "[2,2]"}
                                    ],
                                    "time_variable": "time",
                                    "time_shape_expr": "scalar",
                                },
                            },
                        ]
                    },
                    "test_evidence_requirements": [
                        {
                            "test_id": "test_a",
                            "required_raw_variables": ["h", "time"],
                        }
                    ],
                },
                metrics_basis={
                    "per_test": [
                        {
                            "test_id": "test_a",
                            "raw_variables": {
                                "h": [[1.0, 1.0], [1.0, 1.0]],
                            },
                        }
                    ]
                },
            )
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("metrics_basis.json: test_id test_a missing required_raw_variables (['time'])" in v for v in violations)
            )

    def test_detects_snapshot_shape_mismatch_against_derived_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )

            snapshots_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
                / "raw"
                / "state_snapshots"
            )
            _write_json(
                snapshots_dir / "snapshot000.json",
                {
                    "h": [[1.0, 1.0]],
                    "hu": [[0.0, 0.0], [0.0, 0.0]],
                    "hv": [[0.0, 0.0], [0.0, 0.0]],
                    "time": 0.0,
                },
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("shape [1, 2] does not match declared shape_expr [2,2]" in v for v in violations)
            )

    def test_detects_missing_orchestration_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )

            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(
                any("orchestrations" in v and "missing" in v for v in violations)
            )

    def test_passes_with_orchestration_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            _create_minimal_orchestration_tree(repo_root)

            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertEqual([], violations)

    def test_detects_non_isolated_step_context_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            _create_minimal_orchestration_tree(repo_root)

            runs_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_test_001"
                / "agent_runs.jsonl"
            )
            lines = [line for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            items = [json.loads(line) for line in lines]
            for item in items:
                if item.get("agent_run_id") == "step_run_build_001":
                    item["context_isolated"] = False
                    break
            runs_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
                encoding="utf-8",
            )

            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(
                any("context_isolated must be true for step" in v for v in violations)
            )

    def test_detects_missing_agent_summary_ref_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            _create_minimal_orchestration_tree(repo_root)

            runs_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_test_001"
                / "agent_runs.jsonl"
            )
            lines = [line for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            items = [json.loads(line) for line in lines]
            for item in items:
                if item.get("agent_run_id") == "step_run_build_001":
                    item.pop("agent_summary_ref", None)
                    break
            runs_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
                encoding="utf-8",
            )

            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(
                any("missing agent_summary_ref for step" in v for v in violations)
            )

    def test_detects_missing_launch_prompt_ref_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            _create_minimal_orchestration_tree(repo_root)

            runs_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_test_001"
                / "agent_runs.jsonl"
            )
            lines = [line for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            items = [json.loads(line) for line in lines]
            for item in items:
                if item.get("agent_run_id") == "step_run_build_001":
                    item.pop("launch_prompt_ref", None)
                    break
            runs_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
                encoding="utf-8",
            )

            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(
                any("missing launch_prompt_ref for step" in v for v in violations)
            )

    def test_detects_missing_child_agent_identifier_in_launch_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            _create_minimal_orchestration_tree(repo_root)

            response_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_test_001"
                / "launches"
                / "step_run_build_001.response.json"
            )
            payload = json.loads(response_path.read_text(encoding="utf-8"))
            payload.pop("agent_session_id", None)
            _write_json(response_path, payload)

            child_response_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_test_001"
                / "agents"
                / "step_run_build_001"
                / "dialogs"
                / "child.response.json"
            )
            _write_json(child_response_path, payload)

            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(
                any("child agent identifier missing from launch response" in v for v in violations)
            )

    def test_detects_mismatched_agent_session_id_against_launch_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            _create_minimal_orchestration_tree(repo_root)

            runs_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_test_001"
                / "agent_runs.jsonl"
            )
            items = [
                json.loads(line)
                for line in runs_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            for item in items:
                if item.get("agent_run_id") == "step_run_build_001":
                    item["agent_session_id"] = "sess_step_build_mismatch"
                    break
            runs_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
                encoding="utf-8",
            )

            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(
                any("child agent identifier must equal agent_runs agent_session_id" in v for v in violations)
            )

    def test_detects_uninformative_agent_summary_when_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            _create_minimal_orchestration_tree(repo_root)

            summary_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_test_001"
                / "agents"
                / "step_run_build_001"
                / "dialogs"
                / "agent.summary.txt"
            )
            summary_path.write_text("pass\n", encoding="utf-8")

            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(
                any("agent.summary.txt must include status and output_refs or failure reason" in v for v in violations)
            )

    def test_detects_fabricated_orchestration_pattern_with_generic_launch_reply_and_sequential_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            _create_minimal_orchestration_tree(repo_root)

            orch_root = repo_root / "workspace" / "orchestrations" / "orch_test_001"
            response_path = orch_root / "launches" / "step_run_build_001.response.json"
            response_payload = json.loads(response_path.read_text(encoding="utf-8"))
            response_payload["agent_session_id"] = "session_1_1"
            response_payload["launch_reply"] = "problem/shallow_water2d@0.3.0 build step launched."
            _write_json(response_path, response_payload)
            _write_json(
                orch_root / "agents" / "step_run_build_001" / "dialogs" / "child.response.json",
                response_payload,
            )

            runs_path = orch_root / "agent_runs.jsonl"
            items = [
                json.loads(line)
                for line in runs_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            for item in items:
                if item.get("agent_run_id") == "step_run_build_001":
                    item["agent_session_id"] = "session_1_1"
                    item["context_id"] = "ctx_1_1"
                    break
            runs_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
                encoding="utf-8",
            )

            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(
                any("launch_reply must not be generic launched-only text" in v for v in violations)
            )
            self.assertTrue(
                any("agent_session_id must not be sequential placeholder" in v for v in violations)
            )
            self.assertTrue(
                any("context_id must not be sequential placeholder" in v for v in violations)
            )

    def test_accepts_algorithm_contract_yaml_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'diagnostics only'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            algorithm_path = (
                repo_root
                / "workspace"
                / "plans"
                / "problem__shallow_water2d__0.3.0"
                / "plan_test"
                / "algorithm.resolved.yaml"
            )
            algorithm_path.write_text(
                """algorithm_id: shallow_water2d_test_algorithm
execution_mode: sequence
ordering:
  - compute_flux
control_condition: always
iteration_contract:
  kind: none
steps:
  - step_id: compute_flux
    step_kind: flux_compute
    operation_ref: dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux
    inputs: [h, hu, hv]
    outputs: [h, hu, hv]
update_semantics:
  state_variables:
    - name: h
      shape_expr: "[2,2]"
    - name: hu
      shape_expr: "[2,2]"
    - name: hv
      shape_expr: "[2,2]"
  required_update_paths: [h, hu, hv]
  diagnostics_from_state: true
  fallback_policy: fail_closed
temporaries: []
derived_field_rules: []
invariants: []
splitting_policy:
  kind: none
""",
                encoding="utf-8",
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(any("invalid yaml" in v for v in violations))
            self.assertFalse(any("algorithm.resolved.yaml" in v for v in violations))

    def test_detects_invalid_raw_artifact_vocabulary_in_derived_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'diagnostics only'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                derived_contract={
                    "io_contract": {
                        "inputs": [{"name": "case_resolved", "source": "case.resolved.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/ghost_cells",
                            }
                        ],
                    },
                    "semantic_dependency": {"required_sources": []},
                    "raw_requirements": {
                        "required_evidence": [
                            {"artifact": "ghost_cells", "required": True, "min_samples": 1}
                        ]
                    },
                },
            )
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(any("must be one of" in v and "ghost_cells" in v for v in violations))

    def test_detects_snapshot_output_shape_mismatch_inside_derived_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'diagnostics only'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                derived_contract={
                    "io_contract": {
                        "inputs": [{"name": "case_resolved", "source": "case.resolved.yaml"}],
                        "outputs": [
                            {
                                "name": "U_np1",
                                "shape_expr": "(3, 2, 2)",
                                "evidence_ref": "raw/state_snapshots",
                                "raw_variables": ["h"],
                            }
                        ],
                    },
                    "semantic_dependency": {"required_sources": ["h"]},
                    "raw_requirements": {
                        "required_evidence": [
                            {"artifact": "metrics_basis.json", "required": True},
                            {"artifact": "execution_trace.json", "required": True},
                            {
                                "artifact": "state_snapshots",
                                "required": True,
                                "min_samples": 1,
                                "schema": {
                                    "variables": [
                                        {"name": "h", "shape_expr": "[2,2]"}
                                    ],
                                    "time_variable": "time",
                                    "time_shape_expr": "scalar",
                                },
                            },
                        ]
                    },
                },
            )
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(any("shape_expr must match referenced state_snapshots schema shape" in v for v in violations))

    def test_detects_unknown_required_raw_variables_from_tests_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            tests_path = (
                repo_root
                / "spec"
                / "problem"
                / "shallow_water2d"
                / "tests.md"
            )
            tests_path.parent.mkdir(parents=True, exist_ok=True)
            tests_path.write_text("### 1-1. `l0_case_pass`\n", encoding="utf-8")
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'diagnostics only'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                derived_contract={
                    "source": {"tests": "spec/problem/shallow_water2d/tests.md"},
                    "io_contract": {
                        "inputs": [{"name": "case_resolved", "source": "case.resolved.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/metrics_basis.json",
                                "raw_variables": ["h"],
                            }
                        ],
                    },
                    "semantic_dependency": {"required_sources": ["h"]},
                    "raw_requirements": {
                        "required_evidence": [
                            {"artifact": "metrics_basis.json", "required": True},
                            {"artifact": "execution_trace.json", "required": True},
                            {
                                "artifact": "state_snapshots",
                                "required": True,
                                "min_samples": 1,
                                "schema": {
                                    "variables": [
                                        {"name": "h", "shape_expr": "[2,2]"}
                                    ],
                                    "time_variable": "time",
                                    "time_shape_expr": "scalar",
                                },
                            },
                        ]
                    },
                    "test_evidence_requirements": [
                        {
                            "test_id": "l0_case_pass",
                            "required_raw_variables": ["ghost_cells_x"],
                        }
                    ],
                },
            )
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(any("required_raw_variables[0] must reference declared state_snapshots variable/time_variable" in v for v in violations))

    def test_detects_missing_plan_step_result_when_orchestration_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            _create_minimal_orchestration_tree(repo_root)
            step_result = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_test_001"
                / "steps"
                / "problem__shallow_water2d__0.3.0"
                / "plan"
                / "orch_run_001"
                / "step_result.json"
            )
            step_result.unlink()
            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(any("missing step_result.json" in v and "problem__shallow_water2d__0.3.0/plan" in v for v in violations))

    def test_detects_missing_pipeline_lineage_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
                / "lineage.json"
            ).unlink()
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(any("lineage.json: missing" in v for v in violations))

    def test_detects_missing_graph_child_run_when_orchestration_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            _create_minimal_orchestration_tree(repo_root)
            runs_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_test_001"
                / "agent_runs.jsonl"
            )
            items = [
                json.loads(line)
                for line in runs_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            items = [
                item for item in items if item.get("agent_run_id") != "step_run_execute_001"
            ]
            runs_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
                encoding="utf-8",
            )
            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(
                any("child_agent_run_id not found in agent_runs.jsonl" in v for v in violations)
            )

    def test_detects_non_template_launch_prompt_when_orchestration_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            _create_minimal_orchestration_tree(repo_root)
            prompt_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_test_001"
                / "launches"
                / "step_run_build_001.prompt.txt"
            )
            prompt_path.write_text(
                "Build step for node problem/shallow_water2d@0.3.0\n",
                encoding="utf-8",
            )
            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(
                any("launch_prompt_ref missing workflow-orchestration template markers" in v for v in violations)
            )

    def test_validate_plan_stage_passes_for_resolved_plan_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["x", "y"],
            )
            violations = validate_plan_stage(
                repo_root,
                "workspace",
                "workspace/plans/problem__shallow_water2d__0.3.0/plan_test",
            )
            self.assertEqual(violations, [])

    def test_validate_plan_stage_rejects_non_plans_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            violations = validate_plan_stage(
                repo_root,
                "workspace",
                "workspace/pipelines/foo/bar",
            )
            self.assertTrue(
                any("plan_ref must be under" in v for v in violations), violations
            )

    def test_validate_post_generate_stage_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            dep_model_text = """module dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
  logical, intent(out) :: flag
  flag = .true.
end subroutine dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux
end module dynamics_shallow_water_flux_2d_rusanov_p0_model
"""
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            makefile_text = """FC ?= gfortran
OBJS = dynamics_shallow_water_flux_2d_rusanov_p0_model.o shallow_water2d_model.o shallow_water2d_runner.o

simulate: $(OBJS)
\t$(FC) -o $@ $(OBJS)

dynamics_shallow_water_flux_2d_rusanov_p0_model.o dynamics_shallow_water_flux_2d_rusanov_p0_model.mod: dynamics_shallow_water_flux_2d_rusanov_p0_model.f90
\t$(FC) -c $<

shallow_water2d_model.o shallow_water2d_model.mod: shallow_water2d_model.f90 dynamics_shallow_water_flux_2d_rusanov_p0_model.mod
\t$(FC) -c $<

shallow_water2d_runner.o: shallow_water2d_runner.f90 shallow_water2d_model.mod
\t$(FC) -c $<
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["x", "y"],
                extra_sources={
                    "dynamics_shallow_water_flux_2d_rusanov_p0_model.f90": dep_model_text
                },
                makefile_text=makefile_text,
            )
            violations = validate_post_generate_stage(
                repo_root,
                "workspace",
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "problem__shallow_water2d__0.3.0_test_pipeline",
                generation_id="gen_test_001",
            )
            self.assertEqual(violations, [])

    def test_validate_post_generate_stage_rejects_failed_run_linter_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["x", "y"],
            )
            pipeline_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
            )
            log_path = pipeline_dir / "generate" / "gen_test_001" / "src" / "mcp_command_log.jsonl"
            log_path.write_text(
                json.dumps(
                    {
                        "command_id": "lint_cmd_fixture_001",
                        "tool_name": "run_linter",
                        "command": ["fortitude", "check", "."],
                        "ok": False,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            violations = validate_post_generate_stage(
                repo_root,
                "workspace",
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "problem__shallow_water2d__0.3.0_test_pipeline",
                generation_id="gen_test_001",
            )
            self.assertTrue(
                any("run_linter did not succeed" in v for v in violations), violations
            )

    def test_validate_post_generate_stage_rejects_lint_command_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["x", "y"],
            )
            pipeline_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_test_pipeline"
            )
            log_path = pipeline_dir / "generate" / "gen_test_001" / "src" / "mcp_command_log.jsonl"
            log_path.write_text(
                json.dumps(
                    {
                        "command_id": "lint_cmd_fixture_001",
                        "tool_name": "run_linter",
                        "command": ["cppcheck", "--error-exitcode=1", "."],
                        "ok": True,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            violations = validate_post_generate_stage(
                repo_root,
                "workspace",
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "problem__shallow_water2d__0.3.0_test_pipeline",
                generation_id="gen_test_001",
            )
            self.assertTrue(
                any("logged command does not match preset" in v for v in violations),
                violations,
            )

    def test_validate_generate_meta_accepts_fail_without_lint_command_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_dir = Path(tmp) / "pipeline"
            gen_dir = pipeline_dir / "generate" / "gen_fail_001"
            gen_dir.mkdir(parents=True)
            _write_json(
                gen_dir / "generate_meta.json",
                {
                    "attempt_count": 1,
                    "verification_status": "fail",
                    "last_fail_reason": "lint failed",
                    "debug_mode": False,
                    "context_isolated": True,
                },
            )
            violations: list[str] = []
            _validate_generate_meta_json_files(pipeline_dir, violations)
            self.assertEqual(violations, [])

    def test_validate_generate_lint_rejects_pass_without_lint_command_ref(self) -> None:
        violations: list[str] = []
        meta_path = Path("/tmp/generate_meta.json")
        _validate_generate_lint_command_logs(
            Path("/repo"),
            meta_path,
            {"verification_status": "pass"},
            "fortran",
            violations,
        )
        self.assertTrue(
            any("missing lint_command_ref when verification_status=pass" in v for v in violations),
            violations,
        )

    def test_validate_generate_lint_rejects_non_dict_lint_command_ref_when_pass(self) -> None:
        violations: list[str] = []
        meta_path = Path("/tmp/generate_meta.json")
        _validate_generate_lint_command_logs(
            Path("/repo"),
            meta_path,
            {"verification_status": "pass", "lint_command_ref": []},
            "fortran",
            violations,
        )
        self.assertTrue(
            any(
                "lint_command_ref must be json object when verification_status=pass" in v
                for v in violations
            ),
            violations,
        )

    def test_validate_generate_lint_mixed_requires_exactly_two_entries(self) -> None:
        violations: list[str] = []
        meta_path = Path("/tmp/generate_meta.json")
        data = {
            "verification_status": "pass",
            "lint_command_ref": {
                "run_linter": [
                    {
                        "command_id": "a",
                        "command_log_ref": "workspace/pipelines/x/y/z/mcp_command_log.jsonl",
                        "preset": "fortitude",
                    },
                    {
                        "command_id": "b",
                        "command_log_ref": "workspace/pipelines/x/y/z/mcp_command_log.jsonl",
                        "preset": "fortitude",
                    },
                    {
                        "command_id": "c",
                        "command_log_ref": "workspace/pipelines/x/y/z/mcp_command_log.jsonl",
                        "preset": "cppcheck",
                    },
                ]
            },
        }
        _validate_generate_lint_command_logs(Path("/repo"), meta_path, data, "mixed", violations)
        self.assertTrue(
            any("requires exactly two run_linter entries" in v for v in violations),
            violations,
        )

    def test_validate_post_build_stage_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            dep_model_text = """module dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
  logical, intent(out) :: flag
  flag = .true.
end subroutine dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux
end module dynamics_shallow_water_flux_2d_rusanov_p0_model
"""
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            makefile_text = """FC ?= gfortran
OBJS = dynamics_shallow_water_flux_2d_rusanov_p0_model.o shallow_water2d_model.o shallow_water2d_runner.o

simulate: $(OBJS)
\t$(FC) -o $@ $(OBJS)

dynamics_shallow_water_flux_2d_rusanov_p0_model.o dynamics_shallow_water_flux_2d_rusanov_p0_model.mod: dynamics_shallow_water_flux_2d_rusanov_p0_model.f90
\t$(FC) -c $<

shallow_water2d_model.o shallow_water2d_model.mod: shallow_water2d_model.f90 dynamics_shallow_water_flux_2d_rusanov_p0_model.mod
\t$(FC) -c $<

shallow_water2d_runner.o: shallow_water2d_runner.f90 shallow_water2d_model.mod
\t$(FC) -c $<
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["x", "y"],
                extra_sources={
                    "dynamics_shallow_water_flux_2d_rusanov_p0_model.f90": dep_model_text
                },
                makefile_text=makefile_text,
            )
            violations = validate_post_build_stage(
                repo_root,
                "workspace",
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "problem__shallow_water2d__0.3.0_test_pipeline",
                generation_id="gen_test_001",
            )
            self.assertEqual(violations, [])


    def test_validate_rejects_all_zero_metrics_basis(self) -> None:
        """metrics_basis.json の全数値が 0.0 のとき violation が発生すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                metrics_basis={"value_a": 0.0, "value_b": 0.0},
            )
            violations = validate(repo_root, workspace_root="workspace")
            self.assertTrue(
                any("trivial placeholder" in v for v in violations),
                f"Expected trivial placeholder violation, got: {violations}",
            )

    def test_validate_rejects_all_null_metrics_basis(self) -> None:
        """metrics_basis.json の全フィールドが null のとき violation が発生すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                metrics_basis={"value_a": None, "value_b": None},
            )
            violations = validate(repo_root, workspace_root="workspace")
            self.assertTrue(
                any("trivial placeholder" in v for v in violations),
                f"Expected trivial placeholder violation, got: {violations}",
            )

    def test_validate_accepts_partially_nonzero_metrics_basis(self) -> None:
        """metrics_basis.json の一部に非ゼロ実数値があれば通過すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                metrics_basis={"value_a": 0.0, "value_b": 1.5},
            )
            violations = validate(repo_root, workspace_root="workspace")
            self.assertFalse(
                any("trivial placeholder" in v for v in violations),
                f"Expected no trivial placeholder violation, got: {violations}",
            )

    def test_validate_skips_metrics_basis_check_if_no_numeric_fields(self) -> None:
        """metrics_basis.json に数値フィールドが一切なければ trivial チェックをスキップする。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
implicit none
write(*,*) 'ok'
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                metrics_basis={"label": "test", "tags": ["a", "b"]},
            )
            violations = validate(repo_root, workspace_root="workspace")
            self.assertFalse(
                any("trivial placeholder" in v for v in violations),
                f"Expected no trivial placeholder violation, got: {violations}",
            )


if __name__ == "__main__":
    unittest.main()
