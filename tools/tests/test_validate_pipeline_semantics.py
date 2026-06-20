#!/usr/bin/env python3
"""Regression tests for pipeline semantic validation anti-cheat rules."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

import tools.validate_pipeline_semantics as vps
from tools.validate_pipeline_semantics import (
    _BUNDLED_SHAPE_EXPR_SCHEMA_PATH,
    NodeExecution,
    _diagnostics_contract_check_ids,
    _diagnostics_contract_verdict_fields,
    _node_executions,
    _parse_makefile_rules,
    _required_raw_evidence,
    _validate_diagnostics_contract,
    _validate_diagnostics_contract_output,
    _validate_fortran_identifier_length,
    _validate_fortran_makefile_src_dir,
    _impl_toolchain_from_pipeline_dir,
    _validate_generate_lint_command_logs,
    _validate_makefile_test_no_relink,
    _validate_source_meta_json_files,
    validate,
    validate_compile_stage,
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
    target = repo_root / "spec" / "schema" / "ir" / "shape_expr.schema.json"
    if target.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_BUNDLED_SHAPE_EXPR_SCHEMA_PATH.read_bytes())


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_STEP_PHASE_PATH = {
    "compile": "docs/workflow/phases/phase_01_compile.md",
    "generate": "docs/workflow/phases/phase_02_generate.md",
    "build": "docs/workflow/phases/phase_03_build.md",
    "validate": "docs/workflow/phases/phase_04_validate.md",
}


def _dependency_ref_for_step(step: str) -> str:
    """Phase-specific dependency_ref per ORCHESTRATION.md:151.

    Compile records the spec deps.yaml *file*; Generate and later phases record
    the IR phase-root *directory* (ir_ref).
    """
    if step == "compile":
        return "spec/problem/dynamics/shallow_water/shallow_water2d/deps.yaml"
    return "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001"


def _step_prompt_fixture(orchestration_id: str, node_key: str, step: str, run_id: str) -> str:
    phase_path = _STEP_PHASE_PATH.get(step, "docs/workflow/phases/phase_01_plan.md")
    refs = (
        f"skills/workflow-{step}/SKILL.md,"
        f"docs/workflow/WORKFLOW_CORE.md,docs/ORCHESTRATION.md,{phase_path}"
    )
    return f"""You are a step agent.
