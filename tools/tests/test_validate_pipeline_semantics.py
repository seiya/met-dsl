#!/usr/bin/env python3
"""Regression tests for pipeline semantic validation anti-cheat rules."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from tools.validate_pipeline_semantics import (
    _BUNDLED_SHAPE_EXPR_SCHEMA_PATH,
    _validate_generate_lint_command_logs,
    _validate_generate_meta_json_files,
    validate,
    validate_plan_stage,
    validate_post_build_stage,
    validate_post_generate_stage,
)


def _seed_shape_expr_schema_into(repo_root: Path) -> None:
    """Copy the validator-bundled shape_expr.schema.json into a test's tmp
    repo so the public validate_*() entrypoints (which fail-closed when a
    repo_root is in scope without a target schema) work under realistic
    fixtures. Tests that intentionally exercise the missing-schema path
    must NOT call this helper.

    Idempotent — safe to invoke after `repo_root` is created and before
    any validate_*() call. Uses bytes copy so canonical-source equivalence
    is preserved (no JSON re-serialization drift)."""
    target = repo_root / "spec" / "schema" / "plan" / "shape_expr.schema.json"
    if target.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_BUNDLED_SHAPE_EXPR_SCHEMA_PATH.read_bytes())


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
plan_ref: workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001
pipeline_ref: workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001
dependency_ref: workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml
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
plan_ref: workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001
pipeline_ref: workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001
dependency_ref: workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml
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
    pipeline_id = "shallow-water2d_20260415_001"
    exec_id = "exe_test_001"

    pipeline_dir = workspace / "pipelines" / node_safe / pipeline_id
    node_dir = pipeline_dir / "execute" / exec_id / "problem" / "shallow_water2d"
    raw_dir = node_dir / "raw"
    snapshots_dir = raw_dir / "state_snapshots"
    src_dir = pipeline_dir / "generate" / "gen_20260415_001" / "src"
    # Canonical placement for in-phase MCP audit log: sibling of trial_meta.
    log_path = node_dir / "mcp_command_log.jsonl"

    _write_json(
        pipeline_dir / "lineage.json",
        {
            "node_key": "problem/shallow_water2d@0.3.0",
            "pipeline_id": pipeline_id,
            "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
            "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/dependency.resolved.yaml",
        },
    )
    lint_command_id = "lint_cmd_fixture_001"
    rel_lint_log = (
        f"workspace/pipelines/{node_safe}/{pipeline_id}/generate/gen_20260415_001/src/mcp_command_log.jsonl"
    )
    if dependency_resolved is None:
        dependency_resolved = {
            "node_key": "problem/shallow_water2d@0.3.0",
            "direct_deps": [f"component/{dep_spec_id}@0.1.0"],
            "transitive_deps": [f"component/{dep_spec_id}@0.1.0"],
            "topo_level": 1,
        }
    _write_json(
        workspace / "plans" / "problem__shallow_water2d__0.3.0" / "shallow-water2d_20260415_001" / "dependency.resolved.yaml",
        dependency_resolved,
    )
    _write_json(
        workspace / "plans" / "problem__shallow_water2d__0.3.0" / "shallow-water2d_20260415_001" / "plan_meta.json",
        {
            "attempt_count": 1,
            "verification_status": "pass",
            "last_fail_reason": None,
            "debug_mode": False,
            "context_isolated": True,
        },
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
        workspace / "plans" / "problem__shallow_water2d__0.3.0" / "shallow-water2d_20260415_001" / "algorithm.resolved.yaml",
        algorithm_contract,
    )
    if derived_contract is None:
        derived_contract = {
            "io_contract": {
                "inputs": [
                    {
                        "name": "case_resolved",
                        "source": "case.resolved.yaml",
                        "evidence_ref": "case.resolved.yaml",
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
        workspace / "plans" / "problem__shallow_water2d__0.3.0" / "shallow-water2d_20260415_001" / "derived_contract.json",
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
        workspace / "plans" / "problem__shallow_water2d__0.3.0" / "shallow-water2d_20260415_001" / "impl.resolved.yaml",
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
    # Plant a build directory + binary so trial_meta.source_build_id resolves
    # under <pipeline>/build/<id>/bin/ and run_program executable validation
    # binds to the declared build.
    build_id_for_fixture = "build_20260415_001"
    build_bin_dir = pipeline_dir / "build" / build_id_for_fixture / "bin"
    build_bin_dir.mkdir(parents=True, exist_ok=True)
    (build_bin_dir / "simulate").write_text("binary\n", encoding="utf-8")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(
            {
                "command_id": command_id,
                "tool_name": "run_program",
                "command": run_command,
                "cwd": str(build_bin_dir),
                "ok": True,
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
            "source_generation_id": "gen_20260415_001",
            "source_build_id": build_id_for_fixture,
            "source_command_ref": {
                "run_threads_1": {
                    "command_id": command_id,
                    "tool_name": "run_program",
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
        pipeline_dir / "generate" / "gen_20260415_001" / "generate_meta.json",
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
            _seed_shape_expr_schema_into(repo_root)
            violations = validate(repo_root=repo_root, workspace_root="workspace/runs/sample")
            self.assertTrue(
                any("workspace_root must be exactly 'workspace'" in v for v in violations)
            )

    def test_ignores_empty_execution_node_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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

    def test_state_variables_shorthand_in_snapshot_schema_is_rejected(self) -> None:
        """Regression: the `state_variables: [name, ...]` shorthand (variable
        names only, no per-variable `shape_expr`) is no longer a valid form
        for snapshot schemas. The previous wildcard-sentinel handling let
        corrupted/wrong-rank state_snapshot payloads survive into pre_judge
        because every shape comparison short-circuited to True. The contract
        is now: `variables: [{name, shape_expr}, ...]` everywhere.

        This test asserts:
          (a) snapshot_schema.json with `state_variables` shorthand fails
              validation with a specific 'shorthand is not supported' message;
          (b) derived_contract.json with the same shorthand also fails with
              the parallel violation on `raw_requirements.required_evidence`;
          (c) the shape-validation path is no longer disabled — `_shape_matches_expr`
              has no sentinel bypass."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
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

            workspace = repo_root / "workspace"
            node_safe = "problem__shallow_water2d__0.3.0"
            pipeline_id = "shallow-water2d_20260415_001"
            snapshots_dir = (
                workspace / "pipelines" / node_safe / pipeline_id
                / "execute" / "exe_test_001" / "problem" / "shallow_water2d"
                / "raw" / "state_snapshots"
            )
            (snapshots_dir / "snapshot_schema.json").write_text(
                json.dumps({
                    "state_variables": ["h", "hu", "hv"],
                    "time_variable": "time",
                    "time_shape_expr": "scalar",
                }),
                encoding="utf-8",
            )
            derived_path = (
                workspace / "plans" / node_safe / "shallow-water2d_20260415_001"
                / "derived_contract.json"
            )
            derived = json.loads(derived_path.read_text(encoding="utf-8"))
            for entry in derived["raw_requirements"]["required_evidence"]:
                if entry.get("artifact") == "state_snapshots":
                    entry["schema"] = {
                        "state_variables": ["h", "hu", "hv"],
                        "time_variable": "time",
                        "time_shape_expr": "scalar",
                    }
            derived_path.write_text(json.dumps(derived), encoding="utf-8")

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            # (a) snapshot_schema rejected
            self.assertTrue(
                any(
                    "snapshot_schema.json" in v
                    and "'state_variables' shorthand is not supported" in v
                    for v in violations
                ),
                f"Expected snapshot_schema shorthand rejection; got: {violations}",
            )
            # (b) derived_contract rejected
            self.assertTrue(
                any(
                    "derived_contract.json" in v
                    and "must not use 'state_variables' shorthand" in v
                    for v in violations
                ),
                f"Expected derived_contract shorthand rejection; got: {violations}",
            )

    def test_shape_expr_schema_cache_invalidates_on_file_mtime_change(self) -> None:
        """Regression: long-lived processes must observe schema-content
        changes at the same path within a single process (rebases, branch
        switches, schema repairs). Previously the cache was keyed by path
        only, so a mutated schema kept reading the old (potentially broken
        or stricter) ruleset until process restart — reintroducing the
        version-skew hazard the schema-driven design was supposed to
        prevent."""
        import os
        import time
        from tools.validate_pipeline_semantics import (
            _active_repo_root_for_schema,
            _load_shape_expr_patterns_cached,
            _parse_shape_expr,
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            sd = repo_root / "spec" / "schema" / "plan"
            sd.mkdir(parents=True)
            schema_path = sd / "shape_expr.schema.json"
            # Initial schema: integer-only list form (rejects identifiers).
            schema_path.write_text(
                json.dumps({
                    "oneOf": [
                        {"x-shape-form": "scalar", "pattern": r"^[Ss][Cc][Aa][Ll][Aa][Rr]$"},
                        {"x-shape-form": "list", "pattern": r"^\[[0-9]+\]$"},
                    ]
                }),
                encoding="utf-8",
            )
            _load_shape_expr_patterns_cached.cache_clear()
            token = _active_repo_root_for_schema.set(repo_root)
            try:
                ok_int_initial, _, _ = _parse_shape_expr("[3]")
                ok_id_initial, _, _ = _parse_shape_expr("[nx]")
                self.assertTrue(ok_int_initial, "[3] must pass under integer-only schema")
                self.assertFalse(ok_id_initial, "[nx] must fail under integer-only schema")
                # Mutate schema to identifier-only list form. Bump mtime to
                # ensure invalidation is observed regardless of filesystem
                # mtime resolution.
                schema_path.write_text(
                    json.dumps({
                        "oneOf": [
                            {"x-shape-form": "scalar", "pattern": r"^[Ss][Cc][Aa][Ll][Aa][Rr]$"},
                            {"x-shape-form": "list", "pattern": r"^\[[A-Za-z_][A-Za-z0-9_]*\]$"},
                        ]
                    }),
                    encoding="utf-8",
                )
                future = time.time() + 1.0
                os.utime(schema_path, (future, future))
                # WITHOUT manually clearing the cache, the loader must observe
                # the new content via mtime invalidation.
                ok_int_after, _, _ = _parse_shape_expr("[3]")
                ok_id_after, _, _ = _parse_shape_expr("[nx]")
                self.assertFalse(
                    ok_int_after,
                    "[3] must now fail; cache must invalidate on mtime change",
                )
                self.assertTrue(
                    ok_id_after,
                    "[nx] must now pass; cache must invalidate on mtime change",
                )
            finally:
                _active_repo_root_for_schema.reset(token)
                _load_shape_expr_patterns_cached.cache_clear()

    def test_shape_expr_schema_loader_accepts_identifier_only_repo_local_grammar(self) -> None:
        """Regression: a repo-local schema that legitimately allows ONLY
        symbolic dim tokens (no integer literals) must NOT be rejected as
        'malformed'. Previously the loader's classifier only tried integer
        probes (`[1]`, `(1)`) and treated any branch failing those probes
        as malformed — making startup hard-fail for schemas that exclude
        integer literals.

        Two paths are now supported:
          (a) explicit `x-shape-form` metadata → trusted regardless of probe
              behavior;
          (b) richer probe set (`[a]`, `[Nx]`, etc.) → probe-classifies
              identifier-only schemas correctly without explicit metadata."""
        from tools.validate_pipeline_semantics import (
            _active_repo_root_for_schema,
            _load_shape_expr_patterns_cached,
            _parse_shape_expr,
        )
        # (a) Probe-based: identifier-only list-form schema, no metadata.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            sd = repo_root / "spec" / "schema" / "plan"
            sd.mkdir(parents=True)
            schema = {
                "oneOf": [
                    {"pattern": r"^[Ss][Cc][Aa][Ll][Aa][Rr]$"},
                    {
                        "pattern": r"^\[[A-Za-z_][A-Za-z0-9_]*(?:,[A-Za-z_][A-Za-z0-9_]*)*\]$",
                    },
                ]
            }
            (sd / "shape_expr.schema.json").write_text(json.dumps(schema), encoding="utf-8")
            _load_shape_expr_patterns_cached.cache_clear()
            token = _active_repo_root_for_schema.set(repo_root)
            try:
                ok, dims, _ = _parse_shape_expr("[nx]")
                self.assertTrue(ok, "id-only schema must accept [nx] via probe matrix")
                self.assertEqual(dims, ["nx"])
                ok_int, _, _ = _parse_shape_expr("[3]")
                self.assertFalse(ok_int, "id-only schema must reject [3] (regex disallows)")
            finally:
                _active_repo_root_for_schema.reset(token)
                _load_shape_expr_patterns_cached.cache_clear()
        # (b) Explicit x-shape-form: regex matches NEITHER probe (requires
        # 2+ char identifier) but author asserts it is list-form.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            sd = repo_root / "spec" / "schema" / "plan"
            sd.mkdir(parents=True)
            schema = {
                "oneOf": [
                    {"x-shape-form": "scalar", "pattern": r"^[Ss][Cc][Aa][Ll][Aa][Rr]$"},
                    {
                        "x-shape-form": "list",
                        "pattern": r"^\[[A-Za-z_][A-Za-z0-9_]+(?:,[A-Za-z_][A-Za-z0-9_]+)*\]$",
                    },
                ]
            }
            (sd / "shape_expr.schema.json").write_text(json.dumps(schema), encoding="utf-8")
            _load_shape_expr_patterns_cached.cache_clear()
            token = _active_repo_root_for_schema.set(repo_root)
            try:
                ok_two, _, _ = _parse_shape_expr("[nx]")
                self.assertTrue(ok_two, "explicit x-shape-form list accepts 2+ char id [nx]")
                ok_one, _, _ = _parse_shape_expr("[a]")
                self.assertFalse(ok_one, "exotic 2+-char regex correctly rejects single-char [a]")
            finally:
                _active_repo_root_for_schema.reset(token)
                _load_shape_expr_patterns_cached.cache_clear()
        # (c) Negative regression: unclassifiable branch (no metadata, no
        # probe match) still raises RuntimeError with a hint to use
        # `x-shape-form` for disambiguation.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            sd = repo_root / "spec" / "schema" / "plan"
            sd.mkdir(parents=True)
            broken = {
                "oneOf": [
                    {"pattern": r"^[Ss][Cc][Aa][Ll][Aa][Rr]$"},
                    {"pattern": r"^XXXXXX$"},  # matches nothing canonical
                ]
            }
            (sd / "shape_expr.schema.json").write_text(json.dumps(broken), encoding="utf-8")
            _load_shape_expr_patterns_cached.cache_clear()
            token = _active_repo_root_for_schema.set(repo_root)
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    _parse_shape_expr("[3]")
                self.assertIn("x-shape-form", str(ctx.exception))
            finally:
                _active_repo_root_for_schema.reset(token)
                _load_shape_expr_patterns_cached.cache_clear()

    def test_shape_expr_schema_loader_rejects_structural_malformations(self) -> None:
        """Regression: structurally invalid (but JSON-decodable) schemas must
        produce `RuntimeError`, not `AttributeError` from `.get()` on the
        wrong type. The CLI / run_workflow guards only recover from
        `RuntimeError`; an opaque traceback would defeat the structured
        FAIL output that orchestration gates parse for recovery."""
        from tools.validate_pipeline_semantics import (
            _active_repo_root_for_schema,
            _load_shape_expr_patterns_cached,
            _parse_shape_expr,
        )
        cases = [
            ("null", "null", "top-level must be a JSON object"),
            ("list", "[]", "top-level must be a JSON object"),
            ("missing oneOf", json.dumps({"title": "no oneOf"}), "must declare a top-level 'oneOf' array"),
            ("oneOf as string", json.dumps({"oneOf": "oops"}), "'oneOf' must be a JSON array"),
            ("oneOf with non-dict", json.dumps({"oneOf": [42]}), "must be a JSON object"),
            ("branch missing pattern", json.dumps({"oneOf": [{"title": "x"}]}), "pattern must be a non-empty string"),
            ("branch pattern empty", json.dumps({"oneOf": [{"pattern": ""}]}), "pattern must be a non-empty string"),
        ]
        for label, content, expected_msg_fragment in cases:
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                sd = repo / "spec" / "schema" / "plan"
                sd.mkdir(parents=True)
                (sd / "shape_expr.schema.json").write_text(content, encoding="utf-8")
                _load_shape_expr_patterns_cached.cache_clear()
                token = _active_repo_root_for_schema.set(repo)
                try:
                    with self.assertRaises(RuntimeError) as ctx:
                        _parse_shape_expr("[3]")
                    self.assertIn(
                        expected_msg_fragment, str(ctx.exception),
                        f"case {label!r}: expected {expected_msg_fragment!r} in error message; got: {ctx.exception}",
                    )
                except AssertionError:
                    raise
                except Exception as exc:  # pragma: no cover
                    self.fail(
                        f"case {label!r}: expected RuntimeError, got {type(exc).__name__}: {exc}"
                    )
                finally:
                    _active_repo_root_for_schema.reset(token)
                    _load_shape_expr_patterns_cached.cache_clear()

    def test_parser_grammar_is_schema_driven_no_hardcoded_dim_token_limit(self) -> None:
        """Regression: dim-token grammar must be owned by the active schema's
        list-form regex, NOT shadowed by a hardcoded post-check. Previously
        the parser re-validated each dim token against `_SHAPE_EXPR_DIM_TOKEN`
        (`[0-9]+|[A-Za-z_][A-Za-z0-9_]*`), so a repo-local schema that
        legitimately accepts e.g. arithmetic-like dim tokens (`n+1`) would be
        rejected by the parser AFTER the regex matched — a drift hazard.

        With the schema fully driving grammar:
          - bundled schema (strict grammar) still rejects function-call etc.
            via its own regex.
          - a repo-local schema that allows arithmetic dim tokens accepts
            them and `_shape_matches_expr` treats them as bindable
            identifiers (case-sensitive)."""
        from tools.validate_pipeline_semantics import (
            _active_repo_root_for_schema,
            _load_shape_expr_patterns_cached,
            _parse_shape_expr,
            _shape_matches_expr,
        )
        # Bundled schema continues to reject function-call notation by its
        # own regex (no hidden post-check needed).
        _load_shape_expr_patterns_cached.cache_clear()
        for expr in ("[vector(3)]", "[matrix(3,3)]", "[3, vector(2)]", "vector(3)", "tensor"):
            ok, _, _ = _parse_shape_expr(expr)
            self.assertFalse(ok, f"bundled schema must reject {expr!r} via its own regex")
        # Repo-local schema with extended dim-token grammar must work.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            sd = repo / "spec" / "schema" / "plan"
            sd.mkdir(parents=True)
            schema = {
                "oneOf": [
                    {"pattern": r"^[Ss][Cc][Aa][Ll][Aa][Rr]$"},
                    # Allow tokens that include `+` and `-` (arithmetic-like)
                    # in addition to alnum.
                    {"pattern": r"^\[[A-Za-z0-9+\-]+(?:,[A-Za-z0-9+\-]+)*\]$"},
                ]
            }
            (sd / "shape_expr.schema.json").write_text(json.dumps(schema), encoding="utf-8")
            _load_shape_expr_patterns_cached.cache_clear()
            token = _active_repo_root_for_schema.set(repo)
            try:
                ok, dims, _ = _parse_shape_expr("[n+1]")
                self.assertTrue(ok, "repo-local extended schema must accept [n+1]")
                self.assertEqual(dims, ["n+1"])
                ok, dims, _ = _parse_shape_expr("[3,n+1,m-2]")
                self.assertTrue(ok)
                self.assertEqual(dims, ["3", "n+1", "m-2"])
                # Symbolic identifier semantics still apply for non-digit tokens.
                self.assertTrue(_shape_matches_expr("[n+1,n+1]", [4, 4]))
                self.assertFalse(_shape_matches_expr("[n+1,n+1]", [4, 5]))
            finally:
                _active_repo_root_for_schema.reset(token)
                _load_shape_expr_patterns_cached.cache_clear()

    def test_shape_expr_schema_loader_classifies_branches_structurally(self) -> None:
        """Regression: branch classification must use the regex behavior, not
        the human-readable `title`. A schema with valid regexes but renamed
        or localized titles must still load successfully. A schema whose
        regexes do not accept any canonical probe must be rejected with a
        clear error so operators do not ship broken patterns."""
        from tools.validate_pipeline_semantics import (
            _active_repo_root_for_schema,
            _load_shape_expr_patterns_cached,
            _parse_shape_expr,
        )
        # Case A: localized titles, structurally valid regexes → must load.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            sd = repo_root / "spec" / "schema" / "plan"
            sd.mkdir(parents=True)
            schema = {
                "oneOf": [
                    {"title": "0次元 (zero-dim)", "pattern": r"^[Ss][Cc][Aa][Ll][Aa][Rr]$"},
                    {"title": "リスト形 (list form)", "pattern": r"^\[[0-9]+(?:,[0-9]+)*\]$"},
                ]
            }
            (sd / "shape_expr.schema.json").write_text(
                json.dumps(schema, ensure_ascii=False), encoding="utf-8"
            )
            _load_shape_expr_patterns_cached.cache_clear()
            token = _active_repo_root_for_schema.set(repo_root)
            try:
                ok_scalar, _, err_scalar = _parse_shape_expr("scalar")
                self.assertTrue(ok_scalar, f"localized scalar branch should match: {err_scalar}")
                ok_list, _, err_list = _parse_shape_expr("[3]")
                self.assertTrue(ok_list, f"localized list branch should match: {err_list}")
            finally:
                _active_repo_root_for_schema.reset(token)
                _load_shape_expr_patterns_cached.cache_clear()
        # Case B: branch with a regex that matches NEITHER probe is a
        # malformed schema. Must raise a clear RuntimeError naming the
        # offending pattern so operators can repair it.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            sd = repo_root / "spec" / "schema" / "plan"
            sd.mkdir(parents=True)
            broken = {
                "oneOf": [
                    {"title": "scalar", "pattern": r"^scalar$"},
                    {"title": "garbage", "pattern": r"^XXXXXX$"},  # matches nothing canonical
                ]
            }
            (sd / "shape_expr.schema.json").write_text(json.dumps(broken), encoding="utf-8")
            _load_shape_expr_patterns_cached.cache_clear()
            token = _active_repo_root_for_schema.set(repo_root)
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    _parse_shape_expr("[3]")
                msg = str(ctx.exception)
                self.assertIn("matches no probe", msg)
                self.assertIn("XXXXXX", msg)
                self.assertIn("x-shape-form", msg)
            finally:
                _active_repo_root_for_schema.reset(token)
                _load_shape_expr_patterns_cached.cache_clear()

    def test_shape_expr_identifiers_are_case_sensitive(self) -> None:
        """Regression: identifier-style dim tokens are case-SENSITIVE so
        `Nx` and `nx` are distinct. Previously canonicalization lowercased
        every dim, silently merging `[Nx, nx]` into `[nx,nx]` and then
        enforcing equal extents — over-constraining shape contracts. Only
        the literal scalar form is normalized to lowercase `"scalar"`
        because the schema explicitly defines that form case-insensitively."""
        from tools.validate_pipeline_semantics import (
            _canonical_shape_expr,
            _shape_matches_expr,
        )
        # Distinct case-different identifiers stay independent.
        self.assertTrue(_shape_matches_expr("[Nx, nx]", [2, 3]))
        self.assertTrue(_shape_matches_expr("[Nx, nx]", [3, 3]))
        # Repeated identifier (same case) still binds.
        self.assertFalse(_shape_matches_expr("[Nx, Nx]", [2, 3]))
        self.assertTrue(_shape_matches_expr("[Nx, Nx]", [5, 5]))
        # Canonicalization preserves identifier case (only whitespace is
        # collapsed; only the scalar literal is normalized).
        self.assertEqual(_canonical_shape_expr("[Nx, nx]"), "[Nx,nx]")
        self.assertEqual(_canonical_shape_expr("[NX, nX, Ny]"), "[NX,nX,Ny]")
        # Scalar literal is still case-insensitive (schema regex matches all).
        self.assertEqual(_canonical_shape_expr("Scalar"), "scalar")
        self.assertEqual(_canonical_shape_expr("SCALAR"), "scalar")

    def test_shape_matches_expr_binds_repeated_symbolic_dimensions(self) -> None:
        """Regression: a symbolic `shape_expr` like `[n,n]` is a CONTRACT that
        the two extents are equal at runtime. The matcher must enforce that
        repeated identifiers bind to the same observed value; previously every
        symbolic token was unconstrained, so `[n,n]` accepted `[2,3]` and the
        canonical contract was effectively a wildcard.

        Distinct identifiers (e.g. `[nx,ny]`) are independently bound — they
        may but need not coincide. Identifier matching is case-insensitive
        because `_canonical_shape_expr` lowercases dim tokens."""
        from tools.validate_pipeline_semantics import _shape_matches_expr
        # Repeated identifier: must agree.
        self.assertTrue(_shape_matches_expr("[n,n]", [4, 4]))
        self.assertFalse(_shape_matches_expr("[n,n]", [2, 3]))
        self.assertFalse(_shape_matches_expr("[nx,nx]", [2, 3]))
        self.assertTrue(_shape_matches_expr("[nx,nx]", [5, 5]))
        # Distinct identifiers: independent bindings — any rectangular shape OK.
        self.assertTrue(_shape_matches_expr("[nx,ny]", [2, 3]))
        self.assertTrue(_shape_matches_expr("[nx,ny]", [3, 3]))
        # Mixed literal + repeated symbolic.
        self.assertTrue(_shape_matches_expr("[3,n,n]", [3, 5, 5]))
        self.assertFalse(_shape_matches_expr("[3,n,n]", [3, 5, 7]))
        # Literal mismatch never recovers from earlier bindings.
        self.assertFalse(_shape_matches_expr("[3,n,n]", [4, 5, 5]))

    def test_shape_matches_expr_has_no_sentinel_bypass(self) -> None:
        """Regression: `_shape_matches_expr` must NOT short-circuit to True on
        any internal sentinel value. A wrong-rank payload against the canonical
        `variables: [{name, shape_expr}, ...]` form must fail."""
        from tools.validate_pipeline_semantics import _shape_matches_expr
        # Concrete shape_expr behaves correctly.
        self.assertTrue(_shape_matches_expr("[2,2]", [2, 2]))
        self.assertFalse(_shape_matches_expr("[2,2]", [2, 3]))  # wrong dim
        self.assertFalse(_shape_matches_expr("[2,2]", [2]))     # wrong rank
        # No sentinel string should be treated as wildcard.
        self.assertFalse(_shape_matches_expr("__any_shape_sentinel__", [10]))
        self.assertFalse(_shape_matches_expr("[*]", [10]))

    def test_ignores_aux_model_file_for_dependency_usage_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/execute/exe_test_001/problem/shallow_water2d/perf.json"
            )
            perf_path.write_text('{"walltime_sec":.000002}\n', encoding="utf-8")

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(v == f"{perf_path}: invalid json" for v in violations)
            )

    def test_detects_unsafe_fortran_f0_perf_serialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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

    def test_non_state_snapshots_artifact_schema_variable_does_not_authorize_step_token(self) -> None:
        # Regression: schema.variables on a non-state_snapshots artifact must not be
        # harvested into direct_spec_vars; doing so would let arbitrary names bypass the
        # undefined-binding provenance check.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["x", "y"],
                algorithm_contract={
                    "algorithm_id": "shallow_water2d_test_algorithm",
                    "execution_mode": "sequence",
                    "steps": [
                        {
                            "step_id": "compute_flux",
                            "step_kind": "flux_compute",
                            "operation_ref": "dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux",
                            "inputs": ["bogus_var"],
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
                },
                derived_contract={
                    "io_contract": {
                        "inputs": [
                            {
                                "name": "case_resolved",
                                "source": "case.resolved.yaml",
                                "evidence_ref": "case.resolved.yaml",
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
                            # bogus_var is only declared under a non-state_snapshots
                            # artifact schema — it must NOT be harvested into
                            # direct_spec_vars and must NOT authorize step tokens.
                            {
                                "artifact": "metrics_basis.json",
                                "required": True,
                                "schema": {
                                    "variables": [{"name": "bogus_var"}],
                                },
                            },
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
            violations = validate_plan_stage(
                repo_root,
                "workspace",
                "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
            )
            self.assertTrue(
                any("bogus_var" in v and "undefined binding" in v for v in violations),
                f"expected undefined-binding violation for bogus_var; got: {violations}",
            )

    def test_detects_makefile_missing_fortran_module_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            # run_quality_checks canonical placement: cross-phase under
            # generate/<gen>/src/mcp_command_log.jsonl. Append to the existing
            # canonical log written by the fixture.
            qc_log_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/generate/gen_20260415_001/src/"
                "mcp_command_log.jsonl"
            )
            trial_meta["source_command_ref"]["run_quality_checks"] = {
                "command_id": "cmd_quality_001",
                "tool_name": "run_quality_checks",
                "command_log_ref": qc_log_ref,
            }
            trial_meta["source_generation_id"] = "gen_20260415_001"
            _write_json(trial_meta_path, trial_meta)

            qc_log_path = repo_root / qc_log_ref
            with qc_log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "command_id": "cmd_quality_001",
                            "tool_name": "run_quality_checks",
                            "command": ["python3", "quality_check.py", ".", "."],
                            "ok": True,
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
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
                / "shallow-water2d_20260415_001"
                / "generate"
                / "gen_20260415_001"
                / "src"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            qc_log_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/generate/gen_20260415_001/src/"
                "mcp_command_log.jsonl"
            )
            trial_meta["source_command_ref"]["run_quality_checks"] = {
                "command_id": "cmd_quality_001",
                "tool_name": "run_quality_checks",
                "command_log_ref": qc_log_ref,
            }
            trial_meta["source_generation_id"] = "gen_20260415_001"
            _write_json(trial_meta_path, trial_meta)

            qc_log_path = repo_root / qc_log_ref
            with qc_log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "command_id": "cmd_quality_001",
                            "tool_name": "run_quality_checks",
                            "cwd": str(src_dir),
                            "command": ["pytest", "-q"],
                            "ok": True,
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
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
                / "shallow-water2d_20260415_001"
                / "generate"
                / "gen_20260415_001"
                / "src"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            qc_log_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/generate/gen_20260415_001/src/"
                "mcp_command_log.jsonl"
            )
            trial_meta["source_command_ref"]["run_quality_checks"] = {
                "command_id": "cmd_quality_001",
                "tool_name": "run_quality_checks",
                "command_log_ref": qc_log_ref,
            }
            trial_meta["source_generation_id"] = "gen_20260415_001"
            _write_json(trial_meta_path, trial_meta)

            qc_log_path = repo_root / qc_log_ref
            with qc_log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "command_id": "cmd_quality_001",
                            "tool_name": "run_quality_checks",
                            "cwd": str(src_dir),
                            "command": ["make", "test"],
                            "ok": True,
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
                / "lineage.json"
            ).unlink()
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(any("lineage.json: missing" in v for v in violations))

    def test_detects_missing_graph_child_run_when_orchestration_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
                "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
            )
            self.assertEqual(violations, [])

    def test_validate_plan_stage_rejects_non_plans_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            violations = validate_plan_stage(
                repo_root,
                "workspace",
                "workspace/pipelines/foo/bar",
            )
            self.assertTrue(
                any("plan_ref must be under" in v for v in violations), violations
            )

    def test_validate_plan_stage_rejects_missing_context_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["x", "y"],
            )
            meta_path = (
                repo_root
                / "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json"
            )
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            data.pop("context_isolated", None)
            meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            violations = validate_plan_stage(
                repo_root,
                "workspace",
                "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
            )
            self.assertTrue(
                any("plan_meta.json: missing required key 'context_isolated'" in v for v in violations),
                violations,
            )

    def test_validate_plan_stage_requires_constraint_reason_when_not_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["x", "y"],
            )
            meta_path = (
                repo_root
                / "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/plan_meta.json"
            )
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            data["context_isolated"] = False
            meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            violations = validate_plan_stage(
                repo_root,
                "workspace",
                "workspace/plans/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
            )
            self.assertTrue(
                any("requires non-empty constraint_reason when context_isolated=false" in v for v in violations),
                violations,
            )

    def test_validate_post_generate_stage_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
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
                "shallow-water2d_20260415_001",
                generation_id="gen_20260415_001",
            )
            self.assertEqual(violations, [])

    def test_validate_post_generate_stage_rejects_failed_run_linter_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
            )
            log_path = pipeline_dir / "generate" / "gen_20260415_001" / "src" / "mcp_command_log.jsonl"
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
                "shallow-water2d_20260415_001",
                generation_id="gen_20260415_001",
            )
            self.assertTrue(
                any("run_linter did not succeed" in v for v in violations), violations
            )

    def test_validate_post_generate_stage_rejects_lint_command_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
            )
            log_path = pipeline_dir / "generate" / "gen_20260415_001" / "src" / "mcp_command_log.jsonl"
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
                "shallow-water2d_20260415_001",
                generation_id="gen_20260415_001",
            )
            self.assertTrue(
                any("logged command does not match preset" in v for v in violations),
                violations,
            )

    def test_validate_post_generate_stage_rejects_noncanonical_lint_command_log_ref(self) -> None:
        """Defense against forged MCP execution evidence at non-canonical paths.

        A child agent that writes a forged mcp_command_log.jsonl at a non-
        canonical placement (e.g. <gen>/src/notes/mcp_command_log.jsonl) and
        points lint_command_ref.run_linter[].command_log_ref at it must be
        rejected by the post_generate validator. The canonical placement is
        <gen>/src/mcp_command_log.jsonl (sibling of model/runner sources).
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
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
                / "shallow-water2d_20260415_001"
            )
            # Plant a forged log at a NON-canonical placement (under src/notes/).
            forged_log = (
                pipeline_dir
                / "generate"
                / "gen_20260415_001"
                / "src"
                / "notes"
                / "mcp_command_log.jsonl"
            )
            forged_log.parent.mkdir(parents=True, exist_ok=True)
            forged_log.write_text(
                json.dumps(
                    {
                        "command_id": "lint_cmd_fixture_001",
                        "tool_name": "run_linter",
                        "command": ["fortitude", "check", "."],
                        "ok": True,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            # Rewrite generate_meta.json's lint_command_ref to point at the forged log.
            meta_path = (
                pipeline_dir / "generate" / "gen_20260415_001" / "generate_meta.json"
            )
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            forged_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/generate/gen_20260415_001/src/"
                "notes/mcp_command_log.jsonl"
            )
            meta["lint_command_ref"]["run_linter"][0]["command_log_ref"] = forged_ref
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            violations = validate_post_generate_stage(
                repo_root,
                "workspace",
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001",
                generation_id="gen_20260415_001",
            )
            self.assertTrue(
                any(
                    "canonical MCP audit log placement" in v for v in violations
                ),
                violations,
            )

    def test_trial_meta_rejects_role_mismatch_compile_project_for_run_program(self) -> None:
        """Defense against forged role substitution in source_command_ref.

        A child writes a log record with `tool_name=compile_project` at the
        canonical placement and points the run_program slot in trial_meta at
        it. Without role binding, the trial_meta whitelist accepts the record
        (compile_project is recognized) and downstream
        `_validate_run_program_inputs` silently skips because the matched
        tool_name != run_program. The new validator must reject this role
        substitution explicitly via declared/resolved tool_name match.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            # Replace the canonical log with a compile_project record (wrong
            # tool for the run_program slot the fixture's trial_meta declares).
            log_path = node_dir / "mcp_command_log.jsonl"
            log_path.write_text(
                json.dumps(
                    {
                        "command_id": "fixture_run_program_001",
                        "tool_name": "compile_project",
                        "command": ["make", "all"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            trial_meta["source_command_ref"]["run_threads_1"]["command_id"] = (
                "fixture_run_program_001"
            )
            _write_json(trial_meta_path, trial_meta)
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            # compile_project is no longer accepted in execute trial_meta's
            # tool_name whitelist (it's a build-phase tool). The forged log
            # record's tool_name=compile_project is rejected by the
            # recognized-tool-names check, not role mismatch.
            self.assertTrue(
                any(
                    "log record must declare tool_name" in v
                    and "compile_project" in v
                    for v in violations
                ),
                violations,
            )

    def test_trial_meta_rejects_log_record_without_recognized_tool_name(self) -> None:
        """Defense against forged source_command_ref records lacking tool_name.

        A child agent that writes a log record with command_id only (no
        tool_name field) at the canonical placement, then points trial_meta
        source_command_ref at it, must be rejected. Without tool_name the
        downstream tool-specific validators silently skip the entry, leaving
        the forged provenance unverified.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            # Overwrite the canonical log file with a record that has no
            # tool_name field — only command_id.
            log_path = node_dir / "mcp_command_log.jsonl"
            log_path.write_text(
                json.dumps(
                    {
                        "command_id": "fixture_run_program_001",
                        "command": ["./simulate", "case.resolved.yaml", "out"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            trial_meta["source_command_ref"]["run_threads_1"]["command_id"] = (
                "fixture_run_program_001"
            )
            _write_json(trial_meta_path, trial_meta)
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "log record must declare tool_name" in v
                    and "fixture_run_program_001" in v
                    for v in violations
                ),
                violations,
            )

    def test_trial_meta_requires_source_generation_id(self) -> None:
        """Strict policy: every execute trial_meta must declare
        `source_generation_id`. Without it, validators cannot bind
        provenance and the field could be omitted to silently bypass
        per-entry tool_name and mandatory run_program checks.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            trial_meta.pop("source_generation_id", None)
            _write_json(trial_meta_path, trial_meta)
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "source_generation_id is required" in v
                    for v in violations
                ),
                violations,
            )

    def test_trial_meta_requires_source_build_id(self) -> None:
        """source_build_id binds run_program to the specific build whose
        binary the execute used. Omission must be rejected.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            trial_meta.pop("source_build_id", None)
            _write_json(trial_meta_path, trial_meta)
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("source_build_id is required" in v for v in violations),
                violations,
            )

    def test_run_program_rejects_executable_outside_source_build_id(self) -> None:
        """The matched run_program record's executable must resolve under
        `<pipeline>/build/<source_build_id>/bin/`. A trial_meta that claims
        provenance for build A but executes a sibling build B's binary must
        be rejected.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            pipeline_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
            )
            node_dir = (
                pipeline_dir / "execute" / "exe_test_001" / "problem" / "shallow_water2d"
            )
            # Plant a sibling build whose binary the run actually used.
            sibling_build = pipeline_dir / "build" / "build_sibling_999" / "bin"
            sibling_build.mkdir(parents=True, exist_ok=True)
            (sibling_build / "simulate").write_text("sibling\n", encoding="utf-8")
            # Rewrite the log record's cwd to point at the sibling bin/, but
            # leave trial_meta.source_build_id pointing at the (declared)
            # canonical fixture build.
            log_path = node_dir / "mcp_command_log.jsonl"
            recs = [
                json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()
            ]
            for rec in recs:
                if rec.get("tool_name") == "run_program":
                    rec["cwd"] = str(sibling_build)
            log_path.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                encoding="utf-8",
            )
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "executable" in v and "must resolve under" in v
                    for v in violations
                ),
                violations,
            )

    def test_run_program_rejects_failed_record(self) -> None:
        """A run_program record with ok!=true cannot serve as evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            # Get the command_id the fixture used.
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            cmd_id = trial_meta["source_command_ref"]["run_threads_1"]["command_id"]
            log_path = node_dir / "mcp_command_log.jsonl"
            log_path.write_text(
                json.dumps(
                    {
                        "command_id": cmd_id,
                        "tool_name": "run_program",
                        "command": ["./simulate", "case.resolved.yaml", "out"],
                        "ok": False,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    f"run_program command_id={cmd_id}" in v
                    and "ok must be true" in v
                    for v in violations
                ),
                violations,
            )

    def test_run_quality_checks_rejects_failed_source_generation(self) -> None:
        """source_generation_id must point to a generation in pass state.

        Pointing trial_meta at a failed/stale generation under the same
        pipeline (even with a valid mcp_command_log.jsonl) must be rejected,
        otherwise stale evidence credits the current execute run.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            pipeline_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
            )
            node_dir = (
                pipeline_dir / "execute" / "exe_test_001" / "problem" / "shallow_water2d"
            )
            # Plant a stale generation in fail state with its own canonical log.
            stale_gen_id = "gen_stale_001"
            stale_dir = pipeline_dir / "generate" / stale_gen_id
            stale_dir.mkdir(parents=True, exist_ok=True)
            _write_json(
                stale_dir / "generate_meta.json",
                {
                    "attempt_count": 1,
                    "verification_status": "fail",
                    "last_fail_reason": "lint failed",
                    "debug_mode": False,
                    "context_isolated": True,
                },
            )
            stale_src = stale_dir / "src"
            stale_src.mkdir(parents=True, exist_ok=True)
            stale_log_ref = (
                f"workspace/pipelines/problem__shallow_water2d__0.3.0/"
                f"shallow-water2d_20260415_001/generate/{stale_gen_id}/src/"
                "mcp_command_log.jsonl"
            )
            (stale_src / "mcp_command_log.jsonl").write_text(
                json.dumps(
                    {
                        "command_id": "cmd_quality_stale",
                        "tool_name": "run_quality_checks",
                        "cwd": str(stale_src),
                        "command": ["make", "test"],
                        "ok": True,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            trial_meta["source_generation_id"] = stale_gen_id
            trial_meta["source_command_ref"]["run_quality_checks"] = {
                "command_id": "cmd_quality_stale",
                "tool_name": "run_quality_checks",
                "command_log_ref": stale_log_ref,
            }
            _write_json(trial_meta_path, trial_meta)
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "verification_status='fail'" in v
                    and stale_gen_id in v
                    for v in violations
                ),
                violations,
            )

    def test_run_quality_checks_rejects_sibling_generation_log(self) -> None:
        """Cross-phase canonical placement is bound strictly to source_generation_id.

        An execute trial_meta that points run_quality_checks at a sibling
        generation's canonical log (different gen_id) must be rejected, even
        though the path is technically a canonical placement under the same
        pipeline. Provenance must match the trial's own generation.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            pipeline_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
            )
            node_dir = (
                pipeline_dir / "execute" / "exe_test_001" / "problem" / "shallow_water2d"
            )
            # Plant a sibling generation with its own canonical log.
            sibling_gen_id = "gen_sibling_001"
            sibling_dir = pipeline_dir / "generate" / sibling_gen_id
            sibling_dir.mkdir(parents=True, exist_ok=True)
            (sibling_dir / "generate_meta.json").write_text(
                '{"verification_status": "pass"}\n', encoding="utf-8"
            )
            sibling_src = sibling_dir / "src"
            sibling_src.mkdir(parents=True, exist_ok=True)
            sibling_log_ref = (
                f"workspace/pipelines/problem__shallow_water2d__0.3.0/"
                f"shallow-water2d_20260415_001/generate/{sibling_gen_id}/src/"
                "mcp_command_log.jsonl"
            )
            (sibling_src / "mcp_command_log.jsonl").write_text(
                json.dumps(
                    {
                        "command_id": "cmd_quality_sibling",
                        "tool_name": "run_quality_checks",
                        "cwd": str(sibling_src),
                        "command": ["make", "test"],
                        "ok": True,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            # trial_meta declares the real (fixture) generation but references
            # the sibling generation's log.
            trial_meta["source_generation_id"] = "gen_20260415_001"
            trial_meta["source_command_ref"]["run_quality_checks"] = {
                "command_id": "cmd_quality_sibling",
                "tool_name": "run_quality_checks",
                "command_log_ref": sibling_log_ref,
            }
            _write_json(trial_meta_path, trial_meta)
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "run_quality_checks command_id=cmd_quality_sibling" in v
                    and "canonical MCP audit log placement" in v
                    for v in violations
                ),
                violations,
            )

    def test_run_program_rejects_noncanonical_command_log_ref(self) -> None:
        """Defense against forged run_program evidence at non-canonical paths.

        A child agent that writes a synthetic JSONL under raw/ (which is
        permitted as an execute output directory) and points
        source_command_ref.run_threads_1.command_log_ref at it must be
        rejected. The canonical placement for run_program is sibling of
        trial_meta.json (in-phase canonical for execute).
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
            )
            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
            )
            # Plant a forged log under raw/ (a writable execute output directory).
            forged_log = node_dir / "raw" / "forged_run.jsonl"
            forged_log.parent.mkdir(parents=True, exist_ok=True)
            forged_log.write_text(
                json.dumps(
                    {
                        "command_id": "forged_cmd_001",
                        "tool_name": "run_program",
                        "command": ["./simulate", "case.resolved.yaml", "out"],
                        "ok": True,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            # Repoint trial_meta source_command_ref to the forged log.
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            trial_meta["source_command_ref"]["run_threads_1"] = {
                "command_id": "forged_cmd_001",
                "command_log_ref": (
                    "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                    "shallow-water2d_20260415_001/execute/exe_test_001/"
                    "problem/shallow_water2d/raw/forged_run.jsonl"
                ),
            }
            _write_json(trial_meta_path, trial_meta)
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "run_program command_id=forged_cmd_001" in v
                    and "canonical MCP audit log placement" in v
                    for v in violations
                ),
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

    def test_validate_generate_meta_rejects_pass_without_lint_command_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_dir = Path(tmp) / "pipeline"
            gen_dir = pipeline_dir / "generate" / "gen_pass_001"
            gen_dir.mkdir(parents=True)
            _write_json(
                gen_dir / "generate_meta.json",
                {
                    "attempt_count": 1,
                    "verification_status": "pass",
                    "last_fail_reason": None,
                    "debug_mode": False,
                    "context_isolated": True,
                },
            )
            violations: list[str] = []
            _validate_generate_meta_json_files(pipeline_dir, violations)
            self.assertTrue(
                any("missing lint_command_ref" in v for v in violations),
                violations,
            )

    def test_validate_generate_meta_rejects_empty_run_linter_when_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_dir = Path(tmp) / "pipeline"
            gen_dir = pipeline_dir / "generate" / "gen_pass_002"
            gen_dir.mkdir(parents=True)
            _write_json(
                gen_dir / "generate_meta.json",
                {
                    "attempt_count": 1,
                    "verification_status": "pass",
                    "last_fail_reason": None,
                    "debug_mode": False,
                    "context_isolated": True,
                    "lint_command_ref": {"run_linter": []},
                },
            )
            violations: list[str] = []
            _validate_generate_meta_json_files(pipeline_dir, violations)
            self.assertTrue(
                any("lint_command_ref.run_linter must be non-empty" in v for v in violations),
                violations,
            )

    def test_validate_generate_meta_ignores_lint_shape_when_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_dir = Path(tmp) / "pipeline"
            gen_dir = pipeline_dir / "generate" / "gen_fail_002"
            gen_dir.mkdir(parents=True)
            _write_json(
                gen_dir / "generate_meta.json",
                {
                    "attempt_count": 1,
                    "verification_status": "fail",
                    "last_fail_reason": "compile failed",
                    "debug_mode": False,
                    "context_isolated": True,
                    "lint_command_ref": "invalid-shape",
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
            _seed_shape_expr_schema_into(repo_root)
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
                "shallow-water2d_20260415_001",
                generation_id="gen_20260415_001",
            )
            self.assertEqual(violations, [])

    def test_create_minimal_execution_tree_writes_metrics_basis_to_raw(self) -> None:
        """metrics_basis 引数が raw/metrics_basis.json に反映されること（trivial 検証テストの前提）。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
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
            payload = {"probe": 3.25}
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/case.resolved.yaml", "workspace/outdir"],
                metrics_basis=payload,
            )
            metrics_path = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "execute"
                / "exe_test_001"
                / "problem"
                / "shallow_water2d"
                / "raw"
                / "metrics_basis.json"
            )
            self.assertTrue(metrics_path.is_file())
            self.assertEqual(json.loads(metrics_path.read_text(encoding="utf-8")), payload)


    def test_validate_rejects_all_zero_metrics_basis(self) -> None:
        """metrics_basis.json の全数値が 0.0 のとき violation が発生すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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
            _seed_shape_expr_schema_into(repo_root)
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

    def test_shape_expr_schema_load_is_lazy_and_errors_are_structured(self) -> None:
        """Regression: a missing or malformed shape_expr schema must NOT crash
        at import time (which would block `--help` and unrelated CLI flows).
        First parse-time access surfaces a `RuntimeError` whose message names
        the offending path so operators can repair it."""
        from tools.validate_pipeline_semantics import (
            _BUNDLED_SHAPE_EXPR_SCHEMA_PATH,
            _active_repo_root_for_schema,
            _load_shape_expr_patterns_cached,
            _parse_shape_expr,
        )
        with tempfile.TemporaryDirectory() as tmp:
            broken_root = Path(tmp)
            schema_dir = broken_root / "spec" / "schema" / "plan"
            schema_dir.mkdir(parents=True)
            (schema_dir / "shape_expr.schema.json").write_text(
                "{ this is not json", encoding="utf-8"
            )
            _load_shape_expr_patterns_cached.cache_clear()
            token = _active_repo_root_for_schema.set(broken_root)
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    _parse_shape_expr("[3]")
                msg = str(ctx.exception)
                self.assertIn("shape_expr schema", msg)
                self.assertIn("malformed JSON", msg)
                self.assertIn(str(broken_root / "spec" / "schema" / "plan" / "shape_expr.schema.json"), msg)
            finally:
                _active_repo_root_for_schema.reset(token)
                _load_shape_expr_patterns_cached.cache_clear()
        # Bundled schema continues to work after the cache reset.
        ok, _, _ = _parse_shape_expr("[3]")
        self.assertTrue(ok)
        # Sanity: the bundled path resolution is still valid.
        self.assertTrue(_BUNDLED_SHAPE_EXPR_SCHEMA_PATH.is_file())

    def test_shape_expr_schema_resolves_from_active_repo_root(self) -> None:
        """Regression: the active repo_root's spec/schema/plan/shape_expr.schema.json
        is the canonical source — its rules apply, and missing the schema while
        a repo_root is in scope must FAIL CLOSED rather than silently falling
        back to the validator-bundled copy. Bundled fallback is reserved for
        ad-hoc / library-style invocation with no target repo in scope."""
        from tools.validate_pipeline_semantics import (
            _active_repo_root_for_schema,
            _load_shape_expr_patterns_cached,
            _parse_shape_expr,
        )
        with tempfile.TemporaryDirectory() as tmp_strict, tempfile.TemporaryDirectory() as tmp_no_schema:
            strict_root = Path(tmp_strict)
            no_schema_root = Path(tmp_no_schema)
            schema_dir = strict_root / "spec" / "schema" / "plan"
            schema_dir.mkdir(parents=True)
            strict_schema = {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "oneOf": [
                    {"title": "scalar literal", "pattern": r"^[Ss][Cc][Aa][Ll][Aa][Rr]$"},
                    {
                        "title": "bracket list form (integer-only)",
                        "pattern": r"^\[\s*[0-9]+(?:\s*,\s*[0-9]+)*\s*\]$",
                    },
                ],
            }
            (schema_dir / "shape_expr.schema.json").write_text(
                json.dumps(strict_schema), encoding="utf-8"
            )

            _load_shape_expr_patterns_cached.cache_clear()
            token = _active_repo_root_for_schema.set(strict_root)
            try:
                # Identifier dim now rejected (target schema is integer-only).
                ok_id, _, _ = _parse_shape_expr("[nx]")
                self.assertFalse(ok_id, "identifier dim should be rejected by strict target schema")
                # Integer dim still passes.
                ok_int, _, _ = _parse_shape_expr("[3]")
                self.assertTrue(ok_int, "integer dim should pass strict target schema")
                # Paren tuple form not declared in strict target schema → rejected.
                ok_paren, _, _ = _parse_shape_expr("(d1, d2)")
                self.assertFalse(ok_paren, "paren form absent from strict schema should be rejected")
            finally:
                _active_repo_root_for_schema.reset(token)
                _load_shape_expr_patterns_cached.cache_clear()

            # Target with no schema while in scope must FAIL CLOSED — no silent
            # bundled fallback once a repo_root is pinned, otherwise version
            # skew between target and validator-bundle goes undetected.
            token2 = _active_repo_root_for_schema.set(no_schema_root)
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    _parse_shape_expr("[3]")
                msg = str(ctx.exception)
                self.assertIn("shape_expr schema not found", msg)
                self.assertIn(
                    str(no_schema_root / "spec" / "schema" / "plan" / "shape_expr.schema.json"),
                    msg,
                )
            finally:
                _active_repo_root_for_schema.reset(token2)
                _load_shape_expr_patterns_cached.cache_clear()

            # When NO repo_root is in scope (library / ad-hoc / unit-test
            # invocation), bundled fallback IS used. This keeps test and
            # importer paths working without forcing every caller to provide
            # a target repo.
            ok_bundled, _, _ = _parse_shape_expr("[nx]")
            self.assertTrue(ok_bundled, "bundled schema applies when no repo_root is in scope")
            _load_shape_expr_patterns_cached.cache_clear()

    def test_validate_plan_stage_fails_closed_when_target_repo_lacks_schema(self) -> None:
        """Regression: public validate_*() entrypoints must bind the active
        repo_root context themselves so a target repo without
        spec/schema/plan/shape_expr.schema.json fails closed. Previously the
        context was set only by CLI main(), so library callers silently fell
        back to the validator-bundled schema, defeating the fail-closed
        protection against version skew between target and validator-bundle."""
        from tools.validate_pipeline_semantics import (
            _active_repo_root_for_schema,
            _load_shape_expr_patterns_cached,
            validate_plan_stage,
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # Deliberately do NOT seed the schema (this test exercises the
            # missing-schema path). Build minimal plan artifacts so the
            # validator reaches shape_expr parsing.
            plan_dir = repo_root / "workspace" / "plans" / "x" / "p1"
            plan_dir.mkdir(parents=True)
            algo = {
                "algorithm_id": "alg",
                "execution_mode": "sequence",
                "steps": [],
                "ordering": [],
                "control_condition": "",
                "iteration_contract": {},
                "update_semantics": {},
                "temporaries": [{"name": "t", "shape_expr": "[3]"}],
                "derived_field_rules": [],
                "invariants": ["x"],
                "splitting_policy": {"kind": "none"},
            }
            (plan_dir / "algorithm.resolved.yaml").write_text(
                yaml.safe_dump(algo), encoding="utf-8"
            )
            _load_shape_expr_patterns_cached.cache_clear()
            # Pre-condition: no leaked context from prior tests.
            self.assertIsNone(_active_repo_root_for_schema.get())
            # Direct library-style call must fail closed.
            with self.assertRaises(RuntimeError) as ctx:
                validate_plan_stage(repo_root, "workspace", "workspace/plans/x/p1")
            msg = str(ctx.exception)
            self.assertIn("shape_expr schema not found", msg)
            self.assertIn(
                str(repo_root / "spec" / "schema" / "plan" / "shape_expr.schema.json"),
                msg,
            )
            # Post-condition: validate_plan_stage MUST reset the context so
            # subsequent in-process calls don't see the failed repo's root.
            self.assertIsNone(
                _active_repo_root_for_schema.get(),
                "validate_plan_stage must reset the active context after returning/raising",
            )
            _load_shape_expr_patterns_cached.cache_clear()

    def test_consecutive_main_calls_do_not_leak_repo_root_context(self) -> None:
        """Regression: main() must scope `_active_repo_root_for_schema` to its
        own invocation. A long-lived process or batch tooling that invokes
        main() against repo A then repo B must NOT carry repo A's context
        into repo B's resolution. The leak is process-local and order-
        dependent, which makes it expensive to diagnose without a guard test."""
        import io
        from contextlib import redirect_stdout
        from tools.validate_pipeline_semantics import (
            _active_repo_root_for_schema,
            _load_shape_expr_patterns_cached,
            main,
        )

        def _build_repo(tmp: str) -> Path:
            repo = Path(tmp)
            sd = repo / "spec" / "schema" / "plan"
            sd.mkdir(parents=True)
            (sd / "shape_expr.schema.json").write_bytes(
                _BUNDLED_SHAPE_EXPR_SCHEMA_PATH.read_bytes()
            )
            plan_dir = repo / "workspace" / "plans" / "x" / "p1"
            plan_dir.mkdir(parents=True)
            (plan_dir / "algorithm.resolved.yaml").write_text(
                yaml.safe_dump(
                    {
                        "algorithm_id": "a",
                        "execution_mode": "sequence",
                        "steps": [],
                        "ordering": [],
                        "control_condition": "",
                        "iteration_contract": {},
                        "update_semantics": {},
                        "temporaries": [{"name": "t", "shape_expr": "[3]"}],
                        "derived_field_rules": [],
                        "invariants": ["x"],
                        "splitting_policy": {"kind": "none"},
                    }
                ),
                encoding="utf-8",
            )
            return repo

        _load_shape_expr_patterns_cached.cache_clear()
        # Pre-condition baseline.
        baseline = _active_repo_root_for_schema.get()
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            repo_a = _build_repo(tmp_a)
            repo_b = _build_repo(tmp_b)
            for repo in (repo_a, repo_b):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    main(
                        [
                            "--repo-root", str(repo),
                            "--stage", "plan",
                            "--plan-ref", "workspace/plans/x/p1",
                        ]
                    )
                # After each main() call the context must be reset.
                self.assertEqual(
                    _active_repo_root_for_schema.get(),
                    baseline,
                    f"main() against {repo} leaked context "
                    f"(active={_active_repo_root_for_schema.get()!r}, expected={baseline!r})",
                )

    def test_cli_emits_structured_fail_on_broken_schema(self) -> None:
        """Regression: a broken canonical schema (missing or malformed) must
        surface as `pipeline semantic validation: FAIL\\n- schema_load_failed: ...`
        on stdout rather than an uncaught traceback. Orchestration gates parse
        the structured output; an opaque crash blocks recovery and observability."""
        import io
        from contextlib import redirect_stdout
        from tools.validate_pipeline_semantics import (
            _active_repo_root_for_schema,
            _load_shape_expr_patterns_cached,
            main,
        )
        with tempfile.TemporaryDirectory() as tmp:
            broken_root = Path(tmp)
            schema_dir = broken_root / "spec" / "schema" / "plan"
            schema_dir.mkdir(parents=True)
            (schema_dir / "shape_expr.schema.json").write_text(
                "{ broken json", encoding="utf-8"
            )
            # Need a minimal plan_ref so plan-stage actually exercises shape_expr.
            plan_ref = broken_root / "workspace" / "plans" / "x" / "p1"
            plan_ref.mkdir(parents=True)
            algo = {
                "algorithm_id": "alg",
                "execution_mode": "sequence",
                "steps": [],
                "ordering": [],
                "control_condition": "",
                "iteration_contract": {},
                "update_semantics": {},
                "temporaries": [{"name": "t", "shape_expr": "[3]"}],
                "derived_field_rules": [],
                "invariants": ["x"],
                "splitting_policy": {"kind": "none"},
            }
            (plan_ref / "algorithm.resolved.yaml").write_text(
                yaml.safe_dump(algo), encoding="utf-8"
            )
            _load_shape_expr_patterns_cached.cache_clear()
            # Reset any leaked context from earlier tests.
            try:
                prev = _active_repo_root_for_schema.get()
            except LookupError:
                prev = None
            _active_repo_root_for_schema.set(None)
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    rc = main([
                        "--repo-root", str(broken_root),
                        "--stage", "plan",
                        "--plan-ref", "workspace/plans/x/p1",
                    ])
            finally:
                _active_repo_root_for_schema.set(prev)
                _load_shape_expr_patterns_cached.cache_clear()
            output = buf.getvalue()
        self.assertEqual(rc, 1, f"expected exit code 1, got {rc}; output: {output!r}")
        self.assertIn("pipeline semantic validation: FAIL", output)
        self.assertIn("schema_load_failed", output)
        self.assertIn("shape_expr.schema.json", output)

    def test_shape_expr_rejects_wrapped_function_call_notation(self) -> None:
        """Regression: function-call notation must be rejected even when wrapped
        inside the bracket/paren forms (e.g. `[vector(3)]`, `[3, vector(2)]`,
        `(d1, matrix(2,2))`). The previous schema regex was permissive on
        dimension tokens; the parser now restricts each dim token to integer
        literal or identifier-style symbol.

        Single-element paren tuples like `(tensor)` and `(d1)` REMAIN valid
        because their structure is a legitimate 1-D shape — `tensor` here is
        treated as an identifier-named dimension, not a function-call."""
        from tools.validate_pipeline_semantics import _parse_shape_expr
        forbidden = [
            "[vector(3)]",
            "[matrix(3,3)]",
            "[3, vector(2)]",
            "(d1, matrix(2,2))",
            "[1+2]",
            "[3.0]",
            "[-3]",
        ]
        for expr in forbidden:
            ok, _, err = _parse_shape_expr(expr)
            self.assertFalse(ok, f"{expr!r} should be rejected, got ok=True")
            self.assertTrue(err, f"{expr!r} rejection must carry a non-empty error message")

        accepted = [
            "scalar",
            "[3]",
            "[nx, ny]",
            "(nx)",
            "(d1, d2)",
            "(tensor)",  # 1-D paren tuple with identifier-named dim — structurally valid
        ]
        for expr in accepted:
            ok, _, _ = _parse_shape_expr(expr)
            self.assertTrue(ok, f"{expr!r} should be accepted")

    def test_object_form_temporaries_must_include_shape_expr(self) -> None:
        """Regression: phase_01_plan.md L26 mandates that object-form temporaries
        entries carry both `name` and `shape_expr` (canonical source:
        spec/schema/plan/shape_expr.schema.json). Missing shape_expr must fail
        Plan validation rather than silently leak into Generate."""
        from tools.validate_pipeline_semantics import _validate_algorithm_contract_file
        contract = {
            "algorithm_id": "alg_test",
            "execution_mode": "sequence",
            "steps": [
                {
                    "step_id": "s1",
                    "step_kind": "flux_compute",
                    "operation_ref": "op_dummy",
                    "inputs": ["U_L", "U_R"],
                    "outputs": ["F_h"],
                }
            ],
            "ordering": ["s1"],
            "control_condition": "",
            "iteration_contract": {},
            "update_semantics": {"target_variables": ["F_h"], "update_order": "sequential"},
            "temporaries": [
                {"name": "guard_pass"},  # missing shape_expr — must violate
            ],
            "derived_field_rules": [],
            "invariants": ["dummy"],
            "splitting_policy": {"kind": "none"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            contract_path = repo_root / "algorithm.resolved.yaml"
            contract_path.write_text(yaml.safe_dump(contract), encoding="utf-8")
            violations: list[str] = []
            _validate_algorithm_contract_file(
                repo_root,
                contract_path,
                violations,
                multidim_node_key=None,
                direct_spec_vars=None,
            )
        offending = [
            v for v in violations
            if "temporaries[0].shape_expr" in v and "required" in v
        ]
        self.assertTrue(
            offending,
            f"Expected required-shape_expr violation, got: {violations}",
        )

    def test_object_form_temporaries_with_shape_expr_passes(self) -> None:
        """Negative regression of the previous test: a complete object-form
        entry with valid shape_expr must NOT produce a temporaries violation."""
        from tools.validate_pipeline_semantics import _validate_algorithm_contract_file
        contract = {
            "algorithm_id": "alg_test",
            "execution_mode": "sequence",
            "steps": [
                {
                    "step_id": "s1",
                    "step_kind": "flux_compute",
                    "operation_ref": "op_dummy",
                    "inputs": ["U_L", "U_R"],
                    "outputs": ["F_h"],
                }
            ],
            "ordering": ["s1"],
            "control_condition": "",
            "iteration_contract": {},
            "update_semantics": {"target_variables": ["F_h"], "update_order": "sequential"},
            "temporaries": [
                {"name": "guard_pass", "shape_expr": "scalar"},
                {"name": "flux_vec", "shape_expr": "[3]"},
            ],
            "derived_field_rules": [],
            "invariants": ["dummy"],
            "splitting_policy": {"kind": "none"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            contract_path = repo_root / "algorithm.resolved.yaml"
            contract_path.write_text(yaml.safe_dump(contract), encoding="utf-8")
            violations: list[str] = []
            _validate_algorithm_contract_file(
                repo_root,
                contract_path,
                violations,
                multidim_node_key=None,
                direct_spec_vars=None,
            )
        self.assertFalse(
            any("temporaries[" in v for v in violations),
            f"Unexpected temporaries violation: {[v for v in violations if 'temporaries[' in v]}",
        )


if __name__ == "__main__":
    unittest.main()