Target node_key: {node_key}
Target step: {step}
orchestration_id: {orchestration_id}
agent_run_id: {run_id}
parent_agent_run_id: orch_run_001
ir_ref: workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001
pipeline_ref: workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001
dependency_ref: {_dependency_ref_for_step(step)}
skill_name: workflow-{step}
skill_ref: skills/workflow-{step}/SKILL.md
skill_must_read_refs: {refs}
Required requirements:
- Complete the contract.
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
    return f"""You are a substep agent.
Target node_key: {node_key}
Target step: {step}
Target substep: {substep}
orchestration_id: {orchestration_id}
agent_run_id: {run_id}
parent_agent_run_id: orch_run_001
ir_ref: workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001
pipeline_ref: workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001
dependency_ref: {_dependency_ref_for_step(step)}
skill_name: workflow-{step}-{substep}
skill_ref: skills/workflow-{step}-{substep}/SKILL.md
skill_must_read_refs: {refs}
Required requirements:
- Complete the contract.
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
    io_contract: dict[str, object] | None = None,
    dependency_resolved: dict[str, object] | None = None,
    impl_resolved: dict[str, object] | None = None,
    metrics_basis: object | None = None,
) -> None:
    workspace = repo_root / "workspace"
    node_safe = "problem__shallow_water2d__0.3.0"
    pipeline_id = "shallow-water2d_20260415_001"
    run_id = "run_test_001"

    pipeline_dir = workspace / "pipelines" / node_safe / pipeline_id
    node_dir = pipeline_dir / "runs" / run_id / "problem__shallow_water2d__0.3.0"
    raw_dir = node_dir / "raw"
    snapshots_dir = raw_dir / "state_snapshots"
    src_dir = pipeline_dir / "source" / "src_20260415_001" / "src"
    # Canonical placement for in-phase MCP audit log: sibling of trial_meta.
    log_path = node_dir / "mcp_command_log.jsonl"

    _write_json(
        pipeline_dir / "lineage.json",
        {
            "node_key": "problem/shallow_water2d@0.3.0",
            "pipeline_id": pipeline_id,
            "ir_ref": "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
            "dependency_ref": "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
        },
    )
    lint_command_id = "lint_cmd_fixture_001"
    rel_lint_log = (
        f"workspace/pipelines/{node_safe}/{pipeline_id}/source/src_20260415_001/src/mcp_command_log.jsonl"
    )
    if dependency_resolved is None:
        dependency_resolved = {
            "node_key": "problem/shallow_water2d@0.3.0",
            "direct_deps": [f"component/{dep_spec_id}@0.1.0"],
            "transitive_deps": [f"component/{dep_spec_id}@0.1.0"],
            "topo_level": 1,
        }
    _write_json(
        workspace / "ir" / "problem__shallow_water2d__0.3.0" / "shallow-water2d_20260415_001" / "ir_meta.json",
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
    if io_contract is None:
        # New IR: spec.ir.yaml.io_contract holds inputs / outputs /
        # semantic_dependency / raw_requirements / test_evidence_requirements
        # as siblings (the legacy `io_contract` wrapper that derived_contract.json
        # used is dropped).
        io_contract = {
            "inputs": [
                {
                    "name": "case_resolved",
                    "source": "spec.ir.yaml",
                    "evidence_ref": "spec.ir.yaml",
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

    # Merge all 5 sections into a single spec.ir.yaml. The new IR design puts
    # algorithm / io_contract / impl_defaults / dependency under their own
    # top-level keys; case is optional here.
    spec_ir_doc: dict[str, object] = {
        "algorithm": algorithm_contract,
        "io_contract": io_contract,
        "impl_defaults": impl_resolved,
        "dependency": dependency_resolved,
    }
    _write_json(
        workspace / "ir" / "problem__shallow_water2d__0.3.0" / "shallow-water2d_20260415_001" / "spec.ir.yaml",
        spec_ir_doc,
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
    build_id_for_fixture = "bin_20260415_001"
    build_bin_dir = pipeline_dir / "binary" / build_id_for_fixture / "bin"
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
            "generated_by_stage": "validate",
            "source_source_id": "src_20260415_001",
            "source_binary_id": build_id_for_fixture,
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
        pipeline_dir / "source" / "src_20260415_001" / "source_meta.json",
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
        step: f"step_run_{step}_001" for step in ("build",)
    }
    substep_ids = {
        "compile": ["substep_run_compile_generate_001", "substep_run_compile_verify_001"],
        "generate": ["substep_run_generate_generate_001", "substep_run_generate_verify_001"],
        "validate": ["substep_run_validate_execute_001", "substep_run_validate_judge_001"],
    }
    graph_data = {"edges": []}
    for step in ("build",):
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
    for step in ("build",):
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
                    "node_key": node_key,
                    "step": step,
                    # substep_run_<step>_<substep>_<seq> → recover the substep label.
                    "substep": substep_id.split("_")[3],
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
                f"agent_run_id: {substep_id}\nstatus: pass\noutput_refs:\n- workspace/ir/{node_safe}/plan_{step}_{idx}\n",
                encoding="utf-8",
            )
            run_items.append(substep_payload)
    agent_runs_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in run_items) + "\n",
        encoding="utf-8",
    )

    for step in ("compile", "generate", "validate"):
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

    for step in ("build",):
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


class LegacyLaunchPromptMarkerTests(unittest.TestCase):
    def test_marker_present_accepts_english_and_legacy_japanese(self) -> None:
        # Backward compatibility: a launch_prompt_ref persisted before the English
        # translation contains Japanese template markers. pre_judge / full
        # validation must accept both the current English marker and its legacy form.
        from tools.validate_pipeline_semantics import (
            _launch_prompt_marker_present,
            _required_launch_prompt_markers_for_role,
        )

        legacy_step_prompt = (
            "あなたは step agent である。\n"
            "対象 node_key: component/x@0.1.0\n"
            "対象 step: compile\n"
            "orchestration_id: o\nagent_run_id: a\nparent_agent_run_id: p\n"
            "ir_ref: i\npipeline_ref: pp\ndependency_ref: spec/x/deps.yaml\n"
            "skill_name: s\nskill_ref: sr\nskill_must_read_refs: smr\n"
            "必須要件:\n- x\n"
        )
        markers = _required_launch_prompt_markers_for_role("step")
        missing = [m for m in markers if not _launch_prompt_marker_present(m, legacy_step_prompt)]
        self.assertEqual(missing, [], f"legacy Japanese prompt rejected: {missing}")

        # The current English prompt markers are still accepted.
        english_step_prompt = legacy_step_prompt.replace(
            "あなたは step agent である。", "You are a step agent."
        ).replace("対象 node_key:", "Target node_key:").replace(
            "対象 step:", "Target step:"
        ).replace("必須要件:", "Required requirements:")
        missing_en = [m for m in markers if not _launch_prompt_marker_present(m, english_step_prompt)]
        self.assertEqual(missing_en, [], f"english prompt rejected: {missing_en}")


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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            empty_node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "problem__shallow_water2d__0.3.0_empty_pipeline"
                / "runs"
                / "exe_empty_001"
                / "problem__shallow_water2d__0.3.0"
            )
            empty_node_dir.mkdir(parents=True, exist_ok=True)

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertEqual([], violations)

    def test_node_executions_run_id_scope_filters_siblings(self) -> None:
        """--run-id scoping must restrict post_execute discovery to the target
        run so that append-only sibling runs from prior attempts (which cannot be
        deleted) do not permanently fail the pipeline."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            workspace = repo_root / "workspace"
            node_safe = "problem__shallow_water2d__0.3.0"
            pipeline_dir = (
                workspace / "pipelines" / node_safe / "shallow-water2d_20260415_001"
            )
            for run_id in ("run_test_001", "run_test_002"):
                node_dir = pipeline_dir / "runs" / run_id / node_safe
                node_dir.mkdir(parents=True, exist_ok=True)
                _write_json(node_dir / "perf.json", {"walltime_sec": 0.0})

            # Unscoped (run_ids=None): every sibling run is discovered (legacy).
            all_runs = _node_executions(workspace, pipeline_roots=[pipeline_dir])
            self.assertEqual(
                {"run_test_001", "run_test_002"},
                {e.exec_dir.name for e in all_runs},
            )

            # Scoped: only the target run is discovered.
            scoped = _node_executions(
                workspace, pipeline_roots=[pipeline_dir], run_ids={"run_test_001"}
            )
            self.assertEqual({"run_test_001"}, {e.exec_dir.name for e in scoped})

    def test_required_raw_evidence_execution_trace_is_ir_driven(self) -> None:
        """RC1: execution_trace.json must be IR-driven, not a fixed default.

        When the IR's raw_requirements does not declare execution_trace, it must
        NOT be required (so a node whose IR is silent about it passes without a
        spurious "raw/execution_trace.json: missing"). Declaring it required:true
        in the IR must still re-mandate it. metrics_basis.json stays as the
        baseline requirement either way.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            node_safe = "problem__shallow_water2d__0.3.0"
            ir_ref = (
                "workspace/ir/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001"
            )
            pipeline_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / node_safe
                / "shallow-water2d_20260415_001"
            )
            ir_dir = repo_root / ir_ref
            pipeline_dir.mkdir(parents=True, exist_ok=True)
            ir_dir.mkdir(parents=True, exist_ok=True)
            _write_json(pipeline_dir / "lineage.json", {"ir_ref": ir_ref})

            execution = NodeExecution(
                node_key="problem/shallow_water2d@0.3.0",
                node_dir=pipeline_dir / "runs" / "run_test_001" / node_safe,
                exec_dir=pipeline_dir / "runs" / "run_test_001",
                pipeline_dir=pipeline_dir,
            )

            spec_ir_path = ir_dir / "spec.ir.yaml"

            # IR silent about execution_trace -> not required; baseline kept.
            _write_json(
                spec_ir_path,
                {
                    "io_contract": {
                        "raw_requirements": {
                            "required_evidence": [
                                {"artifact": "metrics_basis.json", "required": True},
                                {
                                    "artifact": "state_snapshots",
                                    "required": True,
                                    "min_samples": 1,
                                },
                            ]
                        }
                    }
                },
            )
            required = _required_raw_evidence(repo_root, execution)
            self.assertNotIn("execution_trace.json", required)
            self.assertIn("metrics_basis.json", required)

            # IR explicitly declares execution_trace required -> re-mandated.
            _write_json(
                spec_ir_path,
                {
                    "io_contract": {
                        "raw_requirements": {
                            "required_evidence": [
                                {"artifact": "metrics_basis.json", "required": True},
                                {"artifact": "execution_trace.json", "required": True},
                            ]
                        }
                    }
                },
            )
            required = _required_raw_evidence(repo_root, execution)
            self.assertIn("execution_trace.json", required)

    def test_post_execute_run_id_scope_requires_every_pipeline_root(self) -> None:
        """With repeated --pipeline-root, --run-id scoping must fail when any
        requested root lacks an execution for the requested run, instead of
        silently validating only the subset of roots that contain it (false
        PASS for dependency/all_nodes roots)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            workspace = repo_root / "workspace"
            node_safe = "problem__shallow_water2d__0.3.0"
            root_with_run = (
                workspace / "pipelines" / node_safe / "shallow-water2d_20260415_001"
            )
            node_dir = root_with_run / "runs" / "run_test_001" / node_safe
            node_dir.mkdir(parents=True, exist_ok=True)
            _write_json(node_dir / "perf.json", {"walltime_sec": 0.0})

            # Second requested root holds only a different (sibling) run id.
            root_without_run = (
                workspace / "pipelines" / node_safe / "shallow-water2d_20260415_002"
            )
            other_node_dir = root_without_run / "runs" / "run_test_999" / node_safe
            other_node_dir.mkdir(parents=True, exist_ok=True)
            _write_json(other_node_dir / "perf.json", {"walltime_sec": 0.0})

            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                pipeline_roots=[root_with_run, root_without_run],
                require_llm_review=False,
                require_orchestration=False,
                run_ids={"run_test_001"},
            )
            self.assertTrue(
                any(
                    "no execution artifacts found for requested --run-id" in v
                    and "shallow-water2d_20260415_002" in v
                    for v in violations
                ),
                violations,
            )

    def test_rejects_run_node_dir_not_matching_pipeline_node_safe(self) -> None:
        """A run node dir whose name != the pipeline's node_key_safe must fail.

        Discovery reconstructs node_key from the inner directory name; downstream
        node_key matching normalizes away @version, so a forged version segment
        (e.g. ...__9.9.9 under a ...__0.3.0 pipeline) would otherwise pass. The
        validator must reject it instead of treating it as the execution node.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            forged_node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__9.9.9"
            )
            forged_node_dir.mkdir(parents=True, exist_ok=True)
            (forged_node_dir / "perf.json").write_text("{}\n", encoding="utf-8")

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "run node directory name must equal pipeline node_key_safe" in v
                    and "problem__shallow_water2d__9.9.9" in v
                    for v in violations
                ),
                violations,
            )
            self.assertFalse(
                any("no execution artifacts found" in v for v in violations),
                violations,
            )

    def test_rejects_unparseable_run_node_dir_with_artifacts(self) -> None:
        """An artifact-bearing run dir whose name is not a valid node_key_safe
        must be reported, not silently dropped, even when a canonical execution
        exists elsewhere."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            # Plant an artifact-bearing run dir with an unparseable name (no
            # `<kind>__<id>__<version>` structure) alongside the canonical run.
            bad_node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_002"
                / "garbage_dir"
            )
            bad_node_dir.mkdir(parents=True, exist_ok=True)
            (bad_node_dir / "perf.json").write_text("{}\n", encoding="utf-8")

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "run node directory name must equal pipeline node_key_safe" in v
                    and "garbage_dir" in v
                    for v in violations
                ),
                violations,
            )

    def test_rejects_judge_only_noncanonical_run_dir(self) -> None:
        """A non-canonical run dir holding only Validate.judge outputs (no execute
        markers) must still be reported, even when a canonical execution exists."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            judge_only_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_003"
                / "problem__shallow_water2d__9.9.9"
            )
            judge_only_dir.mkdir(parents=True, exist_ok=True)
            (judge_only_dir / "verdict.json").write_text("{}\n", encoding="utf-8")

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "run node directory name must equal pipeline node_key_safe" in v
                    and "problem__shallow_water2d__9.9.9" in v
                    for v in violations
                ),
                violations,
            )

    def test_rejects_legacy_nested_run_artifacts(self) -> None:
        """Legacy nested run artifacts (runs/<run_id>/<kind>/<spec>/perf.json),
        where markers sit a level below the immediate run child, must be reported
        alongside a canonical execution — not silently skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            legacy_spec_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_004"
                / "problem"
                / "shallow_water2d"
            )
            legacy_spec_dir.mkdir(parents=True, exist_ok=True)
            (legacy_spec_dir / "perf.json").write_text("{}\n", encoding="utf-8")

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "run node directory name must equal pipeline node_key_safe" in v
                    and "got 'problem'" in v
                    for v in violations
                ),
                violations,
            )

    def test_rejects_noncanonical_run_dir_with_only_log_output(self) -> None:
        """A non-canonical run dir holding only a non-marker Validate output
        (e.g. stdout.log / validate_meta.json, not in the fixed marker set) must
        still be reported — content detection is general, not basename-keyed."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            log_only_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_005"
                / "problem__shallow_water2d__9.9.9"
            )
            log_only_dir.mkdir(parents=True, exist_ok=True)
            (log_only_dir / "stdout.log").write_text("ran\n", encoding="utf-8")

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "run node directory name must equal pipeline node_key_safe" in v
                    and "problem__shallow_water2d__9.9.9" in v
                    for v in violations
                ),
                violations,
            )

    def test_rejects_malformed_pipeline_node_safe_parent_with_artifacts(self) -> None:
        """A pipeline whose node_key_safe parent dir is unparseable must be
        reported when it holds run artifacts, even though _node_executions skips
        it and a canonical execution exists elsewhere."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            # Malformed pipeline parent ("bad" is not <kind>__<id>__<version>);
            # the run child matches that malformed parent name.
            bad_run_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "bad"
                / "shallow-water2d_20260415_009"
                / "runs"
                / "run_test_001"
                / "bad"
            )
            bad_run_dir.mkdir(parents=True, exist_ok=True)
            (bad_run_dir / "perf.json").write_text("{}\n", encoding="utf-8")

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "is not a valid '<spec_kind>__<spec_id>__<spec_version>'" in v
                    and f"{bad_run_dir}" in v
                    for v in violations
                ),
                violations,
            )

    def test_rejects_lineage_node_key_mismatched_with_pipeline_parent(self) -> None:
        """lineage.json.node_key must resolve to the pipeline's node_key_safe
        parent directory. A bumped @version in node_key (while the parent dir and
        run dir agree) must be rejected, otherwise the tree would validate against
        another node's IR since downstream node matching strips @version."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            lineage_path = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "lineage.json"
            )
            lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
            lineage["node_key"] = "problem/shallow_water2d@9.9.9"
            _write_json(lineage_path, lineage)

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "must match pipeline node_key_safe directory" in v
                    and "problem/shallow_water2d@9.9.9" in v
                    for v in violations
                ),
                violations,
            )

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
            self.assertTrue(any("must include spec.ir.yaml" in v for v in violations))

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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertEqual([], violations)

    def test_broken_append_only_sibling_source_does_not_poison_in_scope_run(self) -> None:
        """post_execute structural SOURCE checks are scoped to the source the
        in-scope run declares via trial_meta.source_source_id — a historically
        broken append-only sibling source (left by an earlier Generate attempt,
        unremovable under the append-only contract) must NOT permanently fail an
        otherwise-conformant run. Regression for orch_20260608T012651Z_e906113b,
        where pipeline-wide source scanning let src_002 (Makefile OBJDIR-prefix
        defect) poison the clean src_20260609_001 / run_20260609_002.
        """
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            # Plant a broken append-only sibling source. Its Makefile has the
            # OBJDIR-prefix defect (a bare object prerequisite on the link rule
            # whose producing rule targets $(OBJDIR)/main.o) — flagged when
            # scanned directly, but the in-scope run never references it.
            pipeline_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
            )
            broken_src = pipeline_dir / "source" / "src_20260415_999" / "src"
            broken_src.mkdir(parents=True, exist_ok=True)
            (broken_src / "main.f90").write_text(
                "program app\nimplicit none\nwrite(*,*) 1\nend program app\n",
                encoding="utf-8",
            )
            broken_makefile = (
                "FC ?= gfortran\n"
                "OBJDIR ?= .\n"
                "BINDIR ?= .\n"
                "BIN := app\n"
                "$(BINDIR)/$(BIN): main.o | $(BINDIR)\n"
                "\t$(FC) -o $@ main.o\n"
                "$(OBJDIR)/main.o: main.f90 | $(OBJDIR)\n"
                "\t$(FC) -c $< -o $@\n"
            )
            (broken_src / "Makefile").write_text(broken_makefile, encoding="utf-8")

            # Sanity: the planted sibling IS genuinely defective when scanned.
            sibling_violations: list[str] = []
            _validate_fortran_makefile_src_dir(broken_src, sibling_violations)
            self.assertTrue(
                any(
                    "must carry the same $(OBJDIR)/ prefix" in v
                    for v in sibling_violations
                ),
                f"planted sibling must be a real defect; got: {sibling_violations}",
            )

            # The pipeline must still PASS: the broken sibling is out of scope.
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertEqual([], violations)

    def test_path_like_source_source_id_is_rejected(self) -> None:
        """The scoped source lookup uses trial_meta.source_source_id directly as a
        path component. A value containing a separator / absolute path / traversal
        must be rejected (not used to scan an unintended directory), so a forged or
        malformed trial_meta cannot escape `<pipeline>/source/`.
        """
        for bad_id in ("../../escape", "/abs/source", "a/b"):
            with self.subTest(source_source_id=bad_id):
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
                        run_command=[
                            "./simulate",
                            "workspace/spec.ir.yaml",
                            "workspace/outdir",
                        ],
                    )
                    trial_meta_path = (
                        repo_root
                        / "workspace"
                        / "pipelines"
                        / "problem__shallow_water2d__0.3.0"
                        / "shallow-water2d_20260415_001"
                        / "runs"
                        / "run_test_001"
                        / "problem__shallow_water2d__0.3.0"
                        / "trial_meta.json"
                    )
                    trial_meta = json.loads(
                        trial_meta_path.read_text(encoding="utf-8")
                    )
                    trial_meta["source_source_id"] = bad_id
                    trial_meta_path.write_text(
                        json.dumps(trial_meta, ensure_ascii=False),
                        encoding="utf-8",
                    )

                    violations = validate(
                        repo_root=repo_root, workspace_root="workspace"
                    )
                    self.assertTrue(
                        any(
                            "must be a plain source directory name" in v
                            for v in violations
                        ),
                        f"path-like source_source_id must be rejected; got: {violations}",
                    )

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
          (b) spec.ir.yaml with the same shorthand also fails with
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            workspace = repo_root / "workspace"
            node_safe = "problem__shallow_water2d__0.3.0"
            pipeline_id = "shallow-water2d_20260415_001"
            snapshots_dir = (
                workspace / "pipelines" / node_safe / pipeline_id
                / "runs" / "run_test_001" / "problem__shallow_water2d__0.3.0"
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
                workspace / "ir" / node_safe / "shallow-water2d_20260415_001"
                / "spec.ir.yaml"
            )
            derived = json.loads(derived_path.read_text(encoding="utf-8"))
            io = derived.get("io_contract", derived)
            for entry in io["raw_requirements"]["required_evidence"]:
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
            # (b) io_contract rejected
            self.assertTrue(
                any(
                    "spec.ir.yaml" in v
                    and "must not use 'state_variables' shorthand" in v
                    for v in violations
                ),
                f"Expected io_contract shorthand rejection; got: {violations}",
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
            sd = repo_root / "spec" / "schema" / "ir"
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
            sd = repo_root / "spec" / "schema" / "ir"
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
            sd = repo_root / "spec" / "schema" / "ir"
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
            sd = repo_root / "spec" / "schema" / "ir"
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
                sd = repo / "spec" / "schema" / "ir"
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
            sd = repo / "spec" / "schema" / "ir"
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
            sd = repo_root / "spec" / "schema" / "ir"
            sd.mkdir(parents=True)
            schema = {
                "oneOf": [
                    {"title": "0-dimensional (zero-dim)", "pattern": r"^[Ss][Cc][Aa][Ll][Aa][Rr]$"},
                    {"title": "list form", "pattern": r"^\[[0-9]+(?:,[0-9]+)*\]$"},
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
            sd = repo_root / "spec" / "schema" / "ir"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            perf_path = (
                repo_root
                / "workspace/pipelines/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/runs/run_test_001/problem__shallow_water2d__0.3.0/perf.json"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran F0/F0.d descriptor" in v for v in violations)
            )

    def test_detects_unsafe_fortran_l1_boolean_serialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
logical :: guard_pass
integer :: u
call shallow_water2d__step(guard_pass)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
write(u,'(a,l1,a)') '{"guard_pass":', guard_pass, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"expected L-descriptor violation, got: {violations}",
            )

    def test_detects_unsafe_serialization_with_keyword_fmt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass, walltime_sec)
  logical, intent(out) :: guard_pass
  real(8), intent(out) :: walltime_sec
  guard_pass = .true.
  walltime_sec = 2.0d-6
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # Both writes use valid Fortran keyword `fmt=` syntax (and one uses
            # `unit=`), which must still be caught.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
logical :: guard_pass
real(8) :: walltime_sec
integer :: u
call shallow_water2d__step(guard_pass, walltime_sec)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
write(u, fmt='(a,l1,a)') '{"guard_pass":', guard_pass, '}'
close(u)
open(newunit=u, file='perf.json', status='replace', action='write')
write(unit=u, fmt='(a,f0.6,a)') '{"walltime_sec":', walltime_sec, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"expected L-descriptor violation (keyword fmt=), got: {violations}",
            )
            self.assertTrue(
                any("Fortran F0/F0.d descriptor" in v for v in violations),
                msg=f"expected F0 violation (keyword fmt=/unit=), got: {violations}",
            )

    def test_detects_f0_after_scale_factor(self) -> None:
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
            # 1P scale factor precedes the F0 descriptor; still leading-zero unsafe.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
real(8) :: walltime_sec
integer :: u
call shallow_water2d__step(walltime_sec)
open(newunit=u, file='perf.json', status='replace', action='write')
write(u,'(a,1pf0.6,a)') '{"walltime_sec":', walltime_sec, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran F0/F0.d descriptor" in v for v in violations),
                msg=f"expected F0 violation after 1P scale factor, got: {violations}",
            )

    def test_detects_unsafe_serialization_across_continuation_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # The write statement wraps over a free-form `&` continuation; the
            # format literal lands on a physical line without the word `write`.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
logical :: guard_pass
integer :: u
call shallow_water2d__step(guard_pass)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
write(unit=u, &
      fmt='(a,l1,a)') '{"guard_pass":', guard_pass, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"expected L violation across continuation, got: {violations}",
            )

    def test_read_with_write_named_variable_is_not_flagged(self) -> None:
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
            # `read(...) write_flag` is legitimate logical INPUT parsing; the
            # variable name contains "write" but must not trip the output gate.
            # The JSON write itself uses a safe literal format.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
real(8) :: walltime_sec
logical :: write_flag
integer :: u
read(u,'(l1)') write_flag
call shallow_water2d__step(walltime_sec)
open(newunit=u, file='perf.json', status='replace', action='write')
write(u,'(a,f12.6,a)') '{"walltime_sec":', walltime_sec, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(
                    "Fortran L edit descriptor" in v or "Fortran F0/F0.d descriptor" in v
                    for v in violations
                ),
                msg=f"read-input L descriptor must not be flagged, got: {violations}",
            )

    def test_detects_unsafe_serialization_via_labeled_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # Format referenced by integer label bound to a FORMAT statement.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
logical :: guard_pass
integer :: u
call shallow_water2d__step(guard_pass)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
write(u, 100) '{"guard_pass":', guard_pass, '}'
100 format(a,l1,a)
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"expected L violation via labeled FORMAT, got: {violations}",
            )

    def test_detects_unsafe_serialization_via_named_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # Format referenced by a named character constant.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
character(*), parameter :: jfmt = '(a,l1,a)'
logical :: guard_pass
integer :: u
call shallow_water2d__step(guard_pass)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
write(u, jfmt) '{"guard_pass":', guard_pass, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"expected L violation via named FORMAT, got: {violations}",
            )

    def test_embedded_string_in_format_is_not_flagged(self) -> None:
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
            # The format embeds literal JSON text whose bytes happen to contain
            # "l1"/"f0"; those are data, not edit descriptors, and the actual
            # numeric descriptor (f12.6) is leading-zero safe.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
real(8) :: walltime_sec
integer :: u
call shallow_water2d__step(walltime_sec)
open(newunit=u, file='perf.json', status='replace', action='write')
write(u,'("{""walltime_l1_f0"":",f12.6,"}")') walltime_sec
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(
                    "Fortran L edit descriptor" in v or "Fortran F0/F0.d descriptor" in v
                    for v in violations
                ),
                msg=f"embedded format text must not be flagged, got: {violations}",
            )

    def test_real_descriptor_alongside_embedded_string_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # A genuine L descriptor outside the embedded text must still be caught.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
logical :: guard_pass
integer :: u
call shallow_water2d__step(guard_pass)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
write(u,'("{""guard_pass"":",l1,"}")') guard_pass
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"real L descriptor must still be flagged, got: {violations}",
            )

    def test_reassigned_format_var_uses_reaching_definition(self) -> None:
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
            # `fmt` holds an L descriptor for read-side parsing, then is reassigned
            # a safe format before the JSON write. The reaching definition at the
            # write is the safe one, so nothing must be flagged.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
real(8) :: walltime_sec
logical :: flag
character(len=32) :: fmt
integer :: u
fmt = '(l1)'
read(u, fmt) flag
call shallow_water2d__step(walltime_sec)
fmt = '(a,f12.6,a)'
open(newunit=u, file='perf.json', status='replace', action='write')
write(u, fmt) '{"walltime_sec":', walltime_sec, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(
                    "Fortran L edit descriptor" in v or "Fortran F0/F0.d descriptor" in v
                    for v in violations
                ),
                msg=f"read-side format must not reach the write, got: {violations}",
            )

    def test_reassigned_format_var_flags_unsafe_reaching_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # The reaching definition at the write IS the unsafe L format.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
logical :: guard_pass
character(len=32) :: fmt
integer :: u
call shallow_water2d__step(guard_pass)
fmt = '(a,a,a)'
fmt = '(a,l1,a)'
open(newunit=u, file='diagnostics.json', status='replace', action='write')
write(u, fmt) '{"guard_pass":', guard_pass, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"unsafe reaching format must be flagged, got: {violations}",
            )

    def test_noop_reassignment_does_not_bypass_unsafe_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # An unsafe literal followed by a non-literal (no-op) reassignment must
            # not hide the still-unsafe descriptor reaching the write.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
logical :: guard_pass
character(len=32) :: fmt
integer :: u
call shallow_water2d__step(guard_pass)
fmt = '(a,l1,a)'
fmt = fmt
open(newunit=u, file='diagnostics.json', status='replace', action='write')
write(u, fmt) '{"guard_pass":', guard_pass, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"no-op reassignment must not bypass unsafe format, got: {violations}",
            )

    def test_format_label_scoped_to_program_unit(self) -> None:
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
            # The main program's write(u,100) uses a safe FORMAT label 100. A helper
            # subroutine reuses label 100 with an unsafe descriptor but only for a
            # read. Labels are per-scope, so the main write must not be flagged.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
real(8) :: walltime_sec
integer :: u
call shallow_water2d__step(walltime_sec)
open(newunit=u, file='perf.json', status='replace', action='write')
write(u, 100) '{"walltime_sec":', walltime_sec, '}'
100 format(a,f12.6,a)
close(u)
call helper(u)
contains
subroutine helper(unit)
  integer, intent(in) :: unit
  logical :: flag
  read(unit, 100) flag
100 format(l1)
end subroutine helper
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(
                    "Fortran L edit descriptor" in v or "Fortran F0/F0.d descriptor" in v
                    for v in violations
                ),
                msg=f"per-scope label must not cross units, got: {violations}",
            )

    def test_detects_host_associated_named_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # `jfmt` is declared in the host program and used by an internal
            # subroutine via host association; the unsafe descriptor must be found.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
character(*), parameter :: jfmt = '(a,l1,a)'
logical :: guard_pass
integer :: u
call shallow_water2d__step(guard_pass)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
call emit(u, guard_pass)
close(u)
contains
subroutine emit(unit, gp)
  integer, intent(in) :: unit
  logical, intent(in) :: gp
  write(unit, jfmt) '{"guard_pass":', gp, '}'
end subroutine emit
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"host-associated format must be resolved, got: {violations}",
            )

    def test_detects_unsafe_format_in_semicolon_statement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # Assignment and write share one physical line via `;`.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
logical :: guard_pass
character(len=32) :: fmt
integer :: u
call shallow_water2d__step(guard_pass)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
fmt = '(a,l1,a)'; write(u, fmt) '{"guard_pass":', guard_pass, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"semicolon-joined write must be scanned, got: {violations}",
            )

    def test_detects_concatenated_named_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # Named format assembled from constant character concatenation.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
character(*), parameter :: jfmt = '(a,' // 'l1,a)'
logical :: guard_pass
integer :: u
call shallow_water2d__step(guard_pass)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
write(u, jfmt) '{"guard_pass":', guard_pass, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"concatenated named format must be resolved, got: {violations}",
            )

    def test_detects_concatenated_inline_format(self) -> None:
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
            # Inline format assembled by concatenation; F0 in the second piece.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
real(8) :: walltime_sec
integer :: u
call shallow_water2d__step(walltime_sec)
open(newunit=u, file='perf.json', status='replace', action='write')
write(u, '(a,' // 'f0.6,a)') '{"walltime_sec":', walltime_sec, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran F0/F0.d descriptor" in v for v in violations),
                msg=f"concatenated inline format must be scanned, got: {violations}",
            )

    def test_same_line_assignment_after_write_does_not_reach(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # On one physical line: safe format, the write, then an unsafe
            # reassignment. The unsafe one is AFTER the write and must not reach it.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
logical :: guard_pass
character(len=32) :: fmt
integer :: u
call shallow_water2d__step(guard_pass)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
fmt = '(a,a,a)'; write(u, fmt) '{"guard_pass":', guard_pass, '}'; fmt = '(a,l1,a)'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(
                    "Fortran L edit descriptor" in v or "Fortran F0/F0.d descriptor" in v
                    for v in violations
                ),
                msg=f"later same-line assignment must not reach the write, got: {violations}",
            )

    def test_same_line_unsafe_assignment_before_write_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # Unsafe assignment precedes the write on the same physical line.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
logical :: guard_pass
character(len=32) :: fmt
integer :: u
call shallow_water2d__step(guard_pass)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
fmt = '(a,a,a)'; fmt = '(a,l1,a)'; write(u, fmt) '{"guard_pass":', guard_pass, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"earlier same-line unsafe assignment must be flagged, got: {violations}",
            )

    def test_formatted_stdout_debug_write_is_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass, walltime_sec)
  logical, intent(out) :: guard_pass
  real(8), intent(out) :: walltime_sec
  guard_pass = .true.
  walltime_sec = 2.0d-6
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # Formatted debug write to stdout (unit *) uses an L descriptor; it is
            # not a JSON artifact and must not be flagged. JSON write is safe.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
logical :: guard_pass
real(8) :: walltime_sec
integer :: u
call shallow_water2d__step(guard_pass, walltime_sec)
write(*,'(a,l1)') 'debug guard_pass=', guard_pass
open(newunit=u, file='perf.json', status='replace', action='write')
write(u,'(a,f12.6,a)') '{"walltime_sec":', walltime_sec, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(
                    "Fortran L edit descriptor" in v or "Fortran F0/F0.d descriptor" in v
                    for v in violations
                ),
                msg=f"stdout debug write must not be flagged, got: {violations}",
            )

    def test_detects_unsafe_format_in_multiname_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            model_text = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine shallow_water2d__step(guard_pass)
  logical, intent(out) :: guard_pass
  guard_pass = .true.
end subroutine shallow_water2d__step
end module shallow_water2d_model
"""
            # Unsafe format declared first in a multi-name parameter declaration.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
character(*), parameter :: jfmt = '(a,l1,a)', kfmt = '(a,i0,a)'
logical :: guard_pass
integer :: u
call shallow_water2d__step(guard_pass)
open(newunit=u, file='diagnostics.json', status='replace', action='write')
write(u, jfmt) '{"guard_pass":', guard_pass, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran L edit descriptor" in v for v in violations),
                msg=f"multi-name declared format must be resolved, got: {violations}",
            )

    def test_detects_unsafe_format_with_blanks_in_descriptor(self) -> None:
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
            # Blank inside the F0 descriptor is insignificant in Fortran formats.
            runner_text = """program shallow_water2d_runner
use shallow_water2d_model
implicit none
real(8) :: walltime_sec
integer :: u
call shallow_water2d__step(walltime_sec)
open(newunit=u, file='perf.json', status='replace', action='write')
write(u,'(a,f 0.6,a)') '{"walltime_sec":', walltime_sec, '}'
close(u)
end program shallow_water2d_runner
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Fortran F0/F0.d descriptor" in v for v in violations),
                msg=f"blank-separated F0 descriptor must be scanned, got: {violations}",
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                io_contract={
                    "inputs": [
                            {
                                "name": "case_resolved",
                                "source": "spec.ir.yaml",
                                "evidence_ref": "spec.ir.yaml",
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
            violations = validate_compile_stage(
                repo_root,
                "workspace",
                "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                extra_sources={
                    "dynamics_shallow_water_flux_2d_rusanov_p0_model.f90": dep_model_text
                },
                makefile_text=makefile_text,
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("missing prerequisite for used module" in v for v in violations)
            )

    def test_accepts_makefile_variable_resolved_fortran_module_dependency(self) -> None:
        # Mirrors the real generated Makefile: the runner object rule declares
        # its used-module prerequisite via a variable ($(MODEL_OBJ)) defined as
        # $(OBJDIR)/..._model.o. This is valid Make and must not be flagged as a
        # missing prerequisite (regression guard against the false positive).
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
OBJDIR ?= .
DEP_OBJ = $(OBJDIR)/dynamics_shallow_water_flux_2d_rusanov_p0_model.o
MODEL_OBJ = $(OBJDIR)/shallow_water2d_model.o
RUNNER_OBJ = $(OBJDIR)/shallow_water2d_runner.o
OBJS = $(DEP_OBJ) $(MODEL_OBJ) $(RUNNER_OBJ)

simulate: $(OBJS)
\t$(FC) -o $@ $(OBJS)

$(DEP_OBJ): dynamics_shallow_water_flux_2d_rusanov_p0_model.f90 | $(OBJDIR)
\t$(FC) -J$(OBJDIR) -c $< -o $@

$(MODEL_OBJ): shallow_water2d_model.f90 $(DEP_OBJ) | $(OBJDIR)
\t$(FC) -J$(OBJDIR) -I$(OBJDIR) -c $< -o $@

$(RUNNER_OBJ): shallow_water2d_runner.f90 $(MODEL_OBJ) | $(OBJDIR)
\t$(FC) -I$(OBJDIR) -c $< -o $@
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id=dep_spec_id,
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                extra_sources={
                    "dynamics_shallow_water_flux_2d_rusanov_p0_model.f90": dep_model_text
                },
                makefile_text=makefile_text,
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any("missing prerequisite for used module" in v for v in violations),
                f"variable-resolved prerequisite should be accepted; got: {violations}",
            )

    def test_detects_makefile_forward_referenced_prereq_variable(self) -> None:
        # A prerequisite variable referenced *before* its definition expands to
        # empty in GNU make (rule prerequisites are expanded at read time), so
        # the dependency is genuinely absent and must still be flagged.
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
            # MODEL_OBJ is used in the runner rule's prerequisites but defined
            # only afterwards -> make sees an empty prerequisite -> violation.
            makefile_text = """FC ?= gfortran
OBJDIR ?= .
DEP_OBJ = $(OBJDIR)/dynamics_shallow_water_flux_2d_rusanov_p0_model.o

$(DEP_OBJ): dynamics_shallow_water_flux_2d_rusanov_p0_model.f90 | $(OBJDIR)
\t$(FC) -J$(OBJDIR) -c $< -o $@

shallow_water2d_model.o: shallow_water2d_model.f90 $(DEP_OBJ) | $(OBJDIR)
\t$(FC) -J$(OBJDIR) -I$(OBJDIR) -c $< -o $@

shallow_water2d_runner.o: shallow_water2d_runner.f90 $(MODEL_OBJ) | $(OBJDIR)
\t$(FC) -I$(OBJDIR) -c $< -o $@

MODEL_OBJ = $(OBJDIR)/shallow_water2d_model.o
"""
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id=dep_spec_id,
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                extra_sources={
                    "dynamics_shallow_water_flux_2d_rusanov_p0_model.f90": dep_model_text
                },
                makefile_text=makefile_text,
            )

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("missing prerequisite for used module" in v for v in violations),
                f"forward-referenced prereq variable should be flagged; got: {violations}",
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            review_path = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                io_contract={
                    "inputs": [{"name": "case_resolved", "source": "spec.ir.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/metrics_basis.json",
                            }
                        ],
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
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                io_contract={
                    "inputs": [{"name": "case_resolved", "source": "spec.ir.yaml"}],
                        "outputs": [],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            # run_quality_checks canonical placement: cross-phase under
            # generate/<gen>/src/mcp_command_log.jsonl. Append to the existing
            # canonical log written by the fixture.
            qc_log_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/source/src_20260415_001/src/"
                "mcp_command_log.jsonl"
            )
            trial_meta["source_command_ref"]["run_quality_checks"] = {
                "command_id": "cmd_quality_001",
                "tool_name": "run_quality_checks",
                "command_log_ref": qc_log_ref,
            }
            trial_meta["source_source_id"] = "src_20260415_001"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
            )
            src_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "source"
                / "src_20260415_001"
                / "src"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            qc_log_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/source/src_20260415_001/src/"
                "mcp_command_log.jsonl"
            )
            trial_meta["source_command_ref"]["run_quality_checks"] = {
                "command_id": "cmd_quality_001",
                "tool_name": "run_quality_checks",
                "command_log_ref": qc_log_ref,
            }
            trial_meta["source_source_id"] = "src_20260415_001"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
            )
            src_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "source"
                / "src_20260415_001"
                / "src"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            qc_log_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/source/src_20260415_001/src/"
                "mcp_command_log.jsonl"
            )
            trial_meta["source_command_ref"]["run_quality_checks"] = {
                "command_id": "cmd_quality_001",
                "tool_name": "run_quality_checks",
                "command_log_ref": qc_log_ref,
            }
            trial_meta["source_source_id"] = "src_20260415_001"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            outside_log = repo_root / "mcp_command_log.jsonl"
            outside_log.write_text(
                json.dumps(
                    {
                        "command_id": "cmd_run_001",
                        "tool_name": "run_program",
                        "command": ["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                "## 7. Test definitions\n"
                "### 7-1. `test_a`\n"
                "### 7-2. `test_b`\n",
                encoding="utf-8",
            )

            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                io_contract={
                    "source": {
                        "tests": "spec/problem/mock_domain/mock_family/mock_spec/tests.md"
                    },
                    "inputs": [{"name": "case_resolved", "source": "spec.ir.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/metrics_basis.json",
                            }
                        ],
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
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
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
                "## 7. Test definitions\n"
                "### 7-1. `l1_refinement_mass_and_positivity`\n",
                encoding="utf-8",
            )

            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                io_contract={
                    "source": {
                        "tests": "spec/problem/dynamics/shallow_water/shallow_water2d/tests.md"
                    },
                    "inputs": [{"name": "case_resolved", "source": "spec.ir.yaml"}],
                        "outputs": [
                            {"name": "metric_mass", "shape_expr": "scalar", "evidence_ref": "raw/metrics_basis.json"},
                        ],
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
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
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
                "## 7. Test definitions\n"
                "### 7-1. `test_a`\n"
                "### 7-2. `test_b`\n",
                encoding="utf-8",
            )
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=model_text,
                runner_text=runner_text,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                io_contract={
                    "source": {"tests": "spec/problem/mock_domain/mock_family/mock_spec/tests.md"},
                    "inputs": [{"name": "case_resolved", "source": "spec.ir.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/metrics_basis.json",
                                "raw_variables": ["h", "hu", "hv", "time"],
                            }
                        ],
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
                "## 7. Test definitions\n"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                io_contract={
                    "source": {"tests": "spec/problem/mock_domain/mock_family/mock_spec/tests.md"},
                    "inputs": [{"name": "case_resolved", "source": "spec.ir.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/metrics_basis.json",
                                "raw_variables": ["h", "time"],
                            }
                        ],
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
                "## 7. Test definitions\n"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                io_contract={
                    "source": {"tests": "spec/problem/mock_domain/mock_family/mock_spec/tests.md"},
                    "inputs": [{"name": "case_resolved", "source": "spec.ir.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/metrics_basis.json",
                                "raw_variables": ["h", "time"],
                            }
                        ],
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

    def test_detects_snapshot_shape_mismatch_against_io_contract(self) -> None:
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )

            snapshots_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            algorithm_path = (
                repo_root
                / "workspace"
                / "ir"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "spec.ir.yaml"
            )
            # Rewrite spec.ir.yaml as YAML (was JSON in test helper). The new
            # IR-centric design requires the algorithm content nested under an
            # `algorithm:` key inside spec.ir.yaml; preserve the other sections
            # the helper wrote so the downstream validators have IR / impl_defaults
            # / io_contract / dependency available.
            import yaml  # noqa: F401  (validator uses pyyaml internally too)
            existing = json.loads(algorithm_path.read_text(encoding="utf-8"))
            algorithm_block = {
                "algorithm_id": "shallow_water2d_test_algorithm",
                "execution_mode": "sequence",
                "ordering": ["compute_flux"],
                "control_condition": "always",
                "iteration_contract": {"kind": "none"},
                "steps": [
                    {
                        "step_id": "compute_flux",
                        "step_kind": "flux_compute",
                        "operation_ref": "dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux",
                        "inputs": ["h", "hu", "hv"],
                        "outputs": ["h", "hu", "hv"],
                    }
                ],
                "update_semantics": {
                    "state_variables": [
                        {"name": "h", "shape_expr": "[2,2]"},
                        {"name": "hu", "shape_expr": "[2,2]"},
                        {"name": "hv", "shape_expr": "[2,2]"},
                    ],
                    "required_update_paths": ["h", "hu", "hv"],
                    "diagnostics_from_state": True,
                    "fallback_policy": "fail_closed",
                },
                "temporaries": [],
                "derived_field_rules": [],
                "invariants": [],
                "splitting_policy": {"kind": "none"},
            }
            existing["algorithm"] = algorithm_block
            algorithm_path.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(any("invalid yaml" in v for v in violations))
            self.assertFalse(any("spec.ir.yaml" in v for v in violations))

    def test_detects_invalid_raw_artifact_vocabulary_in_io_contract(self) -> None:
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                io_contract={
                    "inputs": [{"name": "case_resolved", "source": "spec.ir.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/ghost_cells",
                            }
                        ],
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

    def test_detects_snapshot_output_shape_mismatch_inside_io_contract(self) -> None:
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                io_contract={
                    "inputs": [{"name": "case_resolved", "source": "spec.ir.yaml"}],
                        "outputs": [
                            {
                                "name": "U_np1",
                                "shape_expr": "(3, 2, 2)",
                                "evidence_ref": "raw/state_snapshots",
                                "raw_variables": ["h"],
                            }
                        ],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                io_contract={
                    "source": {"tests": "spec/problem/shallow_water2d/tests.md"},
                    "inputs": [{"name": "case_resolved", "source": "spec.ir.yaml"}],
                        "outputs": [
                            {
                                "name": "metric",
                                "shape_expr": "scalar",
                                "evidence_ref": "raw/metrics_basis.json",
                                "raw_variables": ["h"],
                            }
                        ],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            _create_minimal_orchestration_tree(repo_root)
            step_result = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_test_001"
                / "steps"
                / "problem__shallow_water2d__0.3.0"
                / "compile"
                / "orch_run_001"
                / "step_result.json"
            )
            step_result.unlink()
            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(any("missing step_result.json" in v and "problem__shallow_water2d__0.3.0/compile" in v for v in violations))

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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
                item for item in items if item.get("agent_run_id") != "substep_run_validate_execute_001"
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

    _INFLIGHT_NODE_SAFE = "problem__shallow_water2d__0.3.0"

    def _violations_with_removed_child(
        self,
        repo_root: Path,
        *,
        removed_arid: str | None = None,
        in_flight_arids: list[str] | None = None,
        remove_validate_step_result: bool = False,
        phantom_judge_request_arid: str | None = None,
        reparent_removed_child_to: str | None = None,
        divert_removed_child_to_invalid: bool = False,
        superseded_arids: list[str] | None = None,
    ) -> list[str]:
        """Build the minimal execution + orchestration tree, optionally drop
        ``removed_arid`` from agent_runs.jsonl (leaving its agent_graph edge
        dangling) and/or delete the validate step_result.json, then run pre_judge
        passing ``in_flight_arids`` as --in-flight-agent-run-id declarations.

        ``phantom_judge_request_arid`` writes a validate/judge launch request for
        an arid that is NOT present in agent_graph.json or agent_runs.jsonl — a
        stale/mistyped/cross-orchestration id with no dangling edge — to verify it
        cannot trigger any exemption.

        ``divert_removed_child_to_invalid`` re-appends the removed child's row to
        agent_runs_invalid.jsonl (the terminal-payload-validation diversion, e.g. an
        unauthorized write). ``superseded_arids`` writes
        reopen/superseded_runs.json — together these model a reopen-consumed
        unauthorized-write trigger whose kept agent_graph edge must be exempted."""
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
            run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
        )
        _create_minimal_orchestration_tree(repo_root)
        orch_root = repo_root / "workspace" / "orchestrations" / "orch_test_001"
        runs_path = orch_root / "agent_runs.jsonl"
        items = [
            json.loads(line)
            for line in runs_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        removed_items = [
            item for item in items if item.get("agent_run_id") == removed_arid
        ] if removed_arid is not None else []
        if removed_arid is not None:
            items = [item for item in items if item.get("agent_run_id") != removed_arid]
        runs_path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
            encoding="utf-8",
        )
        if divert_removed_child_to_invalid and removed_items:
            (orch_root / "agent_runs_invalid.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in removed_items)
                + "\n",
                encoding="utf-8",
            )
        if superseded_arids:
            (orch_root / "reopen").mkdir(parents=True, exist_ok=True)
            _write_json(
                orch_root / "reopen" / "superseded_runs.json",
                {
                    "orchestration_id": "orch_test_001",
                    "superseded_agent_run_ids": list(superseded_arids),
                },
            )
        if reparent_removed_child_to is not None and removed_arid is not None:
            graph_path = orch_root / "agent_graph.json"
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            for edge in graph.get("edges", []):
                if edge.get("child_agent_run_id") == removed_arid:
                    edge["parent_agent_run_id"] = reparent_removed_child_to
            _write_json(graph_path, graph)
        if remove_validate_step_result:
            (
                orch_root / "steps" / self._INFLIGHT_NODE_SAFE / "validate"
                / "orch_run_001" / "step_result.json"
            ).unlink()
        if phantom_judge_request_arid is not None:
            _write_json(
                orch_root / "launches" / f"{phantom_judge_request_arid}.request.json",
                {
                    "agent_run_id": phantom_judge_request_arid,
                    "role": "substep",
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "step": "validate",
                    "substep": "judge",
                },
            )
        return validate(
            repo_root=repo_root,
            workspace_root="workspace",
            require_orchestration=True,
            in_flight_agent_run_ids=set(in_flight_arids) if in_flight_arids else None,
        )

    @staticmethod
    def _has_dangling_edge(violations: list[str], arid: str) -> bool:
        return any(
            f"child_agent_run_id not found in agent_runs.jsonl ({arid})" in v
            for v in violations
        )

    @staticmethod
    def _has_missing_validate_step_result(violations: list[str]) -> bool:
        return any(
            "missing step_result.json for" in v and "/validate" in v for v in violations
        )

    def test_pre_judge_passes_after_repair_backfills_legacy_records(self) -> None:
        """End-to-end: pre-caa10ab records (missing parent_agent_run_id /
        agent_model) make the orchestration-hierarchy gate fail; running
        repair_legacy_agent_runs backfills them from authoritative sources
        (step_result executor / agent_graph parent + uniform sibling model) so
        the same validation passes without a fresh orchestration."""
        from tools.orchestration_runtime import repair_legacy_agent_runs

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # Build a clean, passing execution + orchestration tree.
            self._violations_with_removed_child(repo)
            orch_root = repo / "workspace" / "orchestrations" / "orch_test_001"
            runs_path = orch_root / "agent_runs.jsonl"
            items = [
                json.loads(line)
                for line in runs_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            # Degrade a step + two substep records to the pre-fix shape.
            legacy = {
                "step_run_build_001",
                "substep_run_compile_generate_001",
                "substep_run_compile_verify_001",
            }
            for item in items:
                if item.get("agent_run_id") in legacy:
                    item.pop("parent_agent_run_id", None)
                    item.pop("agent_model", None)
            runs_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
                encoding="utf-8",
            )

            before = validate(
                repo_root=repo,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertTrue(
                any("missing parent_agent_run_id" in v for v in before), before
            )
            self.assertTrue(any("missing agent_model" in v for v in before), before)

            out = repair_legacy_agent_runs(repo, "orch_test_001")
            self.assertEqual(out["status"], "repaired", out)
            self.assertEqual(out["agent_model"], "gpt-5-codex", out)

            after = validate(
                repo_root=repo,
                workspace_root="workspace",
                require_orchestration=True,
            )
            self.assertFalse(
                any("missing parent_agent_run_id" in v for v in after), after
            )
            self.assertFalse(any("missing agent_model" in v for v in after), after)
            self.assertFalse(
                any("must equal executor_agent_run_id" in v for v in after), after
            )

    def test_inflight_judge_tolerated_with_explicit_flag(self) -> None:
        """When the live judge declares its own agent_run_id via
        --in-flight-agent-run-id, its not-yet-recorded edge AND its not-yet-written
        validate step_result are tolerated."""
        judge_arid = "substep_run_validate_judge_001"
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._violations_with_removed_child(
                Path(tmp),
                removed_arid=judge_arid,
                in_flight_arids=[judge_arid],
                remove_validate_step_result=True,
            )
            self.assertFalse(
                self._has_dangling_edge(violations, judge_arid),
                msg=f"declared in-flight judge edge must be tolerated; got: {violations}",
            )
            self.assertFalse(
                self._has_missing_validate_step_result(violations),
                msg=f"declared in-flight judge validate step_result must be tolerated; got: {violations}",
            )

    def test_dangling_judge_edge_without_flag_is_not_tolerated(self) -> None:
        """Fail-closed: a dangling judge edge / missing validate step_result with
        NO --in-flight-agent-run-id declaration is NOT tolerated (this is the
        crash / stale-marker / wrong-backend case — no live caller vouches for it)."""
        judge_arid = "substep_run_validate_judge_001"
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._violations_with_removed_child(
                Path(tmp),
                removed_arid=judge_arid,
                in_flight_arids=None,
                remove_validate_step_result=True,
            )
            self.assertTrue(
                self._has_dangling_edge(violations, judge_arid),
                msg=f"undeclared dangling judge edge must NOT be tolerated; got: {violations}",
            )
            self.assertTrue(
                self._has_missing_validate_step_result(violations),
                msg=f"undeclared missing validate step_result must NOT be tolerated; got: {violations}",
            )

    def test_in_flight_flag_for_non_judge_arid_does_nothing(self) -> None:
        """The flag exempts only the validate/judge substep: declaring a non-judge
        arid (e.g. execute) must NOT suppress its orphaned-edge violation."""
        execute_arid = "substep_run_validate_execute_001"
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._violations_with_removed_child(
                Path(tmp),
                removed_arid=execute_arid,
                in_flight_arids=[execute_arid],
            )
            self.assertTrue(
                self._has_dangling_edge(violations, execute_arid),
                msg=f"in-flight flag must not exempt a non-judge child; got: {violations}",
            )

    def test_in_flight_flag_does_not_mask_missing_validate_result_once_judge_recorded(
        self,
    ) -> None:
        """The step_result exemption is gated on the judge being genuinely
        unrecorded. If the judge already has an agent_runs entry, a missing
        validate step_result is a real gap and must still surface even when its
        arid is (stale-ly) passed via the flag."""
        judge_arid = "substep_run_validate_judge_001"
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._violations_with_removed_child(
                Path(tmp),
                removed_arid=None,  # judge stays recorded in agent_runs
                in_flight_arids=[judge_arid],
                remove_validate_step_result=True,
            )
            self.assertTrue(
                self._has_missing_validate_step_result(violations),
                msg=f"missing validate step_result must surface once judge is recorded; got: {violations}",
            )

    def test_in_flight_flag_without_dangling_edge_does_not_mask_missing_validate_result(
        self,
    ) -> None:
        """The validate step_result exemption requires graph evidence: a flag that
        names a validate/judge arid which is NOT an actual dangling edge (stale /
        mistyped / cross-orchestration — it has a launch request but no edge in
        agent_graph.json) must NOT suppress the missing validate step_result."""
        phantom_judge = "substep_run_validate_judge_phantom"
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._violations_with_removed_child(
                Path(tmp),
                removed_arid=None,  # all real runs recorded; no dangling edge
                in_flight_arids=[phantom_judge],
                remove_validate_step_result=True,
                phantom_judge_request_arid=phantom_judge,
            )
            self.assertTrue(
                self._has_missing_validate_step_result(violations),
                msg=(
                    "a declared in-flight judge with no dangling edge must not mask "
                    f"the missing validate step_result; got: {violations}"
                ),
            )

    def test_in_flight_judge_edge_with_substep_parent_still_fails(self) -> None:
        """The in-flight exemption suppresses ONLY the missing-child record. The
        parent role is known from agent_runs.jsonl, so a malformed edge whose
        parent is a substep must still fail closed even when the (in-flight) judge
        child is exempted."""
        judge_arid = "substep_run_validate_judge_001"
        substep_parent = "substep_run_compile_generate_001"  # a recorded substep
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._violations_with_removed_child(
                Path(tmp),
                removed_arid=judge_arid,
                in_flight_arids=[judge_arid],
                reparent_removed_child_to=substep_parent,
            )
            # The missing-child record itself is still tolerated...
            self.assertFalse(
                self._has_dangling_edge(violations, judge_arid),
                msg=f"in-flight judge missing-child record should be tolerated; got: {violations}",
            )
            # ...but the substep-parent hierarchy violation must surface.
            self.assertTrue(
                any("substep must not be parent role" in v for v in violations),
                msg=f"substep-parent edge must still fail closed; got: {violations}",
            )

    def test_pre_judge_exempts_superseded_invalid_unauthorized_write_edge(self) -> None:
        """A reopen-consumed unauthorized-write trigger lives only in
        agent_runs_invalid.jsonl (no agent_runs.jsonl row), is listed in
        reopen/superseded_runs.json, and its agent_graph edge is deliberately KEPT.
        The pre_judge edge scan must tolerate that kept edge (mirroring
        _validate_orchestration_completion_for_pass) so a clean reopened run can pass."""
        execute_arid = "substep_run_validate_execute_001"
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._violations_with_removed_child(
                Path(tmp),
                removed_arid=execute_arid,
                divert_removed_child_to_invalid=True,
                superseded_arids=[execute_arid],
            )
            self.assertFalse(
                self._has_dangling_edge(violations, execute_arid),
                msg=(
                    "a superseded child diverted to agent_runs_invalid.jsonl must not "
                    f"trip the dangling-edge check; got: {violations}"
                ),
            )

    def test_pre_judge_still_fails_unconsumed_invalid_edge(self) -> None:
        """Safety: an invalid-log diversion that has NOT been consumed by reopen
        (absent from superseded_runs.json) must still fail closed — the kept edge
        exists precisely to surface an un-consumed invalid terminal attempt."""
        execute_arid = "substep_run_validate_execute_001"
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._violations_with_removed_child(
                Path(tmp),
                removed_arid=execute_arid,
                divert_removed_child_to_invalid=True,
                superseded_arids=None,  # not reopen-consumed
            )
            self.assertTrue(
                self._has_dangling_edge(violations, execute_arid),
                msg=(
                    "an un-consumed invalid-log child (no superseded_runs entry) must "
                    f"still fail the dangling-edge check; got: {violations}"
                ),
            )

    def test_superseded_invalid_edge_with_substep_parent_still_fails(self) -> None:
        """The superseded-invalid exemption suppresses ONLY the missing-child
        record. The parent role is known from agent_runs.jsonl, so a malformed edge
        whose parent is a substep must still fail closed even when the child is a
        reopen-consumed invalid-log run (mirrors the in-flight exemption's behavior)."""
        execute_arid = "substep_run_validate_execute_001"
        substep_parent = "substep_run_compile_generate_001"  # a recorded substep
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._violations_with_removed_child(
                Path(tmp),
                removed_arid=execute_arid,
                divert_removed_child_to_invalid=True,
                superseded_arids=[execute_arid],
                reparent_removed_child_to=substep_parent,
            )
            # The missing-child record itself is still tolerated...
            self.assertFalse(
                self._has_dangling_edge(violations, execute_arid),
                msg=f"superseded-invalid missing-child record should be tolerated; got: {violations}",
            )
            # ...but the substep-parent hierarchy violation must surface.
            self.assertTrue(
                any("substep must not be parent role" in v for v in violations),
                msg=f"substep-parent edge must still fail closed; got: {violations}",
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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

    def test_validate_compile_stage_passes_for_resolved_plan_directory(self) -> None:
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
            violations = validate_compile_stage(
                repo_root,
                "workspace",
                "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
            )
            self.assertEqual(violations, [])

    def test_validate_compile_stage_rejects_non_plans_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            violations = validate_compile_stage(
                repo_root,
                "workspace",
                "workspace/pipelines/foo/bar",
            )
            self.assertTrue(
                any("ir_ref must be under" in v for v in violations), violations
            )

    def test_validate_compile_stage_rejects_missing_context_isolated(self) -> None:
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
                / "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/ir_meta.json"
            )
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            data.pop("context_isolated", None)
            meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            violations = validate_compile_stage(
                repo_root,
                "workspace",
                "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
            )
            self.assertTrue(
                any("ir_meta.json: missing required key 'context_isolated'" in v for v in violations),
                violations,
            )

    def test_validate_compile_stage_requires_constraint_reason_when_not_isolated(self) -> None:
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
                / "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/ir_meta.json"
            )
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            data["context_isolated"] = False
            meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            violations = validate_compile_stage(
                repo_root,
                "workspace",
                "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001",
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
                source_id="src_20260415_001",
            )
            self.assertEqual(violations, [])

    def test_validate_post_generate_stage_rejects_bad_top_level_pipeline_id(self) -> None:
        """post_generate must enforce the lineage top-level pipeline_id schema (the
        same check post_execute runs), so a malformed pipeline_id surfaces at Generate
        rather than far downstream at Validate (audit: orch_20260615T095217Z_74450292)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            dep_model_text = (
                "module dynamics_shallow_water_flux_2d_rusanov_p0_model\n"
                "implicit none\ncontains\n"
                "subroutine dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)\n"
                "  logical, intent(out) :: flag\n  flag = .true.\n"
                "end subroutine dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux\n"
                "end module dynamics_shallow_water_flux_2d_rusanov_p0_model\n"
            )
            model_text = (
                "module shallow_water2d_model\n"
                "use dynamics_shallow_water_flux_2d_rusanov_p0_model\n"
                "implicit none\ncontains\n"
                "subroutine solve(flag)\n  logical, intent(out) :: flag\n"
                "  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)\n"
                "end subroutine solve\nend module shallow_water2d_model\n"
            )
            runner_text = (
                "program shallow_water2d_runner\nimplicit none\n"
                "write(*,*) 'ok'\nend program shallow_water2d_runner\n"
            )
            makefile_text = (
                "FC ?= gfortran\nOBJS = dynamics_shallow_water_flux_2d_rusanov_p0_model.o "
                "shallow_water2d_model.o shallow_water2d_runner.o\n\n"
                "simulate: $(OBJS)\n\t$(FC) -o $@ $(OBJS)\n"
            )
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
            pipeline_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001"
            )
            # Corrupt the top-level pipeline_id so it no longer matches the directory.
            lineage_path = repo_root / pipeline_ref / "lineage.json"
            lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
            lineage["pipeline_id"] = "not-the-pipeline-id"
            lineage_path.write_text(json.dumps(lineage), encoding="utf-8")

            violations = validate_post_generate_stage(
                repo_root, "workspace", pipeline_ref, source_id="src_20260415_001"
            )
            self.assertTrue(
                any("pipeline_id" in v for v in violations),
                f"expected a pipeline_id violation, got: {violations}",
            )

    def test_validate_post_generate_stage_leaf_node_directory_dependency_ref(self) -> None:
        """Regression: a leaf node (direct_deps=[]) with dependency_ref pointing at the
        IR phase-root *directory* (per ORCHESTRATION.md:151) must resolve the dependency
        block from <ir>/spec.ir.yaml instead of crashing with IsADirectoryError."""
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
                dependency_resolved={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "direct_deps": [],
                    "transitive_deps": [],
                    "topo_level": 0,
                },
            )
            # dependency_ref written by the fixture is the IR phase-root directory.
            violations = validate_post_generate_stage(
                repo_root,
                "workspace",
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001",
                source_id="src_20260415_001",
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
            log_path = pipeline_dir / "source" / "src_20260415_001" / "src" / "mcp_command_log.jsonl"
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
                source_id="src_20260415_001",
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
            log_path = pipeline_dir / "source" / "src_20260415_001" / "src" / "mcp_command_log.jsonl"
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
                source_id="src_20260415_001",
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
                / "source"
                / "src_20260415_001"
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
            # Rewrite source_meta.json's lint_command_ref to point at the forged log.
            meta_path = (
                pipeline_dir / "source" / "src_20260415_001" / "source_meta.json"
            )
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            forged_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/source/src_20260415_001/src/"
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
                source_id="src_20260415_001",
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
            )
            # Overwrite the canonical log file with a record that has no
            # tool_name field — only command_id.
            log_path = node_dir / "mcp_command_log.jsonl"
            log_path.write_text(
                json.dumps(
                    {
                        "command_id": "fixture_run_program_001",
                        "command": ["./simulate", "spec.ir.yaml", "out"],
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

    def test_trial_meta_requires_source_source_id(self) -> None:
        """Strict policy: every execute trial_meta must declare
        `source_source_id`. Without it, validators cannot bind
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            trial_meta.pop("source_source_id", None)
            _write_json(trial_meta_path, trial_meta)
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(
                    "source_source_id is required" in v
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            trial_meta.pop("source_binary_id", None)
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            pipeline_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
            )
            node_dir = (
                pipeline_dir / "runs" / "run_test_001" / "problem__shallow_water2d__0.3.0"
            )
            # Plant a sibling build whose binary the run actually used.
            sibling_build = pipeline_dir / "binary" / "build_sibling_999" / "bin"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
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
                        "command": ["./simulate", "spec.ir.yaml", "out"],
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
        """source_source_id must point to a generation in pass state.

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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            pipeline_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
            )
            node_dir = (
                pipeline_dir / "runs" / "run_test_001" / "problem__shallow_water2d__0.3.0"
            )
            # Plant a stale generation in fail state with its own canonical log.
            stale_gen_id = "gen_stale_001"
            stale_dir = pipeline_dir / "source" / stale_gen_id
            stale_dir.mkdir(parents=True, exist_ok=True)
            _write_json(
                stale_dir / "source_meta.json",
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
                f"shallow-water2d_20260415_001/source/{stale_gen_id}/src/"
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
            trial_meta["source_source_id"] = stale_gen_id
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
        """Cross-phase canonical placement is bound strictly to source_source_id.

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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            pipeline_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
            )
            node_dir = (
                pipeline_dir / "runs" / "run_test_001" / "problem__shallow_water2d__0.3.0"
            )
            # Plant a sibling generation with its own canonical log.
            sibling_gen_id = "gen_sibling_001"
            sibling_dir = pipeline_dir / "source" / sibling_gen_id
            sibling_dir.mkdir(parents=True, exist_ok=True)
            (sibling_dir / "source_meta.json").write_text(
                '{"verification_status": "pass"}\n', encoding="utf-8"
            )
            sibling_src = sibling_dir / "src"
            sibling_src.mkdir(parents=True, exist_ok=True)
            sibling_log_ref = (
                f"workspace/pipelines/problem__shallow_water2d__0.3.0/"
                f"shallow-water2d_20260415_001/source/{sibling_gen_id}/src/"
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
            trial_meta["source_source_id"] = "src_20260415_001"
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
            )
            node_dir = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
            )
            # Plant a forged log under raw/ (a writable execute output directory).
            forged_log = node_dir / "raw" / "forged_run.jsonl"
            forged_log.parent.mkdir(parents=True, exist_ok=True)
            forged_log.write_text(
                json.dumps(
                    {
                        "command_id": "forged_cmd_001",
                        "tool_name": "run_program",
                        "command": ["./simulate", "spec.ir.yaml", "out"],
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
                    "shallow-water2d_20260415_001/runs/run_test_001/"
                    "problem__shallow_water2d__0.3.0/raw/forged_run.jsonl"
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

    def test_validate_source_meta_accepts_fail_without_lint_command_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_dir = Path(tmp) / "pipeline"
            gen_dir = pipeline_dir / "source" / "gen_fail_001"
            gen_dir.mkdir(parents=True)
            _write_json(
                gen_dir / "source_meta.json",
                {
                    "attempt_count": 1,
                    "verification_status": "fail",
                    "last_fail_reason": "lint failed",
                    "debug_mode": False,
                    "context_isolated": True,
                },
            )
            violations: list[str] = []
            _validate_source_meta_json_files(pipeline_dir, violations)
            self.assertEqual(violations, [])

    def test_validate_source_meta_rejects_pass_without_lint_command_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_dir = Path(tmp) / "pipeline"
            gen_dir = pipeline_dir / "source" / "gen_pass_001"
            gen_dir.mkdir(parents=True)
            _write_json(
                gen_dir / "source_meta.json",
                {
                    "attempt_count": 1,
                    "verification_status": "pass",
                    "last_fail_reason": None,
                    "debug_mode": False,
                    "context_isolated": True,
                },
            )
            violations: list[str] = []
            _validate_source_meta_json_files(pipeline_dir, violations)
            self.assertTrue(
                any("missing lint_command_ref" in v for v in violations),
                violations,
            )

    def test_validate_source_meta_rejects_empty_run_linter_when_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_dir = Path(tmp) / "pipeline"
            gen_dir = pipeline_dir / "source" / "gen_pass_002"
            gen_dir.mkdir(parents=True)
            _write_json(
                gen_dir / "source_meta.json",
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
            _validate_source_meta_json_files(pipeline_dir, violations)
            self.assertTrue(
                any("lint_command_ref.run_linter must be non-empty" in v for v in violations),
                violations,
            )

    def test_validate_source_meta_ignores_lint_shape_when_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_dir = Path(tmp) / "pipeline"
            gen_dir = pipeline_dir / "source" / "gen_fail_002"
            gen_dir.mkdir(parents=True)
            _write_json(
                gen_dir / "source_meta.json",
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
            _validate_source_meta_json_files(pipeline_dir, violations)
            self.assertEqual(violations, [])

    def test_validate_generate_lint_rejects_pass_without_lint_command_ref(self) -> None:
        violations: list[str] = []
        meta_path = Path("/tmp/source_meta.json")
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
        meta_path = Path("/tmp/source_meta.json")
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
        meta_path = Path("/tmp/source_meta.json")
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
                source_id="src_20260415_001",
            )
            self.assertEqual(violations, [])

    def test_create_minimal_execution_tree_writes_metrics_basis_to_raw(self) -> None:
        """The metrics_basis argument is reflected in raw/metrics_basis.json (premise of the trivial-verification test)."""
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                metrics_basis=payload,
            )
            metrics_path = (
                repo_root
                / "workspace"
                / "pipelines"
                / "problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
                / "runs"
                / "run_test_001"
                / "problem__shallow_water2d__0.3.0"
                / "raw"
                / "metrics_basis.json"
            )
            self.assertTrue(metrics_path.is_file())
            self.assertEqual(json.loads(metrics_path.read_text(encoding="utf-8")), payload)


    def test_validate_rejects_all_zero_metrics_basis(self) -> None:
        """A violation occurs when all numbers in metrics_basis.json are 0.0."""
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                metrics_basis={"value_a": 0.0, "value_b": 0.0},
            )
            violations = validate(repo_root, workspace_root="workspace")
            self.assertTrue(
                any("trivial placeholder" in v for v in violations),
                f"Expected trivial placeholder violation, got: {violations}",
            )

    def test_validate_rejects_all_null_metrics_basis(self) -> None:
        """A violation occurs when all fields of metrics_basis.json are null."""
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                metrics_basis={"value_a": None, "value_b": None},
            )
            violations = validate(repo_root, workspace_root="workspace")
            self.assertTrue(
                any("trivial placeholder" in v for v in violations),
                f"Expected trivial placeholder violation, got: {violations}",
            )

    def test_validate_accepts_partially_nonzero_metrics_basis(self) -> None:
        """It passes if some part of metrics_basis.json has a non-zero real value."""
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                metrics_basis={"value_a": 0.0, "value_b": 1.5},
            )
            violations = validate(repo_root, workspace_root="workspace")
            self.assertFalse(
                any("trivial placeholder" in v for v in violations),
                f"Expected no trivial placeholder violation, got: {violations}",
            )

    def test_validate_skips_metrics_basis_check_if_no_numeric_fields(self) -> None:
        """If metrics_basis.json has no numeric field at all, skip the trivial check."""
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
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
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
            schema_dir = broken_root / "spec" / "schema" / "ir"
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
                self.assertIn(str(broken_root / "spec" / "schema" / "ir" / "shape_expr.schema.json"), msg)
            finally:
                _active_repo_root_for_schema.reset(token)
                _load_shape_expr_patterns_cached.cache_clear()
        # Bundled schema continues to work after the cache reset.
        ok, _, _ = _parse_shape_expr("[3]")
        self.assertTrue(ok)
        # Sanity: the bundled path resolution is still valid.
        self.assertTrue(_BUNDLED_SHAPE_EXPR_SCHEMA_PATH.is_file())

    def test_shape_expr_schema_resolves_from_active_repo_root(self) -> None:
        """Regression: the active repo_root's spec/schema/ir/shape_expr.schema.json
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
            schema_dir = strict_root / "spec" / "schema" / "ir"
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
                    str(no_schema_root / "spec" / "schema" / "ir" / "shape_expr.schema.json"),
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

    def test_validate_compile_stage_fails_closed_when_target_repo_lacks_schema(self) -> None:
        """Regression: public validate_*() entrypoints must bind the active
        repo_root context themselves so a target repo without
        spec/schema/ir/shape_expr.schema.json fails closed. Previously the
        context was set only by CLI main(), so library callers silently fell
        back to the validator-bundled schema, defeating the fail-closed
        protection against version skew between target and validator-bundle."""
        from tools.validate_pipeline_semantics import (
            _active_repo_root_for_schema,
            _load_shape_expr_patterns_cached,
            validate_compile_stage,
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # Deliberately do NOT seed the schema (this test exercises the
            # missing-schema path). Build minimal plan artifacts so the
            # validator reaches shape_expr parsing.
            plan_dir = repo_root / "workspace" / "ir" / "x" / "p1"
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
            (plan_dir / "spec.ir.yaml").write_text(
                yaml.safe_dump(algo), encoding="utf-8"
            )
            _load_shape_expr_patterns_cached.cache_clear()
            # Pre-condition: no leaked context from prior tests.
            self.assertIsNone(_active_repo_root_for_schema.get())
            # Direct library-style call must fail closed.
            with self.assertRaises(RuntimeError) as ctx:
                validate_compile_stage(repo_root, "workspace", "workspace/ir/x/p1")
            msg = str(ctx.exception)
            self.assertIn("shape_expr schema not found", msg)
            self.assertIn(
                str(repo_root / "spec" / "schema" / "ir" / "shape_expr.schema.json"),
                msg,
            )
            # Post-condition: validate_compile_stage MUST reset the context so
            # subsequent in-process calls don't see the failed repo's root.
            self.assertIsNone(
                _active_repo_root_for_schema.get(),
                "validate_compile_stage must reset the active context after returning/raising",
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
            sd = repo / "spec" / "schema" / "ir"
            sd.mkdir(parents=True)
            (sd / "shape_expr.schema.json").write_bytes(
                _BUNDLED_SHAPE_EXPR_SCHEMA_PATH.read_bytes()
            )
            plan_dir = repo / "workspace" / "ir" / "x" / "p1"
            plan_dir.mkdir(parents=True)
            (plan_dir / "spec.ir.yaml").write_text(
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
                            "--stage", "compile",
                            "--ir-ref", "workspace/ir/x/p1",
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
            schema_dir = broken_root / "spec" / "schema" / "ir"
            schema_dir.mkdir(parents=True)
            (schema_dir / "shape_expr.schema.json").write_text(
                "{ broken json", encoding="utf-8"
            )
            # Need a minimal ir_ref so plan-stage actually exercises shape_expr.
            ir_ref = broken_root / "workspace" / "ir" / "x" / "p1"
            ir_ref.mkdir(parents=True)
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
            (ir_ref / "spec.ir.yaml").write_text(
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
                        "--stage", "compile",
                        "--ir-ref", "workspace/ir/x/p1",
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
        spec/schema/ir/shape_expr.schema.json). Missing shape_expr must fail
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
            contract_path = repo_root / "spec.ir.yaml"
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
            contract_path = repo_root / "spec.ir.yaml"
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


class ParseMakefileRulesTest(unittest.TestCase):
    def test_recursive_assignment_expands_lazily(self) -> None:
        rules = _parse_makefile_rules(
            "OBJDIR ?= .\n"
            "MODEL_OBJ = $(OBJDIR)/foo_model.o\n"
            "$(OBJDIR)/foo_runner.o: foo_runner.f90 $(MODEL_OBJ)\n"
        )
        self.assertIn("foo_model.o", rules.get("foo_runner.o", set()))

    def test_conditional_assignment_does_not_overwrite_defined_variable(self) -> None:
        # `?=` is a no-op when the variable is already defined. GNU make keeps
        # the (wrong) earlier value, so the model.o dependency is genuinely
        # absent and must NOT be hidden by treating ?= as an overwrite.
        rules = _parse_makefile_rules(
            "OBJDIR ?= .\n"
            "MODEL_OBJ = wrong.o\n"
            "MODEL_OBJ ?= $(OBJDIR)/foo_model.o\n"
            "foo_runner.o: foo_runner.f90 $(MODEL_OBJ)\n"
        )
        prereqs = rules.get("foo_runner.o", set())
        self.assertIn("wrong.o", prereqs)
        self.assertNotIn("foo_model.o", prereqs)

    def test_conditional_assignment_sets_when_undefined(self) -> None:
        rules = _parse_makefile_rules(
            "OBJDIR ?= .\n"
            "MODEL_OBJ ?= $(OBJDIR)/foo_model.o\n"
            "foo_runner.o: foo_runner.f90 $(MODEL_OBJ)\n"
        )
        self.assertIn("foo_model.o", rules.get("foo_runner.o", set()))

    def test_simply_expanded_assignment_snapshots_at_definition(self) -> None:
        # `:=` expands its RHS immediately, so a later redefinition of the
        # referenced variable must not change the prerequisite. make resolves
        # PREQ to wrong.o here, so the foo_model.o dependency is absent.
        rules = _parse_makefile_rules(
            "DEP := wrong.o\n"
            "PREQ := $(DEP)\n"
            "DEP := foo_model.o\n"
            "foo_runner.o: foo_runner.f90 $(PREQ)\n"
        )
        prereqs = rules.get("foo_runner.o", set())
        self.assertIn("wrong.o", prereqs)
        self.assertNotIn("foo_model.o", prereqs)

    def test_forward_reference_variable_is_not_resolved(self) -> None:
        rules = _parse_makefile_rules(
            "foo_runner.o: foo_runner.f90 $(MODEL_OBJ)\n"
            "MODEL_OBJ = foo_model.o\n"
        )
        self.assertNotIn("foo_model.o", rules.get("foo_runner.o", set()))

    def test_simply_expanded_assignment_drops_forward_reference(self) -> None:
        # `:=` expands immediately, so a forward reference in its RHS becomes
        # empty *now*; a later definition of that variable must not retroactively
        # resolve it (make has no foo_model.o prerequisite -> still a violation).
        rules = _parse_makefile_rules(
            "MODEL_OBJ := $(DEP_OBJ)\n"
            "DEP_OBJ = foo_model.o\n"
            "foo_runner.o: foo_runner.f90 $(MODEL_OBJ)\n"
        )
        self.assertNotIn("foo_model.o", rules.get("foo_runner.o", set()))

    def test_append_to_simply_expanded_variable_expands_immediately(self) -> None:
        # `+=` onto a `:=` variable expands the appended text immediately, so a
        # forward reference becomes empty now and is not resolved by a later
        # definition (make sees no foo_model.o prerequisite).
        rules = _parse_makefile_rules(
            "OBJS := other.o\n"
            "OBJS += $(MODEL_OBJ)\n"
            "MODEL_OBJ = foo_model.o\n"
            "foo_runner.o: foo_runner.f90 $(OBJS)\n"
        )
        prereqs = rules.get("foo_runner.o", set())
        self.assertIn("other.o", prereqs)
        self.assertNotIn("foo_model.o", prereqs)

    def test_append_to_recursive_variable_expands_lazily(self) -> None:
        # `+=` onto a `=` variable keeps the appended text raw, so it resolves
        # at rule-expansion time against the then-current value (make does see
        # the foo_model.o prerequisite here).
        rules = _parse_makefile_rules(
            "OBJS = other.o\n"
            "OBJS += $(MODEL_OBJ)\n"
            "MODEL_OBJ = foo_model.o\n"
            "foo_runner.o: foo_runner.f90 $(OBJS)\n"
        )
        prereqs = rules.get("foo_runner.o", set())
        self.assertIn("other.o", prereqs)
        self.assertIn("foo_model.o", prereqs)


class FortranMakefileObjdirPrefixTest(unittest.TestCase):
    """Out-of-source correctness: a used-module prerequisite must carry the same
    `$(OBJDIR)/` prefix as its producing object rule. A bare basename passes the
    basename-normalizing `_parse_makefile_rules` view but breaks `make -j` once
    OBJDIR is overridden — the escaped defect that passed Generate.verify but
    failed Build for orch_20260608T012651Z_e906113b (src_002).
    """

    _MODEL = (
        "module swm_model\n"
        "implicit none\n"
        "contains\n"
        "subroutine solve(flag)\n"
        "  logical, intent(out) :: flag\n"
        "  flag = .true.\n"
        "end subroutine solve\n"
        "end module swm_model\n"
    )
    _RUNNER = (
        "program swm_runner\n"
        "use swm_model\n"
        "implicit none\n"
        "logical :: flag\n"
        "call solve(flag)\n"
        "write(*,*) flag\n"
        "end program swm_runner\n"
    )

    def _run(self, makefile_text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp)
            (src_dir / "swm_model.f90").write_text(self._MODEL, encoding="utf-8")
            (src_dir / "swm_runner.f90").write_text(self._RUNNER, encoding="utf-8")
            (src_dir / "Makefile").write_text(makefile_text, encoding="utf-8")
            violations: list[str] = []
            _validate_fortran_makefile_src_dir(src_dir, violations)
            return violations

    def _run_files(
        self, sources: dict[str, str], makefile_text: str
    ) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp)
            for name, text in sources.items():
                (src_dir / name).write_text(text, encoding="utf-8")
            (src_dir / "Makefile").write_text(makefile_text, encoding="utf-8")
            violations: list[str] = []
            _validate_fortran_makefile_src_dir(src_dir, violations)
            return violations

    _MAIN = "program app\nimplicit none\nwrite(*,*) 1\nend program app\n"
    _HELPER = "subroutine helper()\nimplicit none\nend subroutine helper\n"

    def test_bare_link_prereq_single_source_no_use_dep_is_flagged(self) -> None:
        # No local `use` dependency at all, but the link rule consumes a bare
        # object name while the object rule produces $(OBJDIR)/main.o. The
        # prefix check must run even though required_object_deps is empty.
        makefile = (
            "FC ?= gfortran\n"
            "OBJDIR ?= .\n"
            "BINDIR ?= .\n"
            "BIN := app\n"
            "$(BINDIR)/$(BIN): main.o | $(BINDIR)\n"
            "\t$(FC) -o $@ main.o\n"
            "$(OBJDIR)/main.o: main.f90 | $(OBJDIR)\n"
            "\t$(FC) -c $< -o $@\n"
        )
        violations = self._run_files({"main.f90": self._MAIN}, makefile)
        self.assertTrue(
            any("must carry the same $(OBJDIR)/ prefix" in v for v in violations),
            f"bare link prereq with no use-dep must be flagged; got: {violations}",
        )

    def test_bare_link_prereq_two_independent_sources_is_flagged(self) -> None:
        makefile = (
            "FC ?= gfortran\n"
            "OBJDIR ?= .\n"
            "BINDIR ?= .\n"
            "BIN := app\n"
            "$(BINDIR)/$(BIN): main.o helper.o | $(BINDIR)\n"
            "\t$(FC) -o $@ main.o helper.o\n"
            "$(OBJDIR)/main.o: main.f90 | $(OBJDIR)\n"
            "\t$(FC) -c $< -o $@\n"
            "$(OBJDIR)/helper.o: helper.f90 | $(OBJDIR)\n"
            "\t$(FC) -c $< -o $@\n"
        )
        violations = self._run_files(
            {"main.f90": self._MAIN, "helper.f90": self._HELPER}, makefile
        )
        self.assertTrue(
            any("must carry the same $(OBJDIR)/ prefix" in v for v in violations),
            f"bare link prereqs (independent sources) must be flagged; got: {violations}",
        )

    def test_prefixed_link_prereq_single_source_is_accepted(self) -> None:
        makefile = (
            "FC ?= gfortran\n"
            "OBJDIR ?= .\n"
            "BINDIR ?= .\n"
            "BIN := app\n"
            "MAIN_OBJ := $(OBJDIR)/main.o\n"
            "$(BINDIR)/$(BIN): $(MAIN_OBJ) | $(BINDIR)\n"
            "\t$(FC) -o $@ $(MAIN_OBJ)\n"
            "$(MAIN_OBJ): main.f90 | $(OBJDIR)\n"
            "\t$(FC) -c $< -o $@\n"
        )
        violations = self._run_files({"main.f90": self._MAIN}, makefile)
        self.assertEqual(
            [], violations, f"prefixed link prereq must be accepted; got: {violations}"
        )

    def test_bare_prereq_against_objdir_producing_rule_is_flagged(self) -> None:
        # src_002-like: model object/.mod produced under $(OBJDIR), but the
        # runner rule lists them as bare basenames -> no rule under override.
        makefile = (
            "FC ?= gfortran\n"
            "OBJDIR ?= .\n"
            "$(OBJDIR)/swm_model.o: swm_model.f90 | $(OBJDIR)\n"
            "\t$(FC) -J$(OBJDIR) -c $< -o $@\n"
            "$(OBJDIR)/swm_runner.o: swm_runner.f90 swm_model.o swm_model.mod | $(OBJDIR)\n"
            "\t$(FC) -I$(OBJDIR) -c $< -o $@\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("must carry the same $(OBJDIR)/ prefix" in v for v in violations),
            f"bare prereq vs $(OBJDIR)/ producing rule must be flagged; got: {violations}",
        )

    def test_objdir_prefixed_variable_prereq_is_accepted(self) -> None:
        # src_003-like: $(MODEL_OBJ)/$(MODEL_MOD) defined before the rule and
        # resolving to $(OBJDIR)/-prefixed paths. Valid out-of-source make.
        makefile = (
            "FC ?= gfortran\n"
            "OBJDIR ?= .\n"
            "MODEL_OBJ := $(OBJDIR)/swm_model.o\n"
            "MODEL_MOD := $(OBJDIR)/swm_model.mod\n"
            "RUNNER_OBJ := $(OBJDIR)/swm_runner.o\n"
            "$(MODEL_OBJ): swm_model.f90 | $(OBJDIR)\n"
            "\t$(FC) -J$(OBJDIR) -c $< -o $@\n"
            "$(MODEL_MOD): $(MODEL_OBJ)\n"
            "\t@true\n"
            "$(RUNNER_OBJ): swm_runner.f90 $(MODEL_OBJ) $(MODEL_MOD) | $(OBJDIR)\n"
            "\t$(FC) -I$(OBJDIR) -c $< -o $@\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"OBJDIR-prefixed variable prereqs must be accepted; got: {violations}"
        )

    def test_bare_object_prereq_on_link_rule_is_flagged(self) -> None:
        # The used-module object rule is correctly $(OBJDIR)/-prefixed, but the
        # link/default rule consumes the objects as bare basenames. Under an
        # OBJDIR override `make -j` aborts with "No rule to make target". The
        # prefix check must inspect the link rule, not just object rules of
        # use-dependent sources.
        makefile = (
            "FC ?= gfortran\n"
            "OBJDIR ?= .\n"
            "BINDIR ?= .\n"
            "BIN := app\n"
            "MODEL_OBJ := $(OBJDIR)/swm_model.o\n"
            "MODEL_MOD := $(OBJDIR)/swm_model.mod\n"
            "RUNNER_OBJ := $(OBJDIR)/swm_runner.o\n"
            "$(BINDIR)/$(BIN): swm_runner.o swm_model.o | $(BINDIR)\n"
            "\t$(FC) -o $@ swm_runner.o swm_model.o\n"
            "$(MODEL_OBJ): swm_model.f90 | $(OBJDIR)\n"
            "\t$(FC) -J$(OBJDIR) -c $< -o $@\n"
            "$(MODEL_MOD): $(MODEL_OBJ)\n"
            "\t@true\n"
            "$(RUNNER_OBJ): swm_runner.f90 $(MODEL_OBJ) $(MODEL_MOD) | $(OBJDIR)\n"
            "\t$(FC) -I$(OBJDIR) -c $< -o $@\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("must carry the same $(OBJDIR)/ prefix" in v for v in violations),
            f"bare object prereq on the link rule must be flagged; got: {violations}",
        )

    def test_in_source_bare_makefile_is_accepted(self) -> None:
        # Fully in-source (no $(OBJDIR) in targets): bare prerequisites are
        # legitimate and must not be flagged by the directory-aware check.
        makefile = (
            "FC ?= gfortran\n"
            "swm_model.o: swm_model.f90\n"
            "\t$(FC) -c $<\n"
            "swm_runner.o: swm_runner.f90 swm_model.o swm_model.mod\n"
            "\t$(FC) -c $<\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"in-source bare Makefile must stay valid; got: {violations}"
        )


class MakefileTestNoRelinkTest(unittest.TestCase):
    """post_generate gate: the `test`/`check` target must use a non-relinking
    fail-closed guard and must not recurse into make. A relinking guard in
    Validate.execute writes into the read-only-bound binary/ and escalates a
    binary-name/availability mismatch into an unauthorized_write_violation ->
    fail_closed (orch_20260619T113225Z_f48fe14b)."""

    def _run(
        self,
        makefile_text: str,
        build_system: str = "make",
        language: str = "fortran",
    ) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp)
            (src_dir / "Makefile").write_text(makefile_text, encoding="utf-8")
            violations: list[str] = []
            _validate_makefile_test_no_relink(
                src_dir, violations, build_system=build_system, language=language
            )
            return violations

    def test_non_make_toolchain_is_skipped(self) -> None:
        # The non-relinking contract only applies to make-based quality checks; a
        # relinking Makefile under another toolchain must not be flagged here.
        makefile = (
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) || $(MAKE) $(BINDIR)/$(BIN)\n"
        )
        self.assertEqual([], self._run(makefile, build_system="cmake", language="cpp"))
        self.assertEqual(
            [], self._run(makefile, build_system="make", language="python")
        )

    def test_relinking_test_target_make_var_is_flagged(self) -> None:
        makefile = (
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) || $(MAKE) $(BINDIR)/$(BIN)\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("relinks the binary" in v for v in violations),
            f"relinking test target ($(MAKE)) must be flagged; got: {violations}",
        )

    def test_relinking_test_target_bare_make_is_flagged(self) -> None:
        makefile = (
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) || make $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("relinks the binary" in v for v in violations),
            f"relinking test target (bare make) must be flagged; got: {violations}",
        )

    def test_relinking_check_target_is_flagged(self) -> None:
        makefile = (
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "check:\n"
            "\ttest -x $(BINDIR)/$(BIN) || ${MAKE} $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("relinks the binary" in v and "check" in v for v in violations),
            f"relinking check target must be flagged; got: {violations}",
        )

    def test_non_relinking_failclosed_guard_is_accepted(self) -> None:
        # The exact guard text the four canonical docs recommend — its echo
        # message contains the word `make` ("run 'make all' first"), which must
        # NOT be mistaken for a recursive make invocation (quoted spans are
        # stripped before the relink scan).
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) || { echo \"error: $(BINDIR)/$(BIN) not built; run 'make all' first\" >&2; exit 1; }\n"
            "\tmkdir -p $(RUNDIR)/raw/state_snapshots\n"
            "\tcd $(RUNDIR) && $(abspath $(BINDIR)/$(BIN))\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"non-relinking fail-closed guard must be accepted; got: {violations}"
        )

    def test_make_word_in_double_and_single_quoted_message_is_not_flagged(self) -> None:
        makefile = (
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) || echo 'please run make all' >&2\n"
            "\ttest -x $(BINDIR)/$(BIN) || { echo \"need make\" >&2; exit 1; }\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"make inside quoted messages must not be flagged; got: {violations}"
        )

    def test_comment_mentioning_make_in_recipe_is_not_flagged(self) -> None:
        # A `make` token inside a recipe comment must not trip the check — even
        # when the comment text contains a shell separator before the tool word
        # (a trailing comment is not executed by the shell).
        for comment in (
            "# do not relink with make here",
            "# build first; make all",
            "# see docs | make help",
            "# rebuild (gfortran -o ...)",
        ):
            makefile = (
                "BINDIR ?= .\n"
                "RUNDIR ?= .\n"
                "BIN := app_runner\n"
                "test:\n"
                f"\t{comment}\n"
                "\ttest -x $(BINDIR)/$(BIN) || { echo no >&2; exit 1; }\n"
                f"\tcd $(RUNDIR) && $(abspath $(BINDIR)/$(BIN))  {comment}\n"
            )
            violations = self._run(makefile)
            self.assertEqual(
                [], violations, f"recipe comment {comment!r} must not be flagged; got: {violations}"
            )

    def test_attached_hash_is_not_treated_as_comment(self) -> None:
        # `#` not at a word boundary (`a#b`) is literal, so a later command on the
        # same line is still scanned.
        makefile = (
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\techo a#b; $(MAKE) $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("relinks the binary" in v for v in violations),
            f"command after an attached-# token must still be scanned; got: {violations}",
        )

    def test_binary_prerequisite_relink_is_flagged(self) -> None:
        # `test: $(BINDIR)/$(BIN)` makes `make test` (re)build the binary even
        # though the recipe itself does not call make -> relinks in Validate.execute.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN := app_runner\n"
            "test: $(BINDIR)/$(BIN)\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("build prerequisite" in v for v in violations),
            f"binary build prerequisite must be flagged; got: {violations}",
        )

    def test_all_prerequisite_relink_is_flagged(self) -> None:
        # `check: all` where `all` is a build rule target -> relink risk.
        makefile = (
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "all: $(BINDIR)/$(BIN)\n"
            "\t@true\n"
            "check: all\n"
            "\t$(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("build prerequisite" in v and "check" in v for v in violations),
            f"`check: all` build prerequisite must be flagged; got: {violations}",
        )

    def test_check_aliasing_test_is_accepted(self) -> None:
        # Canonical: `check: test` aliases the phony test entrypoint (not a build
        # target). It must not be read as a relink-triggering prerequisite.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) || { echo \"error: run 'make all' first\" >&2; exit 1; }\n"
            "\tcd $(RUNDIR) && $(abspath $(BINDIR)/$(BIN))\n"
            "check: test\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"`check: test` alias must be accepted; got: {violations}"
        )

    def test_order_only_dir_prerequisite_is_accepted(self) -> None:
        # An order-only directory prerequisite (mkdir, not a relink) is fine.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN := app_runner\n"
            "$(RUNDIR):\n"
            "\tmkdir -p $(RUNDIR)\n"
            "test: | $(RUNDIR)\n"
            "\ttest -x $(BINDIR)/$(BIN) || { echo no >&2; exit 1; }\n"
            "\tcd $(RUNDIR) && $(abspath $(BINDIR)/$(BIN))\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"order-only dir prerequisite must be accepted; got: {violations}"
        )

    def test_inline_recipe_make_is_flagged(self) -> None:
        # A single-line rule `test: ; $(MAKE) ...` puts the relink in the inline
        # recipe after `;`, not on a tab-indented line.
        makefile = (
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "test: ; $(MAKE) $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("recipe relinks the binary" in v for v in violations),
            f"inline-recipe relink must be flagged; got: {violations}",
        )

    def test_variable_indirect_binary_prerequisite_is_flagged(self) -> None:
        # The binary prerequisite is stored behind a variable; expansion must
        # resolve it to `$(BINDIR)/$(BIN)` so the relink is still detected.
        makefile = (
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "TEST_DEPS := $(BINDIR)/$(BIN)\n"
            "test: $(TEST_DEPS)\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("build prerequisite" in v for v in violations),
            f"variable-indirect binary prerequisite must be flagged; got: {violations}",
        )

    def test_order_only_binary_prerequisite_is_flagged(self) -> None:
        # GNU make still builds an order-only prerequisite if missing/out of
        # date, so an order-only binary prerequisite still relinks.
        makefile = (
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "test: | $(BINDIR)/$(BIN)\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("build prerequisite" in v for v in violations),
            f"order-only binary prerequisite must be flagged; got: {violations}",
        )

    def test_order_only_build_helper_is_flagged(self) -> None:
        # An order-only helper whose recipe links the binary must be flagged
        # (only pure directory/no-op helpers are safe).
        makefile = (
            "FC ?= gfortran\n"
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "build-bin:\n"
            "\t$(FC) -o $(BINDIR)/$(BIN) main.o\n"
            "test: | build-bin\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("build prerequisite" in v for v in violations),
            f"order-only build helper must be flagged; got: {violations}",
        )

    def test_compiler_relink_in_recipe_is_flagged(self) -> None:
        # A recipe that rebuilds the binary with the compiler (no make) still
        # writes into the read-only binary/ in Validate.execute.
        makefile = (
            "FC ?= gfortran\n"
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) || $(FC) -o $(BINDIR)/$(BIN) main.o\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("relinks the binary" in v for v in violations),
            f"compiler relink in recipe must be flagged; got: {violations}",
        )

    def test_variable_named_binary_target_dependency_is_flagged(self) -> None:
        # The binary rule is variable-named (`$(BIN):`) and `test` depends on the
        # binary by its resolved name (no literal `$(BIN)`). Resolving variable
        # targets is required to catch this.
        makefile = (
            "FC ?= gfortran\n"
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "RUNNER := app_runner\n"
            "$(BIN): main.o\n"
            "\t$(FC) -o $(BIN) main.o\n"
            "test: $(RUNNER)\n"
            "\t./run\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("build prerequisite" in v for v in violations),
            f"variable-named binary target dependency must be flagged; got: {violations}",
        )

    def test_quoted_command_word_relink_is_flagged(self) -> None:
        # The relink command word is quoted; the shell still executes it, so it
        # must be flagged (command-position detection unquotes the command word).
        for cmd in ('"$(MAKE)" $(BINDIR)/$(BIN)', "'make' $(BINDIR)/$(BIN)"):
            makefile = (
                "BINDIR ?= .\n"
                "BIN := app_runner\n"
                "test:\n"
                f"\ttest -x $(BINDIR)/$(BIN) || {cmd}\n"
            )
            violations = self._run(makefile)
            self.assertTrue(
                any("relinks the binary" in v for v in violations),
                f"quoted command-word relink ({cmd!r}) must be flagged; got: {violations}",
            )

    def test_separator_then_tool_inside_message_is_accepted(self) -> None:
        # A quoted diagnostic message containing a shell separator before a tool
        # name (e.g. "missing; make all") is an argument, not an executed
        # command, and must not be flagged.
        for msg in ("missing; make all", "do && make it", "use | make", "x; $(MAKE)"):
            makefile = (
                "BINDIR ?= .\n"
                "BIN := app_runner\n"
                "test:\n"
                f'\ttest -x $(BINDIR)/$(BIN) || {{ echo "{msg}" >&2; exit 1; }}\n'
            )
            self.assertEqual(
                [], self._run(makefile), f"message {msg!r} must not be flagged"
            )

    def test_make_force_prefix_relink_is_flagged(self) -> None:
        # GNU make's `+` recipe prefix forces command execution; a `+$(MAKE)` /
        # `+make` recipe still relinks and must be flagged.
        for recipe in ("+$(MAKE) $(BINDIR)/$(BIN)", "+make $(BINDIR)/$(BIN)"):
            makefile = (
                "BINDIR ?= .\n"
                "BIN := app_runner\n"
                "test:\n"
                f"\t{recipe}\n"
            )
            violations = self._run(makefile)
            self.assertTrue(
                any("relinks the binary" in v for v in violations),
                f"`+` prefixed relink ({recipe!r}) must be flagged; got: {violations}",
            )

    def test_shell_conditional_relink_is_flagged(self) -> None:
        # A single-line shell conditional whose body relinks must be flagged; the
        # relink command follows shell control keywords (`then`/`!`).
        makefile = (
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\tif ! test -x $(BINDIR)/$(BIN); then $(MAKE) $(BINDIR)/$(BIN); fi\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("relinks the binary" in v for v in violations),
            f"shell-conditional relink must be flagged; got: {violations}",
        )

    def test_compiler_wrapper_relink_is_flagged(self) -> None:
        # A relink behind a command wrapper (ccache/distcc/env …) must be flagged.
        for cmd in (
            "ccache gfortran -o $(BINDIR)/$(BIN) main.o",
            "distcc $(FC) -o $(BINDIR)/$(BIN) main.o",
            "env FC=gfortran make $(BINDIR)/$(BIN)",
        ):
            makefile = (
                "FC ?= gfortran\n"
                "BINDIR ?= .\n"
                "BIN := app_runner\n"
                "test:\n"
                f"\ttest -x $(BINDIR)/$(BIN) || {cmd}\n"
            )
            violations = self._run(makefile)
            self.assertTrue(
                any("relinks the binary" in v for v in violations),
                f"wrapped relink ({cmd!r}) must be flagged; got: {violations}",
            )

    def test_build_driver_utility_mode_is_accepted(self) -> None:
        # Build-driver names are NOT treated as relinks: in a make Makefile they
        # mostly appear in non-building utility modes, so flagging the bare command
        # word would be a false positive (e.g. cmake's portable mkdir).
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\tcmake -E make_directory $(RUNDIR)\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"build-driver utility mode must be accepted; got: {violations}"
        )

    def test_gmake_and_escaped_make_relink_is_flagged(self) -> None:
        # `gmake` (GNU make on BSD/macOS) and a recipe-escaped `$${MAKE}` both
        # relink and must be flagged.
        for cmd in ("gmake $(BINDIR)/$(BIN)", "$${MAKE} $(BINDIR)/$(BIN)"):
            makefile = (
                "BINDIR ?= .\n"
                "BIN := app_runner\n"
                "test:\n"
                f"\ttest -x $(BINDIR)/$(BIN) || {cmd}\n"
            )
            violations = self._run(makefile)
            self.assertTrue(
                any("relinks the binary" in v for v in violations),
                f"gmake/escaped-make relink ({cmd!r}) must be flagged; got: {violations}",
            )

    def test_combined_flag_sh_c_relink_is_flagged(self) -> None:
        # `bash -lc 'make all'` (combined short flags) must still be descended into.
        makefile = (
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\tbash -lc 'make all'\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("relinks the binary" in v for v in violations),
            f"combined-flag sh -c relink must be flagged; got: {violations}",
        )

    def test_nested_substitution_relink_is_flagged(self) -> None:
        # A relink hidden in a command substitution or `sh -c` body must be flagged.
        for recipe in (
            "$(shell $(MAKE) $(BINDIR)/$(BIN))",
            "X=$$(make $(BINDIR)/$(BIN)); ./run",
            "OUT=`gfortran -o $(BINDIR)/$(BIN) main.o`",
            'bash -c "make $(BINDIR)/$(BIN)"',
            "sh -c 'make all'",
        ):
            makefile = (
                "BINDIR ?= .\n"
                "BIN := app_runner\n"
                "test:\n"
                f"\t{recipe}\n"
            )
            violations = self._run(makefile)
            self.assertTrue(
                any("relinks the binary" in v for v in violations),
                f"nested-substitution relink ({recipe!r}) must be flagged; got: {violations}",
            )

    def test_nested_substitution_run_only_is_accepted(self) -> None:
        # A command substitution / `sh -c` body that does not relink stays accepted.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\tOUT=$$($(BINDIR)/$(BIN) --version); echo $$OUT\n"
            "\tsh -c 'cd $(RUNDIR) && echo done'\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"non-relinking nested substitution must be accepted; got: {violations}"
        )

    def test_builtin_variable_alias_relink_is_flagged(self) -> None:
        # A relink reached via an alias of a make built-in tool variable
        # (`M := $(MAKE)`, `L := $(LD)`) must be flagged even though the built-in
        # is never explicitly assigned in the Makefile.
        for setup, cmd in (
            ("M := $(MAKE)\n", "$(M) $(BINDIR)/$(BIN)"),
            ("L := $(LD)\n", "$(L) -o $(BINDIR)/$(BIN) main.o"),
        ):
            makefile = (
                f"{setup}"
                "BINDIR ?= .\n"
                "BIN := app_runner\n"
                "test:\n"
                f"\t{cmd}\n"
            )
            violations = self._run(makefile)
            self.assertTrue(
                any("relinks the binary" in v for v in violations),
                f"built-in alias relink ({cmd!r}) must be flagged; got: {violations}",
            )

    def test_make_variable_alias_relink_is_flagged(self) -> None:
        # The relink command is reached through a make-variable alias; the command
        # word must be expanded with the variable map before matching.
        makefile = (
            "FC ?= gfortran\n"
            "LINK := $(FC)\n"
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) || $(LINK) -o $(BINDIR)/$(BIN) main.o\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("relinks the binary" in v for v in violations),
            f"make-variable alias relink must be flagged; got: {violations}",
        )

    def test_env_assignment_prefixed_relink_is_flagged(self) -> None:
        # A relink command preceded by shell env assignments must be flagged.
        for cmd in ("VAR=1 $(MAKE) $(BINDIR)/$(BIN)", "FC=gfortran make $(BINDIR)/$(BIN)"):
            makefile = (
                "BINDIR ?= .\n"
                "BIN := app_runner\n"
                "test:\n"
                f"\t{cmd}\n"
            )
            violations = self._run(makefile)
            self.assertTrue(
                any("relinks the binary" in v for v in violations),
                f"env-assignment-prefixed relink ({cmd!r}) must be flagged; got: {violations}",
            )

    def test_shell_conditional_run_only_is_accepted(self) -> None:
        # The same conditional shape that only runs the binary must be accepted.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\tif test -x $(BINDIR)/$(BIN); then cd $(RUNDIR) && $(BINDIR)/$(BIN); fi\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"shell-conditional run-only must be accepted; got: {violations}"
        )

    def test_run_and_cleanup_helper_prerequisite_is_accepted(self) -> None:
        # A prerequisite helper that only cleans up and runs the already-built
        # binary (no make/compiler) does not relink and must be accepted.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN := app_runner\n"
            "prepare-run:\n"
            "\trm -rf $(RUNDIR)/old\n"
            "\tmkdir -p $(RUNDIR)/raw\n"
            "\tcd $(RUNDIR) && $(abspath $(BINDIR)/$(BIN))\n"
            "test: prepare-run\n"
            "\t@true\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"run/cleanup helper prerequisite must be accepted; got: {violations}"
        )

    def test_chained_build_after_mkdir_prereq_is_flagged(self) -> None:
        # A prerequisite helper recipe that chains a compiler after mkdir must not
        # be classified directory-only by its leading `mkdir`.
        makefile = (
            "FC ?= gfortran\n"
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "prepare:\n"
            "\tmkdir -p $(BINDIR) && $(FC) -o $(BINDIR)/$(BIN) main.o\n"
            "test: prepare\n"
            "\t./run\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("build prerequisite" in v for v in violations),
            f"mkdir-then-compiler prerequisite must be flagged; got: {violations}",
        )

    def test_inline_recipe_build_prerequisite_is_flagged(self) -> None:
        # A prerequisite target whose *inline* recipe links the binary must be
        # classified as a build target.
        makefile = (
            "FC ?= gfortran\n"
            "BINDIR ?= .\n"
            "BIN := app_runner\n"
            "build-bin: ; $(FC) -o $(BINDIR)/$(BIN) main.o\n"
            "test: build-bin\n"
            "\t./run\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("build prerequisite" in v for v in violations),
            f"inline-recipe build prerequisite must be flagged; got: {violations}",
        )

    def test_running_the_binary_in_recipe_is_accepted(self) -> None:
        # Running the binary (not building it) must not be mistaken for a relink.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN := app_runner\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) || { echo no >&2; exit 1; }\n"
            "\tmkdir -p $(RUNDIR)/raw\n"
            "\tcd $(RUNDIR) && $(abspath $(BINDIR)/$(BIN)) --run\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"running the binary must be accepted; got: {violations}"
        )

    def test_variable_defined_after_rule_is_accepted(self) -> None:
        # `test: $(TEST_DEPS)` before `TEST_DEPS := ...`: make expands the
        # prerequisite immediately and sees nothing, so it must not be flagged.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN := app_runner\n"
            "test: $(TEST_DEPS)\n"
            "\ttest -x $(BINDIR)/$(BIN) || { echo no >&2; exit 1; }\n"
            "\tcd $(RUNDIR) && $(abspath $(BINDIR)/$(BIN))\n"
            "TEST_DEPS := $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"forward-referenced prereq variable must be accepted; got: {violations}"
        )

    def test_order_only_helper_rule_is_accepted(self) -> None:
        # An order-only prerequisite naming a helper rule that only creates run
        # directories must not be read as a relinking build prerequisite.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN := app_runner\n"
            "prepare-run:\n"
            "\tmkdir -p $(RUNDIR)/raw/state_snapshots\n"
            "test: | prepare-run\n"
            "\ttest -x $(BINDIR)/$(BIN) || { echo no >&2; exit 1; }\n"
            "\tcd $(RUNDIR) && $(abspath $(BINDIR)/$(BIN))\n"
        )
        violations = self._run(makefile)
        self.assertEqual(
            [], violations, f"order-only helper rule must be accepted; got: {violations}"
        )

    def test_missing_makefile_is_noop(self) -> None:
        # Pass the make toolchain so the missing-Makefile branch is actually
        # exercised (not short-circuited by the toolchain gate).
        with tempfile.TemporaryDirectory() as tmp:
            violations: list[str] = []
            _validate_makefile_test_no_relink(
                Path(tmp), violations, build_system="make", language="fortran"
            )
            self.assertEqual([], violations)

    def test_malformed_lineage_does_not_crash_toolchain_lookup(self) -> None:
        # A malformed lineage.json must resolve to (None, None) rather than raise,
        # so the post_build/post_generate stage reports a violation instead of a
        # traceback.
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_dir = Path(tmp)
            (pipeline_dir / "lineage.json").write_text(
                "{ not: valid json ", encoding="utf-8"
            )
            self.assertEqual(
                (None, None),
                _impl_toolchain_from_pipeline_dir(pipeline_dir, pipeline_dir),
            )


class DiagnosticsContractTest(unittest.TestCase):
    """Tests for the io_contract.diagnostics_contract (tests.md §3) field."""

    @staticmethod
    def _struct(contract: dict) -> list[str]:
        violations: list[str] = []
        _validate_diagnostics_contract(Path("spec.ir.yaml"), contract, violations)
        return violations

    def test_absent_diagnostics_contract_is_allowed(self) -> None:
        # A node whose tests.md has no §3 contract omits the field entirely.
        self.assertEqual([], self._struct({"io_contract": {}}))

    def test_wellformed_contract_passes_lifted_and_nested(self) -> None:
        section = {
            "checks": [
                {"id": "equal_state_consistency"},
                {"id": "wave_speed_nonnegative"},
                {"id": "input_guard"},
            ],
            "verdict": {"required": True, "fields": ["overall", "failed_checks"]},
        }
        self.assertEqual([], self._struct({"diagnostics_contract": section}))
        self.assertEqual([], self._struct({"io_contract": {"diagnostics_contract": section}}))

    def test_empty_checks_and_required_verdict_without_fields_flagged(self) -> None:
        violations = self._struct(
            {"diagnostics_contract": {"checks": [], "verdict": {"required": True}}}
        )
        self.assertTrue(any("checks must be non-empty list" in v for v in violations))
        self.assertTrue(
            any("verdict.fields must be non-empty list" in v for v in violations)
        )

    def test_present_but_non_object_section_is_flagged(self) -> None:
        # Regression: a present-but-malformed section (e.g. `diagnostics_contract: []`)
        # must be rejected, not silently skipped as if absent.
        for malformed in ([], "nope", 3):
            self.assertTrue(
                any(
                    "diagnostics_contract must be object when present" in v
                    for v in self._struct({"diagnostics_contract": malformed})
                ),
                f"malformed value {malformed!r} must be flagged",
            )
            self.assertTrue(
                any(
                    "diagnostics_contract must be object when present" in v
                    for v in self._struct(
                        {"io_contract": {"diagnostics_contract": malformed}}
                    )
                ),
                f"nested malformed value {malformed!r} must be flagged",
            )

    def test_missing_id_and_duplicate_id_flagged(self) -> None:
        violations = self._struct(
            {"diagnostics_contract": {"checks": [{"id": "a"}, {"id": "a"}, {"foo": 1}]}}
        )
        self.assertTrue(any("duplicate (a)" in v for v in violations))
        self.assertTrue(any("must be non-empty string" in v for v in violations))

    def test_accessors_only_return_verdict_fields_when_required(self) -> None:
        contract = {
            "diagnostics_contract": {
                "checks": [{"id": "c1"}, {"id": "c2"}],
                "verdict": {"required": False, "fields": ["overall"]},
            }
        }
        self.assertEqual(["c1", "c2"], _diagnostics_contract_check_ids(contract))
        self.assertEqual([], _diagnostics_contract_verdict_fields(contract))

    def test_compile_stage_unaffected_when_field_absent(self) -> None:
        # Regression guard: existing IRs without diagnostics_contract stay valid.
        self.assertEqual([], self._struct({"io_contract": {"inputs": [], "outputs": []}}))


class DiagnosticsContractOutputTest(unittest.TestCase):
    """Tests for the post_execute/pre_judge diagnostics.json output check."""

    CONTRACT = {
        "diagnostics_contract": {
            "checks": [
                {"id": "equal_state_consistency"},
                {"id": "wave_speed_nonnegative"},
                {"id": "input_guard"},
            ],
            "verdict": {"required": True, "fields": ["overall", "failed_checks"]},
        }
    }

    def _run(self, diagnostics: dict, contract: dict | None = CONTRACT) -> list[str]:
        node_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, node_dir, ignore_errors=True)
        (node_dir / "diagnostics.json").write_text(json.dumps(diagnostics))
        execution = NodeExecution(
            node_key="n", node_dir=node_dir, exec_dir=node_dir, pipeline_dir=node_dir
        )
        original = vps._io_contract_for_execution
        vps._io_contract_for_execution = lambda repo_root, ex: contract
        self.addCleanup(setattr, vps, "_io_contract_for_execution", original)
        violations: list[str] = []
        _validate_diagnostics_contract_output(Path("."), execution, violations)
        return violations

    def test_broken_per_case_array_is_flagged(self) -> None:
        # The exact failure shape from orch_20260616T071613Z_5d13cc57.
        violations = self._run(
            {"cases": [{"case_id": "x", "guard_pass": True, "a_x": 1.0, "a_y": 2.0}]}
        )
        self.assertTrue(any("checks must be an object" in v for v in violations))
        self.assertTrue(any("verdict must be an object" in v for v in violations))

    def test_conformant_diagnostics_passes(self) -> None:
        good = {
            "checks": {
                "equal_state_consistency": {"pass": True},
                "wave_speed_nonnegative": {"pass": True},
                "input_guard": {"pass": True},
            },
            "verdict": {"overall": "fail", "failed_checks": ["input_guard"]},
        }
        self.assertEqual([], self._run(good))

    def test_partial_checks_and_verdict_flagged(self) -> None:
        partial = {
            "checks": {"equal_state_consistency": {}, "input_guard": {}},
            "verdict": {"overall": "pass"},
        }
        violations = self._run(partial)
        self.assertTrue(any("wave_speed_nonnegative" in v for v in violations))
        self.assertTrue(any("failed_checks" in v for v in violations))

    def test_no_contract_means_no_check(self) -> None:
        self.assertEqual([], self._run({"cases": []}, contract={"io_contract": {}}))


class FortranIdentifierLengthTests(unittest.TestCase):
    """post_generate flags over-63-char Fortran identifiers (f2008 name limit).

    An over-limit name only fails at the Build step as a compile_error,
    forcing a regenerate -> rebuild retry. Catching it at post_generate fails
    the cheap generate.verify substep first.
    """

    def _src_dir(self, body: str) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "mod.f90").write_text(body, encoding="utf-8")
        return d

    def test_boundary_63_ok_64_flagged(self) -> None:
        ok = "a" * 63
        bad = "b" * 64
        src = self._src_dir(
            f"subroutine {ok}()\nend subroutine\n"
            f"subroutine {bad}(x)\nend subroutine\n"
        )
        violations: list[str] = []
        _validate_fortran_identifier_length(src, violations)
        self.assertEqual(len(violations), 1, msg=violations)
        self.assertIn(bad, violations[0])
        self.assertNotIn(ok, violations[0])

    def test_ignores_comments_and_strings(self) -> None:
        long_tok = "c" * 70
        src = self._src_dir(
            f"! a comment with {long_tok}\n"
            f'call foo("{long_tok}")\n'
        )
        violations: list[str] = []
        _validate_fortran_identifier_length(src, violations)
        self.assertEqual(violations, [])

    def test_reports_each_distinct_name_once(self) -> None:
        bad = "d" * 80
        src = self._src_dir(
            f"call {bad}(1)\ncall {bad}(2)\ninteger :: {bad}\n"
        )
        violations: list[str] = []
        _validate_fortran_identifier_length(src, violations)
        self.assertEqual(len(violations), 1, msg=violations)

    def test_only_scans_fortran_suffixes(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "notes.txt").write_text("e" * 80 + "\n", encoding="utf-8")
        violations: list[str] = []
        _validate_fortran_identifier_length(d, violations)
        self.assertEqual(violations, [])

    def test_ignores_long_word_in_continued_string_literal(self) -> None:
        # A free-form `&`-continued character literal carries its in-string state
        # to the continuation line; a long word there must not be flagged.
        long_word = "x" * 80
        src = self._src_dir(
            'print *, "a long diagnostic message that wraps &\n'
            f'&with {long_word} inside the string"\n'
            "end\n"
        )
        violations: list[str] = []
        _validate_fortran_identifier_length(src, violations)
        self.assertEqual(violations, [])

    def test_still_flags_long_identifier_after_continued_string(self) -> None:
        # State must reset once the string closes: a real over-limit identifier
        # on a later line is still caught.
        long_word = "y" * 80
        bad = "z" * 64
        src = self._src_dir(
            'print *, "wrapped &\n'
            f'&{long_word}"\n'
            f"call {bad}()\n"
        )
        violations: list[str] = []
        _validate_fortran_identifier_length(src, violations)
        self.assertEqual(len(violations), 1, msg=violations)
        self.assertIn(bad, violations[0])

    def test_identifier_split_across_continuation_is_not_flagged(self) -> None:
        # Documented accepted limitation: an identifier split by a free-form `&`
        # continuation is seen as two short tokens, so it is NOT flagged here —
        # the build step's compile_error is the backstop. This test pins that
        # behavior so a future reader does not assume completeness.
        half = "n" * 40  # each half < 63, joined name would be 80 > 63
        src = self._src_dir(f"subroutine very_{half}&\n&{half}()\nend subroutine\n")
        violations: list[str] = []
        _validate_fortran_identifier_length(src, violations)
        self.assertEqual(violations, [])

    def test_missing_src_dir_is_safe(self) -> None:
        missing = Path(tempfile.mkdtemp()) / "does_not_exist"
        violations: list[str] = []
        _validate_fortran_identifier_length(missing, violations)
        self.assertEqual(violations, [])

    def test_does_not_scan_fixed_form_sources(self) -> None:
        # Fixed-form .f / .for use column-1 C/c/* comment markers the free-form
        # stripper does not understand; scanning them would mis-flag a long word
        # in a comment. The generator emits free-form .f90, so they are skipped.
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        long_word = "f" * 80
        (d / "legacy.f").write_text(f"C this fixed-form comment has {long_word}\n", encoding="utf-8")
        (d / "legacy.for").write_text(f"* another comment {long_word}\n", encoding="utf-8")
        violations: list[str] = []
        _validate_fortran_identifier_length(d, violations)
        self.assertEqual(violations, [])


class ModelSourceNotFoundMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.src = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)

    def test_spec_id_unknown_stays_generic(self) -> None:
        msg = vps._model_source_not_found_violation(self.src, None)
        self.assertEqual(msg, f"{self.src}: model source not found")

    def test_no_model_file_emitted(self) -> None:
        msg = vps._model_source_not_found_violation(self.src, "foo_model.f90")
        self.assertEqual(
            msg, f"{self.src}: node model source not found (foo_model.f90)"
        )

    def test_abbreviated_model_file_names_offender_and_instructs_rename(self) -> None:
        # A *_model.f90 exists but under an abbreviated (non-literal) name: the
        # message must name the offender and say "rename", not the misleading
        # "not found" that reads as if no file was written.
        (self.src / "advdiff_bndry_pcopy_model.f90").write_text("", encoding="utf-8")
        msg = vps._model_source_not_found_violation(
            self.src, "dynamics_advection_diffusion_boundary_1d_periodic_copy_model.f90"
        )
        self.assertIn("advdiff_bndry_pcopy_model.f90 present", msg)
        self.assertIn(
            "dynamics_advection_diffusion_boundary_1d_periodic_copy_model.f90", msg
        )
        self.assertIn("rename", msg)
        self.assertNotIn("not found", msg)

    def test_overlong_literal_name_reports_spec_level_not_rename(self) -> None:
        # When <spec_id>_model exceeds the f2008 63-char identifier limit, no
        # legal literal name exists, so renaming an abbreviated file cannot fix
        # it. The message must point at the spec-level problem, not say "rename".
        spec_id = "x" * 70  # <spec_id>_model = 76 chars > 63
        expected = f"{spec_id}_model.f90"
        (self.src / "abbrev_model.f90").write_text("", encoding="utf-8")
        msg = vps._model_source_not_found_violation(self.src, expected)
        self.assertIn("exceeds", msg)
        self.assertIn("spec-level", msg)
        self.assertNotIn("rename", msg)

    def test_dependency_usage_does_not_duplicate_no_model_report(self) -> None:
        # _validate_dependency_operation_usage runs right after
        # _validate_generate_outputs on the same src_dir, which already reports an
        # absent / mis-named model source. The dependency check must stay silent
        # in the no-model case so the abbreviated-name diagnostic is not paired
        # with the stale "node model source not found" wording.
        node = vps.NodeExecution(
            node_key="component/dynamics_advection_diffusion_boundary_1d_periodic_copy@0.1.0",
            node_dir=self.src,
            exec_dir=self.src,
            pipeline_dir=self.src,
        )
        # Abbreviated model file present (not the literal <spec_id>_model.f90).
        (self.src / "advdiff_bndry_pcopy_model.f90").write_text("", encoding="utf-8")
        original = vps._component_dep_spec_ids
        vps._component_dep_spec_ids = lambda repo_root, ex: ["some_dependency_component"]
        self.addCleanup(setattr, vps, "_component_dep_spec_ids", original)
        violations: list[str] = []
        vps._validate_dependency_operation_usage(self.src, node, self.src, violations)
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
