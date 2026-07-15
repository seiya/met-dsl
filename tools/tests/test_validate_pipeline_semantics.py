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
    _validate_fortran_makefile_src_dir,
    _impl_toolchain_from_pipeline_dir,
    _validate_generate_lint_command_logs,
    _validate_makefile_test_no_relink,
    _validate_makefile_test_invokes_cases,
    _validate_source_meta_json_files,
    _validate_compile_dependency_consistency,
    _validate_infrastructure_public_api,
    _parse_public_api_from_controlled_spec,
    _dependency_expected_node_keys,
    _algorithm_state_contract,
    _require_ir_section,
    _tests_path_from_ir_document,
    validate,
    validate_compile_stage,
    validate_post_build_stage,
    validate_post_generate_stage,
)


# The mock spec the shared fixture's IR points at through `meta.source_refs` (the real IR's only
# route to controlled_spec.md / tests.md / deps.yaml). A test that wants the tests.md-dependent
# gates to run writes its tests.md at MOCK_TESTS_REF; one that does not simply leaves the file
# absent, and those gates no-op exactly as they do for an IR whose tests.md is missing.
MOCK_SPEC_DIR = "spec/problem/mock_domain/mock_family/mock_spec"
MOCK_TESTS_REF = f"{MOCK_SPEC_DIR}/tests.md"


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


# Sentinel for "omit this key entirely" in test fixtures (distinct from a None value).
_OMIT = object()


def _write_dep_graph_sidecar(ir_dir: Path, *, node_key: str,
                             all_nodes: list, transitive_deps: list) -> None:
    """Author a conductor-shaped dependency_graph.json sidecar (the derived closure/topo
    graph the consumers read now; see workflow_conductor._write_dependency_graph)."""
    _write_json(ir_dir / "dependency_graph.json", {
        "node_key": node_key,
        "all_nodes": all_nodes,
        "transitive_deps": transitive_deps,
        "generated_by": "conductor",
    })


def _write_dep_graph_sidecar_from_resolved(ir_dir: Path, dependency_resolved: dict) -> None:
    """Author the conductor-shaped dependency_graph.json sidecar for the shared fixture.

    When `dependency_resolved` provides an explicit `all_nodes` list, the sidecar HONORS it
    (the derived closure the run-stage DAG check reads) and derives the sidecar transitive_deps
    as `all_nodes − self − direct_deps`. Otherwise it emits a LEAF sidecar (`all_nodes={self}`).

    Rationale: the historical default fixture declared `direct_deps=[dep]` ONLY to exercise the
    dependency-operation-usage check (which still reads the IR's direct_deps); its
    `_dependency_expected_node_keys` result was `{self}` (all_nodes absent + node_key present),
    so the run-stage DAG-completeness check required NO separate dep pipeline. The derived graph
    now lives in the sidecar, so a leaf sidecar reproduces that exact DAG behavior, while a
    fixture that explicitly declares `all_nodes` (a genuine multi-node closure test) drives the
    DAG check as before."""
    from tools.validate_pipeline_semantics import _normalize_node_key_token

    def _nk(entry: object) -> object:
        return entry.get("node_key") if isinstance(entry, dict) else entry

    self_nk = dependency_resolved.get("node_key") or "problem/shallow_water2d@0.3.0"
    explicit_all = dependency_resolved.get("all_nodes")
    if not (isinstance(explicit_all, list) and explicit_all):
        _write_dep_graph_sidecar(
            ir_dir, node_key=self_nk,
            all_nodes=[{"node_key": self_nk, "topo_level": 0}],
            transitive_deps=[])
        return
    self_tok = _normalize_node_key_token(self_nk)
    direct_toks = {
        _normalize_node_key_token(_nk(d))
        for d in (dependency_resolved.get("direct_deps") or [])
        if isinstance(_nk(d), str) and _nk(d).strip()
    }
    all_nodes: list[dict] = []
    for entry in explicit_all:
        nk = _nk(entry)
        if not (isinstance(nk, str) and nk.strip()):
            continue
        level = entry.get("topo_level") if isinstance(entry, dict) else None
        all_nodes.append({"node_key": nk, "topo_level": level if isinstance(level, int) else 0})
    trans_side = [
        {"node_key": n["node_key"], "via": []}
        for n in all_nodes
        if _normalize_node_key_token(n["node_key"]) not in (direct_toks | {self_tok})
    ]
    _write_dep_graph_sidecar(
        ir_dir, node_key=self_nk, all_nodes=all_nodes, transitive_deps=trans_side)


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
    log_path = node_dir / "command_log.jsonl"

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
    syntax_command_id = "syntax_cmd_fixture_001"
    rel_lint_log = (
        f"workspace/pipelines/{node_safe}/{pipeline_id}/source/src_20260415_001/src/command_log.jsonl"
    )
    if dependency_resolved is None:
        # Real shape: `direct_deps` entries are objects (node_key / kind / operations) in every
        # certified IR — never bare strings.
        dependency_resolved = {
            "node_key": "problem/shallow_water2d@0.3.0",
            "direct_deps": [
                {
                    "node_key": f"component/{dep_spec_id}@0.1.0",
                    "kind": "component",
                    "operations": [f"{dep_spec_id}__compute_flux"],
                }
            ],
            "transitive_deps": [],
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
    # Author the conductor-authored dependency-graph sidecar consistent with the IR's
    # dependency block (the derived closure/topo graph lives here now; see
    # _write_dependency_graph). Built so host_direct = {all_nodes} - {self} - {transitive}
    # equals the IR direct_deps, and every transitive node is present in all_nodes.
    _write_dep_graph_sidecar_from_resolved(
        workspace / "ir" / "problem__shallow_water2d__0.3.0" / "shallow-water2d_20260415_001",
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
            # Real shape: the state contract's four fields are direct children of `algorithm:`.
            # No certified IR nests them under a `state_contract:` block.
            "state_variables": [
                {"name": "h", "shape_expr": "[2,2]"},
                {"name": "hu", "shape_expr": "[2,2]"},
                {"name": "hv", "shape_expr": "[2,2]"},
            ],
            "required_update_paths": ["h", "hu", "hv"],
            "diagnostics_from_state": True,
            "fallback_policy": "fail_closed",
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

    # A real spec.ir.yaml always carries schema_version / meta / case as well as the four
    # sections below. Fixtures that omitted them silently disabled every gate that reaches the
    # IR through them — notably `meta.source_refs.tests`, which is how tests.md is located
    # (see _tests_path_from_ir_document). Keep this document real-shaped; the shape is pinned by
    # `IrFixtureShapeTests`.
    spec_ir_doc: dict[str, object] = {
        "schema_version": "1.0",
        "meta": {
            "node_key": "problem/shallow_water2d@0.3.0",
            "spec_kind": "problem",
            "spec_id": "shallow_water2d",
            "spec_version": "0.3.0",
            "ir_id": pipeline_id,
            "source_refs": {
                "controlled_spec": f"{MOCK_SPEC_DIR}/controlled_spec.md",
                "tests": MOCK_TESTS_REF,
                "deps": f"{MOCK_SPEC_DIR}/deps.yaml",
            },
        },
        # Every certified IR carries at least one case; an empty test_case_set is a shape the
        # pipeline never produces.
        "case": {"test_case_set": [{"case_id": "c1", "inputs": {}}]},
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

    (src_dir / "command_log.jsonl").write_text(
        json.dumps(
            {
                "command_id": lint_command_id,
                "tool_name": "run_linter",
                "command": ["fortitude", "check", "."],
                "ok": True,
            },
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {
                "command_id": syntax_command_id,
                "tool_name": "run_syntax_check",
                "command": [
                    "gfortran", "-fsyntax-only", "-std=f2008",
                    "-Werror=unused-dummy-argument", "-Werror=unused-variable",
                    "-J", ".mods", "-I", ".mods",
                    "shallow_water2d_model.f90", "shallow_water2d_runner.f90",
                ],
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
        },
    )
    # Conductor-authored, leaf-non-writable lint evidence (pipeline-root). post_generate
    # certifies the conductor-run lint against this, not source_meta.lint_command_ref.
    _write_json(
        pipeline_dir / "lint_evidence" / "src_20260415_001.json",
        {
            "checked_at": "2026-04-15T00:00:00Z",
            "source_id": "src_20260415_001",
            "preset": "fortitude",
            "ok": True,
            "run_linter": [
                {
                    "preset": "fortitude",
                    "command_id": lint_command_id,
                    "command_log_ref": rel_lint_log,
                }
            ],
        },
    )
    # Conductor-authored, leaf-non-writable syntax evidence (pipeline-root). post_generate
    # certifies the conductor-run generate.syntax gate (gfortran -fsyntax-only) against it.
    _write_json(
        pipeline_dir / "syntax_evidence" / "src_20260415_001.json",
        {
            "checked_at": "2026-04-15T00:00:00Z",
            "source_id": "src_20260415_001",
            "ok": True,
            "stages": [
                {
                    "compiler": "gfortran",
                    "status": "pass",
                    "compiler_version": "GNU Fortran (fixture) 13.0.0",
                    "command_id": syntax_command_id,
                    "command_log_ref": rel_lint_log,
                }
            ],
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


def _seed_metrics_basis_per_test_tree(
    repo_root: Path, metrics_basis: object, target_cases: list[str] | None = None
) -> None:
    """Seed a tree whose contract demands raw variables `h` and `time` for `test_a`.

    `target_cases` is `test_a`'s predicate range — the (test_id, case_id) evidence rows the
    metrics_basis is checked against. Defaults to the single-target `["case_a"]`."""
    target_cases = ["case_a"] if target_cases is None else list(target_cases)
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
        "## 7. Test definitions\n### 7-1. `test_a`\n",
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
                            "variables": [{"name": "h", "shape_expr": "[2,2]"}],
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
            # The (test_id, case_id) evidence matrix is anchored on target_cases, so a
            # metrics_basis fixture must name the case each of its entries came from.
            "test_predicates": [
                {
                    "test_id": "test_a",
                    "expected_outcome": "pass",
                    "target_cases": target_cases,
                    "pass_when": {
                        "all": [{"ref": "verdict.overall", "op": "eq", "value": "pass"}]
                    },
                }
            ],
        },
        metrics_basis=metrics_basis,
    )


class MetricsBasisEvidenceMatrixTests(unittest.TestCase):
    """R3-core: post_execute pins metrics_basis against the (test_id, target case_id) product.

    The anchor is `io_contract.test_predicates[].target_cases` — the same field the
    host-rendered runner emits its entries from (`runner_renderer._target_cases`), so the
    gate mirrors the renderer instead of guessing.
    """

    _VARS = {"h": [[1.0, 1.0], [1.0, 1.0]], "time": 0.5}

    def _violations_for(self, metrics_basis: object,
                        target_cases: list[str] | None = None) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_metrics_basis_per_test_tree(repo_root, metrics_basis, target_cases)
            return validate(repo_root=repo_root, workspace_root="workspace")

    @classmethod
    def _entry(cls, case_id: str) -> dict:
        return {"test_id": "test_a", "case_id": case_id, **cls._VARS}

    def _matrix_violations(self, violations: list[str]) -> list[str]:
        return [v for v in violations if "per-test evidence for (test_id, case_id)" in v
                or "per-test evidence (test_id, case_id)" in v]

    def test_multi_target_test_needs_one_entry_per_target_case(self) -> None:
        v = self._violations_for(
            {"per_test": [self._entry("case_a"), self._entry("case_b")]},
            target_cases=["case_a", "case_b"])
        self.assertEqual(self._matrix_violations(v), [], v)

    def test_missing_row_is_rejected(self) -> None:
        # The old shape — one entry per test_id — silently dropped the second case.
        v = self._violations_for({"per_test": [self._entry("case_a")]},
                                 target_cases=["case_a", "case_b"])
        matched = [x for x in v if "missing per-test evidence for (test_id, case_id)" in x]
        self.assertEqual(len(matched), 1, v)
        self.assertIn("('test_a', 'case_b')", matched[0])

    def test_extra_row_is_rejected(self) -> None:
        v = self._violations_for(
            {"per_test": [self._entry("case_a"), self._entry("case_ghost")]},
            target_cases=["case_a"])
        matched = [x for x in v if "unknown per-test evidence (test_id, case_id)" in x]
        self.assertEqual(len(matched), 1, v)
        self.assertIn("('test_a', 'case_ghost')", matched[0])

    def test_entry_without_case_id_is_rejected(self) -> None:
        v = self._violations_for({"per_test": [{"test_id": "test_a", **self._VARS}]})
        self.assertTrue(any("must carry a non-empty `case_id`" in x for x in v), v)

    def test_duplicate_test_case_pair_is_rejected(self) -> None:
        v = self._violations_for(
            {"per_test": [self._entry("case_a"), self._entry("case_a")]})
        self.assertTrue(
            any("duplicated (test_id, case_id) ((test_a, case_a))" in x for x in v), v)

    def test_tests_object_form_cannot_express_a_multi_target_test(self) -> None:
        # Keyed by test_id, it physically cannot hold both rows. Say that, rather than
        # reporting the second row as merely "missing".
        v = self._violations_for(
            {"tests": {"test_a": {"case_id": "case_a", **self._VARS}}},
            target_cases=["case_a", "case_b"])
        matched = [x for x in v if "deprecated `tests` object form" in x]
        self.assertEqual(len(matched), 1, v)
        self.assertIn("emit a `per_test` LIST with one entry per (test_id, case_id)", matched[0])
        # It is reported INSTEAD of a bare missing-row message, not alongside it.
        self.assertEqual(self._matrix_violations(v), [], v)

    def test_tests_object_keys_that_normalize_to_one_test_id_are_rejected(self) -> None:
        # JSON object keys are unique as written, but this reader strips them, so `"test_a"`
        # and `" test_a "` name one entry. Silently keeping the last would let a malformed row
        # (here, the one missing `time`) vanish.
        v = self._violations_for({"tests": {
            "test_a": {"case_id": "case_a", "h": self._VARS["h"]},
            " test_a ": {"case_id": "case_a", **self._VARS},
        }})
        self.assertTrue(
            any("tests has duplicated (test_id, case_id) ((test_a, case_a))" in x for x in v), v)

    def test_tests_object_form_still_accepted_for_a_single_target_test(self) -> None:
        v = self._violations_for({"tests": {"test_a": {"case_id": "case_a", **self._VARS}}})
        self.assertEqual(self._matrix_violations(v), [], v)
        self.assertEqual([x for x in v if "deprecated `tests` object form" in x], [], v)

    def test_test_with_no_target_case_is_reported_not_silently_dropped(self) -> None:
        # Without target_cases the expected rows cannot be derived; reporting them as
        # "unknown" entries instead would blame the runner for an IR defect.
        v = self._violations_for({"per_test": [self._entry("case_a")]}, target_cases=[])
        self.assertTrue(
            any("no io_contract.test_predicates[].target_cases" in x for x in v), v)
        self.assertEqual(self._matrix_violations(v), [], v)


class MetricsBasisWrapperGuidanceTests(unittest.TestCase):
    """The `values`-wrapper repair guidance appended to the missing-variable violation.

    Acceptance must not move: only the message text changes.

    The `stay_accepted` cases assert an absence, so they would pass vacuously if
    the fixture ever stopped reaching this gate. `test_values_wrapper_...` seeds
    the same tree and asserts the violation *is* raised, so a fixture that fails
    to reach the gate fails there loudly rather than silently greening these.
    """

    _MISSING_PREFIX = (
        "metrics_basis.json: test_id test_a case_id case_a missing required_raw_variables"
    )

    def _violations_for(self, metrics_basis: object) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_metrics_basis_per_test_tree(repo_root, metrics_basis)
            return validate(repo_root=repo_root, workspace_root="workspace")

    def test_values_wrapper_violation_names_the_wrapper_and_shows_sibling_form(self) -> None:
        violations = self._violations_for(
            {
                "per_test": [
                    {
                        "test_id": "test_a",
                        "case_id": "case_a",
                        "values": {
                            "h": [[1.0, 1.0], [1.0, 1.0]],
                            "time": 0.5,
                        },
                    }
                ]
            }
        )
        matched = [v for v in violations if self._MISSING_PREFIX in v]
        self.assertEqual(len(matched), 1, violations)
        message = matched[0]
        self.assertIn("missing required_raw_variables (['h', 'time'])", message)
        self.assertIn("unrecognized wrapper key 'values'", message)
        self.assertIn("direct sibling key of test_id", message)
        self.assertIn('{"test_id": ..., "h": ...}', message)
        self.assertIn("do not wrap them under 'values'", message)

    def test_partial_coverage_message_claims_only_what_the_wrapper_holds(self) -> None:
        violations = self._violations_for(
            {
                "per_test": [
                    {
                        "test_id": "test_a",
                        "case_id": "case_a",
                        "aa": {"h": [[1.0, 1.0], [1.0, 1.0]]},
                        "zz": {"time": 0.5},
                    }
                ]
            }
        )
        matched = [v for v in violations if self._MISSING_PREFIX in v]
        self.assertEqual(len(matched), 1, violations)
        message = matched[0]
        self.assertIn("missing required_raw_variables (['h', 'time'])", message)
        self.assertIn("the missing variables ['h'] are nested under", message)
        self.assertIn("unrecognized wrapper key 'aa'", message)
        self.assertIn('{"test_id": ..., "h": ...}', message)

    def test_missing_variable_without_wrapper_keeps_the_bare_message(self) -> None:
        violations = self._violations_for(
            {
                "per_test": [
                    {
                        "test_id": "test_a",
                        "case_id": "case_a",
                        "raw_variables": {"h": [[1.0, 1.0], [1.0, 1.0]]},
                    }
                ]
            }
        )
        matched = [v for v in violations if self._MISSING_PREFIX in v]
        self.assertEqual(len(matched), 1, violations)
        self.assertTrue(matched[0].endswith("missing required_raw_variables (['time'])"))
        self.assertNotIn("wrapper", matched[0])

    def test_recognized_nesting_keys_stay_accepted(self) -> None:
        for field_name in ("raw_variables", "variables", "evidence"):
            with self.subTest(field_name=field_name):
                violations = self._violations_for(
                    {
                        "per_test": [
                            {
                                "test_id": "test_a",
                                "case_id": "case_a",
                                field_name: {
                                    "h": [[1.0, 1.0], [1.0, 1.0]],
                                    "time": 0.5,
                                },
                            }
                        ]
                    }
                )
                self.assertEqual(
                    [v for v in violations if self._MISSING_PREFIX in v], [], violations
                )

    def test_flat_sibling_entry_stays_accepted(self) -> None:
        violations = self._violations_for(
            {
                "per_test": [
                    {
                        "test_id": "test_a",
                        "case_id": "case_a",
                        "h": [[1.0, 1.0], [1.0, 1.0]],
                        "time": 0.5,
                    }
                ]
            }
        )
        self.assertEqual(
            [v for v in violations if self._MISSING_PREFIX in v], [], violations
        )


class MetricsBasisUnrecognizedWrapperUnitTests(unittest.TestCase):
    def test_returns_none_when_no_dict_valued_key_holds_a_missing_variable(self) -> None:
        entry = {"test_id": "t", "h": [1.0], "notes": {"h": "irrelevant"}}
        self.assertIsNone(
            vps._metrics_basis_unrecognized_wrapper(entry, ["time"], {"h", "time"})
        )

    def test_bookkeeping_keys_are_never_reported_as_wrappers(self) -> None:
        entry = {"test_id": "t", "meta": {"time": 0.5}, "artifacts": {"time": 0.5}}
        self.assertIsNone(
            vps._metrics_basis_unrecognized_wrapper(entry, ["time"], {"time"})
        )

    def test_recognized_nesting_keys_are_never_reported_as_wrappers(self) -> None:
        entry = {"test_id": "t", "raw_variables": {"h": 1.0}}
        self.assertIsNone(
            vps._metrics_basis_unrecognized_wrapper(entry, ["time"], {"h", "time"})
        )

    def test_shadowed_nesting_key_is_never_reported_as_a_wrapper(self) -> None:
        """`evidence` is ignored by the reader only because `raw_variables` wins.

        It is still a contract-recognized nesting name, so calling it
        "unrecognized" would misdirect the repair turn.
        """
        entry = {
            "test_id": "t",
            "raw_variables": {"h": 1.0},
            "evidence": {"time": 0.5},
        }
        self.assertIsNone(
            vps._metrics_basis_unrecognized_wrapper(entry, ["time"], {"h", "time"})
        )

    def test_padded_nesting_key_is_reported_verbatim_because_the_reader_rejects_it(
        self,
    ) -> None:
        """`_metrics_basis_variable_keys` matches nesting fields via exact `get()`.

        A padded variant is therefore a real wrapper, not a recognized field, and
        it must be reported with its padding intact — reporting the stripped name
        would call the recognized nesting key "unrecognized".
        """
        entry = {"test_id": "t", " raw_variables ": {"h": 1.0, "time": 0.5}}
        self.assertEqual(
            vps._metrics_basis_unrecognized_wrapper(
                entry, ["h", "time"], {"h", "time"}
            ),
            (" raw_variables ", ["h", "time"]),
        )

    def test_keys_differing_only_by_padding_resolve_deterministically(self) -> None:
        forward = {"test_id": "t", "values": {"time": 0.5}, " values ": {"time": 0.5}}
        reverse = {"test_id": "t", " values ": {"time": 0.5}, "values": {"time": 0.5}}
        self.assertEqual(
            vps._metrics_basis_unrecognized_wrapper(forward, ["time"], {"time"}),
            vps._metrics_basis_unrecognized_wrapper(reverse, ["time"], {"time"}),
        )

    def test_dict_valued_contract_variable_is_not_named_as_its_own_wrapper(self) -> None:
        """`h` is an accepted flat variable; only `time` is missing.

        Naming `h` would tell the model not to wrap under a key it must emit.
        """
        entry = {"test_id": "t", "h": {"time": 0.5}}
        self.assertEqual(vps._metrics_basis_variable_keys(entry), {"h"})
        self.assertIsNone(
            vps._metrics_basis_unrecognized_wrapper(entry, ["time"], {"h", "time"})
        )

    def test_reports_the_wrapper_covering_the_most_missing_variables(self) -> None:
        entry = {
            "test_id": "t",
            "zz_payload": {"h": 1.0, "time": 0.5},
            "aa_partial": {"h": 1.0},
        }
        self.assertEqual(
            vps._metrics_basis_unrecognized_wrapper(entry, ["h", "time"], {"h", "time"}),
            ("zz_payload", ["h", "time"]),
        )

    def test_reports_only_the_variables_the_winning_wrapper_actually_holds(self) -> None:
        """Two wrappers, one variable each: the winner must not claim both."""
        entry = {"test_id": "t", "aa": {"h": 1.0}, "zz": {"time": 0.5}}
        self.assertEqual(
            vps._metrics_basis_unrecognized_wrapper(entry, ["h", "time"], {"h", "time"}),
            ("aa", ["h"]),
        )

    def test_equal_coverage_ties_break_lexicographically(self) -> None:
        entry = {"test_id": "t", "zz": {"time": 0.5}, "aa": {"time": 0.5}}
        self.assertEqual(
            vps._metrics_basis_unrecognized_wrapper(entry, ["time"], {"time"}),
            ("aa", ["time"]),
        )

    def test_tie_break_is_independent_of_insertion_order(self) -> None:
        forward = {"test_id": "t", "aa": {"time": 0.5}, "zz": {"time": 0.5}}
        reverse = {"test_id": "t", "zz": {"time": 0.5}, "aa": {"time": 0.5}}
        self.assertEqual(
            vps._metrics_basis_unrecognized_wrapper(forward, ["time"], {"time"}),
            vps._metrics_basis_unrecognized_wrapper(reverse, ["time"], {"time"}),
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

    def test_snapshot_state_variables_scoped_to_per_case_evidence(self) -> None:
        """A state_snapshot file is only required to carry the raw variables its
        own case's test declares in io_contract.test_evidence_requirements, not
        the global union of declared variables.

        Motivating case (demo_dep_base, billed E2E 2026-06-25): an input-guard
        rejection case (n <= 0) produces no output state, so its snapshot
        legitimately carries only the rejected input `x` and omits the output
        `y`. The IR correctly scopes that case to required_raw_variables=[x],
        yet the post_execute completeness gate demanded {x, y} in *every*
        snapshot and falsely failed validate.execute. This asserts:
          (a) the guard case (y-less) does NOT raise 'declared state_variables
              missing' now that the gate honors per-case evidence;
          (b) strictness is preserved: a *valid* case that needs y but omits it
              still raises the missing-variable violation.
        """
        for omit_y_from_valid_case in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                node_safe = "component__demo_scope__0.1.0"
                ir_ref = f"workspace/ir/{node_safe}/demo-scope_20260625_001"
                pipeline_dir = (
                    repo_root / "workspace" / "pipelines" / node_safe
                    / "demo-scope_20260625_001"
                )
                ir_dir = repo_root / ir_ref
                pipeline_dir.mkdir(parents=True, exist_ok=True)
                ir_dir.mkdir(parents=True, exist_ok=True)
                _write_json(pipeline_dir / "lineage.json", {"ir_ref": ir_ref})

                schema = {
                    "variables": [
                        {"name": "x", "shape_expr": "[n]"},
                        {"name": "y", "shape_expr": "[n]"},
                    ],
                    "time_variable": "snapshot_index",
                    "time_shape_expr": "scalar",
                }
                _write_json(
                    ir_dir / "spec.ir.yaml",
                    {
                        "case": {
                            "test_case_set": [
                                {"case_id": "c_valid", "test_id": "t_valid"},
                                {"case_id": "c_guard", "test_id": "t_guard"},
                            ]
                        },
                        "io_contract": {
                            "inputs": [{"name": "x", "shape_expr": "[n]"}],
                            "outputs": [{"name": "y", "shape_expr": "[n]"}],
                            "raw_requirements": {
                                "required_evidence": [
                                    {
                                        "artifact": "state_snapshots",
                                        "required": True,
                                        "min_samples": 1,
                                        "schema": schema,
                                    }
                                ]
                            },
                            "test_evidence_requirements": [
                                {
                                    "test_id": "t_valid",
                                    "required_raw_variables": ["x", "y"],
                                },
                                {
                                    "test_id": "t_guard",
                                    "required_raw_variables": ["x"],
                                },
                            ],
                        },
                    },
                )

                node_dir = pipeline_dir / "runs" / "run_test_001" / node_safe
                snapshots_dir = node_dir / "raw" / "state_snapshots"
                snapshots_dir.mkdir(parents=True, exist_ok=True)
                _write_json(snapshots_dir / "snapshot_schema.json", {
                    **schema,
                    "min_samples": 1,
                    "samples": ["c_valid.json", "c_guard.json"],
                })
                valid_case = {"snapshot_index": 0, "case_id": "c_valid",
                              "x": [1.0, 2.0, 3.0]}
                if not omit_y_from_valid_case:
                    valid_case["y"] = [2.0, 4.0, 6.0]
                _write_json(snapshots_dir / "c_valid.json", valid_case)
                # Guard (rejection) case: only the rejected input, no output y.
                _write_json(snapshots_dir / "c_guard.json",
                            {"snapshot_index": 1, "case_id": "c_guard", "x": []})

                execution = NodeExecution(
                    node_key="component/demo_scope@0.1.0",
                    node_dir=node_dir,
                    exec_dir=pipeline_dir / "runs" / "run_test_001",
                    pipeline_dir=pipeline_dir,
                )
                violations: list[str] = []
                vps._validate_raw_evidence(repo_root, execution, violations)

                missing_state = [
                    v for v in violations
                    if "declared state_variables missing in snapshot files" in v
                ]
                # The guard case must never be flagged for the absent output y.
                self.assertFalse(
                    any("c_guard.json" in v for v in missing_state),
                    f"guard case wrongly flagged; got: {missing_state}",
                )
                if omit_y_from_valid_case:
                    # (b) strictness preserved: the valid case needs y.
                    self.assertTrue(
                        any("c_valid.json" in v for v in missing_state),
                        f"valid case missing y should be flagged; got: {violations}",
                    )
                else:
                    # (a) fully-formed run is clean of the completeness violation.
                    self.assertFalse(
                        missing_state,
                        f"unexpected missing-state violation; got: {missing_state}",
                    )

    def test_snapshot_scope_resolves_via_in_file_test_id_when_case_map_empty(self) -> None:
        """C-class IR-shape robustness: scope per-case evidence even when
        `case.test_case_set` omits `test_id` (so the case_id->test_id map is
        empty) by reading the snapshot's own `test_id` field.

        This is the second observed Compile/runner output shape (billed dev E2E
        2026-06-25, orch `…150418Z_6571ad31`): snapshots are named
        `<test_id>_NNNN.json` and carry in-file `test_id` (and a `case_id` equal
        to the test_id), while `test_case_set[].test_id` is null. The first fix
        keyed only on the case_id->test_id map and fell back to the strict union
        here, wrongly failing the guard case for the absent output `y`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            node_safe = "component__demo_tid__0.1.0"
            ir_ref = f"workspace/ir/{node_safe}/demo-tid_20260625_001"
            pipeline_dir = (
                repo_root / "workspace" / "pipelines" / node_safe
                / "demo-tid_20260625_001"
            )
            ir_dir = repo_root / ir_ref
            pipeline_dir.mkdir(parents=True, exist_ok=True)
            ir_dir.mkdir(parents=True, exist_ok=True)
            _write_json(pipeline_dir / "lineage.json", {"ir_ref": ir_ref})

            schema = {
                "variables": [
                    {"name": "x", "shape_expr": "[n]"},
                    {"name": "y", "shape_expr": "[n]"},
                ],
                "time_variable": "snapshot_index",
                "time_shape_expr": "scalar",
            }
            _write_json(
                ir_dir / "spec.ir.yaml",
                {
                    # test_case_set carries case_id but NULL test_id -> the
                    # case_id->test_id map is empty.
                    "case": {
                        "test_case_set": [
                            {"case_id": "l0_scale_identity_pass", "test_id": None},
                            {"case_id": "l0_invalid_length_xfail", "test_id": None},
                        ]
                    },
                    "io_contract": {
                        "inputs": [{"name": "x", "shape_expr": "[n]"}],
                        "outputs": [{"name": "y", "shape_expr": "[n]"}],
                        "raw_requirements": {
                            "required_evidence": [
                                {
                                    "artifact": "state_snapshots",
                                    "required": True,
                                    "min_samples": 1,
                                    "schema": schema,
                                }
                            ]
                        },
                        "test_evidence_requirements": [
                            {"test_id": "l0_scale_identity_pass",
                             "required_raw_variables": ["x", "y"]},
                            {"test_id": "l0_invalid_length_xfail",
                             "required_raw_variables": ["x"]},
                        ],
                    },
                },
            )

            node_dir = pipeline_dir / "runs" / "run_test_001" / node_safe
            snapshots_dir = node_dir / "raw" / "state_snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            _write_json(snapshots_dir / "snapshot_schema.json", {
                **schema, "min_samples": 1,
                "samples": ["l0_scale_identity_pass_0000.json",
                            "l0_invalid_length_xfail_0000.json"],
            })
            _write_json(snapshots_dir / "l0_scale_identity_pass_0000.json", {
                "snapshot_index": 0, "case_id": "l0_scale_identity_pass",
                "test_id": "l0_scale_identity_pass",
                "x": [1.0, 2.0, 3.0], "y": [2.0, 4.0, 6.0],
            })
            # Guard case: in-file test_id present, only the rejected input x.
            _write_json(snapshots_dir / "l0_invalid_length_xfail_0000.json", {
                "snapshot_index": 0, "case_id": "l0_invalid_length_xfail",
                "test_id": "l0_invalid_length_xfail", "x": [],
            })

            execution = NodeExecution(
                node_key="component/demo_tid@0.1.0",
                node_dir=node_dir,
                exec_dir=pipeline_dir / "runs" / "run_test_001",
                pipeline_dir=pipeline_dir,
            )
            violations: list[str] = []
            vps._validate_raw_evidence(repo_root, execution, violations)

            missing_state = [
                v for v in violations
                if "declared state_variables missing in snapshot files" in v
            ]
            self.assertFalse(
                missing_state,
                f"guard case should be excused via in-file test_id; got: {missing_state}",
            )

    def _write_target_cases_ir(self, repo_root: Path, node_safe: str, ir_ref: str,
                               pipeline_dir: Path, *, predicates: list[dict] | None,
                               evidence: list[dict], schema: dict,
                               test_case_set: list[dict] | None = None) -> None:
        ir_dir = repo_root / ir_ref
        pipeline_dir.mkdir(parents=True, exist_ok=True)
        ir_dir.mkdir(parents=True, exist_ok=True)
        _write_json(pipeline_dir / "lineage.json", {"ir_ref": ir_ref})
        io_contract: dict = {
            "inputs": [{"name": "U_L", "shape_expr": "[3]"}],
            "outputs": [{"name": "F_star", "shape_expr": "[3]"}],
            "raw_requirements": {
                "required_evidence": [
                    {"artifact": "state_snapshots", "required": True,
                     "min_samples": 1, "schema": schema}
                ]
            },
            "test_evidence_requirements": evidence,
        }
        if predicates is not None:
            io_contract["test_predicates"] = predicates
        _write_json(
            ir_dir / "spec.ir.yaml",
            {
                # The M3c shape by default: case_id declared, `test_id` absent entirely (it is
                # not a required field per phase_01_compile.md), so the case->test map is empty.
                "case": {"test_case_set": test_case_set or [{"case_id": "case_equal_state"},
                                                            {"case_id": "case_dry_state"}]},
                "io_contract": io_contract,
            },
        )

    def test_snapshot_scope_resolves_via_target_cases_when_no_test_id_anywhere(self) -> None:
        """A host-rendered (M3c) runner writes `<case_id>.json` carrying `case_id` and no
        `test_id`, while the IR's `case.test_case_set[]` omits `test_id` too — so neither the
        case->test map, the in-file `test_id`, nor the file stem resolves a per-test contract.
        `io_contract.test_predicates[].target_cases` is the remaining anchor, and it is the
        exact field `runner_renderer._per_case_vars` renders the snapshot from.

        Regression for the billed dev E2E 2026-07-09 (orch `…075057Z_89f9f59a`,
        `dynamics_shallow_water_flux_2d_rusanov_p0`): the gate fell back to the strict union
        and reported `F_star`/`G_star`/`guard_fired` missing from cases that legitimately omit
        them, contradicting the `state_snapshots` bullet of phase_04_validate.md. The runner
        output was conformant.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            node_safe = "component__demo_tc__0.1.0"
            ir_ref = f"workspace/ir/{node_safe}/demo-tc_20260709_001"
            pipeline_dir = (repo_root / "workspace" / "pipelines" / node_safe
                            / "demo-tc_20260709_001")
            schema = {
                "variables": [
                    {"name": "U_L", "shape_expr": "[3]"},
                    {"name": "F_star", "shape_expr": "[3]"},
                    {"name": "guard_fired", "shape_expr": "scalar"},
                ],
                "time_variable": "t",
                "time_shape_expr": "scalar",
            }
            self._write_target_cases_ir(
                repo_root, node_safe, ir_ref, pipeline_dir, schema=schema,
                predicates=[
                    {"test_id": "l0_equal_state_pass", "target_cases": ["case_equal_state"]},
                    {"test_id": "l0_dry_state_xfail", "target_cases": ["case_dry_state"]},
                ],
                evidence=[
                    {"test_id": "l0_equal_state_pass",
                     "required_raw_variables": ["U_L", "F_star"]},
                    {"test_id": "l0_dry_state_xfail",
                     "required_raw_variables": ["U_L", "guard_fired"]},
                ],
            )
            node_dir = pipeline_dir / "runs" / "run_test_001" / node_safe
            snapshots_dir = node_dir / "raw" / "state_snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            _write_json(snapshots_dir / "snapshot_schema.json", {
                **schema, "min_samples": 1,
                "samples": ["case_equal_state.json", "case_dry_state.json"]})
            # Exactly what the host renderer emits: only this case's required variables.
            _write_json(snapshots_dir / "case_equal_state.json", {
                "t": 0.0, "case_id": "case_equal_state",
                "U_L": [1.0, 2.0, 3.0], "F_star": [0.5, 0.5, 0.5]})
            _write_json(snapshots_dir / "case_dry_state.json", {
                "t": 0.0, "case_id": "case_dry_state",
                "U_L": [0.0, 0.5, 0.25], "guard_fired": 1.0})

            execution = NodeExecution(
                node_key="component/demo_tc@0.1.0", node_dir=node_dir,
                exec_dir=pipeline_dir / "runs" / "run_test_001",
                pipeline_dir=pipeline_dir)
            violations: list[str] = []
            vps._validate_raw_evidence(repo_root, execution, violations)
            missing_state = [v for v in violations
                             if "declared state_variables missing in snapshot files" in v]
            self.assertFalse(
                missing_state,
                f"per-case snapshots scoped via target_cases must pass; got: {missing_state}")

    def test_snapshot_scope_in_file_test_id_outranks_the_target_cases_union(self) -> None:
        """Anchor order is load-bearing: a snapshot that names its own `test_id` is a per-TEST
        file and must be scoped to that test, not to the union over every test ranging over its
        case. Scoping it to the union would require variables the file legitimately omits.

        Pins the ordering against a reorder that puts the union anchor first."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            node_safe = "component__demo_ord__0.1.0"
            ir_ref = f"workspace/ir/{node_safe}/demo-ord_20260709_001"
            pipeline_dir = (repo_root / "workspace" / "pipelines" / node_safe
                            / "demo-ord_20260709_001")
            schema = {
                "variables": [
                    {"name": "U_L", "shape_expr": "[3]"},
                    {"name": "F_star", "shape_expr": "[3]"},
                    {"name": "guard_fired", "shape_expr": "scalar"},
                ],
                "time_variable": "t",
                "time_shape_expr": "scalar",
            }
            # One case, two tests: the union is {U_L, F_star, guard_fired}.
            self._write_target_cases_ir(
                repo_root, node_safe, ir_ref, pipeline_dir, schema=schema,
                predicates=[
                    {"test_id": "t_a", "target_cases": ["case_equal_state"]},
                    {"test_id": "t_b", "target_cases": ["case_equal_state"]},
                    {"test_id": "t_c", "target_cases": ["case_dry_state"]},
                ],
                evidence=[
                    {"test_id": "t_a", "required_raw_variables": ["U_L", "F_star"]},
                    {"test_id": "t_b", "required_raw_variables": ["U_L", "guard_fired"]},
                    {"test_id": "t_c", "required_raw_variables": ["U_L"]},
                ],
            )
            node_dir = pipeline_dir / "runs" / "run_test_001" / node_safe
            snapshots_dir = node_dir / "raw" / "state_snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            _write_json(snapshots_dir / "snapshot_schema.json", {
                **schema, "min_samples": 1,
                "samples": ["case_equal_state.json", "case_dry_state.json"]})
            # A per-TEST snapshot for t_a: it carries `test_id` and omits `guard_fired`, which
            # only the SIBLING test t_b requires. Correct under the t_a scope; a violation if
            # the union anchor were consulted first.
            _write_json(snapshots_dir / "case_equal_state.json", {
                "t": 0.0, "case_id": "case_equal_state", "test_id": "t_a",
                "U_L": [1.0, 2.0, 3.0], "F_star": [0.5, 0.5, 0.5]})
            _write_json(snapshots_dir / "case_dry_state.json", {
                "t": 0.0, "case_id": "case_dry_state", "U_L": [0.0, 0.5, 0.25]})

            execution = NodeExecution(
                node_key="component/demo_ord@0.1.0", node_dir=node_dir,
                exec_dir=pipeline_dir / "runs" / "run_test_001",
                pipeline_dir=pipeline_dir)
            violations: list[str] = []
            vps._validate_raw_evidence(repo_root, execution, violations)
            missing_state = [v for v in violations
                             if "declared state_variables missing in snapshot files" in v]
            self.assertFalse(
                missing_state,
                f"an in-file test_id must outrank the union anchor; got: {missing_state}")

    def _untargeted_case_violations(self, repo_root: Path, *, snapshot_case_id: str) -> list[str]:
        """One targeted case + one DECLARED case no predicate ranges over. The second snapshot's
        in-file `case_id` is parameterized so the undeclared-token path can be exercised too."""
        node_safe = "component__demo_untargeted__0.1.0"
        ir_ref = f"workspace/ir/{node_safe}/demo-untargeted_20260709_001"
        pipeline_dir = (repo_root / "workspace" / "pipelines" / node_safe
                        / "demo-untargeted_20260709_001")
        schema = {
            "variables": [
                {"name": "U_L", "shape_expr": "[3]"},
                {"name": "F_star", "shape_expr": "[3]"},
                {"name": "guard_fired", "shape_expr": "scalar"},
            ],
            "time_variable": "t",
            "time_shape_expr": "scalar",
        }
        self._write_target_cases_ir(
            repo_root, node_safe, ir_ref, pipeline_dir, schema=schema,
            # Only case_equal_state is targeted; case_dry_state is declared but targeted by nothing.
            predicates=[{"test_id": "t_a", "target_cases": ["case_equal_state"]}],
            evidence=[{"test_id": "t_a", "required_raw_variables": ["U_L", "F_star"]}],
        )
        node_dir = pipeline_dir / "runs" / "run_test_001" / node_safe
        snapshots_dir = node_dir / "raw" / "state_snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        _write_json(snapshots_dir / "snapshot_schema.json", {
            **schema, "min_samples": 1,
            "samples": ["case_equal_state.json", "other.json"]})
        _write_json(snapshots_dir / "case_equal_state.json", {
            "t": 0.0, "case_id": "case_equal_state",
            "U_L": [1.0, 2.0, 3.0], "F_star": [0.5, 0.5, 0.5]})
        # The renderer emits an EMPTY-state snapshot for an untargeted case (`allocate(vals(0))`).
        _write_json(snapshots_dir / "other.json", {"t": 0.0, "case_id": snapshot_case_id})

        execution = NodeExecution(
            node_key="component/demo_untargeted@0.1.0", node_dir=node_dir,
            exec_dir=pipeline_dir / "runs" / "run_test_001",
            pipeline_dir=pipeline_dir)
        violations: list[str] = []
        vps._validate_raw_evidence(repo_root, execution, violations)
        return [v for v in violations
                if "declared state_variables missing in snapshot files" in v]

    def test_snapshot_scope_declared_but_untargeted_case_requires_nothing(self) -> None:
        """A case declared in `case.test_case_set[]` that NO predicate ranges over has an empty
        union, and `runner_renderer._per_case_vars` renders it as an empty-state snapshot
        (`allocate(vals(0))`). The gate must mirror that instead of demanding every declared
        variable — otherwise it false-rejects a conformant runner, the very defect the
        `target_cases` anchor was added to remove. An untargeted case is schema-valid:
        `validate_predicate_schema` checks each `target_cases` entry is a declared case, never
        that every declared case is targeted."""
        with tempfile.TemporaryDirectory() as tmp:
            missing_state = self._untargeted_case_violations(
                Path(tmp), snapshot_case_id="case_dry_state")
            self.assertFalse(
                missing_state,
                f"a declared-but-untargeted case requires no state variables; got: {missing_state}")

    def test_snapshot_scope_undeclared_case_token_keeps_the_strict_requirement(self) -> None:
        """The empty-union relaxation is gated on the case being DECLARED. A snapshot whose
        `case_id` matches no declared case cannot be scoped by anything, so the strict
        all-declared requirement stands (it is not a renderer-emitted per-case file)."""
        with tempfile.TemporaryDirectory() as tmp:
            missing_state = self._untargeted_case_violations(
                Path(tmp), snapshot_case_id="case_not_in_the_ir")
            self.assertTrue(missing_state, "an unknown case token keeps the strict requirement")
            self.assertIn("other.json", missing_state[0])
            self.assertNotIn("case_equal_state.json", missing_state[0])

    def test_snapshot_scope_union_outranks_the_legacy_single_test_mapping(self) -> None:
        """A hybrid IR carries BOTH a legacy `case.test_case_set[].test_id` and predicates that
        range several tests over the same case. The union is authoritative — it mirrors
        `_per_case_vars`, which is what the runner emits — while the legacy map names one test
        and would under-require a multi-test case. Pins anchor (2) ahead of anchor (3)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            node_safe = "component__demo_hybrid__0.1.0"
            ir_ref = f"workspace/ir/{node_safe}/demo-hybrid_20260709_001"
            pipeline_dir = (repo_root / "workspace" / "pipelines" / node_safe
                            / "demo-hybrid_20260709_001")
            schema = {
                "variables": [{"name": "U_L", "shape_expr": "[3]"},
                              {"name": "F_star", "shape_expr": "[3]"}],
                "time_variable": "t",
                "time_shape_expr": "scalar",
            }
            self._write_target_cases_ir(
                repo_root, node_safe, ir_ref, pipeline_dir, schema=schema,
                predicates=[{"test_id": "t_a", "target_cases": ["c1"]},
                            {"test_id": "t_b", "target_cases": ["c1"]}],
                test_case_set=[{"case_id": "c1", "test_id": "t_legacy"}],
                evidence=[{"test_id": "t_legacy", "required_raw_variables": ["U_L"]},
                          {"test_id": "t_a", "required_raw_variables": ["U_L"]},
                          {"test_id": "t_b", "required_raw_variables": ["U_L", "F_star"]}],
            )
            node_dir = pipeline_dir / "runs" / "run_test_001" / node_safe
            snapshots_dir = node_dir / "raw" / "state_snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            _write_json(snapshots_dir / "snapshot_schema.json", {
                **schema, "min_samples": 1, "samples": ["c1.json"]})
            # Omits F_star, which the union {t_a, t_b} requires but the legacy t_legacy does not.
            _write_json(snapshots_dir / "c1.json", {
                "t": 0.0, "case_id": "c1", "U_L": [1.0, 2.0, 3.0]})

            execution = NodeExecution(
                node_key="component/demo_hybrid@0.1.0", node_dir=node_dir,
                exec_dir=pipeline_dir / "runs" / "run_test_001",
                pipeline_dir=pipeline_dir)
            violations: list[str] = []
            vps._validate_raw_evidence(repo_root, execution, violations)
            missing_state = [v for v in violations
                             if "declared state_variables missing in snapshot files" in v]
            self.assertTrue(missing_state, "the union must win over the legacy single-test map")
            self.assertIn("F_star", missing_state[0])

    def test_snapshot_scope_empty_union_relaxation_requires_predicates(self) -> None:
        """The empty-union relaxation is confined to the predicate-driven world by the
        `case_to_tests` guard. A legacy IR that declares `test_evidence_requirements` but NO
        `test_predicates` must keep the strict all-declared requirement for a declared case
        it cannot otherwise scope — dropping the guard would silently accept a snapshot that
        omits variables a test really requires."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            node_safe = "component__demo_nopred__0.1.0"
            ir_ref = f"workspace/ir/{node_safe}/demo-nopred_20260709_001"
            pipeline_dir = (repo_root / "workspace" / "pipelines" / node_safe
                            / "demo-nopred_20260709_001")
            schema = {
                "variables": [{"name": "U_L", "shape_expr": "[3]"},
                              {"name": "F_star", "shape_expr": "[3]"}],
                "time_variable": "t",
                "time_shape_expr": "scalar",
            }
            self._write_target_cases_ir(
                repo_root, node_safe, ir_ref, pipeline_dir, schema=schema,
                predicates=None,  # no test_predicates at all
                test_case_set=[{"case_id": "case_equal_state"}],
                evidence=[{"test_id": "t_a", "required_raw_variables": ["U_L", "F_star"]}],
            )
            node_dir = pipeline_dir / "runs" / "run_test_001" / node_safe
            snapshots_dir = node_dir / "raw" / "state_snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            _write_json(snapshots_dir / "snapshot_schema.json", {
                **schema, "min_samples": 1, "samples": ["case_equal_state.json"]})
            _write_json(snapshots_dir / "case_equal_state.json", {
                "t": 0.0, "case_id": "case_equal_state", "U_L": [1.0, 2.0, 3.0]})

            execution = NodeExecution(
                node_key="component/demo_nopred@0.1.0", node_dir=node_dir,
                exec_dir=pipeline_dir / "runs" / "run_test_001",
                pipeline_dir=pipeline_dir)
            violations: list[str] = []
            vps._validate_raw_evidence(repo_root, execution, violations)
            missing_state = [v for v in violations
                             if "declared state_variables missing in snapshot files" in v]
            self.assertTrue(missing_state, "without predicates the strict requirement stands")
            self.assertIn("F_star", missing_state[0])

    def test_snapshot_scope_legacy_mapping_outranks_the_empty_union(self) -> None:
        """The empty-union relaxation is ordered LAST. A case that no predicate ranges over but
        that the legacy `case.test_case_set[].test_id` map does resolve must take the legacy
        test's required set, not the empty union. Pins anchor (4) behind anchor (3)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            node_safe = "component__demo_mixed__0.1.0"
            ir_ref = f"workspace/ir/{node_safe}/demo-mixed_20260709_001"
            pipeline_dir = (repo_root / "workspace" / "pipelines" / node_safe
                            / "demo-mixed_20260709_001")
            schema = {
                "variables": [{"name": "U_L", "shape_expr": "[3]"},
                              {"name": "F_star", "shape_expr": "[3]"}],
                "time_variable": "t",
                "time_shape_expr": "scalar",
            }
            self._write_target_cases_ir(
                repo_root, node_safe, ir_ref, pipeline_dir, schema=schema,
                # c_legacy is declared and carries a legacy test_id, but no predicate targets it.
                predicates=[{"test_id": "t_a", "target_cases": ["c_used"]}],
                test_case_set=[{"case_id": "c_used"},
                               {"case_id": "c_legacy", "test_id": "t_b"}],
                evidence=[{"test_id": "t_a", "required_raw_variables": ["U_L"]},
                          {"test_id": "t_b", "required_raw_variables": ["U_L", "F_star"]}],
            )
            node_dir = pipeline_dir / "runs" / "run_test_001" / node_safe
            snapshots_dir = node_dir / "raw" / "state_snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            _write_json(snapshots_dir / "snapshot_schema.json", {
                **schema, "min_samples": 1, "samples": ["c_used.json", "c_legacy.json"]})
            _write_json(snapshots_dir / "c_used.json", {
                "t": 0.0, "case_id": "c_used", "U_L": [1.0, 2.0, 3.0]})
            # c_legacy omits F_star, which its legacy test t_b requires.
            _write_json(snapshots_dir / "c_legacy.json", {
                "t": 0.0, "case_id": "c_legacy", "U_L": [1.0, 2.0, 3.0]})

            execution = NodeExecution(
                node_key="component/demo_mixed@0.1.0", node_dir=node_dir,
                exec_dir=pipeline_dir / "runs" / "run_test_001",
                pipeline_dir=pipeline_dir)
            violations: list[str] = []
            vps._validate_raw_evidence(repo_root, execution, violations)
            missing_state = [v for v in violations
                             if "declared state_variables missing in snapshot files" in v]
            self.assertTrue(missing_state, "the legacy mapping must win over the empty union")
            self.assertIn("c_legacy.json", missing_state[0])
            self.assertIn("F_star", missing_state[0])
            self.assertNotIn("c_used.json", missing_state[0])

    def test_snapshot_scope_targeted_case_whose_tests_declare_no_evidence(self) -> None:
        """A predicate whose `test_id` has no `test_evidence_requirements` entry contributes
        nothing to the union. The renderer likewise emits no state for it, so the gate must not
        raise (the `t in per_test_required` filter) nor demand every declared variable."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            node_safe = "component__demo_noevidence__0.1.0"
            ir_ref = f"workspace/ir/{node_safe}/demo-noevidence_20260709_001"
            pipeline_dir = (repo_root / "workspace" / "pipelines" / node_safe
                            / "demo-noevidence_20260709_001")
            schema = {
                "variables": [
                    {"name": "U_L", "shape_expr": "[3]"},
                    {"name": "F_star", "shape_expr": "[3]"},
                    {"name": "guard_fired", "shape_expr": "scalar"},
                ],
                "time_variable": "t",
                "time_shape_expr": "scalar",
            }
            self._write_target_cases_ir(
                repo_root, node_safe, ir_ref, pipeline_dir, schema=schema,
                predicates=[
                    {"test_id": "t_a", "target_cases": ["case_equal_state"]},
                    # t_missing ranges over the case but declares no evidence requirement.
                    {"test_id": "t_missing", "target_cases": ["case_equal_state"]},
                ],
                evidence=[{"test_id": "t_a", "required_raw_variables": ["U_L"]}],
            )
            node_dir = pipeline_dir / "runs" / "run_test_001" / node_safe
            snapshots_dir = node_dir / "raw" / "state_snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            _write_json(snapshots_dir / "snapshot_schema.json", {
                **schema, "min_samples": 1, "samples": ["case_equal_state.json"]})
            _write_json(snapshots_dir / "case_equal_state.json", {
                "t": 0.0, "case_id": "case_equal_state", "U_L": [1.0, 2.0, 3.0]})

            execution = NodeExecution(
                node_key="component/demo_noevidence@0.1.0", node_dir=node_dir,
                exec_dir=pipeline_dir / "runs" / "run_test_001",
                pipeline_dir=pipeline_dir)
            violations: list[str] = []
            vps._validate_raw_evidence(repo_root, execution, violations)  # must not raise
            missing_state = [v for v in violations
                             if "declared state_variables missing in snapshot files" in v]
            self.assertFalse(missing_state, f"scoped to t_a only; got: {missing_state}")

    def test_case_id_to_test_ids_reads_the_unflattened_io_contract(self) -> None:
        """`_io_contract_for_execution` hoists `test_predicates` out of the nested section, but a
        doc that was never flattened still nests it; both shapes must resolve."""
        predicates = [{"test_id": "t_a", "target_cases": ["c1", "c2"]},
                      {"test_id": "t_b", "target_cases": ["c2"]}]
        expected = {"c1": ["t_a"], "c2": ["t_a", "t_b"]}
        self.assertEqual(vps._case_id_to_test_ids({"test_predicates": predicates}), expected)
        self.assertEqual(
            vps._case_id_to_test_ids({"io_contract": {"test_predicates": predicates}}), expected)
        # Malformed entries are ignored rather than raising.
        self.assertEqual(vps._case_id_to_test_ids({"test_predicates": "nope"}), {})
        self.assertEqual(vps._case_id_to_test_ids({}), {})
        self.assertEqual(
            vps._case_id_to_test_ids({"test_predicates": [
                "not-a-dict",
                {"test_id": "", "target_cases": ["c1"]},
                {"test_id": "t_x", "target_cases": [" ", 3, None]},
                {"test_id": "t_y"},
            ]}),
            {},
        )
        # Identifiers are normalized, and a test naming the same case twice is recorded once.
        self.assertEqual(
            vps._case_id_to_test_ids({"test_predicates": [
                {"test_id": " t_a ", "target_cases": [" c1 ", "c1"]},
                {"test_id": "t_a", "target_cases": ["c1"]},
            ]}),
            {"c1": ["t_a"]},
        )

    def test_snapshot_scope_target_cases_takes_the_union_and_stays_strict(self) -> None:
        """A case ranged over by SEVERAL tests requires the union of their raw variables
        (what `_per_case_vars` emits) — and omitting one of them is still a violation, so the
        anchor narrows the required set without weakening it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            node_safe = "component__demo_tcu__0.1.0"
            ir_ref = f"workspace/ir/{node_safe}/demo-tcu_20260709_001"
            pipeline_dir = (repo_root / "workspace" / "pipelines" / node_safe
                            / "demo-tcu_20260709_001")
            schema = {
                "variables": [
                    {"name": "U_L", "shape_expr": "[3]"},
                    {"name": "F_star", "shape_expr": "[3]"},
                    {"name": "guard_fired", "shape_expr": "scalar"},
                ],
                "time_variable": "t",
                "time_shape_expr": "scalar",
            }
            # Two tests both range over case_equal_state -> union {U_L, F_star, guard_fired}.
            self._write_target_cases_ir(
                repo_root, node_safe, ir_ref, pipeline_dir, schema=schema,
                predicates=[
                    {"test_id": "t_a", "target_cases": ["case_equal_state"]},
                    {"test_id": "t_b", "target_cases": ["case_equal_state"]},
                    {"test_id": "t_c", "target_cases": ["case_dry_state"]},
                ],
                evidence=[
                    {"test_id": "t_a", "required_raw_variables": ["U_L", "F_star"]},
                    {"test_id": "t_b", "required_raw_variables": ["U_L", "guard_fired"]},
                    {"test_id": "t_c", "required_raw_variables": ["U_L"]},
                ],
            )
            node_dir = pipeline_dir / "runs" / "run_test_001" / node_safe
            snapshots_dir = node_dir / "raw" / "state_snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            _write_json(snapshots_dir / "snapshot_schema.json", {
                **schema, "min_samples": 1,
                "samples": ["case_equal_state.json", "case_dry_state.json"]})
            # Union member `guard_fired` omitted -> must be flagged.
            _write_json(snapshots_dir / "case_equal_state.json", {
                "t": 0.0, "case_id": "case_equal_state",
                "U_L": [1.0, 2.0, 3.0], "F_star": [0.5, 0.5, 0.5]})
            # Its own test needs only U_L; omitting F_star/guard_fired is legitimate.
            _write_json(snapshots_dir / "case_dry_state.json", {
                "t": 0.0, "case_id": "case_dry_state", "U_L": [0.0, 0.5, 0.25]})

            execution = NodeExecution(
                node_key="component/demo_tcu@0.1.0", node_dir=node_dir,
                exec_dir=pipeline_dir / "runs" / "run_test_001",
                pipeline_dir=pipeline_dir)
            violations: list[str] = []
            vps._validate_raw_evidence(repo_root, execution, violations)
            missing_state = [v for v in violations
                             if "declared state_variables missing in snapshot files" in v]
            self.assertTrue(missing_state, "the missing union member must be flagged")
            self.assertIn("case_equal_state.json", missing_state[0])
            self.assertIn("guard_fired", missing_state[0])
            self.assertNotIn("case_dry_state.json", missing_state[0])

    def test_snapshot_completeness_falls_back_to_strict_union_without_per_test_contract(self) -> None:
        """Backward-compat: when the IR carries no per-test evidence scoping
        (`io_contract.test_evidence_requirements`) and/or no `case.test_case_set`
        mapping, the snapshot completeness gate falls back to requiring EVERY
        declared schema variable in every snapshot file. A snapshot omitting a
        declared variable must still be flagged — the per-case fix must not
        silently weaken the gate for specs that don't declare per-test evidence.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            node_safe = "component__demo_strict__0.1.0"
            ir_ref = f"workspace/ir/{node_safe}/demo-strict_20260625_001"
            pipeline_dir = (
                repo_root / "workspace" / "pipelines" / node_safe
                / "demo-strict_20260625_001"
            )
            ir_dir = repo_root / ir_ref
            pipeline_dir.mkdir(parents=True, exist_ok=True)
            ir_dir.mkdir(parents=True, exist_ok=True)
            _write_json(pipeline_dir / "lineage.json", {"ir_ref": ir_ref})

            schema = {
                "variables": [
                    {"name": "x", "shape_expr": "[n]"},
                    {"name": "y", "shape_expr": "[n]"},
                ],
                "time_variable": "snapshot_index",
                "time_shape_expr": "scalar",
            }
            # No `case` section and no `test_evidence_requirements` -> no per-case
            # scoping is resolvable, so the strict union {x, y} applies.
            _write_json(
                ir_dir / "spec.ir.yaml",
                {
                    "io_contract": {
                        "inputs": [{"name": "x", "shape_expr": "[n]"}],
                        "outputs": [{"name": "y", "shape_expr": "[n]"}],
                        "raw_requirements": {
                            "required_evidence": [
                                {
                                    "artifact": "state_snapshots",
                                    "required": True,
                                    "min_samples": 1,
                                    "schema": schema,
                                }
                            ]
                        },
                    },
                },
            )

            node_dir = pipeline_dir / "runs" / "run_test_001" / node_safe
            snapshots_dir = node_dir / "raw" / "state_snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            _write_json(snapshots_dir / "snapshot_schema.json", {
                **schema, "min_samples": 1, "samples": ["c_only.json"],
            })
            # Snapshot omits the declared output `y`.
            _write_json(snapshots_dir / "c_only.json",
                        {"snapshot_index": 0, "case_id": "c_only", "x": [1.0]})

            execution = NodeExecution(
                node_key="component/demo_strict@0.1.0",
                node_dir=node_dir,
                exec_dir=pipeline_dir / "runs" / "run_test_001",
                pipeline_dir=pipeline_dir,
            )
            violations: list[str] = []
            vps._validate_raw_evidence(repo_root, execution, violations)

            self.assertTrue(
                any(
                    "declared state_variables missing in snapshot files" in v
                    and "c_only.json" in v
                    for v in violations
                ),
                f"strict fallback should flag the absent y; got: {violations}",
            )

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

    # --- cross-pipeline dependency (the --with-deps model): a closure node absent from the
    # current validation scope but BUILT in its own pipeline is DAG-satisfied (token-less branch). ---

    _XP_MODEL = """module shallow_water2d_model
use dynamics_shallow_water_flux_2d_rusanov_p0_model
implicit none
contains
subroutine solve(flag)
  logical, intent(out) :: flag
  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)
end subroutine solve
end module shallow_water2d_model
"""
    _XP_RUNNER = """program shallow_water2d_runner
implicit none
write(*,*) 'diagnostics only'
end program shallow_water2d_runner
"""
    _XP_DEP_RESOLVED = {
        "node_key": "problem/shallow_water2d@0.3.0",
        "direct_deps": ["component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0"],
        "transitive_deps": ["component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0"],
        "topo_level": 1,
        # NOTE: no `resolved_at` -> token-less "validation scope" branch (the one relaxed).
        "all_nodes": [
            {"node_key": "component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0"},
            {"node_key": "problem/shallow_water2d@0.3.0"},
        ],
    }

    _XP_DEP_SAFE = "component__dynamics_shallow_water_flux_2d_rusanov_p0__0.1.0"

    @classmethod
    def _seed_built_dep_pipeline(cls, repo_root: Path, *, binary_status: str = "pass",
                                 verdict: str | None = "pass", binary_id: str = "bin_20260415_001",
                                 verdict_binary_id: str | None = None) -> None:
        """Seed the dependency's OWN pipeline. A genuinely-completed dep needs a passing
        binary_meta AND a pass/xfail aggregate_verdict whose sibling trial_meta.json binds it
        (source_binary_id) to that SAME passing binary. `verdict=None` models a half-built
        (binary only, never validated) leftover; `verdict_binary_id` overrides the binding target
        to model cross-run mixing (verdict bound to a different/absent binary)."""
        pipe = (repo_root / "workspace" / "pipelines" / cls._XP_DEP_SAFE / "flux_20260415_001")
        shutil.rmtree(pipe, ignore_errors=True)  # reset so each call is a clean, well-defined state
        bm = pipe / "binary" / binary_id / "binary_meta.json"
        bm.parent.mkdir(parents=True, exist_ok=True)
        bm.write_text(json.dumps({"verification_status": binary_status}), encoding="utf-8")
        if verdict is not None:
            run_node = pipe / "runs" / "run_20260415_001" / cls._XP_DEP_SAFE
            run_node.mkdir(parents=True, exist_ok=True)
            (run_node / "aggregate_verdict.json").write_text(
                json.dumps({"aggregate_verdict": verdict}), encoding="utf-8")
            (run_node / "trial_meta.json").write_text(
                json.dumps({"source_binary_id": verdict_binary_id or binary_id}), encoding="utf-8")

    def test_cross_pipeline_built_dependency_excused_from_dag_scope(self) -> None:
        # --with-deps runs the dependency as a SEPARATE pipeline; it is not in the dependent's
        # validation scope, but it IS built in its own pipeline -> no DAG-incomplete violation.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=self._XP_MODEL, runner_text=self._XP_RUNNER,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                dependency_resolved=dict(self._XP_DEP_RESOLVED),
            )
            self._seed_built_dep_pipeline(repo_root)
            pipeline_root = (repo_root / "workspace" / "pipelines"
                             / "problem__shallow_water2d__0.3.0" / "shallow-water2d_20260415_001")
            violations = validate(repo_root=repo_root, workspace_root="workspace",
                                  pipeline_roots=[pipeline_root])
            self.assertFalse(any("dependency DAG incomplete" in v for v in violations), violations)
            self.assertFalse(any("not issued for validation scope" in v for v in violations), violations)

    def test_cross_pipeline_unbuilt_dependency_still_flagged(self) -> None:
        # Same token-less setup but the dependency has NO built pipeline anywhere -> the DAG
        # relaxation must NOT excuse it (a genuinely-missing dependency still fails).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text=self._XP_MODEL, runner_text=self._XP_RUNNER,
                run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                dependency_resolved=dict(self._XP_DEP_RESOLVED),
            )
            # no _seed_built_dep_pipeline
            pipeline_root = (repo_root / "workspace" / "pipelines"
                             / "problem__shallow_water2d__0.3.0" / "shallow-water2d_20260415_001")
            violations = validate(repo_root=repo_root, workspace_root="workspace",
                                  pipeline_roots=[pipeline_root])
            self.assertTrue(any("dependency DAG incomplete" in v for v in violations), violations)

    def test_cross_pipeline_helper_requires_full_validated_chain(self) -> None:
        # The helper excuses a node ONLY when its own pipeline is fully built+validated. A
        # non-pass binary, a binary-only (never-validated) pipeline, a non-pass verdict, a
        # missing node, and a traversal token must all be rejected.
        from tools.validate_pipeline_semantics import _closure_node_validated_in_own_pipeline as ok
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            tok = "component/dynamics_shallow_water_flux_2d_rusanov_p0"
            self.assertFalse(ok(repo_root, tok))  # nothing yet
            self._seed_built_dep_pipeline(repo_root, binary_status="fail", verdict="pass")
            self.assertFalse(ok(repo_root, tok))  # binary fail -> not validated
            self._seed_built_dep_pipeline(repo_root, binary_status="pass", verdict=None)
            self.assertFalse(ok(repo_root, tok))  # binary pass but NO verdict (half-built leftover)
            self._seed_built_dep_pipeline(repo_root, binary_status="pass", verdict="fail")
            self.assertFalse(ok(repo_root, tok))  # verdict fail -> not validated
            self._seed_built_dep_pipeline(repo_root, binary_status="pass", verdict="pass")
            self.assertTrue(ok(repo_root, tok))  # full built+validated chain (verdict bound to the binary)
            # cross-run mixing: passing binary present, but the verdict is bound (source_binary_id)
            # to a DIFFERENT binary that is not a passing binary here -> must NOT excuse.
            self._seed_built_dep_pipeline(repo_root, binary_status="pass", verdict="pass",
                                          binary_id="bin_20260415_002",
                                          verdict_binary_id="bin_absent_other")
            self.assertFalse(ok(repo_root, tok))
            # path-traversal guard
            self.assertFalse(ok(repo_root, "component/../etc"))

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

    def test_detects_wrong_review_method_literal(self) -> None:
        # Drift guard for the doc claim (SKILL.md / phase_04_validate.md): the gate
        # requires the EXACT literal `review_method: "llm_semantic_review"`. A billed E2E
        # once failed fail_closed because the judge wrote "llm_semantic_recompute". If this
        # literal ever changes in the gate, this test forces the docs to be updated too.
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
            review = json.loads(review_path.read_text())
            review["review_method"] = "llm_semantic_recompute"
            review_path.write_text(json.dumps(review))

            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("review_method must be llm_semantic_review" in v for v in violations))

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
            # generate/<gen>/src/command_log.jsonl. Append to the existing
            # canonical log written by the fixture.
            qc_log_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/source/src_20260415_001/src/"
                "command_log.jsonl"
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
                "command_log.jsonl"
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
                "command_log.jsonl"
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
            outside_log = repo_root / "command_log.jsonl"
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
            trial_meta["source_command_ref"]["run_threads_1"]["command_log_ref"] = "command_log.jsonl"
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
            tests_md = repo_root / MOCK_TESTS_REF
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
                            "case_id": "case_a",
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
            _seed_metrics_basis_per_test_tree(
                repo_root,
                {
                    "per_test": [
                        {
                            "test_id": "test_a",
                            "case_id": "case_a",
                            "raw_variables": {
                                "h": [[1.0, 1.0], [1.0, 1.0]],
                            },
                        }
                    ]
                },
            )
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("metrics_basis.json: test_id test_a case_id case_a missing required_raw_variables (['time'])" in v for v in violations)
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

    def test_snapshot_time_shape_expr_must_be_scalar(self) -> None:
        """C1: the per-snapshot time index is canonically a scalar loop counter; a
        non-scalar `time_shape_expr` (e.g. "[1]") is rejected at the compile io_contract
        gate so the IR regenerates to scalar instead of failing post_execute."""
        from tools.validate_pipeline_semantics import (
            _active_repo_root_for_schema,
            _validate_io_contract_file,
        )

        def _contract(time_shape_expr: str) -> dict:
            return {
                "io_contract": {
                    "inputs": [{"name": "case", "evidence_ref": "spec.ir.yaml"}],
                    "outputs": [
                        {
                            "name": "U",
                            "shape_expr": "[2,2]",
                            "evidence_ref": "raw/state_snapshots",
                            "raw_variables": ["h"],
                        }
                    ],
                },
                "raw_requirements": {
                    "required_evidence": [
                        {
                            "artifact": "state_snapshots",
                            "required": True,
                            "min_samples": 1,
                            "schema": {
                                "variables": [{"name": "h", "shape_expr": "[2,2]"}],
                                "time_variable": "snapshot_index",
                                "time_shape_expr": time_shape_expr,
                            },
                        }
                    ]
                },
            }

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            token = _active_repo_root_for_schema.set(repo_root)
            try:
                bad_path = repo_root / "bad_contract.json"
                _write_json(bad_path, _contract("[1]"))
                bad_violations: list[str] = []
                _validate_io_contract_file(repo_root, bad_path, bad_violations)
                self.assertTrue(
                    any(
                        "time_shape_expr" in v and 'must be "scalar"' in v
                        for v in bad_violations
                    ),
                    f"expected a scalar violation, got: {bad_violations}",
                )

                good_path = repo_root / "good_contract.json"
                _write_json(good_path, _contract("scalar"))
                good_violations: list[str] = []
                _validate_io_contract_file(repo_root, good_path, good_violations)
                self.assertFalse(
                    any("time_shape_expr" in v and "scalar" in v for v in good_violations),
                    f"scalar must not be flagged, got: {good_violations}",
                )
            finally:
                _active_repo_root_for_schema.reset(token)

    def test_runner_source_name_must_match_spec_id(self) -> None:
        """B2 (cosmetic): a runner whose basename is not `<spec_id>_runner.f90` is
        flagged with a clear violation, matching generate's write-authorization. The
        correctly-named runner is not flagged."""
        from tools.validate_pipeline_semantics import (
            NodeExecution,
            _validate_runner_source_files,
        )
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp)
            execution = NodeExecution(
                node_key="problem/shallow_water2d@0.3.0",
                node_dir=src_dir,
                exec_dir=src_dir,
                pipeline_dir=src_dir,
            )
            wrong = src_dir / "wrong_runner.f90"
            wrong.write_text("program wrong_runner\nend program wrong_runner\n", encoding="utf-8")
            bad: list[str] = []
            _validate_runner_source_files(execution, [wrong], bad)
            self.assertTrue(
                any("must be named shallow_water2d_runner.f90" in v for v in bad),
                f"expected a runner-name violation, got: {bad}",
            )

            wrong.unlink()
            right = src_dir / "shallow_water2d_runner.f90"
            right.write_text(
                "program shallow_water2d_runner\nend program shallow_water2d_runner\n",
                encoding="utf-8",
            )
            good: list[str] = []
            _validate_runner_source_files(execution, [right], good)
            self.assertFalse(
                any("must be named" in v for v in good),
                f"correctly-named runner must not be flagged, got: {good}",
            )

    def test_runner_snapshot_filename_must_be_per_case(self) -> None:
        """D4: a hardcoded raw/state_snapshots/<name>.json literal (e.g.
        snapshot_0001.json) is flagged at post_generate; a per-case name built from
        the case_id (trim(case_id)//'.json'), the conductor-authored
        snapshot_schema.json, and a hardcoded literal that matches a declared
        case_id are not. The runtime deliverable gate is the deterministic
        backstop, so this static check stays conservative."""
        from tools.validate_pipeline_semantics import (
            _validate_runner_snapshot_filenames,
        )
        runner = Path("x_runner.f90")

        def run(src: str, case_ids: set[str] | None = None) -> list[str]:
            out: list[str] = []
            _validate_runner_snapshot_filenames(runner, src.lower(), out, case_ids)
            return out

        # Hardcoded sequential name, no per-case construction -> flagged.
        bad = run(
            "open(unit=10, file='raw/state_snapshots/snapshot_0001.json', "
            "status='replace')\n"
        )
        self.assertTrue(
            any("hardcoded snapshot filename" in v and "snapshot_0001.json" in v
                for v in bad),
            f"expected a hardcoded-snapshot violation, got: {bad}",
        )

        # Name built from the case_id argv -> not flagged.
        self.assertFalse(
            run("open(unit=10, file='raw/state_snapshots/'//trim(case_id)//'.json')\n"),
            "per-case snapshot name must not be flagged",
        )

        # Conductor-authored schema metadata is exempt.
        self.assertFalse(
            run("open(unit=11, file='raw/state_snapshots/snapshot_schema.json')\n"),
            "snapshot_schema.json must be exempt",
        )

        # A non-snapshot output (diagnostics) is ignored.
        self.assertFalse(
            run("open(unit=12, file='diagnostics.json')\n"),
            "non-snapshot open must be ignored",
        )

        # The literal in a non-open/non-file= line (e.g. written as content) is
        # ignored: the open(/file= gating suppresses the false positive.
        self.assertFalse(
            run("write(u, '(a)') 'raw/state_snapshots/snapshot_0001.json'\n"),
            "a snapshot path written as content (not a file= target) must be ignored",
        )

        # A continuation-split open is merged before matching.
        self.assertTrue(
            any("c1.json" in v for v in
                run("open(unit=13, &\n  file='raw/state_snapshots/c1.json')\n")),
            "continuation-split hardcoded snapshot must be flagged",
        )

        # A hardcoded literal whose stem IS a declared case_id is NOT a false
        # positive (it satisfies the per-case deliverable gate).
        self.assertFalse(
            run("open(unit=14, file='raw/state_snapshots/l0_pass.json')\n",
                {"l0_pass", "l0_xfail"}),
            "a hardcoded name matching a declared case_id must not be flagged",
        )
        # ...but a non-matching hardcoded literal still is, even with case_ids known.
        self.assertTrue(
            run("open(unit=14, file='raw/state_snapshots/snapshot_0001.json')\n",
                {"l0_pass", "l0_xfail"}),
            "a hardcoded name not matching any case_id must be flagged",
        )

    def test_detects_unknown_required_raw_variables_from_tests_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            tests_path = repo_root / MOCK_TESTS_REF
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
        seed_foreign_crashed: bool = False,
        current_orchestration_id: str | None = None,
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
        if seed_foreign_crashed:
            # A second, UNRELATED orchestration left as debris by a prior/crashed run:
            # well-formed except for a dangling agent_graph edge (a child present in
            # agent_graph.json but absent from agent_runs.jsonl). The conductor runs one
            # orchestration per node, so such debris accumulates under
            # workspace/orchestrations/ and previously failed a fresh healthy run.
            _create_minimal_orchestration_tree(
                repo_root, orchestration_id="orch_foreign_001"
            )
            foreign_runs = (
                repo_root / "workspace" / "orchestrations" / "orch_foreign_001"
                / "agent_runs.jsonl"
            )
            foreign_items = [
                json.loads(line)
                for line in foreign_runs.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            foreign_items = [
                it for it in foreign_items
                if it.get("agent_run_id") != "step_run_build_001"
            ]
            foreign_runs.write_text(
                "\n".join(json.dumps(it, ensure_ascii=False) for it in foreign_items)
                + "\n",
                encoding="utf-8",
            )
        return validate(
            repo_root=repo_root,
            workspace_root="workspace",
            require_orchestration=True,
            in_flight_agent_run_ids=set(in_flight_arids) if in_flight_arids else None,
            current_orchestration_id=current_orchestration_id,
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

    def test_pre_judge_unscoped_scan_fails_on_foreign_crashed_orchestration(self) -> None:
        """Phase-4 D2 (legacy/unscoped behavior): without --orchestration-id the
        cross-orchestration integrity scan covers ALL orchestrations, so an unrelated
        crashed orchestration's dangling agent_graph edge fails a healthy run."""
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._violations_with_removed_child(
                Path(tmp),
                seed_foreign_crashed=True,
                current_orchestration_id=None,
            )
            self.assertTrue(
                any("orch_foreign_001" in v for v in violations),
                msg=(
                    "unscoped scan must surface the foreign orchestration's debris; "
                    f"got: {violations}"
                ),
            )

    def test_pre_judge_scoped_ignores_foreign_crashed_orchestration(self) -> None:
        """Phase-4 D2: --orchestration-id scopes the integrity scan to the run being
        judged, so an unrelated crashed orchestration's dangling edge no longer fails
        the healthy current run. The current orchestration is itself well-formed, so
        the scoped validation reports no violation referencing the foreign debris."""
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._violations_with_removed_child(
                Path(tmp),
                seed_foreign_crashed=True,
                current_orchestration_id="orch_test_001",
            )
            self.assertFalse(
                any("orch_foreign_001" in v for v in violations),
                msg=(
                    "scoping to the current orchestration must ignore foreign debris; "
                    f"got: {violations}"
                ),
            )
            # Stronger: scoping must not silently drop the current node's own checks.
            # The current orchestration is well-formed, so a correctly-scoped scan is
            # fully clean (no dropped/false violations of any kind).
            self.assertEqual(
                violations, [],
                msg=f"scoped validation of a healthy current run must be clean; got: {violations}",
            )

    def test_pre_judge_scoped_missing_current_orchestration_fails_closed(self) -> None:
        """Phase-4 D2: a --orchestration-id naming a non-existent orchestration must
        fail closed (the run being judged must be present), not silently pass."""
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._violations_with_removed_child(
                Path(tmp),
                current_orchestration_id="orch_does_not_exist",
            )
            self.assertTrue(
                any(
                    "orch_does_not_exist" in v and "not found" in v
                    for v in violations
                ),
                msg=f"missing current orchestration must fail closed; got: {violations}",
            )

    def test_pre_judge_scoped_narrows_node_safes_even_when_own_steps_empty(self) -> None:
        """Phase-4 D2 regression: node_safes must be intersected with the scoped
        orchestration's own nodes UNCONDITIONALLY. If the scoped orchestration has no
        steps dir, leaving node_safes unnarrowed would retain the OTHER executions'
        nodes (e.g. dependencies) and demand their step_results of this single scoped
        dir — defeating the scoping. The orchestration genuinely missing its steps must
        still fail closed via the per-orchestration "steps_root: missing" check, but
        must NOT emit a per-node "missing step_result.json for [...]" that lists nodes
        this orchestration does not own."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._violations_with_removed_child(repo_root)  # build healthy tree
            shutil.rmtree(
                repo_root / "workspace" / "orchestrations" / "orch_test_001" / "steps"
            )
            violations = validate(
                repo_root=repo_root,
                workspace_root="workspace",
                require_orchestration=True,
                current_orchestration_id="orch_test_001",
            )
            # Still fails closed on the genuinely-missing steps dir...
            self.assertTrue(
                any(v.endswith("/steps: missing") for v in violations),
                msg=f"missing steps dir must fail closed; got: {violations}",
            )
            # ...but must NOT demand step_results for nodes not owned by this orch.
            self.assertFalse(
                any("missing step_result.json for" in v for v in violations),
                msg=(
                    "scoping must narrow node_safes to the orchestration's own nodes "
                    f"even when it has no steps; got: {violations}"
                ),
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
                any("launch_prompt_ref missing launch-prompt template markers" in v for v in violations)
            )

    def test_validate_compile_stage_passes_for_resolved_plan_directory(self) -> None:
        # A fully-formed IR (incl. a valid R2 test_predicates DSL + the diagnostics_contract
        # vocabulary its refs resolve against) passes the compile stage cleanly.
        with tempfile.TemporaryDirectory() as tmp:
            preds = [{"test_id": "t1", "expected_outcome": "pass", "target_cases": ["c1"],
                      "pass_when": {"all": [
                          {"ref": "verdict.overall", "op": "eq", "value": "pass"},
                          {"ref": "checks.g.status", "op": "eq", "value": "pass"}]}}]
            v = self._compile_with_io_contract(
                Path(tmp), self._io_contract_with_predicates(preds))
            self.assertEqual(v, [])

    def test_validate_compile_stage_rejects_a_traversal_case_id(self) -> None:
        # Wiring test: a traversal case_id must be rejected THROUGH the full compile stage
        # (`_validate_case_ids` is called from `_validate_compile_stage_impl`), not only when the
        # gate is invoked directly. This is the non-M3c path — the minimal tree declares no
        # infrastructure dep, so the M3c render precondition does not fire.
        preds = [{"test_id": "t1", "expected_outcome": "pass", "target_cases": ["c1"],
                  "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "pass"}]}}]
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            _create_minimal_execution_tree(
                repo_root,
                dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nimplicit none\nend module m\n",
                runner_text="program r\nimplicit none\nend program r\n",
                run_command=["x", "y"],
                io_contract=self._io_contract_with_predicates(preds),
                dependency_resolved={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "direct_deps": ["component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0"],
                    "transitive_deps": [], "topo_level": 1,
                    "all_nodes": [
                        {"node_key": "component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0",
                         "topo_level": 0},
                        {"node_key": "problem/shallow_water2d@0.3.0", "topo_level": 1}]},
            )
            ir_path = (repo_root / "workspace/ir/problem__shallow_water2d__0.3.0"
                       "/shallow-water2d_20260415_001/spec.ir.yaml")
            doc = json.loads(ir_path.read_text())
            doc["case"] = {"test_case_set": [{"case_id": "c1", "inputs": {}},
                                             {"case_id": "../../evil", "inputs": {}}]}
            ir_path.write_text(json.dumps(doc))
            v = validate_compile_stage(
                repo_root, "workspace",
                "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001")
            self.assertTrue(any("not safe tokens" in x and "raw/state_snapshots" in x for x in v), v)

    def _plant_tests_md(self, repo_root: Path, test_ids: tuple[str, ...] = ("t1",)) -> None:
        """A compile-stage fixture needs the tests.md its `meta.source_refs.tests` names: the ref is
        gated (`_validate_ir_source_refs_tests`) and the test-id pins read the file through it."""
        tests_md = repo_root / MOCK_TESTS_REF
        tests_md.parent.mkdir(parents=True, exist_ok=True)
        tests_md.write_text(
            "".join(f"### 1-{i}. `{t}`\n" for i, t in enumerate(test_ids, start=1)),
            encoding="utf-8",
        )

    def _compile_with_io_contract(
        self, repo_root: Path, io_contract: dict, *, plant_tests_md: bool = True
    ):
        _seed_shape_expr_schema_into(repo_root)
        if plant_tests_md:
            self._plant_tests_md(repo_root)
        _create_minimal_execution_tree(
            repo_root,
            dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
            model_text="module m\nimplicit none\nend module m\n",
            runner_text="program r\nimplicit none\nend program r\n",
            run_command=["x", "y"],
            io_contract=io_contract,
            dependency_resolved={
                "node_key": "problem/shallow_water2d@0.3.0",
                "direct_deps": ["component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0"],
                "transitive_deps": [],
                "topo_level": 1,
                "all_nodes": [
                    {"node_key": "component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0",
                     "topo_level": 0},
                    {"node_key": "problem/shallow_water2d@0.3.0", "topo_level": 1},
                ],
            },
        )
        # add a case block for the predicate target_cases to resolve against
        ir_path = (repo_root / "workspace/ir/problem__shallow_water2d__0.3.0"
                   "/shallow-water2d_20260415_001/spec.ir.yaml")
        doc = json.loads(ir_path.read_text())
        doc["case"] = {"test_case_set": [{"case_id": "c1", "inputs": {}}]}
        ir_path.write_text(json.dumps(doc))
        return validate_compile_stage(
            repo_root, "workspace",
            "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001")

    def _io_contract_with_predicates(self, predicates) -> dict:
        return {
            "inputs": [{"name": "case_resolved", "source": "spec.ir.yaml",
                        "evidence_ref": "spec.ir.yaml"}],
            "outputs": [{"name": "metric", "shape_expr": "scalar",
                         "evidence_ref": "raw/metrics_basis.json",
                         "raw_variables": ["h", "hu", "hv", "time"]}],
            "semantic_dependency": {"required_sources": []},
            "raw_requirements": {"required_evidence": [
                {"artifact": "metrics_basis.json", "required": True},
                {"artifact": "execution_trace.json", "required": True},
                {"artifact": "state_snapshots", "required": True, "min_samples": 1,
                 "schema": {"variables": [{"name": "h", "shape_expr": "[2,2]"},
                                          {"name": "hu", "shape_expr": "[2,2]"},
                                          {"name": "hv", "shape_expr": "[2,2]"}],
                            "time_variable": "time", "time_shape_expr": "scalar"}}]},
            "diagnostics_contract": {
                "checks": [{"id": "g"}],
                "verdict": {"required": True, "fields": ["overall", "failed_checks"]},
                "metrics": ["metrics.mass_drift_rel"]},
            # The canonical test-id set of this fixture is tests.md (planted by the compile helpers
            # at MOCK_TESTS_REF), so the evidence requirements must cover it exactly — a real IR
            # always carries both.
            "test_evidence_requirements": [
                {"test_id": "t1", "required_raw_variables": ["h", "time"]}],
            "test_predicates": predicates,
        }

    def _compile_with_state_contract(self, repo_root: Path, state_contract: object):
        """Run the full compile stage over a multidimensional problem node whose algorithm
        carries `state_contract`, so the node_key-conditioned gate is exercised through the
        real wiring (`_plan_dependency_node_key` -> `_validate_algorithm_contract_file`)."""
        _seed_shape_expr_schema_into(repo_root)
        self._plant_tests_md(repo_root)
        preds = [{"test_id": "t1", "expected_outcome": "pass", "target_cases": ["c1"],
                  "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "pass"}]}}]
        _create_minimal_execution_tree(
            repo_root,
            dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
            model_text="module m\nimplicit none\nend module m\n",
            runner_text="program r\nimplicit none\nend program r\n",
            run_command=["x", "y"],
            io_contract=self._io_contract_with_predicates(preds),
            algorithm_contract={
                "algorithm_id": "shallow_water2d_test_algorithm",
                "execution_mode": "sequence",
                "steps": [{"step_id": "compute_flux", "step_kind": "flux_compute",
                           "operation_ref": "dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux",
                           "inputs": ["h", "hu", "hv"], "outputs": ["h", "hu", "hv"]}],
                "ordering": [],
                "control_condition": [],
                "iteration_contract": {"kind": "none"},
                "update_semantics": {"mode": "in_place"},
                "temporaries": [],
                "derived_field_rules": [],
                "invariants": [],
                "splitting_policy": {"kind": "none"},
                "state_contract": state_contract,
            },
            dependency_resolved={
                "node_key": "problem/shallow_water2d@0.3.0",
                "direct_deps": ["component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0"],
                "transitive_deps": [],
                "topo_level": 1,
                "all_nodes": [
                    {"node_key": "component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0",
                     "topo_level": 0},
                    {"node_key": "problem/shallow_water2d@0.3.0", "topo_level": 1},
                ],
            },
        )
        ir_path = (repo_root / "workspace/ir/problem__shallow_water2d__0.3.0"
                   "/shallow-water2d_20260415_001/spec.ir.yaml")
        doc = json.loads(ir_path.read_text())
        doc["case"] = {"test_case_set": [{"case_id": "c1", "inputs": {}}]}
        ir_path.write_text(json.dumps(doc))
        return validate_compile_stage(
            repo_root, "workspace",
            "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001")

    def _valid_state_contract(self) -> dict:
        return {
            "state_variables": [{"name": "h", "shape_expr": "[2,2]"},
                                {"name": "hu", "shape_expr": "[2,2]"},
                                {"name": "hv", "shape_expr": "[2,2]"}],
            "required_update_paths": ["h", "hu", "hv"],
            "diagnostics_from_state": True,
            "fallback_policy": "fail_closed",
        }

    def test_compile_stage_rejects_object_form_required_update_paths(self) -> None:
        """Regression (E2E #4): Compile authored `required_update_paths` as a list of
        `{target, path}` objects. The gate demanding a list of state-variable NAMES is wired into
        the compile stage, but `_plan_dependency_node_key` read a top-level `node_key` that no real
        IR has — so the gate never fired at Compile and the violation only surfaced at the tail of
        Validate, where dev-mode cross-phase rollback kills the workflow."""
        with tempfile.TemporaryDirectory() as tmp:
            contract = self._valid_state_contract()
            contract["required_update_paths"] = [
                {"target": "h", "path": ["step_01_boundary_apply", "step_07_state_commit"]},
                {"target": "hu", "path": ["step_01_boundary_apply", "step_07_state_commit"]},
            ]
            v = self._compile_with_state_contract(Path(tmp), contract)
            self.assertTrue(
                any("state_contract.required_update_paths must be non-empty string list" in x
                    for x in v),
                f"compile stage must reject object-form required_update_paths; got: {v}",
            )

    def test_compile_stage_accepts_string_form_required_update_paths(self) -> None:
        """Negative twin: the canonical string-list form passes the compile stage cleanly, so the
        gate the previous test relies on is not simply rejecting everything."""
        with tempfile.TemporaryDirectory() as tmp:
            v = self._compile_with_state_contract(Path(tmp), self._valid_state_contract())
            self.assertEqual(v, [])

    def test_compile_stage_rejects_missing_state_contract_on_multidim_node(self) -> None:
        """The gate is reached at all only because the node_key now resolves: a 2D problem node
        with no state_contract must fail at Compile."""
        with tempfile.TemporaryDirectory() as tmp:
            v = self._compile_with_state_contract(Path(tmp), None)
            self.assertTrue(
                any("state_contract must be object for multidimensional problem node" in x
                    for x in v),
                f"expected missing-state_contract violation; got: {v}",
            )

    def _compile_with_flat_contract(self, repo_root: Path, overrides: dict):
        """The FLAT placement — the 5 contract fields as direct children of `algorithm`. This is
        what every real IR authors and what the docs mandate, so it is the shape that must be
        pinned; a suite that only ever nests them under `state_contract` tests a shape nothing
        produces."""
        contract = dict(self._valid_state_contract())
        contract.update(overrides)
        v = self._compile_with_state_contract(repo_root, None)  # seeds the tree, no nested block
        del v
        ir_path = (repo_root / "workspace/ir/problem__shallow_water2d__0.3.0"
                   "/shallow-water2d_20260415_001/spec.ir.yaml")
        doc = json.loads(ir_path.read_text())
        doc["algorithm"].pop("state_contract", None)
        doc["algorithm"].update(contract)  # direct children of `algorithm`
        ir_path.write_text(json.dumps(doc))
        return validate_compile_stage(
            repo_root, "workspace",
            "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001")

    def test_compile_stage_accepts_the_flat_contract_placement(self) -> None:
        """The canonical placement (direct children of `algorithm`) passes cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._compile_with_flat_contract(Path(tmp), {}), [])

    def test_compile_stage_rejects_object_form_in_the_flat_placement(self) -> None:
        """The E2E #4 failure verbatim: the object form authored as a direct child of `algorithm`
        (which is how the real IR carried it) must be caught at Compile."""
        with tempfile.TemporaryDirectory() as tmp:
            v = self._compile_with_flat_contract(Path(tmp), {
                "required_update_paths": [
                    {"target": "h", "path": ["step_01_boundary_apply", "step_07_state_commit"]}],
            })
            self.assertTrue(
                any("state_contract.required_update_paths must be non-empty string list" in x
                    for x in v),
                f"the flat object form must be rejected; got: {v}",
            )

    def test_compile_stage_rejects_an_empty_required_update_paths(self) -> None:
        """`all()` over an empty list is True, so `required_update_paths: []` used to pass a check
        whose own message says "non-empty" — a multidimensional problem declaring it updates no
        state at all. The hole was unreachable while the gate was dormant; it is live now."""
        with tempfile.TemporaryDirectory() as tmp:
            v = self._compile_with_flat_contract(Path(tmp), {"required_update_paths": []})
            self.assertTrue(
                any("state_contract.required_update_paths must be non-empty string list" in x
                    for x in v),
                f"an empty required_update_paths must be rejected; got: {v}",
            )

    def test_compile_stage_rejects_an_update_path_that_names_no_state_variable(self) -> None:
        """The string-list rule alone accepts any non-empty token, so a typo — or a diagnostic /
        temporary — passes as an update target. That is an update contract nothing can fulfil, and
        it would surface only at Generate, when the name resolves to nothing. Every token must name
        a declared `state_variables[]` entry."""
        with tempfile.TemporaryDirectory() as tmp:
            v = self._compile_with_flat_contract(Path(tmp), {
                "required_update_paths": ["h", "hu", "hv", "eta"],  # eta is not a state variable
            })
            self.assertTrue(
                any("required_update_paths must name declared state_variables" in x
                    and "eta" in x for x in v),
                f"an update path naming no state variable must be rejected; got: {v}",
            )

    def test_compile_stage_does_not_cascade_when_state_variables_are_invalid(self) -> None:
        """When the declared set is itself malformed, report THAT — do not also accuse every update
        path of naming nothing, which would bury the real cause under noise."""
        with tempfile.TemporaryDirectory() as tmp:
            v = self._compile_with_flat_contract(Path(tmp), {"state_variables": []})
            self.assertTrue(any("state_variables must be non-empty list" in x for x in v), v)
            self.assertFalse(
                any("must name declared state_variables" in x for x in v),
                f"the membership check must not cascade off an invalid declared set; got: {v}",
            )

    def test_update_semantics_shadows_the_flat_contract(self) -> None:
        """`_algorithm_state_contract` resolves `state_contract` -> `update_semantics` (when THAT
        holds any contract key) -> the flat direct children. So a single contract key mislaid under
        `update_semantics` makes the gate resolve the whole contract there and IGNORE perfectly
        good flat fields. This is the doc<->validator drift class that produced E2E #4, so the
        shadowing is pinned here and documented in the SKILLs, phase_01, and the 2D example."""
        with tempfile.TemporaryDirectory() as tmp:
            v = self._compile_with_flat_contract(Path(tmp), {
                # The flat fields stay correct; only `update_semantics` gains a contract key.
                "update_semantics": {"mode": "in_place", "required_update_paths": ["h"]},
            })
            self.assertTrue(
                any("state_contract.state_variables must be non-empty list" in x for x in v),
                f"update_semantics must shadow the correct flat fields; got: {v}",
            )

    def test_compile_predicate_gate_accepts_valid_dsl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Note: the ordered-op threshold must be a YAML float (a decimal point). Bare
            # exponential notation like `1e-10` is parsed by YAML 1.1 as a STRING and is
            # (correctly) rejected by the numeric-threshold gate; use `1.0e-10` / `0.1`.
            preds = [{"test_id": "t1", "expected_outcome": "pass", "target_cases": ["c1"],
                      "pass_when": {"all": [
                          {"ref": "verdict.overall", "op": "eq", "value": "pass"},
                          {"ref": "checks.g.status", "op": "eq", "value": "pass"},
                          {"ref": "metrics.mass_drift_rel", "op": "le", "value": 0.1,
                           "per_case": True}]}}]
            v = self._compile_with_io_contract(Path(tmp), self._io_contract_with_predicates(preds))
            self.assertEqual(v, [])

    def test_compile_predicate_gate_rejects_missing_predicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            io = self._io_contract_with_predicates([])
            io.pop("test_predicates")
            v = self._compile_with_io_contract(Path(tmp), io)
            self.assertTrue(any("test_predicates must be a non-empty list" in x for x in v), v)

    def test_compile_predicate_gate_rejects_unknown_case_and_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            preds = [{"test_id": "t1", "expected_outcome": "pass", "target_cases": ["nope"],
                      "pass_when": {"all": [{"ref": "checks.absent.status", "op": "eq",
                                             "value": "pass"}]}}]
            v = self._compile_with_io_contract(Path(tmp), self._io_contract_with_predicates(preds))
            self.assertTrue(any("unknown case_id" in x for x in v), v)
            self.assertTrue(any("diagnostics_contract.checks" in x for x in v), v)

    def test_compile_predicate_gate_uses_test_evidence_requirements_fallback(self) -> None:
        # F2: when tests.md does NOT resolve, the predicate gate falls back to the same-IR
        # test_evidence_requirements id set, so a predicate id absent from it is still caught.
        # The fixture must therefore withhold tests.md — planting it (the default now) resolves the
        # ref and routes the gate through its tests.md branch instead, leaving this fallback
        # unexercised. An unresolvable ref is itself a violation (_validate_ir_source_refs_tests),
        # which is why this asserts the specific fallback finding rather than the whole list.
        with tempfile.TemporaryDirectory() as tmp:
            io = self._io_contract_with_predicates(
                [{"test_id": "not_in_ter", "expected_outcome": "pass", "target_cases": ["c1"],
                  "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "pass"}]}}])
            io["test_evidence_requirements"] = [
                {"test_id": "t1", "required_raw_variables": ["h"]}]
            v = self._compile_with_io_contract(Path(tmp), io, plant_tests_md=False)
            self.assertTrue(any("unknown test_id not in tests.md" in x for x in v), v)

    def test_parse_test_ids_handles_heading_and_bullet_forms(self) -> None:
        from tools.validate_pipeline_semantics import _parse_test_ids_from_tests_md
        with tempfile.TemporaryDirectory() as tmp:
            # problem-spec heading form (a `case_id`/`N/A` heading with trailing prose is excluded)
            heading = Path(tmp) / "h.md"
            heading.write_text(
                "### 4-2. `case_id` generation rule\n"
                "### 6-1. `l1_refinement`\n### 6-2. `l0_cfl_guard_xfail`\n", encoding="utf-8")
            self.assertEqual(_parse_test_ids_from_tests_md(heading),
                             ["l1_refinement", "l0_cfl_guard_xfail"])
            # component/profile bullet form (sibling `- `pass_when`:` bullets excluded)
            bullet = Path(tmp) / "b.md"
            bullet.write_text(
                "## 6. Test definitions\n"
                "- `test_id`: `l0_scale_identity_pass`\n"
                "  - `expected_outcome`: `pass`\n"
                "- `test_id`: `l0_invalid_length_xfail`\n"
                "- `pass_when`: `checks.input_guard.pass == true`\n", encoding="utf-8")
            self.assertEqual(_parse_test_ids_from_tests_md(bullet),
                             ["l0_scale_identity_pass", "l0_invalid_length_xfail"])

    def test_compile_predicate_gate_flags_tests_md_resolved_but_empty(self) -> None:
        # Fail-closed: a resolvable tests.md that parses to 0 test_ids (unrecognized form) must
        # be a violation, not a silent fall-back to test_evidence_requirements.
        with tempfile.TemporaryDirectory() as tmp:
            io = self._io_contract_with_predicates(
                [{"test_id": "t1", "expected_outcome": "pass", "target_cases": ["c1"],
                  "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "pass"}]}}])
            _seed_shape_expr_schema_into(Path(tmp))
            _create_minimal_execution_tree(
                Path(tmp), dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                model_text="module m\nend module m\n", runner_text="program r\nend program r\n",
                run_command=["x", "y"], io_contract=io,
                dependency_resolved={
                    "node_key": "problem/shallow_water2d@0.3.0",
                    "direct_deps": ["component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0"],
                    "transitive_deps": [], "topo_level": 1,
                    "all_nodes": [
                        {"node_key": "component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0",
                         "topo_level": 0},
                        {"node_key": "problem/shallow_water2d@0.3.0", "topo_level": 1}]})
            ir_path = (Path(tmp) / "workspace/ir/problem__shallow_water2d__0.3.0"
                       "/shallow-water2d_20260415_001/spec.ir.yaml")
            doc = json.loads(ir_path.read_text())
            doc["case"] = {"test_case_set": [{"case_id": "c1", "inputs": {}}]}
            # point meta.source_refs.tests at a real file whose form yields 0 test_ids
            empty_tests = Path(tmp) / "spec" / "empty_tests.md"
            empty_tests.parent.mkdir(parents=True, exist_ok=True)
            empty_tests.write_text("## Tests\nno recognizable test-id lines here\n", encoding="utf-8")
            doc["meta"] = {"source_refs": {"tests": "spec/empty_tests.md"}}
            ir_path.write_text(json.dumps(doc))
            v = validate_compile_stage(
                Path(tmp), "workspace",
                "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001")
            self.assertTrue(any("resolved but parsed 0 test_ids" in x for x in v), v)

    def test_compile_predicate_gate_rejects_verdict_ref_when_not_required(self) -> None:
        # A verdict.* ref is only allowed when diagnostics_contract.verdict.required=true;
        # with required=false the runner is not contracted to emit verdict -> reject at Compile.
        with tempfile.TemporaryDirectory() as tmp:
            io = self._io_contract_with_predicates(
                [{"test_id": "t1", "expected_outcome": "pass", "target_cases": ["c1"],
                  "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "pass"}]}}])
            io["diagnostics_contract"]["verdict"] = {
                "required": False, "fields": ["overall", "failed_checks"]}
            v = self._compile_with_io_contract(Path(tmp), io)
            self.assertTrue(any("verdict.overall" in x for x in v), v)

    def test_compile_predicate_gate_rejects_unpinned_metric(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            preds = [{"test_id": "t1", "expected_outcome": "pass", "target_cases": ["c1"],
                      "pass_when": {"all": [{"ref": "errors.analytic_h.l2_rel_tend", "op": "le",
                                             "value": 0.2, "per_case": True}]}}]
            v = self._compile_with_io_contract(Path(tmp), self._io_contract_with_predicates(preds))
            self.assertTrue(any("diagnostics_contract.metrics" in x for x in v), v)

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
            log_path = pipeline_dir / "source" / "src_20260415_001" / "src" / "command_log.jsonl"
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
            log_path = pipeline_dir / "source" / "src_20260415_001" / "src" / "command_log.jsonl"
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

        A child agent that writes a forged command_log.jsonl at a non-
        canonical placement (e.g. <gen>/src/notes/command_log.jsonl) and
        points lint_command_ref.run_linter[].command_log_ref at it must be
        rejected by the post_generate validator. The canonical placement is
        <gen>/src/command_log.jsonl (sibling of model/runner sources).
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
                / "command_log.jsonl"
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
            # Rewrite the conductor lint evidence to point at the forged log.
            evidence_path = pipeline_dir / "lint_evidence" / "src_20260415_001.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            forged_ref = (
                "workspace/pipelines/problem__shallow_water2d__0.3.0/"
                "shallow-water2d_20260415_001/source/src_20260415_001/src/"
                "notes/command_log.jsonl"
            )
            evidence["run_linter"][0]["command_log_ref"] = forged_ref
            evidence_path.write_text(
                json.dumps(evidence, ensure_ascii=False) + "\n", encoding="utf-8"
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
            log_path = node_dir / "command_log.jsonl"
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
            log_path = node_dir / "command_log.jsonl"
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
            log_path = node_dir / "command_log.jsonl"
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
            log_path = node_dir / "command_log.jsonl"
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
        pipeline (even with a valid command_log.jsonl) must be rejected,
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
                "command_log.jsonl"
            )
            (stale_src / "command_log.jsonl").write_text(
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
                "command_log.jsonl"
            )
            (sibling_src / "command_log.jsonl").write_text(
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

    # -- source_meta sweep lineage scoping (E2E #4) ---------------------------------

    _INCIDENT_DICT_REASON = {
        "violated_convention": "inert_dependency_call",
        "target_artifact": "src/model.f90",
        "reason": "binding probe invented",
    }

    def _plant_sibling_source_meta(self, pipeline_dir: Path, source_id: str, meta: dict) -> None:
        gen_dir = pipeline_dir / "source" / source_id
        gen_dir.mkdir(parents=True, exist_ok=True)
        _write_json(gen_dir / "source_meta.json", meta)

    def _minimal_tree_pipeline_dir(self, repo_root: Path) -> Path:
        _seed_shape_expr_schema_into(repo_root)
        _create_minimal_execution_tree(
            repo_root,
            dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
            model_text="module m\nimplicit none\nend module m\n",
            runner_text="program r\nimplicit none\nend program r\n",
            run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
        )
        return (
            repo_root
            / "workspace"
            / "pipelines"
            / "problem__shallow_water2d__0.3.0"
            / "shallow-water2d_20260415_001"
        )

    def test_superseded_source_dir_dict_last_fail_reason_does_not_fail_post_execute(self) -> None:
        """E2E #4 reproduction: a SUPERSEDED source dir (left by an earlier failed Generate
        attempt) whose source_meta.last_fail_reason is a structured dict must not fail the
        run that declares a different, conformant source. The superseded dir is immutable
        under the append-only contract and a Generate reopen only rotates a fresh dir, so
        flagging it would make the failure permanently unrepairable."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            pipeline_dir = self._minimal_tree_pipeline_dir(repo_root)
            # The tree's trial_meta declares src_20260415_001; this is a stale sibling.
            self._plant_sibling_source_meta(
                pipeline_dir,
                "src_20260415_000",
                {
                    "attempt_count": 2,
                    "verification_status": "fail",
                    "last_fail_reason": self._INCIDENT_DICT_REASON,
                    "debug_mode": False,
                    "context_isolated": True,
                },
            )
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                [v for v in violations if "last_fail_reason" in v], violations
            )

    def test_declared_source_dir_dict_last_fail_reason_still_fails(self) -> None:
        """The gate is scoped, not weakened: the same violation in the source the run
        DECLARES is still reported."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            pipeline_dir = self._minimal_tree_pipeline_dir(repo_root)
            meta_path = (
                pipeline_dir / "source" / "src_20260415_001" / "source_meta.json"
            )
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["last_fail_reason"] = self._INCIDENT_DICT_REASON
            _write_json(meta_path, meta)
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                [
                    v
                    for v in violations
                    if "src_20260415_001" in v
                    and "last_fail_reason must be string or null" in v
                ],
                violations,
            )

    def test_source_meta_sweep_unscoped_call_checks_all_dirs(self) -> None:
        """The default (in_scope_source_ids=None) stays a pipeline-wide sweep: a caller that
        cannot derive a declared-source scope must not silently get a weaker gate."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_dir = Path(tmp) / "pipeline"
            self._plant_sibling_source_meta(
                pipeline_dir,
                "src_stale_001",
                {
                    "attempt_count": 1,
                    "verification_status": "fail",
                    "last_fail_reason": self._INCIDENT_DICT_REASON,
                    "debug_mode": False,
                    "context_isolated": True,
                },
            )
            violations: list[str] = []
            _validate_source_meta_json_files(pipeline_dir, violations)
            self.assertEqual(
                violations,
                [
                    f"{pipeline_dir / 'source' / 'src_stale_001' / 'source_meta.json'}"
                    ":last_fail_reason must be string or null"
                ],
            )

    def test_source_meta_scope_skips_only_undeclared_dirs(self) -> None:
        """An explicit scope strictly checks the declared dirs and skips the rest — the
        superseded dir is not even parsed (its JSON is invalid here, and that is fine)."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_dir = Path(tmp) / "pipeline"
            self._plant_sibling_source_meta(
                pipeline_dir,
                "src_declared_a",
                {
                    "attempt_count": 1,
                    "verification_status": "pass",
                    "last_fail_reason": None,
                    "debug_mode": False,
                    "context_isolated": True,
                },
            )
            self._plant_sibling_source_meta(
                pipeline_dir,
                "src_declared_b",
                {
                    "attempt_count": 1,
                    "verification_status": "fail",
                    "last_fail_reason": self._INCIDENT_DICT_REASON,
                    "debug_mode": False,
                    "context_isolated": True,
                },
            )
            superseded = pipeline_dir / "source" / "src_superseded"
            superseded.mkdir(parents=True)
            (superseded / "source_meta.json").write_text("{not json", encoding="utf-8")

            violations: list[str] = []
            _validate_source_meta_json_files(
                pipeline_dir,
                violations,
                in_scope_source_ids={"src_declared_a", "src_declared_b"},
            )
            self.assertEqual(
                violations,
                [
                    f"{pipeline_dir / 'source' / 'src_declared_b' / 'source_meta.json'}"
                    ":last_fail_reason must be string or null"
                ],
            )

    def test_source_meta_scope_unions_multiple_runs(self) -> None:
        """A full (no --run-id) validate takes the UNION of every run's declared source, so a
        second run's own source is strictly checked too; only a dir NO run references is
        skipped. This is what keeps full-mode scoping equivalent to the structural source
        check's existing semantics."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            pipeline_dir = self._minimal_tree_pipeline_dir(repo_root)
            src_root = pipeline_dir / "source"
            runs_root = pipeline_dir / "runs"
            # Run 2 declares its own (copied) source dir; both are live lineage.
            shutil.copytree(src_root / "src_20260415_001", src_root / "src_20260415_002")
            shutil.copytree(runs_root / "run_test_001", runs_root / "run_test_002")
            node_dir = (
                runs_root / "run_test_002" / "problem__shallow_water2d__0.3.0"
            )
            trial_meta_path = node_dir / "trial_meta.json"
            trial_meta = json.loads(trial_meta_path.read_text(encoding="utf-8"))
            trial_meta["source_source_id"] = "src_20260415_002"
            _write_json(trial_meta_path, trial_meta)
            # The second run's declared source violates the contract...
            meta_path = src_root / "src_20260415_002" / "source_meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["last_fail_reason"] = self._INCIDENT_DICT_REASON
            _write_json(meta_path, meta)
            # ...while an unreferenced sibling violates it too.
            self._plant_sibling_source_meta(
                pipeline_dir,
                "src_20260415_000",
                {
                    "attempt_count": 1,
                    "verification_status": "fail",
                    "last_fail_reason": self._INCIDENT_DICT_REASON,
                    "debug_mode": False,
                    "context_isolated": True,
                },
            )
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            reason_violations = [v for v in violations if "last_fail_reason" in v]
            self.assertTrue(
                [v for v in reason_violations if "src_20260415_002" in v], violations
            )
            self.assertFalse(
                [v for v in reason_violations if "src_20260415_000" in v], violations
            )

    def test_declared_source_dir_without_source_meta_is_flagged(self) -> None:
        """Scoping must not turn the sweep into a no-op: a DECLARED source dir has to carry its
        meta (a required Generate output). Only undeclared/superseded attempt dirs, where a
        missing meta says nothing, are silently skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_dir = Path(tmp) / "pipeline"
            declared = pipeline_dir / "source" / "src_declared"
            declared.mkdir(parents=True)
            superseded = pipeline_dir / "source" / "src_superseded"
            superseded.mkdir(parents=True)

            violations: list[str] = []
            _validate_source_meta_json_files(
                pipeline_dir, violations, in_scope_source_ids={"src_declared"}
            )
            self.assertEqual(
                violations, [f"{declared / 'source_meta.json'}: missing"]
            )

    def test_declared_source_meta_missing_constraint_reason_flagged(self) -> None:
        """WI-2 contract unification: the source sweep now also enforces the conditional
        constraint_reason requirement (previously only the ir_meta sweep and the runtime
        write gate did), scoped to the declared lineage."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            pipeline_dir = self._minimal_tree_pipeline_dir(repo_root)
            meta_path = (
                pipeline_dir / "source" / "src_20260415_001" / "source_meta.json"
            )
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["context_isolated"] = False
            meta.pop("constraint_reason", None)
            _write_json(meta_path, meta)
            violations = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                [
                    v
                    for v in violations
                    if "src_20260415_001" in v
                    and "constraint_reason when context_isolated=false" in v
                ],
                violations,
            )

    def _lint_evidence_fixture(self, repo_root: Path, evidence: dict | None) -> Path:
        """Build <repo>/workspace/pipelines/p/pid/source/src_x/source_meta.json and
        (optionally) the conductor lint evidence at the pipeline root. Returns meta_path."""
        pipe = repo_root / "workspace" / "pipelines" / "p" / "pid"
        gen_dir = pipe / "source" / "src_x"
        gen_dir.mkdir(parents=True)
        meta_path = gen_dir / "source_meta.json"
        if evidence is not None:
            _write_json(pipe / "lint_evidence" / "src_x.json", evidence)
        return meta_path

    def test_validate_generate_lint_rejects_pass_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            meta_path = self._lint_evidence_fixture(repo_root, None)
            violations: list[str] = []
            _validate_generate_lint_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "fortran", violations)
            self.assertTrue(
                any("missing conductor lint evidence" in v for v in violations), violations)

    def test_validate_generate_lint_rejects_evidence_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            meta_path = self._lint_evidence_fixture(repo_root, {
                "checked_at": "t", "source_id": "src_x", "preset": "fortitude",
                "ok": False,
                "run_linter": [{"preset": "fortitude", "command_id": "a",
                                "command_log_ref": "workspace/x/command_log.jsonl"}],
            })
            violations: list[str] = []
            _validate_generate_lint_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "fortran", violations)
            self.assertTrue(
                any("lint did not succeed" in v for v in violations), violations)

    def test_validate_generate_lint_rejects_preset_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            meta_path = self._lint_evidence_fixture(repo_root, {
                "checked_at": "t", "source_id": "src_x", "preset": "cppcheck",
                "ok": True,
                "run_linter": [{"preset": "cppcheck", "command_id": "a",
                                "command_log_ref": "workspace/x/command_log.jsonl"}],
            })
            violations: list[str] = []
            _validate_generate_lint_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "fortran", violations)
            self.assertTrue(
                any("evidence preset must be 'fortitude'" in v for v in violations), violations)

    def test_validate_generate_lint_mixed_requires_exactly_two_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            meta_path = self._lint_evidence_fixture(repo_root, {
                "checked_at": "t", "source_id": "src_x", "preset": "mixed", "ok": True,
                "run_linter": [
                    {"preset": "fortitude", "command_id": "a",
                     "command_log_ref": "workspace/x/command_log.jsonl"},
                    {"preset": "fortitude", "command_id": "b",
                     "command_log_ref": "workspace/x/command_log.jsonl"},
                    {"preset": "cppcheck", "command_id": "c",
                     "command_log_ref": "workspace/x/command_log.jsonl"},
                ],
            })
            violations: list[str] = []
            _validate_generate_lint_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "mixed", violations)
            self.assertTrue(
                any("requires exactly two run_linter entries" in v for v in violations),
                violations,
            )

    def test_validate_generate_lint_certifies_at_static_without_pass(self) -> None:
        # New flow: post_generate runs in generate.static BEFORE verify sets
        # verification_status=pass. The cert must still run (and catch a bad evidence)
        # purely because the conductor's lint_evidence is present — not gated on pass.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            meta_path = self._lint_evidence_fixture(repo_root, {
                "checked_at": "t", "source_id": "src_x", "preset": "cppcheck",
                "ok": True,
                "run_linter": [{"preset": "cppcheck", "command_id": "a",
                                "command_log_ref": "workspace/x/command_log.jsonl"}],
            })
            violations: list[str] = []
            _validate_generate_lint_command_logs(
                repo_root, meta_path, {"verification_status": "fail"}, "fortran", violations)
            self.assertTrue(
                any("evidence preset must be 'fortitude'" in v for v in violations), violations)

    def test_validate_generate_lint_skips_when_no_evidence_and_not_pass(self) -> None:
        # No conductor evidence and not claiming pass (e.g. a manual/pre-lint invocation):
        # nothing to certify, so it must skip silently (no false-positive violation).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            meta_path = self._lint_evidence_fixture(repo_root, None)
            violations: list[str] = []
            _validate_generate_lint_command_logs(
                repo_root, meta_path, {"verification_status": "fail"}, "fortran", violations)
            self.assertEqual(violations, [])

    def _syntax_evidence_fixture(self, repo_root: Path, evidence: dict | None) -> Path:
        """Build <repo>/workspace/pipelines/p/pid/source/src_x/source_meta.json and
        (optionally) the conductor syntax evidence at the pipeline root. Returns meta_path."""
        pipe = repo_root / "workspace" / "pipelines" / "p" / "pid"
        gen_dir = pipe / "source" / "src_x"
        gen_dir.mkdir(parents=True, exist_ok=True)
        meta_path = gen_dir / "source_meta.json"
        if evidence is not None:
            _write_json(pipe / "syntax_evidence" / "src_x.json", evidence)
        return meta_path

    def _seed_syntax_command_log(self, repo_root: Path, records: list[dict]) -> str:
        """Write records into the canonical <gen>/src/command_log.jsonl and return its
        repo-relative ref."""
        log_rel = "workspace/pipelines/p/pid/source/src_x/src/command_log.jsonl"
        log_path = repo_root / log_rel
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8")
        return log_rel

    def test_validate_generate_syntax_rejects_pass_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            meta_path = self._syntax_evidence_fixture(repo_root, None)
            violations: list[str] = []
            vps._validate_generate_syntax_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "fortran", violations)
            self.assertTrue(
                any("missing conductor syntax evidence" in v for v in violations), violations)

    def test_validate_generate_syntax_skips_for_non_fortran_language(self) -> None:
        # cpp has no syntax-check adapter: the gate passes through with no evidence, so
        # certification must not demand it even when verify claims pass.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            meta_path = self._syntax_evidence_fixture(repo_root, None)
            violations: list[str] = []
            vps._validate_generate_syntax_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "cpp", violations)
            self.assertEqual(violations, [])

    def test_validate_generate_syntax_rejects_evidence_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            meta_path = self._syntax_evidence_fixture(repo_root, {
                "checked_at": "t", "source_id": "src_x", "ok": False,
                "stages": [{"compiler": "gfortran", "status": "fail",
                            "command_id": "a",
                            "command_log_ref": "workspace/x/command_log.jsonl"}],
            })
            violations: list[str] = []
            vps._validate_generate_syntax_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "fortran", violations)
            self.assertTrue(
                any("syntax gate did not succeed" in v for v in violations), violations)

    def test_validate_generate_syntax_rejects_skipped_gfortran_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            meta_path = self._syntax_evidence_fixture(repo_root, {
                "checked_at": "t", "source_id": "src_x", "ok": True,
                "stages": [{"compiler": "gfortran", "status": "skipped",
                            "reason": "compiler not available: gfortran"}],
            })
            violations: list[str] = []
            vps._validate_generate_syntax_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "fortran", violations)
            self.assertTrue(
                any("mandatory gfortran" in v for v in violations), violations)

    def test_validate_generate_syntax_rejects_missing_gfortran_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            log_rel = self._seed_syntax_command_log(repo_root, [{
                "command_id": "a", "tool_name": "run_syntax_check",
                "command": ["frt", "-c", "x.f90"], "ok": True,
            }])
            meta_path = self._syntax_evidence_fixture(repo_root, {
                "checked_at": "t", "source_id": "src_x", "ok": True,
                "stages": [{"compiler": "frt", "status": "pass",
                            "command_id": "a", "command_log_ref": log_rel}],
            })
            violations: list[str] = []
            vps._validate_generate_syntax_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "fortran", violations)
            self.assertTrue(
                any("must record a passing gfortran stage" in v for v in violations),
                violations)

    def test_validate_generate_syntax_allows_skipped_optional_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            log_rel = self._seed_syntax_command_log(repo_root, [{
                "command_id": "a", "tool_name": "run_syntax_check",
                "command": ["gfortran", "-fsyntax-only", "-std=f2008", "x.f90"],
                "ok": True,
            }])
            meta_path = self._syntax_evidence_fixture(repo_root, {
                "checked_at": "t", "source_id": "src_x", "ok": True,
                "stages": [
                    {"compiler": "gfortran", "status": "pass",
                     "command_id": "a", "command_log_ref": log_rel},
                    {"compiler": "frt", "status": "skipped",
                     "reason": "compiler not available: frt"},
                ],
            })
            violations: list[str] = []
            vps._validate_generate_syntax_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "fortran", violations)
            self.assertEqual(violations, [])

    def test_validate_generate_syntax_rejects_command_mismatch(self) -> None:
        # The logged argv[0] must match the declared stage compiler — a forged evidence
        # entry pointing at some other tool's record is rejected.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            log_rel = self._seed_syntax_command_log(repo_root, [{
                "command_id": "a", "tool_name": "run_syntax_check",
                "command": ["fortitude", "check", "."], "ok": True,
            }])
            meta_path = self._syntax_evidence_fixture(repo_root, {
                "checked_at": "t", "source_id": "src_x", "ok": True,
                "stages": [{"compiler": "gfortran", "status": "pass",
                            "command_id": "a", "command_log_ref": log_rel}],
            })
            violations: list[str] = []
            vps._validate_generate_syntax_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "fortran", violations)
            self.assertTrue(
                any("does not match compiler 'gfortran'" in v for v in violations),
                violations)

    def test_validate_generate_syntax_rejects_wrong_tool_name_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            log_rel = self._seed_syntax_command_log(repo_root, [{
                "command_id": "a", "tool_name": "run_linter",
                "command": ["gfortran", "-fsyntax-only", "x.f90"], "ok": True,
            }])
            meta_path = self._syntax_evidence_fixture(repo_root, {
                "checked_at": "t", "source_id": "src_x", "ok": True,
                "stages": [{"compiler": "gfortran", "status": "pass",
                            "command_id": "a", "command_log_ref": log_rel}],
            })
            violations: list[str] = []
            vps._validate_generate_syntax_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "fortran", violations)
            self.assertTrue(
                any("tool_name must be run_syntax_check" in v for v in violations),
                violations)

    def test_validate_generate_syntax_rejects_noncanonical_log_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            forged_rel = "workspace/pipelines/p/pid/source/src_x/src/notes/command_log.jsonl"
            forged = repo_root / forged_rel
            forged.parent.mkdir(parents=True, exist_ok=True)
            forged.write_text(json.dumps({
                "command_id": "a", "tool_name": "run_syntax_check",
                "command": ["gfortran", "-fsyntax-only", "x.f90"], "ok": True,
            }) + "\n", encoding="utf-8")
            meta_path = self._syntax_evidence_fixture(repo_root, {
                "checked_at": "t", "source_id": "src_x", "ok": True,
                "stages": [{"compiler": "gfortran", "status": "pass",
                            "command_id": "a", "command_log_ref": forged_rel}],
            })
            violations: list[str] = []
            vps._validate_generate_syntax_command_logs(
                repo_root, meta_path, {"verification_status": "pass"}, "fortran", violations)
            self.assertTrue(
                any("canonical MCP audit log placement" in v for v in violations),
                violations)

    def test_validate_generate_syntax_certifies_at_static_without_pass(self) -> None:
        # Like lint: the cert runs whenever the conductor evidence exists, not only on a
        # verify pass (post_generate runs in generate.static BEFORE verify).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            meta_path = self._syntax_evidence_fixture(repo_root, {
                "checked_at": "t", "source_id": "src_x", "ok": False,
                "stages": [{"compiler": "gfortran", "status": "fail",
                            "command_id": "a",
                            "command_log_ref": "workspace/x/command_log.jsonl"}],
            })
            violations: list[str] = []
            vps._validate_generate_syntax_command_logs(
                repo_root, meta_path, {"verification_status": "fail"}, "fortran", violations)
            self.assertTrue(
                any("syntax gate did not succeed" in v for v in violations), violations)

    def test_validate_generate_syntax_skips_when_no_evidence_and_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            meta_path = self._syntax_evidence_fixture(repo_root, None)
            violations: list[str] = []
            vps._validate_generate_syntax_command_logs(
                repo_root, meta_path, {"verification_status": "fail"}, "fortran", violations)
            self.assertEqual(violations, [])

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

    def _node_key_of(self, doc: dict) -> str | None:
        from tools.validate_pipeline_semantics import _plan_dependency_node_key
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = Path(tmp)
            (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
            return _plan_dependency_node_key(ir_dir)

    def test_plan_dependency_node_key_reads_the_real_ir_placements(self) -> None:
        """Regression: the compile stage resolves the node_key of the IR under validation, and a
        real IR carries it under `dependency` and `meta` — never at the top level. Reading only the
        top level returned None for every real node, silently disabling the node_key-conditioned
        multidimensional state_contract checks at Compile and deferring them to Validate."""
        nk = "problem/shallow_water2d@0.3.0"
        self.assertEqual(self._node_key_of({"dependency": {"node_key": nk}}), nk)
        self.assertEqual(self._node_key_of({"meta": {"node_key": nk}}), nk)
        self.assertEqual(self._node_key_of({"node_key": nk}), nk)  # top level stays supported
        # dependency wins when several are present, and blank/absent sections fall through.
        self.assertEqual(
            self._node_key_of({
                "dependency": {"node_key": nk},
                "meta": {"node_key": "component/other@0.1.0"},
                "node_key": "component/other@0.1.0",
            }),
            nk,
        )
        self.assertEqual(
            self._node_key_of({"dependency": {"node_key": "  "}, "meta": {"node_key": nk}}), nk
        )
        self.assertIsNone(self._node_key_of({"dependency": {}, "meta": {}}))


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
            "BIN ?= app\n"
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


class DeterministicLaunchPromptMarkerTest(unittest.TestCase):
    """Build / Validate.execute run in-process (no leaf, no skill): their minimal
    deterministic launch prompt satisfies a reduced marker set (no skill markers)."""

    def test_sentinel_constants_match_across_modules(self) -> None:
        # The validator detects deterministic prompts by text (sentinel in launch_text);
        # the runtime renders them. A desync would silently break the marker exemption.
        from tools.validate_pipeline_semantics import DETERMINISTIC_PROMPT_SENTINEL as V
        from tools.orchestration_runtime import DETERMINISTIC_PROMPT_SENTINEL as R
        self.assertEqual(V, R)

    def test_reduced_markers_exclude_skill(self) -> None:
        from tools.validate_pipeline_semantics import (
            _required_launch_prompt_markers_for_role, DETERMINISTIC_PROMPT_SENTINEL)
        det = _required_launch_prompt_markers_for_role("step", deterministic=True)
        self.assertIn(DETERMINISTIC_PROMPT_SENTINEL, det)
        self.assertNotIn("skill_ref:", det)
        self.assertNotIn("skill_name:", det)
        # the leaf set still requires skill markers
        leaf = _required_launch_prompt_markers_for_role("step", deterministic=False)
        self.assertIn("skill_ref:", leaf)

    def test_prepare_payload_keeps_deterministic_skill_free(self) -> None:
        # Regression: prepare_launch_request_payload (the real record-launch path) must
        # NOT re-inject skill_name/skill_ref (to a deleted SKILL) for a deterministic
        # build/execute payload — it must mirror build_launch_request's stripping.
        import tools.workflow_conductor as wc
        from tools.orchestration_runtime import prepare_launch_request_payload
        refs = wc.NodeRefs(node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                           ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1",
                           run_id="run_1", source_binary_id="bin_1")
        for step, substep in [("build", None), ("validate", "execute")]:
            req = wc.build_launch_request(
                refs, step=step, substep=substep, orchestration_id="o",
                orchestration_agent_run_id="p", child_agent_run_id="c", agent_model="m",
                workflow_mode="dev", case_ids=("a",), evidence_artifacts=("state_snapshots",))
            prepared = prepare_launch_request_payload(req)
            self.assertIsNone(prepared.get("skill_name"))
            self.assertIsNone(prepared.get("skill_ref"))
            self.assertEqual(prepared.get("skill_must_read_refs"), "")
        # the leaf path (judge) still derives a real skill_ref
        judge = wc.build_launch_request(
            refs, step="validate", substep="judge", orchestration_id="o",
            orchestration_agent_run_id="p", child_agent_run_id="c", agent_model="m",
            workflow_mode="dev")
        self.assertTrue(prepare_launch_request_payload(judge).get("skill_ref"))

    def test_validate_rejects_forged_deterministic_on_leaf_step(self) -> None:
        # Defense-in-depth: deterministic=True is only valid for build / validate.execute.
        from tools.orchestration_runtime import _validate_launch_request_payload
        forged = {"node_key": "component/x@0.1.0", "step": "validate", "substep": "judge",
                  "agent_model": "opus", "deterministic": True}
        with self.assertRaisesRegex(ValueError, "deterministic=True is only valid"):
            _validate_launch_request_payload(forged)
        forged_gen = {"node_key": "component/x@0.1.0", "step": "generate",
                      "substep": "generate", "agent_model": "opus", "deterministic": True}
        with self.assertRaisesRegex(ValueError, "deterministic=True is only valid"):
            _validate_launch_request_payload(forged_gen)

    def test_forged_deterministic_on_leaf_step_still_renders_full_prompt(self) -> None:
        # A non-build/execute step that forges deterministic:True must NOT downgrade to
        # the minimal prompt in a way that hides the leaf constraint lines: the renderer
        # branches on the flag, but the invariant we lock is that build_launch_request
        # never sets deterministic for a leaf step, and a forged flag still carries the
        # full leaf prompt's security-constraint lines through record-launch validation.
        from tools.orchestration_runtime import (
            _required_launch_prompt_constraint_lines, _required_launch_prompt_markers)
        # leaf judge payload, no deterministic flag -> full constraint lines required
        leaf = {"step": "validate", "substep": "judge", "node_key": "component/x@0.1.0",
                "skill_ref": "skills/workflow-validate-judge/SKILL.md"}
        self.assertTrue(_required_launch_prompt_constraint_lines(leaf))
        self.assertIn("skill_ref:", _required_launch_prompt_markers(leaf))

    def test_deterministic_prompt_satisfies_reduced_markers(self) -> None:
        import tools.workflow_conductor as wc
        from tools.orchestration_runtime import render_launch_prompt_text
        from tools.validate_pipeline_semantics import (
            _required_launch_prompt_markers_for_role, _launch_prompt_marker_present,
            DETERMINISTIC_PROMPT_SENTINEL)
        refs = wc.NodeRefs(node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                           ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1",
                           run_id="run_1", source_binary_id="bin_1")
        for step, substep, role in [("build", None, "step"), ("validate", "execute", "substep")]:
            req = wc.build_launch_request(
                refs, step=step, substep=substep, orchestration_id="o",
                orchestration_agent_run_id="p", child_agent_run_id="c", agent_model="m",
                workflow_mode="dev", case_ids=("a",), evidence_artifacts=("state_snapshots",))
            self.assertTrue(req.get("deterministic"))
            self.assertNotIn("skill_ref", req)
            prompt = render_launch_prompt_text(req)
            self.assertIn(DETERMINISTIC_PROMPT_SENTINEL, prompt)
            markers = _required_launch_prompt_markers_for_role(role, deterministic=True)
            self.assertEqual([m for m in markers
                              if not _launch_prompt_marker_present(m, prompt)], [])


class SlimRepairLaunchPromptMarkerTest(unittest.TestCase):
    """A warm-resume slim repair turn (Generate.lint / Generate.static / Compile.static
    finding) is rendered directly by the conductor with a REDUCED body — no skill /
    must-read / requirements markers, since the resumed producer leaf already holds them.
    The pipeline-semantic re-check must apply the same reduced marker set (mirror of
    orchestration_runtime._required_launch_prompt_markers slim branch) so it does not
    false-reject the recorded slim launch_prompt_ref. Regression: orch_20260702T065946Z_f05a7224
    (validate.post_judge -> validate_pre_judge_violation)."""

    def _slim_payload(self) -> dict:
        # A minimal request that satisfies _is_slim_repair_request (warm_resume + reuse +
        # non-empty findings) for a generate.generate producer repair.
        return {
            "node_key": "component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0",
            "step": "generate",
            "substep": "generate",
            "orchestration_id": "orch_test_slim_001",
            "agent_run_id": "arid_slim_001",
            "parent_agent_run_id": "orch_run_001",
            "source_id": "src_20260702_002",
            "warm_resume": True,
            "repair_strategy": "reuse",
            "repair_reason": "lint_lint_findings",
            "repair_target_agent_run_id": "arid_prev_000",
            "repair_findings": "runner.f90:270:19: C072 assumed size without intent(in)",
            "allowed_output_paths": [
                "workspace/pipelines/component__x__0.1.0/p_001/source/src_20260702_002/"
                "src/dynamics_shallow_water_flux_2d_rusanov_p0_model.f90",
            ],
        }

    def test_sentinel_constants_match_across_modules(self) -> None:
        # The validator detects slim prompts by text (sentinel in launch_text); the runtime
        # renders them. A desync would silently break the marker exemption.
        from tools.validate_pipeline_semantics import (
            SLIM_REPAIR_PROMPT_SENTINEL as V_SENT,
            SLIM_REPAIR_FINDINGS_HEADER as V_HDR)
        from tools.orchestration_runtime import (
            SLIM_REPAIR_PROMPT_SENTINEL as R_SENT,
            SLIM_REPAIR_FINDINGS_HEADER as R_HDR)
        self.assertEqual(V_SENT, R_SENT)
        self.assertEqual(V_HDR, R_HDR)

    def test_slim_request_predicate_matches_runtime(self) -> None:
        # The validator gates the reduced marker set on the structured launch request via
        # _launch_request_is_slim_repair, a copied mirror of the renderer's own
        # orchestration_runtime._is_slim_repair_request. Guard against behavioral drift.
        from tools.validate_pipeline_semantics import _launch_request_is_slim_repair as V
        from tools.orchestration_runtime import _is_slim_repair_request as R
        base = {"warm_resume": True, "repair_strategy": "reuse", "repair_findings": "x"}
        payloads = [
            base,
            {**base, "warm_resume": False},                 # not warm-resumed
            {**base, "repair_strategy": "restart"},         # not reuse
            {**base, "repair_findings": "   "},             # empty findings
            {**base, "repair_findings": ""},                # missing findings
            {**base, "deterministic": True},                # deterministic never slim
            {},                                             # empty payload
        ]
        for p in payloads:
            self.assertEqual(V(p), R(p), f"slim-request predicate drift for {p}")

    def test_reduced_markers_exclude_skill(self) -> None:
        from tools.validate_pipeline_semantics import (
            _required_launch_prompt_markers_for_role, SLIM_REPAIR_PROMPT_SENTINEL,
            SLIM_REPAIR_FINDINGS_HEADER)
        slim = _required_launch_prompt_markers_for_role("substep", slim=True)
        self.assertIn(SLIM_REPAIR_PROMPT_SENTINEL, slim)
        self.assertIn(SLIM_REPAIR_FINDINGS_HEADER, slim)
        for excluded in ("skill_ref:", "skill_name:", "skill_must_read_refs:",
                         "Required requirements:", "You are a substep agent."):
            self.assertNotIn(excluded, slim)
        # the full (non-slim, non-deterministic) set still requires them
        full = _required_launch_prompt_markers_for_role("substep")
        self.assertIn("skill_ref:", full)
        self.assertIn("Required requirements:", full)

    def test_rendered_slim_prompt_satisfies_reduced_markers(self) -> None:
        # The real reproduction: render a genuine slim prompt via the runtime, then run the
        # validator's actual marker logic against it. Before the fix (slim=False) the slim
        # prompt is reported as missing the full markers; with slim detection it passes.
        from tools.orchestration_runtime import (
            render_launch_prompt_text, _is_slim_repair_request)
        from tools.validate_pipeline_semantics import (
            _required_launch_prompt_markers_for_role, _launch_prompt_marker_present,
            _is_slim_launch_prompt_text,
            SLIM_REPAIR_PROMPT_SENTINEL, DETERMINISTIC_PROMPT_SENTINEL)
        payload = self._slim_payload()
        self.assertTrue(_is_slim_repair_request(payload))
        prompt = render_launch_prompt_text(payload)
        self.assertIn(SLIM_REPAIR_PROMPT_SENTINEL, prompt)
        # Drive the SAME detection the validator call site uses.
        is_slim = _is_slim_launch_prompt_text(prompt)
        is_det = DETERMINISTIC_PROMPT_SENTINEL in prompt
        self.assertTrue(is_slim)
        self.assertFalse(is_det)
        slim_markers = _required_launch_prompt_markers_for_role(
            "substep", deterministic=is_det, slim=is_slim)
        self.assertEqual(
            [m for m in slim_markers if not _launch_prompt_marker_present(m, prompt)],
            [],
            "rendered slim prompt must satisfy the reduced slim marker set",
        )
        # Guard against the branch being too permissive: treated as a FULL prompt (the
        # pre-fix behavior), the slim prompt IS missing the full skill/requirements markers.
        full_markers = _required_launch_prompt_markers_for_role("substep")
        self.assertTrue(
            [m for m in full_markers if not _launch_prompt_marker_present(m, prompt)],
            "slim prompt should still be missing the FULL marker set (bug reproduction)",
        )

    def test_full_substep_prompt_not_misclassified_as_slim(self) -> None:
        # Regression: the FULL substep template documents the slim mechanism in its
        # always-rendered boilerplate, so the slim SENTINEL STRING appears inside every full
        # substep prompt. A whole-body substring `SENTINEL in launch_text` would misclassify
        # the full prompt as slim and false-reject it (missing the slim-only findings header).
        # Detection must anchor on the sentinel's position (first line), matching the call
        # site's `launch_text.lstrip().startswith(...)`.
        import tools.workflow_conductor as wc
        from tools.orchestration_runtime import (
            render_launch_prompt_text, prepare_launch_request_payload)
        from tools.validate_pipeline_semantics import (
            _required_launch_prompt_markers_for_role, _launch_prompt_marker_present,
            _is_slim_launch_prompt_text,
            SLIM_REPAIR_PROMPT_SENTINEL, DETERMINISTIC_PROMPT_SENTINEL)
        refs = wc.NodeRefs(node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                           ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1",
                           run_id="run_1", source_binary_id="bin_1")
        req = wc.build_launch_request(
            refs, step="generate", substep="generate", orchestration_id="o",
            orchestration_agent_run_id="p", child_agent_run_id="c", agent_model="m",
            workflow_mode="dev")
        prompt = render_launch_prompt_text(prepare_launch_request_payload(req))
        # The sentinel string IS present (in boilerplate) but NOT at the prompt's start —
        # so the production detection must NOT classify this full prompt as slim. Driving the
        # real _is_slim_launch_prompt_text here makes this a true regression test: reverting
        # the call site to a whole-body substring check fails this assertion.
        self.assertIn(SLIM_REPAIR_PROMPT_SENTINEL, prompt)
        is_slim = _is_slim_launch_prompt_text(prompt)
        is_det = DETERMINISTIC_PROMPT_SENTINEL in prompt
        self.assertFalse(is_slim)
        self.assertFalse(is_det)
        markers = _required_launch_prompt_markers_for_role(
            "substep", deterministic=is_det, slim=is_slim)
        # It is classified full and satisfies the full marker set (no false-reject).
        self.assertIn("skill_ref:", markers)
        self.assertEqual(
            [m for m in markers if not _launch_prompt_marker_present(m, prompt)],
            [],
            "full substep prompt must satisfy the full marker set (not misclassified as slim)",
        )

    def test_slim_marker_list_matches_runtime(self) -> None:
        # Drift guard on the marker LIST (not just the sentinel constants): the validator's
        # reduced slim set must equal the renderer's slim branch in
        # orchestration_runtime._required_launch_prompt_markers.
        from tools.validate_pipeline_semantics import _required_launch_prompt_markers_for_role
        from tools.orchestration_runtime import _required_launch_prompt_markers
        runtime_slim = _required_launch_prompt_markers(self._slim_payload())
        validator_slim = _required_launch_prompt_markers_for_role("substep", slim=True)
        self.assertEqual(validator_slim, runtime_slim)

    def test_end_to_end_validate_marker_check_uses_request_and_prompt(self) -> None:
        # Integration: drive the real _validate_orchestration_hierarchy call site (not just
        # the detection helpers in isolation) by seeding a substep launch_prompt_ref + its
        # launch request on disk and running validate(require_orchestration=True). Cases:
        #   full     : REAL rendered FULL prompt (carries the slim sentinel in its
        #              always-rendered boilerplate), full request -> not slim -> no violation.
        #              Catches the R1 substring bug + R2 wiring end-to-end.
        #   slim     : REAL rendered SLIM prompt, request confirms warm-resume reuse repair
        #              -> slim -> reduced markers -> no violation (the original bug).
        #   mismatch : REAL rendered SLIM-looking prompt but a FULL (non-slim) request -> must
        #              NOT be downgraded -> full markers required -> violation (the Codex P2:
        #              the exemption is gated on the structured request, not prompt text alone).
        import json as _json
        import tools.workflow_conductor as wc
        from tools.orchestration_runtime import (
            render_launch_prompt_text, prepare_launch_request_payload)
        model_text = "module m\nimplicit none\nend module m\n"
        runner_text = "program r\nimplicit none\nend program r\n"
        refs = wc.NodeRefs(node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                           ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1",
                           run_id="run_1", source_binary_id="bin_1")
        full_req = wc.build_launch_request(
            refs, step="generate", substep="generate", orchestration_id="orch_test_001",
            orchestration_agent_run_id="p", child_agent_run_id="c", agent_model="m",
            workflow_mode="dev")
        full_prompt = render_launch_prompt_text(prepare_launch_request_payload(full_req))
        slim_prompt = render_launch_prompt_text(self._slim_payload())
        # (label, prompt body, seed a slim request?, expect a missing-markers violation)
        cases = [
            ("full", full_prompt, False, False),
            ("slim", slim_prompt, True, False),
            ("mismatch", slim_prompt, False, True),
        ]
        for label, prompt_text, request_is_slim, expect_violation in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                _seed_shape_expr_schema_into(repo_root)
                _create_minimal_execution_tree(
                    repo_root,
                    dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
                    model_text=model_text,
                    runner_text=runner_text,
                    run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
                )
                _create_minimal_orchestration_tree(repo_root)
                launches = (repo_root / "workspace" / "orchestrations" / "orch_test_001"
                            / "launches")
                # Overwrite the generate.generate substep's recorded launch prompt.
                prompt_path = launches / "substep_run_generate_generate_001.prompt.txt"
                self.assertTrue(prompt_path.exists())
                prompt_path.write_text(prompt_text, encoding="utf-8")
                if request_is_slim:
                    # Make the structured request confirm a warm-resume slim repair so the
                    # marker check downgrades to the reduced set (as a real slim record does).
                    req_path = launches / "substep_run_generate_generate_001.request.json"
                    req = _json.loads(req_path.read_text(encoding="utf-8"))
                    req.update({"warm_resume": True, "repair_strategy": "reuse",
                                "repair_findings": "runner.f90:1:1: C000 x"})
                    req_path.write_text(_json.dumps(req), encoding="utf-8")
                violations = validate(
                    repo_root=repo_root, workspace_root="workspace",
                    require_orchestration=True,
                )
                marker_violations = [
                    v for v in violations
                    if "missing launch-prompt template markers" in v]
                if expect_violation:
                    self.assertTrue(
                        marker_violations,
                        f"{label}: slim-looking prompt with a non-slim request must be flagged",
                    )
                else:
                    self.assertEqual(
                        marker_violations, [],
                        f"{label} substep prompt must not be flagged for missing markers",
                    )


class MakefileBinNotPinnedTest(unittest.TestCase):
    """post_generate does not pin the Makefile BIN to a specific VALUE, but BIN must be
    declared OVERRIDABLE (`BIN ?=`): Build and Validate.execute impose the canonical
    <spec_id>_runner name on the Makefile (Build via the make command line, execute via
    the make_test env, which overrides `?=` only). A hard `=`/`:=`/`+=` BIN is rejected."""

    _MODEL = "module foo_model\nimplicit none\nend module foo_model\n"
    _RUNNER = "program foo_runner\nimplicit none\nend program foo_runner\n"

    def _bin_violations(self, bin_line: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "foo_model.f90").write_text(self._MODEL, encoding="utf-8")
            (src / "foo_runner.f90").write_text(self._RUNNER, encoding="utf-8")
            (src / "Makefile").write_text(
                f"SPEC = foo\nOBJDIR ?= .\nBINDIR ?= .\n{bin_line}\nall: $(BINDIR)/$(BIN)\n",
                encoding="utf-8")
            violations: list[str] = []
            _validate_fortran_makefile_src_dir(src, violations)
            return [v for v in violations if "BIN" in v]

    def test_overridable_bin_value_not_pinned(self) -> None:
        # Any VALUE is accepted as long as BIN is overridable (?=): the conductor imposes
        # the canonical name, so the Makefile default is free.
        self.assertEqual(self._bin_violations("BIN ?= $(SPEC)"), [])
        self.assertEqual(self._bin_violations("BIN ?= myslug"), [])
        self.assertEqual(self._bin_violations("BIN ?= foo_runner"), [])

    def test_hard_bin_assignment_rejected(self) -> None:
        # A non-overridable BIN desyncs Validate.execute's make_test (env override applies
        # to ?= only) from the binary Build produced -> rejected at post_generate.
        for hard in ("BIN = $(SPEC)", "BIN := myslug", "BIN += foo"):
            self.assertTrue(
                any("must be declared overridable" in v for v in self._bin_violations(hard)),
                f"hard BIN assignment {hard!r} must be flagged",
            )

    def test_tab_indented_recipe_bin_assignment_not_flagged(self) -> None:
        # A tab-indented shell `BIN=...` inside a recipe body is NOT a make variable
        # assignment (a tab line is a recipe command), so it must not trigger the gate.
        # The overridable top-level `BIN ?=` is what satisfies it.
        self.assertEqual(
            self._bin_violations("BIN ?= foo_runner\nrun:\n\tBIN=/tmp/x $(BINDIR)/$(BIN)"),
            [],
        )

    def test_resolve_exe_name_is_canonical_runner(self) -> None:
        # _resolve_exe_name is now deterministic: always <spec_id>_runner, independent of
        # the Makefile BIN default (the conductor imposes it via build/execute overrides).
        import tools.workflow_conductor as wc
        c = wc.Conductor(repo_root=Path("/tmp/r"), orchestration_id="o",
                         orchestration_agent_run_id="O", backend="claude", env={})
        refs = wc.NodeRefs(node_key="component/foo@0.1.0", spec_path="spec/component/foo",
                           ir_id="i", pipeline_id="p", source_id="s", binary_id="b")
        self.assertEqual(c._resolve_exe_name(refs), "foo_runner")


class MakefileTestInvokesCasesTest(unittest.TestCase):
    """post_generate gate: the `test`/`check` target must invoke the runner with
    `--cases $(SPEC) $(CASES)` so the make-test re-run matches run_program's argv.
    A bare invocation makes the runner abort (no `--cases`) -> the make-test
    candidate emits no diagnostics.json -> quality_check verdict_available=false
    -> Validate.execute fail (orch_20260629T065607Z_011f8fc6)."""

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
            _validate_makefile_test_invokes_cases(
                src_dir, violations, build_system=build_system, language=language
            )
            return violations

    def test_bare_test_target_is_flagged(self) -> None:
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) || { echo \"error: $(BINDIR)/$(BIN) not built\" >&2; exit 1; }\n"
            "\tmkdir -p $(RUNDIR)/raw/state_snapshots\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("test target does not invoke" in v for v in violations),
            f"bare test target must be flagged; got: {violations}",
        )

    def test_hardcoded_cases_ignoring_env_is_flagged(self) -> None:
        # A run that hardcodes `--cases <spec> <ids>` instead of forwarding
        # $(SPEC)/$(CASES) ignores the env Validate.execute injects, so make test
        # would run a different spec/case set than run_program — must be flagged.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "test:\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN) --cases spec.ir.yaml c_old\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("test target does not invoke" in v for v in violations),
            f"hardcoded --cases (ignoring $(SPEC)/$(CASES)) must be flagged; got: {violations}",
        )

    def test_missing_cases_var_but_spec_present_is_flagged(self) -> None:
        # Forwarding only $(SPEC) (no $(CASES)) still desyncs the case set.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "SPEC ?= spec.ir.yaml\n"
            "test:\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) c_old\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("test target does not invoke" in v for v in violations),
            f"missing $(CASES) must be flagged; got: {violations}",
        )

    def test_cases_invocation_is_accepted(self) -> None:
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "SPEC ?= spec.ir.yaml\n"
            "CASES ?= c_alpha c_beta\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) || { echo \"error: $(BINDIR)/$(BIN) not built\" >&2; exit 1; }\n"
            "\tmkdir -p $(RUNDIR)/raw/state_snapshots\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)\n"
        )
        self.assertEqual([], self._run(makefile))

    def test_helper_target_bare_run_is_flagged(self) -> None:
        # `make test` runs the recipes of test's prerequisites too; a bare run in a
        # delegated helper target must be traced and flagged.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "test: run-qc\n"
            "run-qc:\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("test target does not invoke" in v for v in violations),
            f"bare run in delegated helper target must be flagged; got: {violations}",
        )

    def test_helper_target_compliant_is_accepted(self) -> None:
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "SPEC ?= spec.ir.yaml\n"
            "CASES ?= c_alpha\n"
            "test: run-qc\n"
            "run-qc:\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)\n"
        )
        self.assertEqual([], self._run(makefile))

    def test_build_prerequisite_is_not_a_false_run(self) -> None:
        # The binary's own build/link rule (reachable as a prerequisite) must NOT be
        # misread as a runner invocation — only genuine runs count.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "SPEC ?= spec.ir.yaml\n"
            "CASES ?= c_alpha\n"
            "FC := gfortran\n"
            "$(BINDIR)/$(BIN): main.o\n"
            "\t$(FC) main.o -o $(BINDIR)/$(BIN)\n"
            "test: $(BINDIR)/$(BIN)\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)\n"
        )
        self.assertEqual([], self._run(makefile))

    def test_quoted_spec_cases_is_accepted(self) -> None:
        # Shell-quoted forwarding (`--cases "$(SPEC)" "$(CASES)"`) still forwards the
        # env vars and must NOT be flagged: compliance is checked on the non-quote-
        # stripped run segment.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "SPEC ?= spec.ir.yaml\n"
            "CASES ?= c_alpha\n"
            "test:\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN) --cases \"$(SPEC)\" \"$(CASES)\"\n"
        )
        self.assertEqual([], self._run(makefile))

    def test_aliased_bare_runner_is_flagged(self) -> None:
        # A runner factored through a make variable (`RUNNER = $(BINDIR)/$(BIN)`) is
        # still a run after expansion; a bare aliased invocation must be flagged.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "RUNNER = $(BINDIR)/$(BIN)\n"
            "test:\n"
            "\tcd $(RUNDIR) && $(RUNNER)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("test target does not invoke" in v for v in violations),
            f"bare aliased runner must be flagged; got: {violations}",
        )

    def test_aliased_compliant_runner_is_accepted(self) -> None:
        # When the whole command (incl. --cases $(SPEC) $(CASES)) is aliased, expansion
        # reveals the forwarded vars, so it must NOT be flagged.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "SPEC ?= spec.ir.yaml\n"
            "CASES ?= c_alpha\n"
            "RUNNER = $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)\n"
            "test:\n"
            "\tcd $(RUNDIR) && $(RUNNER)\n"
        )
        self.assertEqual([], self._run(makefile))

    def test_cases_on_continuation_line_is_accepted(self) -> None:
        # A correct invocation that line-wraps with a trailing `\` must NOT be flagged:
        # the --cases token lands on the continuation, so the scanner must fold the
        # logical recipe line before checking.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "SPEC ?= spec.ir.yaml\n"
            "CASES ?= c_alpha\n"
            "test:\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN) \\\n"
            "\t  --cases $(SPEC) $(CASES)\n"
        )
        self.assertEqual([], self._run(makefile))

    def test_same_line_guard_and_bare_run_is_flagged(self) -> None:
        # A guard and a bare run sharing one line (`test -x ... && cd ... && $(BIN)`)
        # must still be flagged: the test -x exclusion is per-segment, not per-line.
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) && cd $(RUNDIR) && $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("test target does not invoke" in v for v in violations),
            f"guard+bare-run on one line must be flagged; got: {violations}",
        )

    def test_check_target_without_cases_is_flagged(self) -> None:
        makefile = (
            "BINDIR ?= .\n"
            "RUNDIR ?= .\n"
            "BIN ?= app_runner\n"
            "check:\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN)\n"
        )
        violations = self._run(makefile)
        self.assertTrue(
            any("check target does not invoke" in v for v in violations),
            f"bare check target must be flagged; got: {violations}",
        )

    def test_guard_only_recipe_is_not_flagged(self) -> None:
        # The `test -x $(BINDIR)/$(BIN)` existence guard mentions the binary but does
        # not run it; a test target whose only binary reference is the guard (no run
        # line) must not be flagged for a missing --cases.
        makefile = (
            "BINDIR ?= .\n"
            "BIN ?= app_runner\n"
            "test:\n"
            "\ttest -x $(BINDIR)/$(BIN) || { echo \"error: $(BINDIR)/$(BIN) not built\" >&2; exit 1; }\n"
        )
        self.assertEqual([], self._run(makefile))

    def test_non_make_toolchain_is_skipped(self) -> None:
        makefile = (
            "BINDIR ?= .\n"
            "BIN ?= app_runner\n"
            "test:\n"
            "\tcd $(RUNDIR) && $(BINDIR)/$(BIN)\n"
        )
        self.assertEqual([], self._run(makefile, build_system="cmake", language="cpp"))
        self.assertEqual([], self._run(makefile, build_system="make", language="python"))


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
        """Reach the contract through the production accessor, not a monkeypatch.

        `_io_contract_for_execution` hoists `io_contract.diagnostics_contract` to the top level of
        the dict this gate reads; every real IR nests it, and none carries it at document level.
        Substituting a pre-flattened dict here left that hoist — one entry in a key tuple — as the
        only route to the gate on 109 of 116 certified IRs, with nothing pinning it.
        """
        repo_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo_root, ignore_errors=True)
        pipeline_dir = repo_root / "workspace" / "pipelines" / "n" / "p_001"
        node_dir = pipeline_dir / "runs" / "run_001" / "n"
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / "diagnostics.json").write_text(json.dumps(diagnostics))

        ir_ref = "workspace/ir/component__n__0.1.0/n_001"
        _write_json(
            pipeline_dir / "lineage.json",
            {"node_key": "component/n@0.1.0", "ir_ref": ir_ref, "dependency_ref": ir_ref},
        )
        io_contract: dict[str, object] = {
            "inputs": [{"name": "case_resolved", "source": "spec.ir.yaml"}],
            "outputs": [],
        }
        if contract is not None:
            io_contract.update(contract)  # nested, exactly as a real IR authors it
        _write_json(repo_root / ir_ref / "spec.ir.yaml", {"io_contract": io_contract})

        execution = NodeExecution(
            node_key="component/n@0.1.0",
            node_dir=node_dir,
            exec_dir=node_dir,
            pipeline_dir=pipeline_dir,
        )
        violations: list[str] = []
        _validate_diagnostics_contract_output(repo_root, execution, violations)
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


class ConductorDerivedSummaryConsistencyTest(unittest.TestCase):
    """G6: the conductor authors summary.json (counts) from verdict.json#per_test; the
    `--stage pre_judge` gate (`_validate_tests_verdict_summary_consistency`) re-validates
    it, so a correct-by-construction summary passes and a mutated one still bites."""

    def _execution(self, tmp: Path) -> vps.NodeExecution:
        node_dir = tmp / "node"
        node_dir.mkdir(parents=True, exist_ok=True)
        return vps.NodeExecution(
            node_key="component/spec_x@0.1.0", node_dir=node_dir,
            exec_dir=node_dir, pipeline_dir=tmp)

    def _seed(self, tmp: Path, ex: vps.NodeExecution, counts: dict) -> None:
        # Reach tests.md the way the gate reaches it in production — lineage.json -> the IR's
        # `meta.source_refs.tests`. Monkeypatching `_tests_path_for_execution` here is what let
        # this gate stay green while it was, in fact, unreachable on every real artifact: the
        # resolver read a `source:` key no IR has, so the gate returned before its first check.
        tests_md = tmp / MOCK_TESTS_REF
        tests_md.parent.mkdir(parents=True, exist_ok=True)
        tests_md.write_text("### 1-1. `t1`\n### 1-2. `t2`\n", encoding="utf-8")
        ir_ref = "workspace/ir/component__spec_x__0.1.0/spec-x_20260415_001"
        _write_json(
            ex.pipeline_dir / "lineage.json",
            {"node_key": ex.node_key, "ir_ref": ir_ref, "dependency_ref": ir_ref},
        )
        _write_json(
            tmp / ir_ref / "spec.ir.yaml",
            {"meta": {"node_key": ex.node_key, "source_refs": {"tests": MOCK_TESTS_REF}}},
        )
        (ex.node_dir / "verdict.json").write_text(json.dumps({
            "per_test": [{"test_id": "t1", "status": "pass"},
                         {"test_id": "t2", "status": "xfail"}],
        }), encoding="utf-8")
        (ex.node_dir / "summary.json").write_text(json.dumps({"counts": counts}),
                                                  encoding="utf-8")

    def test_correct_summary_counts_pass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ex = self._execution(tmp)
            self._seed(tmp, ex, {"pass": 1, "fail": 0, "xfail": 1, "skipped": 0})
            violations: list[str] = []
            vps._validate_tests_verdict_summary_consistency(
                tmp, ex, violations, require_verdict=True)
            self.assertEqual(violations, [])

    def test_mutated_summary_counts_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ex = self._execution(tmp)
            # pass count does not match the verdict.per_test aggregate (1).
            self._seed(tmp, ex, {"pass": 2, "fail": 0, "xfail": 1, "skipped": 0})
            violations: list[str] = []
            vps._validate_tests_verdict_summary_consistency(
                tmp, ex, violations, require_verdict=True)
            self.assertTrue(any("counts.pass must equal" in v for v in violations),
                            violations)

    def test_absent_summary_is_flagged(self) -> None:
        """The only place the validator requires summary.json to exist — load-bearing only since the
        gate was revived, so it was pinned by nothing."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ex = self._execution(tmp)
            self._seed(tmp, ex, {"pass": 1, "fail": 0, "xfail": 1, "skipped": 0})
            (ex.node_dir / "summary.json").unlink()
            violations: list[str] = []
            vps._validate_tests_verdict_summary_consistency(
                tmp, ex, violations, require_verdict=True)
        self.assertTrue([v for v in violations if "summary.json: missing" in v], violations)

    def test_duplicated_per_test_entry_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ex = self._execution(tmp)
            self._seed(tmp, ex, {"pass": 1, "fail": 0, "xfail": 1, "skipped": 0})
            (ex.node_dir / "verdict.json").write_text(json.dumps({
                "per_test": [{"test_id": "t1", "status": "pass"},
                             {"test_id": "t1", "status": "pass"},
                             {"test_id": "t2", "status": "xfail"}],
            }), encoding="utf-8")
            violations: list[str] = []
            vps._validate_tests_verdict_summary_consistency(
                tmp, ex, violations, require_verdict=True)
        self.assertTrue(
            [v for v in violations if "per_test has duplicated test_id" in v], violations
        )

    def test_tests_md_without_a_test_id_heading_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ex = self._execution(tmp)
            self._seed(tmp, ex, {"pass": 1, "fail": 0, "xfail": 1, "skipped": 0})
            (tmp / MOCK_TESTS_REF).write_text("## 4. Tests\nprose only\n", encoding="utf-8")
            violations: list[str] = []
            vps._validate_tests_verdict_summary_consistency(
                tmp, ex, violations, require_verdict=True)
        self.assertTrue(
            [v for v in violations if "test_id heading not found" in v], violations
        )

    def test_absent_verdict_is_not_flagged_at_execute_but_is_at_pre_judge(self) -> None:
        """The conductor authors verdict.json AFTER the post_execute gate runs (and clears any
        stale one at the top of execute), so demanding it there would fail-close every node on
        every run. At pre_judge the verdict exists and its absence is a real defect."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ex = self._execution(tmp)
            self._seed(tmp, ex, {"pass": 1, "fail": 0, "xfail": 1, "skipped": 0})
            (ex.node_dir / "verdict.json").unlink()

            at_execute: list[str] = []
            vps._validate_tests_verdict_summary_consistency(
                tmp, ex, at_execute, require_verdict=False)
            self.assertEqual(at_execute, [])

            at_pre_judge: list[str] = []
            vps._validate_tests_verdict_summary_consistency(
                tmp, ex, at_pre_judge, require_verdict=True)
            self.assertTrue(any("verdict.json: missing" in v for v in at_pre_judge),
                            at_pre_judge)


class CompileDependencyConsistencyTests(unittest.TestCase):
    """_validate_compile_dependency_consistency: the deterministic V4 direct_deps gate
    cross-checking the IR's LLM-authored direct_deps against the conductor-authored sidecar."""

    def _seed(self, ir_dir: Path, *, ir_dependency: dict, sidecar: dict | None) -> None:
        _write_json(ir_dir / "spec.ir.yaml", {"dependency": ir_dependency})
        if sidecar is not None:
            _write_json(ir_dir / "dependency_graph.json", sidecar)

    def test_matching_direct_deps_no_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = Path(tmp)
            self._seed(
                ir_dir,
                ir_dependency={"node_key": "component/top@0.1.0",
                               "direct_deps": [{"node_key": "component/mid@0.1.0"}]},
                sidecar={"node_key": "component/top@0.1.0",
                         "all_nodes": [
                             {"node_key": "component/base@0.1.0", "topo_level": 0},
                             {"node_key": "component/mid@0.1.0", "topo_level": 1},
                             {"node_key": "component/top@0.1.0", "topo_level": 2}],
                         "transitive_deps": [
                             {"node_key": "component/base@0.1.0", "via": ["component/mid@0.1.0"]}],
                         "generated_by": "conductor"})
            violations: list[str] = []
            _validate_compile_dependency_consistency(Path(tmp), ir_dir, violations)
            self.assertEqual(violations, [])

    def test_leaf_matching_no_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = Path(tmp)
            self._seed(
                ir_dir,
                ir_dependency={"node_key": "component/base@0.1.0", "direct_deps": []},
                sidecar={"node_key": "component/base@0.1.0",
                         "all_nodes": [{"node_key": "component/base@0.1.0", "topo_level": 0}],
                         "transitive_deps": [], "generated_by": "conductor"})
            violations: list[str] = []
            _validate_compile_dependency_consistency(Path(tmp), ir_dir, violations)
            self.assertEqual(violations, [])

    def test_direct_deps_mismatch_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = Path(tmp)
            # IR claims `base` is a direct dep, but the closure says it is transitive (via mid).
            self._seed(
                ir_dir,
                ir_dependency={"node_key": "component/top@0.1.0",
                               "direct_deps": [{"node_key": "component/base@0.1.0"}]},
                sidecar={"node_key": "component/top@0.1.0",
                         "all_nodes": [
                             {"node_key": "component/base@0.1.0", "topo_level": 0},
                             {"node_key": "component/mid@0.1.0", "topo_level": 1},
                             {"node_key": "component/top@0.1.0", "topo_level": 2}],
                         "transitive_deps": [
                             {"node_key": "component/base@0.1.0", "via": ["component/mid@0.1.0"]}],
                         "generated_by": "conductor"})
            violations: list[str] = []
            _validate_compile_dependency_consistency(Path(tmp), ir_dir, violations)
            self.assertTrue(any("direct_deps disagrees" in v for v in violations), violations)
            self.assertTrue(any("component/mid" in v for v in violations), violations)

    def test_version_drift_is_soft_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = Path(tmp)
            # IR pins @0.2.0 but the closure resolved @0.1.0: version-agnostic gate accepts it
            # (gfortran/link backstop catches a wrong version at Build).
            self._seed(
                ir_dir,
                ir_dependency={"node_key": "component/top@0.1.0",
                               "direct_deps": [{"node_key": "component/mid@0.2.0"}]},
                sidecar={"node_key": "component/top@0.1.0",
                         "all_nodes": [
                             {"node_key": "component/mid@0.1.0", "topo_level": 0},
                             {"node_key": "component/top@0.1.0", "topo_level": 1}],
                         "transitive_deps": [], "generated_by": "conductor"})
            violations: list[str] = []
            _validate_compile_dependency_consistency(Path(tmp), ir_dir, violations)
            self.assertEqual(violations, [])

    def test_missing_sidecar_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = Path(tmp)
            self._seed(
                ir_dir,
                ir_dependency={"node_key": "component/top@0.1.0",
                               "direct_deps": [{"node_key": "component/mid@0.1.0"}]},
                sidecar=None)
            violations: list[str] = []
            _validate_compile_dependency_consistency(Path(tmp), ir_dir, violations)
            self.assertTrue(any("dependency_graph.json sidecar is missing" in v for v in violations),
                            violations)

    def test_self_not_in_all_nodes_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = Path(tmp)
            self._seed(
                ir_dir,
                ir_dependency={"node_key": "component/top@0.1.0", "direct_deps": []},
                sidecar={"node_key": "component/top@0.1.0",
                         "all_nodes": [{"node_key": "component/mid@0.1.0", "topo_level": 0}],
                         "transitive_deps": [], "generated_by": "conductor"})
            violations: list[str] = []
            _validate_compile_dependency_consistency(Path(tmp), ir_dir, violations)
            self.assertTrue(any("not present in all_nodes" in v for v in violations), violations)


class InfrastructurePublicApiGateTests(unittest.TestCase):
    """_validate_infrastructure_public_api: the R1 deterministic gate pinning an
    infrastructure node's IR public_api == controlled_spec §5 published surface."""

    _SPEC_ID = "hx"

    # A canonical §5.1 interface block matching the §5 surface (3 ops + 1 type). The gate's
    # cross-check requires §5.1's symbol set == §5's; the containment check pins these lines.
    _SECTION_51 = (
        "### 5.1 Canonical interface block\n"
        "```fortran\n"
        "integer, parameter :: dp = real64\n"
        "type :: hx__h_named\n"
        "  character(len=:), allocatable :: name\n"
        "  character(len=:), allocatable :: json\n"
        "end type hx__h_named\n"
        "function hx__emit_real(x) result(s)\n"
        "  real(dp), intent(in) :: x\n"
        "  character(len=:), allocatable :: s\n"
        "end function hx__emit_real\n"
        "function hx__emit_int(i) result(s)\n"
        "  integer, intent(in) :: i\n"
        "  character(len=:), allocatable :: s\n"
        "end function hx__emit_int\n"
        "subroutine hx__write_metrics_basis(entries, n)\n"
        "  type(hx__h_named), intent(in) :: entries(:)\n"
        "  integer, intent(in) :: n\n"
        "end subroutine hx__write_metrics_basis\n"
        "```\n"
    )

    def _controlled_spec(self, section_51: str | None = None) -> str:
        # A §5 section in the same shape the harness spec uses: an "operation_ids are
        # exactly" list, a "derived type `<id>__h_named`" sentence, and the §5.1 fenced block.
        if section_51 is None:
            section_51 = self._SECTION_51
        return (
            "# Controlled Spec\n"
            "## 3. Operation definition\n"
            "- `hx__emit_int(i) result(s)` — helper.\n"
            "## 5. Public API and compatibility\n"
            "The published `operation_id`s are exactly: `hx__emit_real`, `hx__emit_int`, "
            "`hx__write_metrics_basis`. The module also publishes the derived type "
            "`hx__h_named` used by `__box`. A change breaking `major` compatibility is renamed.\n"
            + section_51 +
            "## 6. Prohibitions\n- none.\n")

    def _seed(self, tmp: Path, *, public_api: object, spec_kind: str = "infrastructure",
              cs_ref: str | None = "cs.md", write_cs: bool = True) -> Path:
        ir_dir = tmp
        if write_cs:
            (tmp / "cs.md").write_text(self._controlled_spec(), encoding="utf-8")
        meta = {"spec_kind": spec_kind, "spec_id": self._SPEC_ID}
        if cs_ref is not None:
            meta["source_refs"] = {"controlled_spec": cs_ref}
        ir: dict = {"meta": meta}
        if public_api is not _OMIT:
            ir["public_api"] = public_api
        _write_json(ir_dir / "spec.ir.yaml", ir)
        return ir_dir

    def test_parser_extracts_ops_and_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cs = Path(tmp) / "cs.md"
            cs.write_text(self._controlled_spec(), encoding="utf-8")
            ops, types = _parse_public_api_from_controlled_spec(cs, self._SPEC_ID)
            self.assertEqual(ops, {"hx__emit_real", "hx__emit_int", "hx__write_metrics_basis"})
            self.assertEqual(types, {"hx__h_named"})

    def _parse_section5(self, body: str) -> tuple[set, set]:
        with tempfile.TemporaryDirectory() as tmp:
            cs = Path(tmp) / "cs.md"
            cs.write_text(f"## 5. Public API\n{body}\n## 6. x\n", encoding="utf-8")
            return _parse_public_api_from_controlled_spec(cs, "hx")

    def test_parser_captures_op_written_with_signature(self) -> None:
        # An op listed in the §3 signature style must NOT be dropped — dropping it would be a
        # false-accept (the gate would miss the exact drift it exists to catch).
        ops, types = self._parse_section5(
            "exactly: `hx__parse_cases(tokens, ok)`, `hx__emit_real`.")
        self.assertEqual(ops, {"hx__parse_cases", "hx__emit_real"})
        self.assertEqual(types, set())

    def test_parser_classifies_type_plural_and_paren_forms(self) -> None:
        for body in (
            "ops: `hx__emit_real`. The module publishes the derived types `hx__h_named`.",
            "ops: `hx__emit_real`. A derived-type record `hx__h_named` is public.",
            "ops: `hx__emit_real`. The record `type(hx__h_named)` is public.",
        ):
            ops, types = self._parse_section5(body)
            self.assertEqual(ops, {"hx__emit_real"}, body)
            self.assertEqual(types, {"hx__h_named"}, body)

    def test_parser_classifies_multi_type_list(self) -> None:
        # "the derived types `A`, `B`" — the derived-type phrase carries across pure list
        # separators so BOTH are types (not just the first).
        ops, types = self._parse_section5(
            "ops: `hx__emit_real`. The module publishes the derived types "
            "`hx__h_a`, `hx__h_b` and `hx__h_c`.")
        self.assertEqual(ops, {"hx__emit_real"})
        self.assertEqual(types, {"hx__h_a", "hx__h_b", "hx__h_c"})

    def test_parser_type_run_stops_at_prose(self) -> None:
        # An op after a derived-type span in the SAME sentence must NOT be swept into types —
        # the intervening words are not a pure list separator.
        ops, types = self._parse_section5(
            "The derived type `hx__h_named` is consumed by operation `hx__foo`.")
        self.assertEqual(ops, {"hx__foo"})
        self.assertEqual(types, {"hx__h_named"})

    def test_parser_does_not_misclassify_op_on_type_substring(self) -> None:
        # Bare-substring "type" in the lead-in (prototype / typedef / "return type" / a
        # "type-generic" parenthetical leaking from the previous op) must NOT turn the op into
        # a type — that would be an unrepairable false Compile rejection. Requires the phrase
        # "derived type" for a type, so these all stay operations.
        for body in (
            "The prototype `hx__foo` returns.",
            "A typedef `hx__foo` exists.",
            "The return type of `hx__foo` is real.",
            "exactly: `hx__box` (the type-generic constructor), `hx__foo`.",
        ):
            ops, types = self._parse_section5(body)
            self.assertIn("hx__foo", ops, body)
            self.assertEqual(types, set(), body)

    # public_api.signatures transcribing the _SECTION_51 fence (the leaf's source of the
    # signatures). Formatting may differ from §5.1 — the gate compares normalized.
    _SIGNATURES = [
        {"symbol": "hx__h_named", "interface":
            "type :: hx__h_named\n"
            "  character(len=:), allocatable :: name\n"
            "  character(len=:), allocatable :: json\n"
            "end type hx__h_named\n"},
        {"symbol": "hx__emit_real", "interface":
            "function hx__emit_real(x) result(s)\n"
            "  real(dp), intent(in) :: x\n"
            "  character(len=:), allocatable :: s\n"
            "end function hx__emit_real\n"},
        {"symbol": "hx__emit_int", "interface":
            "function hx__emit_int(i) result(s)\n"
            "  integer, intent(in) :: i\n"
            "  character(len=:), allocatable :: s\n"
            "end function hx__emit_int\n"},
        {"symbol": "hx__write_metrics_basis", "interface":
            "subroutine hx__write_metrics_basis(entries, n)\n"
            "  type(hx__h_named), intent(in) :: entries(:)\n"
            "  integer, intent(in) :: n\n"
            "end subroutine hx__write_metrics_basis\n"},
    ]

    def _full_api(self) -> dict:
        return {
            "published_operations": [
                {"operation_id": "hx__emit_real"},
                {"operation_id": "hx__emit_int", "exercised_by": []},
                {"operation_id": "hx__write_metrics_basis"},
            ],
            "published_types": ["hx__h_named"],
            "signatures": [dict(s) for s in self._SIGNATURES],
        }

    def test_exact_match_no_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = self._seed(Path(tmp), public_api=self._full_api())
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertEqual(violations, [])

    def test_dropped_operation_flagged(self) -> None:
        # The exact E2E #2 failure: a published helper writer/emitter omitted from the IR.
        with tempfile.TemporaryDirectory() as tmp:
            api = self._full_api()
            api["published_operations"] = [
                e for e in api["published_operations"]
                if e["operation_id"] not in ("hx__emit_int", "hx__write_metrics_basis")]
            ir_dir = self._seed(Path(tmp), public_api=api)
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertTrue(any("omits controlled_spec §5 operation_id 'hx__emit_int'" in v
                                for v in violations), violations)
            self.assertTrue(any("hx__write_metrics_basis" in v for v in violations), violations)

    def test_extra_operation_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = self._full_api()
            api["published_operations"].append({"operation_id": "hx__not_in_spec"})
            ir_dir = self._seed(Path(tmp), public_api=api)
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertTrue(any("declares operation_id 'hx__not_in_spec' absent" in v
                                for v in violations), violations)

    def test_type_mismatch_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = self._full_api()
            api["published_types"] = ["h_named"]  # short alias, not fully-qualified
            ir_dir = self._seed(Path(tmp), public_api=api)
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertTrue(any("omits controlled_spec §5 derived type 'hx__h_named'" in v
                                for v in violations), violations)
            self.assertTrue(any("declares type 'h_named' absent" in v for v in violations),
                            violations)

    def test_missing_public_api_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = self._seed(Path(tmp), public_api=_OMIT)
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertTrue(any("public_api missing" in v for v in violations), violations)

    def test_unresolvable_controlled_spec_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = self._seed(Path(tmp), public_api=self._full_api(), write_cs=False)
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertTrue(any("unresolvable" in v for v in violations), violations)

    def test_missing_controlled_spec_ref_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = self._seed(Path(tmp), public_api=self._full_api(), cs_ref=None)
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertTrue(any("source_refs.controlled_spec missing" in v for v in violations),
                            violations)

    def test_missing_spec_id_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "cs.md").write_text(self._controlled_spec(), encoding="utf-8")
            _write_json(Path(tmp) / "spec.ir.yaml", {
                "meta": {"spec_kind": "infrastructure",
                         "source_refs": {"controlled_spec": "cs.md"}},
                "public_api": self._full_api()})
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), Path(tmp), violations)
            self.assertTrue(any("meta.spec_id missing" in v for v in violations), violations)

    def test_section5_parsing_zero_ops_fails_closed(self) -> None:
        # A resolvable controlled_spec whose §5 yields no operation_ids (unrecognized form) is a
        # violation, never a silent pass.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "cs.md").write_text(
                "## 5. Public API\nNo backtick operation tokens here.\n## 6. x\n",
                encoding="utf-8")
            _write_json(Path(tmp) / "spec.ir.yaml", {
                "meta": {"spec_kind": "infrastructure", "spec_id": self._SPEC_ID,
                         "source_refs": {"controlled_spec": "cs.md"}},
                "public_api": self._full_api()})
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), Path(tmp), violations)
            self.assertTrue(any("parsed 0 published operation_ids" in v for v in violations),
                            violations)

    def test_non_infrastructure_is_noop(self) -> None:
        # A physics node has no exact-published contract; the gate must not fire even with
        # no public_api present.
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = self._seed(Path(tmp), public_api=_OMIT, spec_kind="component")
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertEqual(violations, [])

    def test_signatures_missing_flagged(self) -> None:
        # public_api present but without a `signatures` block: the leaf would have no source of
        # the signatures to publish, so this is a Compile fail.
        with tempfile.TemporaryDirectory() as tmp:
            api = self._full_api()
            del api["signatures"]
            ir_dir = self._seed(Path(tmp), public_api=api)
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertTrue(any("public_api.signatures missing" in v for v in violations),
                            violations)

    def test_signatures_type_drift_flagged(self) -> None:
        # An IR signature that drifts from §5.1 (here: change entries' element type) is flagged —
        # it is exactly what the leaf would transcribe into the model.
        with tempfile.TemporaryDirectory() as tmp:
            api = self._full_api()
            api["signatures"][3]["interface"] = (
                "subroutine hx__write_metrics_basis(entries, n)\n"
                "  character(len=*), intent(in) :: entries(:)\n"
                "  integer, intent(in) :: n\n"
                "end subroutine hx__write_metrics_basis\n")
            ir_dir = self._seed(Path(tmp), public_api=api)
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertTrue(any("signatures['hx__write_metrics_basis'] does not match" in v
                                for v in violations), violations)

    def test_signatures_component_reorder_flagged(self) -> None:
        # Reordering a derived type's components keeps the line SET identical but is a real
        # compatibility (positional-construction) drift — the ordered comparison must catch it.
        with tempfile.TemporaryDirectory() as tmp:
            api = self._full_api()
            api["signatures"][0]["interface"] = (
                "type :: hx__h_named\n"
                "  character(len=:), allocatable :: json\n"
                "  character(len=:), allocatable :: name\n"
                "end type hx__h_named\n")
            ir_dir = self._seed(Path(tmp), public_api=api)
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertTrue(any("signatures['hx__h_named'] does not match" in v
                                for v in violations), violations)

    def test_signatures_lying_symbol_flagged(self) -> None:
        # A `symbol` that disagrees with the interface it carries is fail-closed.
        with tempfile.TemporaryDirectory() as tmp:
            api = self._full_api()
            api["signatures"][1]["symbol"] = "hx__not_emit_real"
            ir_dir = self._seed(Path(tmp), public_api=api)
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertTrue(any("declares a different symbol" in v for v in violations),
                            violations)

    def test_signatures_non_dict_entry_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = self._full_api()
            api["signatures"].append("not a mapping")
            ir_dir = self._seed(Path(tmp), public_api=api)
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertTrue(any("is not a mapping" in v for v in violations), violations)

    def test_signatures_formatting_difference_passes(self) -> None:
        # A signature transcribed with different alignment / a split continuation still matches
        # (normalized comparison).
        with tempfile.TemporaryDirectory() as tmp:
            api = self._full_api()
            api["signatures"][3]["interface"] = (
                "subroutine hx__write_metrics_basis(entries, &\n"
                "    n)\n"
                "  type(hx__h_named),   intent(in) :: entries(:)   ! boxed\n"
                "  integer,             intent(in) :: n\n"
                "end subroutine hx__write_metrics_basis\n")
            ir_dir = self._seed(Path(tmp), public_api=api)
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), ir_dir, violations)
            self.assertEqual(violations, [])

    def test_section51_missing_fence_fails_closed(self) -> None:
        # An infra controlled_spec whose §5 carries no §5.1 canonical interface block is a
        # violation — the spec must pin its own signatures machine-readably.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "cs.md").write_text(
                self._controlled_spec(section_51=""), encoding="utf-8")
            _write_json(Path(tmp) / "spec.ir.yaml", {
                "meta": {"spec_kind": "infrastructure", "spec_id": self._SPEC_ID,
                         "source_refs": {"controlled_spec": "cs.md"}},
                "public_api": self._full_api()})
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), Path(tmp), violations)
            self.assertTrue(any("§5.1" in v and "missing" in v for v in violations), violations)

    def test_section51_op_set_mismatch_flagged(self) -> None:
        # §5.1 omitting an op that §5 lists (here: drop hx__emit_int from the fence) is a
        # spec-internal-consistency violation caught at Compile.
        broken_fence = self._SECTION_51.replace(
            "function hx__emit_int(i) result(s)\n"
            "  integer, intent(in) :: i\n"
            "  character(len=:), allocatable :: s\n"
            "end function hx__emit_int\n", "")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "cs.md").write_text(
                self._controlled_spec(section_51=broken_fence), encoding="utf-8")
            _write_json(Path(tmp) / "spec.ir.yaml", {
                "meta": {"spec_kind": "infrastructure", "spec_id": self._SPEC_ID,
                         "source_refs": {"controlled_spec": "cs.md"}},
                "public_api": self._full_api()})
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), Path(tmp), violations)
            self.assertTrue(any("§5.1 omits a signature for §5 operation_id 'hx__emit_int'" in v
                                for v in violations), violations)

    def test_section51_extra_type_flagged(self) -> None:
        # §5.1 defining a type absent from §5's derived-type list is flagged.
        extra_fence = self._SECTION_51.replace(
            "```\n",
            "type :: hx__h_extra\n"
            "  integer :: v\n"
            "end type hx__h_extra\n"
            "```\n", 1)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "cs.md").write_text(
                self._controlled_spec(section_51=extra_fence), encoding="utf-8")
            _write_json(Path(tmp) / "spec.ir.yaml", {
                "meta": {"spec_kind": "infrastructure", "spec_id": self._SPEC_ID,
                         "source_refs": {"controlled_spec": "cs.md"}},
                "public_api": self._full_api()})
            violations: list[str] = []
            _validate_infrastructure_public_api(Path(tmp), Path(tmp), violations)
            self.assertTrue(any("defines a derived type 'hx__h_extra' absent" in v
                                for v in violations), violations)


class CanonicalInterfaceParserTests(unittest.TestCase):
    """_parse_canonical_interface_from_controlled_spec + the Fortran normalization helpers:
    parse the §5.1 fenced interface block into per-symbol stanzas, fail-closed on a
    missing/duplicate/unterminated fence."""

    _FENCE = InfrastructurePublicApiGateTests._SECTION_51

    def _cs(self, section_51: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        if section_51 and "### 5.1" not in section_51:
            section_51 = "### 5.1 Canonical interface block\n" + section_51
        (tmp / "cs.md").write_text(
            "## 5. Public API\nprose.\n" + section_51 + "## 6. x\n", encoding="utf-8")
        return tmp / "cs.md"

    def test_parses_op_and_type_stanzas(self) -> None:
        ops, types, err = vps._parse_canonical_interface_from_controlled_spec(self._cs(self._FENCE))
        self.assertIsNone(err)
        self.assertEqual(set(ops), {"hx__emit_real", "hx__emit_int", "hx__write_metrics_basis"})
        self.assertEqual(set(types), {"hx__h_named"})
        # a proc stanza is header + dummy decls, EXCLUDING the `end` line;
        # a type stanza INCLUDES its `end type` line.
        self.assertEqual(ops["hx__emit_real"][0], "function hx__emit_real(x) result(s)")
        self.assertTrue(all("end function" not in l for l in ops["hx__emit_real"]))
        self.assertIn("end type hx__h_named", types["hx__h_named"])
        # a top-level `parameter` declaration is not a stanza
        self.assertNotIn("dp", ops)

    def test_missing_fence_errors(self) -> None:
        _, _, err = vps._parse_canonical_interface_from_controlled_spec(self._cs(""))
        self.assertIsNotNone(err)
        self.assertIn("missing", err)

    def test_multiple_fences_errors(self) -> None:
        _, _, err = vps._parse_canonical_interface_from_controlled_spec(
            self._cs(self._FENCE + "```fortran\ninteger :: x\n```\n"))
        self.assertIsNotNone(err)
        self.assertIn("multiple", err)

    def test_unterminated_stanza_errors(self) -> None:
        bad = "```fortran\nsubroutine hx__foo(a)\n  integer, intent(in) :: a\n```\n"
        _, _, err = vps._parse_canonical_interface_from_controlled_spec(self._cs(bad))
        self.assertIsNotNone(err)
        self.assertIn("unterminated", err)

    def test_duplicate_stanza_errors(self) -> None:
        # A malformed first copy must not hide behind a correct second (dict-overwrite would).
        dup = (
            "```fortran\n"
            "function hx__foo(a) result(s)\n  integer, intent(in) :: a\n"
            "  character(len=:), allocatable :: s\nend function hx__foo\n"
            "function hx__foo(a, b) result(s)\n  integer, intent(in) :: a\n"
            "  integer, intent(in) :: b\n  character(len=:), allocatable :: s\n"
            "end function hx__foo\n```\n")
        _, _, err = vps._parse_canonical_interface_from_controlled_spec(self._cs(dup))
        self.assertIsNotNone(err)
        self.assertIn("duplicate", err)

    def test_declaration_atoms_split_combined_declarators(self) -> None:
        # A combined declarator splits into one atom per entity; array-spec commas stay intact.
        self.assertEqual(
            vps._declaration_atoms("integer, intent(in) :: steps, cells_updated"),
            ["integer, intent(in) :: steps", "integer, intent(in) :: cells_updated"])
        self.assertEqual(
            vps._declaration_atoms("real(dp), intent(in) :: a(:), b(2,2)"),
            ["real(dp), intent(in) :: a(:)", "real(dp), intent(in) :: b(2,2)"])
        # A header (no ::) passes through unchanged.
        self.assertEqual(
            vps._declaration_atoms("subroutine hx__foo(a, b)"), ["subroutine hx__foo(a, b)"])

    def test_combined_and_split_declarations_compare_equal(self) -> None:
        combined = vps._stanza_atoms(["integer, intent(in) :: a, b"])
        split = vps._stanza_atoms(
            ["integer, intent(in) :: a", "integer, intent(in) :: b"])
        self.assertEqual(combined, split)

    def test_bare_end_does_not_swallow_following_procedure(self) -> None:
        # A bare `end` (legal for a module procedure) must not consume the next procedure's header.
        fence = (
            "```fortran\n"
            "function hx__a(x) result(s)\n  real, intent(in) :: x\n  real :: s\nend\n"
            "function hx__b(y) result(s)\n  real, intent(in) :: y\n  real :: s\n"
            "end function hx__b\n```\n")
        ops, types, err = vps._parse_canonical_interface_from_controlled_spec(self._cs(fence))
        self.assertEqual(set(ops), {"hx__a", "hx__b"})

    def test_type_missing_end_type_is_unterminated(self) -> None:
        # A derived type MUST close with `end type` (a bare `end` does not close a type). A type
        # stanza terminated by the next header without `end type` is malformed → fail-closed, while
        # the next symbol still registers (no cascade).
        block = (
            "```fortran\n"
            "type :: hx__a\n  integer :: x\n"
            "type :: hx__b\n  integer :: y\nend type hx__b\n```\n")
        ops, types, err = vps._parse_canonical_interface_from_controlled_spec(self._cs(block))
        self.assertIsNotNone(err)
        self.assertIn("unterminated", err)
        self.assertIn("hx__a", err)

    def test_end_line_canonicalizes_trailing_name(self) -> None:
        # bare `end type` compares equal to `end type NAME` (the name is pinned by the header).
        with_name = vps._stanza_atoms(
            ["type :: hx__t", "  integer :: a", "end type hx__t"])
        bare = vps._stanza_atoms(["type :: hx__t", "  integer :: a", "end type"])
        self.assertEqual(with_name, bare)

    def test_no_space_end_keyword_closes_stanza(self) -> None:
        # `endfunction` / `endtype` (legal free-form) must close a stanza, not swallow the next.
        fence = (
            "```fortran\n"
            "function hx__a(x) result(s)\n  real, intent(in) :: x\n"
            "  real :: s\nendfunction hx__a\n"
            "function hx__b(y) result(s)\n  real, intent(in) :: y\n"
            "  real :: s\nend function hx__b\n```\n")
        ops, types, err = vps._parse_canonical_interface_from_controlled_spec(self._cs(fence))
        self.assertIsNone(err)
        self.assertEqual(set(ops), {"hx__a", "hx__b"})

    def test_continuation_join_skips_interleaved_comment_and_blank(self) -> None:
        # Free-form Fortran allows blank / full-comment lines inside a `&` continuation; the join
        # must span them (the §5.1 write_perf header is >132 cols and MUST wrap).
        joined = vps._fortran_logical_lines(
            "subroutine hx__wp(a, &\n"
            "  ! a comment inside the wrap\n"
            "\n"
            "    b, c)\n")
        self.assertEqual(len(joined), 1)
        self.assertEqual(vps._normalize_fortran_line(joined[0]), "subroutinehx__wp(a,b,c)")

    def test_unrelated_fence_before_subsection_ignored(self) -> None:
        # A code fence in §5 prose BEFORE ### 5.1 must not be mistaken for the interface block.
        body = "```text\nan illustrative example\n```\n" + self._FENCE
        ops, types, err = vps._parse_canonical_interface_from_controlled_spec(self._cs(body))
        self.assertIsNone(err)
        self.assertEqual(set(ops), {"hx__emit_real", "hx__emit_int", "hx__write_metrics_basis"})
        self.assertEqual(set(types), {"hx__h_named"})

    def test_parameter_lines_extracted(self) -> None:
        params = vps._section51_parameter_lines(self._cs(self._FENCE))
        self.assertEqual([vps._normalize_fortran_line(p) for p in params],
                         ["integer,parameter::dp=real64"])

    def test_normalization_joins_continuations_and_folds_case(self) -> None:
        # A continuation-split, differently-cased, comment-bearing header normalizes to the
        # same canonical line as its single-line form.
        joined = vps._fortran_logical_lines(
            "SUBROUTINE Hx__Foo(a, &  ! keep going\n     b)  ! done\n")
        self.assertEqual(len(joined), 1)
        self.assertEqual(
            vps._normalize_fortran_line(joined[0]), "subroutinehx__foo(a,b)")

    def test_comment_strip_honors_strings(self) -> None:
        line = vps._strip_fortran_comment("s = '! not a comment' ! real comment")
        self.assertEqual(vps._normalize_fortran_line(line), "s='!notacomment'")


class InfrastructureGeneratedSignatureGateTests(unittest.TestCase):
    """_validate_infrastructure_generated_signatures: the Generate.static gate pinning the
    generated model source against the §5.1 canonical signatures."""

    _FENCE = InfrastructurePublicApiGateTests._SECTION_51

    # A model source that publishes all three §5.1 ops + the type, with DIFFERENT formatting
    # (alignment whitespace, split continuation, local vars, a body) — must still pass.
    _GOOD_SOURCE = (
        "module hx_model\n"
        "  use, intrinsic :: iso_fortran_env, only: real64\n"
        "  implicit none\n"
        "  integer, parameter :: dp = real64\n"
        "  type :: hx__h_named\n"
        "    character(len=:), allocatable :: name\n"
        "    character(len=:), allocatable :: json\n"
        "  end type hx__h_named\n"
        "contains\n"
        "  function hx__emit_real(x)   result(s)\n"
        "    real(dp),          intent(in) :: x\n"
        "    character(len=:), allocatable :: s\n"
        "    character(len=32) :: buf\n"
        "    write(buf, '(ES24.16E3)') x\n"
        "    s = trim(adjustl(buf))\n"
        "  end function hx__emit_real\n"
        "  function hx__emit_int(i) result(s)\n"
        "    integer, intent(in) :: i\n"
        "    character(len=:), allocatable :: s\n"
        "    s = 'x'\n"
        "  end function hx__emit_int\n"
        "  subroutine hx__write_metrics_basis(entries, &\n"
        "      n)\n"
        "    type(hx__h_named), intent(in) :: entries(:)\n"
        "    integer,           intent(in) :: n\n"
        "  end subroutine hx__write_metrics_basis\n"
        "end module hx_model\n"
    )

    def _seed(self, tmp: Path, *, source: str, spec_kind: str = "infrastructure",
              section_51: str | None = None) -> NodeExecution:
        ir_ref = "workspace/ir/x"
        ir_dir = tmp / ir_ref
        ir_dir.mkdir(parents=True)
        (tmp / "cs.md").write_text(
            "## 5. Public API\nprose.\n" + (self._FENCE if section_51 is None else section_51)
            + "## 6. x\n", encoding="utf-8")
        _write_json(ir_dir / "spec.ir.yaml", {
            "meta": {"spec_kind": spec_kind, "spec_id": "hx",
                     "source_refs": {"controlled_spec": "cs.md"}}})
        pipe = tmp / "pipe"
        src_dir = pipe / "src"
        src_dir.mkdir(parents=True)
        (pipe / "lineage.json").write_text(
            json.dumps({"ir_ref": ir_ref}), encoding="utf-8")
        model = src_dir / "hx_model.f90"
        model.write_text(source, encoding="utf-8")
        return NodeExecution(
            node_key=f"{spec_kind}/hx@0.2.0", node_dir=pipe, exec_dir=pipe, pipeline_dir=pipe)

    def _run(self, execution: NodeExecution, tmp: Path) -> list[str]:
        model = tmp / "pipe" / "src" / "hx_model.f90"
        violations: list[str] = []
        vps._validate_infrastructure_generated_signatures(
            tmp, execution, [model], violations)
        return violations

    def test_faithful_source_passes(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ex = self._seed(tmp, source=self._GOOD_SOURCE)
            self.assertEqual(self._run(ex, tmp), [])

    def test_argument_name_drift_flagged(self) -> None:
        # rename dummy `n` -> `count` in the writer's header AND its decl: the pinned header line
        # is no longer present.
        drift = self._GOOD_SOURCE.replace(
            "  subroutine hx__write_metrics_basis(entries, &\n"
            "      n)\n"
            "    type(hx__h_named), intent(in) :: entries(:)\n"
            "    integer,           intent(in) :: n\n",
            "  subroutine hx__write_metrics_basis(entries, count)\n"
            "    type(hx__h_named), intent(in) :: entries(:)\n"
            "    integer,           intent(in) :: count\n")
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ex = self._seed(tmp, source=drift)
            violations = self._run(ex, tmp)
            self.assertTrue(any("hx__write_metrics_basis" in v and "pinned interface line" in v
                                for v in violations), violations)

    def test_argument_type_drift_flagged(self) -> None:
        # change entries' element type: the pinned decl line no longer matches.
        drift = self._GOOD_SOURCE.replace(
            "    type(hx__h_named), intent(in) :: entries(:)\n",
            "    character(len=*), intent(in) :: entries(:)\n")
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ex = self._seed(tmp, source=drift)
            violations = self._run(ex, tmp)
            self.assertTrue(any("hx__write_metrics_basis" in v for v in violations), violations)

    def test_type_component_drift_flagged(self) -> None:
        drift = self._GOOD_SOURCE.replace(
            "    character(len=:), allocatable :: json\n",
            "    integer :: json\n")
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ex = self._seed(tmp, source=drift)
            violations = self._run(ex, tmp)
            self.assertTrue(any("hx__h_named" in v for v in violations), violations)

    def test_drift_not_masked_by_identical_decl_in_other_proc(self) -> None:
        # The published proc drifts its `n` to intent(out), but a DECOY helper elsewhere declares
        # `integer, intent(in) :: n` verbatim. A global source line-set would false-accept; the
        # per-symbol check must still flag the published proc.
        drift = self._GOOD_SOURCE.replace(
            "    integer,           intent(in) :: n\n"
            "  end subroutine hx__write_metrics_basis\n",
            "    integer,           intent(out) :: n\n"
            "  end subroutine hx__write_metrics_basis\n"
            "  subroutine hx__decoy(n)\n"
            "    integer, intent(in) :: n\n"
            "  end subroutine hx__decoy\n")
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ex = self._seed(tmp, source=drift)
            violations = self._run(ex, tmp)
            self.assertTrue(any("hx__write_metrics_basis" in v and "intent(in) :: n" in v
                                for v in violations), violations)

    def test_combined_declarations_pass(self) -> None:
        # A source that combines the type's two same-type components onto one line (legal Fortran,
        # ABI-identical) must NOT be flagged — the contract permits formatting differences.
        combined = self._GOOD_SOURCE.replace(
            "    character(len=:), allocatable :: name\n"
            "    character(len=:), allocatable :: json\n",
            "    character(len=:), allocatable :: name, json\n")
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ex = self._seed(tmp, source=combined)
            self.assertEqual(self._run(ex, tmp), [])

    def test_bare_end_type_passes(self) -> None:
        # A generated type closed with bare `end type` (no repeated name) is legal + ABI-identical.
        combined = self._GOOD_SOURCE.replace(
            "  end type hx__h_named\n", "  end type\n")
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ex = self._seed(tmp, source=combined)
            self.assertEqual(self._run(ex, tmp), [])

    def test_extra_type_component_flagged(self) -> None:
        # Inserting an extra published component into a derived type widens its layout — the
        # exact ordered atom-list equality must reject it (set equality or subsequence would not).
        widened = self._GOOD_SOURCE.replace(
            "    character(len=:), allocatable :: name\n"
            "    character(len=:), allocatable :: json\n",
            "    character(len=:), allocatable :: name\n"
            "    integer :: secret_extra\n"
            "    character(len=:), allocatable :: json\n")
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ex = self._seed(tmp, source=widened)
            violations = self._run(ex, tmp)
            self.assertTrue(any("hx__h_named" in v and "component layout" in v
                                for v in violations), violations)

    def test_type_component_reorder_flagged(self) -> None:
        # Source reorders h_named's components: set membership would pass, but the exact ordered
        # atom-list equality flags the layout drift.
        drift = self._GOOD_SOURCE.replace(
            "  type :: hx__h_named\n"
            "    character(len=:), allocatable :: name\n"
            "    character(len=:), allocatable :: json\n"
            "  end type hx__h_named\n",
            "  type :: hx__h_named\n"
            "    character(len=:), allocatable :: json\n"
            "    character(len=:), allocatable :: name\n"
            "  end type hx__h_named\n")
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ex = self._seed(tmp, source=drift)
            violations = self._run(ex, tmp)
            self.assertTrue(any("hx__h_named" in v and "component layout" in v
                                for v in violations), violations)

    def test_non_infrastructure_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            # a component node with a garbage source: gate must not fire
            ex = self._seed(tmp, source="module m\nend module m\n", spec_kind="component")
            self.assertEqual(self._run(ex, tmp), [])

    def test_parameter_value_drift_flagged(self) -> None:
        # A drifted `dp = real32` (vs §5.1 `real64`) changes the published ABI but the symbolic
        # `real(dp)` decls still match — the parameter pin must catch the value drift.
        drift = self._GOOD_SOURCE.replace(
            "integer, parameter :: dp = real64", "integer, parameter :: dp = real32")
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ex = self._seed(tmp, source=drift)
            violations = self._run(ex, tmp)
            self.assertTrue(any("module parameter" in v and "dp = real64" in v
                                for v in violations), violations)

    def test_infra_missing_ir_fails_closed(self) -> None:
        # An infrastructure node (per node_key) whose IR cannot be resolved must fail closed —
        # never silently skip the signature pin.
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            pipe = tmp / "pipe"
            (pipe / "src").mkdir(parents=True)
            (pipe / "src" / "hx_model.f90").write_text("module m\nend module m\n")
            # no lineage.json -> IR unresolvable
            ex = NodeExecution(node_key="infrastructure/hx@0.2.0", node_dir=pipe,
                               exec_dir=pipe, pipeline_dir=pipe)
            violations = self._run(ex, tmp)
            self.assertTrue(any("fail-closed" in v for v in violations), violations)

    def test_unparseable_fence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ex = self._seed(tmp, source=self._GOOD_SOURCE, section_51="")
            violations = self._run(ex, tmp)
            self.assertTrue(any("§5.1" in v for v in violations), violations)


class DependencyExpectedNodeKeysTests(unittest.TestCase):
    """_dependency_expected_node_keys: the fallback to direct_deps is keyed on all_nodes being
    ABSENT (not on the set being empty), so a missing sidecar cannot collapse a node with real
    deps to a self-only closure that bypasses the DAG-completeness gate."""

    def test_all_nodes_present_is_trusted_exactly_including_leaf(self) -> None:
        # A leaf sidecar (all_nodes=[self]) is trusted exactly even though the IR block also
        # carries a direct_deps entry (a test/operation-usage artifact) — expected = {self}.
        dep = {
            "node_key": "component/top@0.1.0",
            "all_nodes": [{"node_key": "component/top@0.1.0", "topo_level": 0}],
            "direct_deps": [{"node_key": "component/base@0.1.0"}],
        }
        self.assertEqual(_dependency_expected_node_keys(dep), {"component/top"})

    def test_missing_all_nodes_falls_back_to_direct_deps(self) -> None:
        # No all_nodes (sidecar missing) but node_key + direct_deps present: must include the
        # direct dep (fail-closed), NOT collapse to self-only.
        dep = {
            "node_key": "component/top@0.1.0",
            "direct_deps": [{"node_key": "component/base@0.1.0"}],
        }
        self.assertEqual(_dependency_expected_node_keys(dep),
                         {"component/top", "component/base"})

    def test_all_nodes_superset_of_direct(self) -> None:
        dep = {
            "node_key": "component/top@0.1.0",
            "all_nodes": [
                {"node_key": "component/top@0.1.0", "topo_level": 1},
                {"node_key": "component/base@0.1.0", "topo_level": 0}],
            "direct_deps": [{"node_key": "component/base@0.1.0"}],
        }
        self.assertEqual(_dependency_expected_node_keys(dep),
                         {"component/top", "component/base"})


_CHECKS_OK = """\
module bx_checks
  use, intrinsic :: iso_fortran_env, only: real64
  ! allow(C003)
  implicit none
  private
  public :: case_setup, case_run, get_time
  public :: get_scalar, get_r1, get_r2, get_r3, get_r4
  public :: checks_compute, metric_compute
contains
  subroutine case_setup(case_id, ok)
    character(len=*), intent(in) :: case_id
    logical, intent(out) :: ok
    ok = .true.
    if (len_trim(case_id) < 0) continue
  end subroutine case_setup
end module bx_checks
"""

_MODEL_OK = "module bx_model\n! allow(C003)\nimplicit none\nend module bx_model\n"


class ChecksSourceGateTests(unittest.TestCase):
    """R1/M3c-β `_validate_checks_source_files`: the leaf-authored fixed-ABI checks module."""

    def _exec(self, tmp: Path) -> NodeExecution:
        return NodeExecution(node_key="component/bx@0.1.0", node_dir=tmp,
                             exec_dir=tmp, pipeline_dir=tmp)

    def _run(self, checks: str | None, model: str = _MODEL_OK) -> list[str]:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            src = tmp / "src"
            src.mkdir()
            (src / "bx_model.f90").write_text(model)
            if checks is not None:
                (src / "bx_checks.f90").write_text(checks)
            violations: list[str] = []
            vps._validate_checks_source_files(
                self._exec(tmp), src, [src / "bx_model.f90"], violations)
            return violations

    def test_clean_checks_passes(self) -> None:
        self.assertEqual(self._run(_CHECKS_OK), [])

    def test_missing_checks_file(self) -> None:
        self.assertTrue(any("must author bx_checks.f90" in v for v in self._run(None)))

    def test_wrong_module_name(self) -> None:
        bad = _CHECKS_OK.replace("module bx_checks", "module bx_wrong", 1).replace(
            "end module bx_checks", "end module bx_wrong")
        self.assertTrue(any("module bx_checks" in v for v in self._run(bad)))

    def test_missing_public_name(self) -> None:
        bad = _CHECKS_OK.replace("checks_compute, metric_compute", "checks_compute")
        v = self._run(bad)
        self.assertTrue(any("metric_compute" in x for x in v), v)

    def test_checks_uses_harness_forbidden(self) -> None:
        bad = _CHECKS_OK.replace(
            "  private\n", "  private\n  use harness_fortran_cpu_model\n")
        self.assertTrue(any("must not `use` the harness" in v for v in self._run(bad)))

    def test_checks_uses_harness_double_colon_form_forbidden(self) -> None:
        # `use :: harness_...` and `use, intrinsic :: ...` must also be caught.
        for form in ("use :: harness_fortran_cpu_model",
                     "use, non_intrinsic :: harness_fortran_cpu_model"):
            bad = _CHECKS_OK.replace("  private\n", f"  private\n  {form}\n")
            self.assertTrue(any("must not `use` the harness" in v for v in self._run(bad)),
                            form)

    def test_bare_public_missing_definition_caught(self) -> None:
        # A default-public (bare `public`) module that never DEFINES an ABI name is caught.
        bad = ("module bx_checks\n  implicit none\n  public\ncontains\n"
               "  subroutine case_setup(case_id, ok)\n"
               "    character(len=*), intent(in) :: case_id\n"
               "    logical, intent(out) :: ok\n    ok = .true.\n"
               "  end subroutine case_setup\nend module bx_checks\n")
        v = self._run(bad)
        self.assertTrue(any("metric_compute" in x for x in v), v)

    def test_bare_public_all_defined_passes(self) -> None:
        # A bare-`public` module that DEFINES all ten ABI names passes the name check.
        body = "".join(
            f"  subroutine {n}()\n  end subroutine {n}\n"
            for n in ("case_setup", "case_run", "get_time", "get_scalar",
                      "get_r1", "get_r2", "get_r3", "get_r4",
                      "checks_compute", "metric_compute"))
        ok = f"module bx_checks\n  implicit none\n  public\ncontains\n{body}end module bx_checks\n"
        # (only the ABI-name check is asserted here; other rules are satisfied)
        self.assertFalse(any("must publish the fixed ABI names" in v for v in self._run(ok)))

    def _module_defining_all(self, extra_spec: str = "", private_stmt: str = "") -> str:
        body = "".join(
            f"  subroutine {n}()\n  end subroutine {n}\n"
            for n in ("case_setup", "case_run", "get_time", "get_scalar",
                      "get_r1", "get_r2", "get_r3", "get_r4",
                      "checks_compute", "metric_compute"))
        return (f"module bx_checks\n  implicit none\n{private_stmt}{extra_spec}"
                f"contains\n{body}end module bx_checks\n")

    def test_no_accessibility_statement_all_defined_passes(self) -> None:
        # A module with NEITHER `private` NOR `public` is default-PUBLIC in Fortran; a
        # conformant module that defines all ten ABI names must not be false-rejected.
        ok = self._module_defining_all()
        self.assertFalse(any("must publish the fixed ABI names" in v for v in self._run(ok)),
                         self._run(ok))

    def test_bare_private_without_public_is_caught(self) -> None:
        # A bare module-level `private` flips the default; without an explicit `public ::`
        # the ABI names are NOT exposed and the gate must catch it (no fail-open).
        bad = self._module_defining_all(private_stmt="  private\n")
        self.assertTrue(any("must publish the fixed ABI names" in v for v in self._run(bad)), bad)

    def test_private_entity_excludes_name(self) -> None:
        # Default-public module, but one ABI name is explicitly `private ::`'d → not published.
        bad = self._module_defining_all(extra_spec="  private :: metric_compute\n")
        v = self._run(bad)
        self.assertTrue(any("metric_compute" in x for x in v), v)

    def test_interface_prototype_is_not_a_definition(self) -> None:
        # A default-public module that only PROTOTYPES an ABI name in an `interface` block
        # (never defining it) must still be caught — an interface header is not a definition.
        body = "".join(
            f"  subroutine {n}()\n  end subroutine {n}\n"
            for n in ("case_setup", "case_run", "get_time", "get_scalar",
                      "get_r1", "get_r2", "get_r3", "get_r4", "checks_compute"))
        proto = ("  abstract interface\n    subroutine metric_compute()\n"
                 "    end subroutine metric_compute\n  end interface\n")
        bad = f"module bx_checks\n  implicit none\n{proto}contains\n{body}end module bx_checks\n"
        v = self._run(bad)
        self.assertTrue(any("metric_compute" in x for x in v), v)

    def test_nested_internal_procedure_is_not_a_definition(self) -> None:
        # A default-public module that defines an ABI name ONLY as an internal procedure
        # nested inside another procedure's `contains` must be caught — an internal
        # procedure is not a module entity the host-rendered runner can `use ... only:`.
        nine = "".join(
            f"  subroutine {n}()\n  end subroutine {n}\n"
            for n in ("case_setup", "case_run", "get_time", "get_scalar",
                      "get_r1", "get_r2", "get_r3", "get_r4", "checks_compute"))
        holder = ("  subroutine holder()\n  contains\n"
                  "    subroutine metric_compute()\n    end subroutine metric_compute\n"
                  "  end subroutine holder\n")
        bad = f"module bx_checks\n  implicit none\ncontains\n{nine}{holder}end module bx_checks\n"
        v = self._run(bad)
        self.assertTrue(any("metric_compute" in x for x in v), v)

    def test_abi_defined_in_second_module_is_caught(self) -> None:
        # ABI procedures defined in a SECOND module (or after `end module <sid>_checks`) are
        # not importable via `use <sid>_checks, only:` — only the target module's own
        # definitions count, so an empty/partial target module must be caught.
        ten = "".join(
            f"  subroutine {n}()\n  end subroutine {n}\n"
            for n in ("case_setup", "case_run", "get_time", "get_scalar",
                      "get_r1", "get_r2", "get_r3", "get_r4",
                      "checks_compute", "metric_compute"))
        bad = (f"module bx_checks\n  implicit none\nend module bx_checks\n"
               f"module other\n  implicit none\ncontains\n{ten}end module other\n")
        self.assertTrue(any("must publish the fixed ABI names" in v for v in self._run(bad)), bad)

    def test_target_module_second_in_file_passes(self) -> None:
        # The target checks module need not be the first module in the file; its own
        # module-level definitions still count wherever it appears.
        ten = "".join(
            f"  subroutine {n}()\n  end subroutine {n}\n"
            for n in ("case_setup", "case_run", "get_time", "get_scalar",
                      "get_r1", "get_r2", "get_r3", "get_r4",
                      "checks_compute", "metric_compute"))
        ok = ("module other\n  implicit none\ncontains\n"
              "  subroutine junk()\n  end subroutine junk\nend module other\n"
              f"module bx_checks\n  implicit none\ncontains\n{ten}end module bx_checks\n")
        self.assertFalse(any("must publish the fixed ABI names" in v for v in self._run(ok)),
                         self._run(ok))

    def test_module_level_proc_with_nested_helper_passes(self) -> None:
        # Nesting alone must not cause a false-reject: all ten ABI names ARE module-level;
        # one of them additionally carries an internal helper.
        ten = "".join(
            f"  subroutine {n}()\n  end subroutine {n}\n"
            for n in ("case_setup", "case_run", "get_time", "get_scalar",
                      "get_r1", "get_r2", "get_r3", "get_r4",
                      "checks_compute", "metric_compute"))
        extra = ("  subroutine another()\n  contains\n    subroutine inner()\n"
                 "    end subroutine inner\n  end subroutine another\n")
        ok = f"module bx_checks\n  implicit none\ncontains\n{ten}{extra}end module bx_checks\n"
        self.assertFalse(any("must publish the fixed ABI names" in v for v in self._run(ok)),
                         self._run(ok))

    def test_private_component_type_does_not_flip_module_default(self) -> None:
        # A bare `private` INSIDE a derived-type def is a component attribute, not the
        # module default — it must not turn a default-public module into a false-reject.
        type_def = ("  type :: box\n    private\n    real :: x\n  end type box\n")
        ok = self._module_defining_all(extra_spec=type_def)
        self.assertFalse(any("must publish the fixed ABI names" in v for v in self._run(ok)),
                         self._run(ok))

    def test_model_uses_harness_forbidden(self) -> None:
        bad_model = "module bx_model\nuse harness_fortran_cpu_model\nend module bx_model\n"
        self.assertTrue(any("must not `use` the harness" in v
                            for v in self._run(_CHECKS_OK, model=bad_model)))

    def test_checks_file_io_forbidden(self) -> None:
        bad = _CHECKS_OK.replace(
            "    ok = .true.\n",
            "    ok = .true.\n    open(unit=9, file='x.json')\n")
        self.assertTrue(any("file I/O" in v for v in self._run(bad)))

    def test_checks_forbidden_output_filename(self) -> None:
        bad = _CHECKS_OK.replace(
            "    ok = .true.\n", "    ok = .true.\n    ! writes verdict.json\n")
        self.assertTrue(any("verdict.json" in v for v in self._run(bad)))


class HarnessDependencyConsistencyTests(unittest.TestCase):
    """R1/M3c-β `_validate_harness_dependency_consistency` (compile stage)."""

    def _run(self, *, spec_kind="component", language="fortran", hw_class="cpu",
             infra_ids: list[str] | None = None, bare_string: bool = False) -> list[str]:
        infra_ids = ["harness_fortran_cpu"] if infra_ids is None else infra_ids
        deps: list = (
            [f"infrastructure/{i}@0.2.0" for i in infra_ids] if bare_string
            else [{"node_key": f"infrastructure/{i}@0.2.0"} for i in infra_ids])
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ir_dir = tmp / "ir"
            ir_dir.mkdir()
            ir: dict = {
                "meta": {"spec_kind": spec_kind, "spec_id": "bx"},
                "impl_defaults": {"toolchain": {"language": language},
                                  "target": {"class": hw_class}},
                "dependency": {"direct_deps": deps},
            }
            (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(ir))
            violations: list[str] = []
            vps._validate_harness_dependency_consistency(tmp, ir_dir, violations)
            return violations

    def test_correct_harness_passes(self) -> None:
        self.assertEqual(self._run(), [])

    def test_no_infra_dep_is_noop(self) -> None:
        self.assertEqual(self._run(infra_ids=[]), [])

    def test_infra_node_is_noop(self) -> None:
        self.assertEqual(self._run(spec_kind="infrastructure"), [])

    def test_wrong_harness_id(self) -> None:
        v = self._run(infra_ids=["harness_fortran_gpu"])
        self.assertTrue(any("expected 'harness_fortran_cpu'" in x for x in v), v)

    def test_bare_string_infra_dep_is_parsed(self) -> None:
        # A bare-string infra dep must be seen identically to the dict form — else the
        # conductor host-renders while this gate (and the checks gate) treat it as legacy.
        self.assertEqual(self._run(bare_string=True), [])
        v = self._run(infra_ids=["harness_fortran_gpu"], bare_string=True)
        self.assertTrue(any("expected 'harness_fortran_cpu'" in x for x in v), v)

    def test_two_infra_deps(self) -> None:
        v = self._run(infra_ids=["harness_fortran_cpu", "harness_other_cpu"])
        self.assertTrue(any("exactly one infrastructure" in x for x in v), v)

    def test_missing_target_class(self) -> None:
        v = self._run(hw_class="")
        self.assertTrue(any("cannot derive the expected harness id" in x for x in v), v)


class CaseIdGrammarGateTests(unittest.TestCase):
    """`_validate_case_ids` (compile stage): a case_id becomes the per-case snapshot PATH
    (`raw/state_snapshots/<case_id>.json`) for EVERY node kind, so a `/` or `..` lets the run
    write outside its directory. Unlike the M3c render precondition, this gate applies to
    non-M3c (leaf-authored-runner) nodes too — the wider surface the review found open."""

    def _run(self, case_ids: list) -> list[str]:
        ir = {"case": {"test_case_set": [{"case_id": c} for c in case_ids]}}
        with tempfile.TemporaryDirectory() as tmp:
            ir_dir = Path(tmp)
            (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(ir))
            v: list[str] = []
            vps._validate_case_ids(ir_dir, v)
            return v

    def test_safe_ids_pass(self) -> None:
        self.assertEqual(self._run(["c_a", "l0_v1.2-alpha", "n032", "case.dry_state"]), [])

    def test_traversal_and_separator_rejected(self) -> None:
        for bad in ("../../evil", "a/b", "..", "a..b", "x\\y", "l0_café"):
            with self.subTest(bad=bad):
                v = self._run(["c_ok", bad])
                self.assertEqual(len(v), 1, v)
                self.assertIn(repr(bad.strip()), v[0])
                self.assertIn("raw/state_snapshots", v[0])

    def test_applies_without_any_infrastructure_dep(self) -> None:
        # The IR here has no `dependency` block at all (a non-M3c node), yet the gate fires —
        # this is the gap `_validate_harness_render_preconditions` (M3c-only) left open.
        self.assertEqual(len(self._run(["../escape"])), 1)


class HarnessRenderPreconditionsTests(unittest.TestCase):
    """R1/M3c-β `_validate_harness_render_preconditions` (compile stage): mirror every
    Compile-authored render precondition of an M3c physics node's host-rendered runner, so a
    defect routes to compile.generate instead of render_runner's workflow-killing fail-close.
    The renderer-side unit `IrContentViolationsTest` pins the content surface exhaustively;
    these pin the M3c gating (no-op off M3c) and the compile-path wiring."""

    def _baseline_ir(self, *, spec_kind="component", language="fortran",
                     build_system="make", infra: bool = True) -> dict:
        deps: list = ([{"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}]
                      if infra else [])
        return {
            "meta": {"spec_kind": spec_kind, "spec_id": "bx"},
            "impl_defaults": {"toolchain": {"language": language,
                                            "build_system": build_system},
                              "target": {"class": "cpu"}},
            "dependency": {"direct_deps": deps},
            "case": {"test_case_set": [{"case_id": "c_a"}]},
            "io_contract": {
                "raw_requirements": {"required_evidence": [
                    {"artifact": "state_snapshots", "required": True, "schema": {
                        "time_variable": "t",
                        "variables": [{"name": "U", "shape_expr": "[nx, ny]"}]}}]},
                "diagnostics_contract": {
                    "checks": [{"id": "c1"}],
                    "verdict": {"required": True, "fields": ["overall", "failed_checks"]}},
                "test_predicates": [
                    {"test_id": "t1", "expected_outcome": "pass", "target_cases": ["c_a"]}],
                "test_evidence_requirements": [
                    {"test_id": "t1", "required_raw_variables": ["U"]}],
            },
        }

    def _run(self, ir: dict) -> list[str]:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            ir_dir = tmp / "ir"
            ir_dir.mkdir()
            (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(ir))
            violations: list[str] = []
            vps._validate_harness_render_preconditions(tmp, ir_dir, violations)
            return violations

    def test_clean_baseline_passes(self) -> None:
        self.assertEqual(self._run(self._baseline_ir()), [])

    def test_absent_time_variable_is_noop(self) -> None:
        # Omitted time_variable defaults to `t` in the renderer — not a violation.
        ir = self._baseline_ir()
        del ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "time_variable"]
        self.assertEqual(self._run(ir), [])

    def test_wrong_time_variable_fails(self) -> None:
        ir = self._baseline_ir()
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "time_variable"] = "time"
        v = self._run(ir)
        self.assertTrue(any("time_variable is 'time'" in x and "'t'" in x for x in v), v)

    def test_missing_meta_spec_id_still_hoists_via_node_key(self) -> None:
        # Codex P2: `meta.spec_id` absent must NOT skip the gate. The conductor host-renders with
        # the node key regardless, so a content defect (time_variable) must still surface at
        # compile — derived from the `dependency.node_key` identity, not optional metadata.
        ir = self._baseline_ir()
        ir["meta"].pop("spec_id", None)
        ir["dependency"]["node_key"] = "component/foo_bar@0.1.0"
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "time_variable"] = "time"
        v = self._run(ir)
        self.assertTrue(any("time_variable is 'time'" in x for x in v), v)

    def test_missing_all_identity_still_hoists(self) -> None:
        # Even with neither meta.spec_id nor dependency.node_key, an M3c node's content defect
        # must surface (a placeholder spec_id is used — content checks are spec_id-independent).
        ir = self._baseline_ir()
        ir["meta"].pop("spec_id", None)
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "time_variable"] = "time"
        v = self._run(ir)
        self.assertTrue(any("time_variable is 'time'" in x for x in v), v)

    def test_reserved_key_collision_fails(self) -> None:
        ir = self._baseline_ir()
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "variables"].append({"name": "step", "shape_expr": "scalar"})
        v = self._run(ir)
        self.assertTrue(any("'step'" in x and "reserved" in x for x in v), v)

    def test_verdict_fields_unsupported_fails(self) -> None:
        ir = self._baseline_ir()
        ir["io_contract"]["diagnostics_contract"]["verdict"]["fields"] = [
            "overall", "failed_checks", "score"]
        v = self._run(ir)
        self.assertTrue(any("verdict.fields" in x and "score" in x for x in v), v)

    def test_rank_over_4_fails(self) -> None:
        ir = self._baseline_ir()
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "variables"][0]["shape_expr"] = "[2, 2, 2, 2, 2]"
        v = self._run(ir)
        self.assertTrue(any("rank 5" in x for x in v), v)

    def test_no_state_snapshots_fails(self) -> None:
        # The host-rendered runner needs a snapshot schema; absence is a render fail-close on
        # an M3c node, so the gate hoists it (previously a no-op under the time-only gate).
        ir = self._baseline_ir()
        ir["io_contract"]["raw_requirements"]["required_evidence"] = []
        v = self._run(ir)
        self.assertTrue(any("state_snapshots" in x for x in v), v)

    def test_prefixes_ir_path(self) -> None:
        ir = self._baseline_ir()
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "time_variable"] = "time"
        v = self._run(ir)
        self.assertTrue(v and all("spec.ir.yaml:" in x for x in v), v)

    def test_non_m3c_node_is_noop(self) -> None:
        # No harness dep -> legacy leaf-authored runner; the gate never inspects content.
        ir = self._baseline_ir(infra=False)
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "time_variable"] = "time"
        self.assertEqual(self._run(ir), [])

    def test_infra_node_is_noop(self) -> None:
        ir = self._baseline_ir(spec_kind="infrastructure")
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "time_variable"] = "time"
        self.assertEqual(self._run(ir), [])

    def test_non_make_node_is_noop(self) -> None:
        ir = self._baseline_ir(build_system="cmake")
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "time_variable"] = "time"
        self.assertEqual(self._run(ir), [])


# The shape every certified spec.ir.yaml actually has, measured over the 116 IRs under
# `workspace*/ir/` on 2026-07-14. `workspace/` is untracked, so the corpus cannot be read at test
# time — this constant IS the pin, and it must be re-measured if the IR contract changes.
_REAL_IR_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "meta",
        "case",
        "algorithm",
        "impl_defaults",
        "io_contract",
        "dependency",
    }  # + `public_api`, on infrastructure nodes only
)
_REAL_IR_STATE_CONTRACT_FIELDS = frozenset(
    {"state_variables", "required_update_paths", "diagnostics_from_state", "fallback_policy"}
)

_UNSET = object()

_FIXTURE_IR_REL = (
    "workspace/ir/problem__shallow_water2d__0.3.0/shallow-water2d_20260415_001/spec.ir.yaml"
)


class IrFixtureShapeTests(unittest.TestCase):
    """The testing rule: a gate must be exercised against a fixture with the REAL IR shape.

    Three gates were silently dead under a green suite because their fixtures used a shape the
    pipeline never produces — a passing test proved only that the gate COULD fire, never that it
    DOES on a real artifact (`_plan_dependency_node_key`; `_validate_problem_state_array_usage`;
    and the tests.md resolution behind `_validate_test_evidence_requirements` /
    `_validate_tests_verdict_summary_consistency`, which read a `source:` key no IR has). These
    tests pin the shared factory's document against the real shape so the class cannot recur.
    """

    def _fixture_ir(self, repo_root: Path) -> dict:
        _seed_shape_expr_schema_into(repo_root)
        _create_minimal_execution_tree(
            repo_root,
            dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
            model_text="module m\nimplicit none\nend module m\n",
            runner_text="program r\nimplicit none\nend program r\n",
            run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
        )
        return json.loads((repo_root / _FIXTURE_IR_REL).read_text(encoding="utf-8"))

    def test_shared_fixture_carries_every_document_level_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = self._fixture_ir(Path(tmp))
        self.assertEqual(
            set(doc),
            set(_REAL_IR_TOP_LEVEL_KEYS),
            "the shared fixture's spec.ir.yaml must carry exactly the sections a real IR carries; "
            "a fixture missing `meta` silently disables every gate that reaches the IR through it",
        )
        self.assertTrue(
            doc["case"]["test_case_set"],
            "every certified IR declares at least one case; an empty case set is a shape the "
            "pipeline never produces, and the case->test mapping reads it",
        )

    def test_shared_fixture_places_the_state_contract_where_real_irs_place_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            algorithm = self._fixture_ir(Path(tmp))["algorithm"]
        self.assertNotIn(
            "state_contract",
            algorithm,
            "no certified IR nests the contract under `algorithm.state_contract` — the fixture must "
            "author the canonical flat placement, or the gates run against the shadow branch only",
        )
        self.assertTrue(_REAL_IR_STATE_CONTRACT_FIELDS <= set(algorithm))

    def test_shared_fixture_resolves_tests_md_the_way_a_real_ir_does(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            doc = self._fixture_ir(repo_root)
            tests_path = _tests_path_from_ir_document(repo_root, doc)
        self.assertEqual(
            tests_path,
            repo_root / MOCK_TESTS_REF,
            "tests.md is reachable only through `meta.source_refs.tests`; a fixture that does not "
            "carry it leaves the tests.md-dependent gates unexercised",
        )

    def test_shared_fixture_direct_deps_entries_are_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            direct_deps = self._fixture_ir(Path(tmp))["dependency"]["direct_deps"]
        self.assertTrue(direct_deps)
        for entry in direct_deps:
            self.assertIsInstance(
                entry,
                dict,
                "every `direct_deps` entry of every certified IR is an object (node_key / kind / "
                "operations); a bare string exercises a compat branch no producer emits",
            )
            self.assertIn("node_key", entry)

    def _compile_with_tests_md(
        self,
        repo_root: Path,
        *,
        test_ids: tuple[str, ...] = ("t1",),
        evidence: object = _UNSET,
        drop_tests_ref: bool = False,
        tests_md_body: str | None = None,
    ) -> list[str]:
        """Drive the compile stage over the shared fixture with a real tests.md in place.

        This is the state the revived gates actually run in: `meta.source_refs.tests` resolves, so
        `tests.md` is the canonical test-id set and `io_contract.test_evidence_requirements` is
        pinned against it.
        """
        self._fixture_ir(repo_root)
        tests_md = repo_root / MOCK_TESTS_REF
        tests_md.parent.mkdir(parents=True, exist_ok=True)
        tests_md.write_text(
            tests_md_body
            if tests_md_body is not None
            else "".join(f"### 1-{i}. `{t}`\n" for i, t in enumerate(test_ids, start=1)),
            encoding="utf-8",
        )

        ir_path = repo_root / _FIXTURE_IR_REL
        doc = json.loads(ir_path.read_text(encoding="utf-8"))
        if evidence is not _UNSET:
            doc["io_contract"]["test_evidence_requirements"] = evidence
        if drop_tests_ref:
            doc["meta"]["source_refs"].pop("tests", None)
        ir_path.write_text(json.dumps(doc), encoding="utf-8")
        return validate_compile_stage(
            repo_root, "workspace", str(Path(_FIXTURE_IR_REL).parent)
        )

    def test_evidence_requirements_must_cover_every_tests_md_test(self) -> None:
        """The pin the revival exists for: an IR that omits one test's evidence requirement used to
        certify clean, because tests.md was never resolved."""
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._compile_with_tests_md(
                Path(tmp),
                test_ids=("t1", "t2"),
                evidence=[{"test_id": "t1", "required_raw_variables": ["h"]}],
            )
        self.assertTrue(
            [v for v in violations if "missing tests from tests.md" in v and "t2" in v], violations
        )

    def test_evidence_requirements_reject_a_test_id_absent_from_tests_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._compile_with_tests_md(
                Path(tmp),
                test_ids=("t1",),
                evidence=[
                    {"test_id": "t1", "required_raw_variables": ["h"]},
                    {"test_id": "t_ghost", "required_raw_variables": ["h"]},
                ],
            )
        self.assertTrue(
            [v for v in violations if "unknown test_id" in v and "t_ghost" in v], violations
        )

    def test_evidence_requirements_reject_a_duplicated_test_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._compile_with_tests_md(
                Path(tmp),
                test_ids=("t1",),
                evidence=[
                    {"test_id": "t1", "required_raw_variables": ["h"]},
                    {"test_id": "t1", "required_raw_variables": ["hu"]},
                ],
            )
        self.assertTrue([v for v in violations if "duplicated test_id" in v], violations)

    def test_evidence_requirements_are_clean_when_the_set_matches(self) -> None:
        """The negative twin: the pin does not simply reject everything."""
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._compile_with_tests_md(
                Path(tmp),
                test_ids=("t1",),
                evidence=[{"test_id": "t1", "required_raw_variables": ["h"]}],
            )
        self.assertFalse(
            [v for v in violations if "test_evidence_requirements" in v], violations
        )

    def test_absent_tests_ref_fails_compile(self) -> None:
        """The other branch of the ref gate: the key missing entirely, not merely unresolvable."""
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._compile_with_tests_md(Path(tmp), drop_tests_ref=True)
        self.assertTrue(
            [v for v in violations if "meta.source_refs.tests missing" in v], violations
        )

    def test_tests_ref_naming_a_directory_is_a_violation_not_a_traceback(self) -> None:
        """`meta.source_refs.tests` is LLM-authored: a ref naming the spec directory instead of
        tests.md passes an existence check, and reading it would raise `IsADirectoryError` out of
        the gate — a traceback where the compile leaf expects its repair findings."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._fixture_ir(repo_root)
            spec_dir = repo_root / MOCK_SPEC_DIR
            spec_dir.mkdir(parents=True, exist_ok=True)
            ir_path = repo_root / _FIXTURE_IR_REL
            doc = json.loads(ir_path.read_text(encoding="utf-8"))
            doc["meta"]["source_refs"]["tests"] = MOCK_SPEC_DIR  # the directory, not tests.md
            ir_path.write_text(json.dumps(doc), encoding="utf-8")
            violations = validate_compile_stage(
                repo_root, "workspace", str(Path(_FIXTURE_IR_REL).parent)
            )
        self.assertTrue(
            [v for v in violations if "meta.source_refs.tests" in v and "unresolvable" in v],
            violations,
        )

    def test_tests_md_that_parses_to_zero_test_ids_fails_compile(self) -> None:
        """A resolvable tests.md whose test-id form is unrecognized would silently degrade every
        test-id pin to the same-IR fallback, so it is a violation rather than a no-op."""
        with tempfile.TemporaryDirectory() as tmp:
            violations = self._compile_with_tests_md(
                Path(tmp), tests_md_body="## 4. Tests\nno test ids here\n"
            )
        self.assertTrue([v for v in violations if "parsed 0 test_ids" in v], violations)

    def _validate_stage_via_cli(self, repo_root: Path, argv: list[str]) -> list[str]:
        """Drive `main()` so the STAGE -> flag wiring is what is under test, not a hand-passed flag."""
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            vps.main(["--repo-root", str(repo_root), "--workspace-root", "workspace", *argv])
        return [line for line in buf.getvalue().splitlines() if line.startswith("- ")]

    def test_only_post_execute_waives_the_verdict(self) -> None:
        """Pin the STAGE wiring, not just the parameter.

        `post_execute` is the one stage where verdict.json is legitimately absent (the conductor
        authors it only after that gate returns clean). Every other stage must demand it: hardcode
        `require_verdict=True` and every node fails `verdict.json: missing` on every run; hardcode
        it False and the pre_judge pin is fail-open again — the defect being repaired.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._fixture_ir(repo_root)  # no verdict.json anywhere in the tree
            tests_md = repo_root / MOCK_TESTS_REF
            tests_md.parent.mkdir(parents=True, exist_ok=True)
            tests_md.write_text("### 1-1. `t1`\n", encoding="utf-8")

            at_execute = self._validate_stage_via_cli(repo_root, ["--stage", "post_execute"])
            at_full = self._validate_stage_via_cli(
                repo_root,
                ["--stage", "full", "--legacy-mode", "--allow-missing-llm-review"],
            )
            # `--allow-missing-orchestration` waives the ORCHESTRATION artifacts. Coupling the
            # verdict to it (require_verdict = not allow_missing_orchestration) silently switched
            # the verdict/summary pin off, which is a fail-open the flag never promised.
            at_full_no_orch = self._validate_stage_via_cli(
                repo_root,
                [
                    "--stage", "full", "--legacy-mode",
                    "--allow-missing-llm-review", "--allow-missing-orchestration",
                ],
            )
        self.assertFalse(
            [v for v in at_execute if "verdict.json: missing" in v],
            "post_execute must not demand a verdict the conductor has not authored yet",
        )
        self.assertTrue(
            [v for v in at_full if "verdict.json: missing" in v],
            "every other stage must demand the verdict",
        )
        self.assertTrue(
            [v for v in at_full_no_orch if "verdict.json: missing" in v],
            "--allow-missing-orchestration must not waive the verdict pin",
        )

    def test_unresolvable_tests_ref_fails_compile(self) -> None:
        """The three tests.md pins all no-op silently when the ref does not resolve, so the ref
        itself is gated at compile — otherwise a mistyped (or rename-orphaned) ref re-opens the
        fail-open hole this change closed."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._fixture_ir(repo_root)  # factory points meta.source_refs.tests at MOCK_TESTS_REF
            ir_ref = str(Path(_FIXTURE_IR_REL).parent)

            without_tests_md = validate_compile_stage(repo_root, "workspace", ir_ref)
            self.assertTrue(
                [v for v in without_tests_md if "meta.source_refs.tests" in v and "unresolvable" in v],
                without_tests_md,
            )

            (repo_root / MOCK_TESTS_REF).parent.mkdir(parents=True, exist_ok=True)
            (repo_root / MOCK_TESTS_REF).write_text("### 1-1. `t1`\n", encoding="utf-8")
            with_tests_md = validate_compile_stage(repo_root, "workspace", ir_ref)
        self.assertFalse(
            [v for v in with_tests_md if "meta.source_refs.tests" in v],
            with_tests_md,
        )

    def test_missing_algorithm_section_is_a_violation_not_a_traceback(self) -> None:
        """The section guard must not turn an IR defect into a validator crash: an IR whose
        `algorithm:` is absent (or is not a mapping) is exactly what the gate exists to catch, and
        the compile leaf repairs from the violation text — a `ValueError` would hand it a traceback
        about the validator's own internals and no findings at all."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._fixture_ir(repo_root)
            ir_path = repo_root / _FIXTURE_IR_REL
            doc = json.loads(ir_path.read_text(encoding="utf-8"))
            doc["algorithm"] = None
            ir_path.write_text(json.dumps(doc), encoding="utf-8")
            violations = validate_compile_stage(
                repo_root, "workspace", str(Path(_FIXTURE_IR_REL).parent)
            )
        self.assertTrue(
            [v for v in violations if "algorithm section missing or not a mapping" in v],
            violations,
        )

    def test_document_level_key_inside_the_algorithm_section_does_not_raise(self) -> None:
        """The section guard discriminates document-from-section by key, so a section carrying a
        document-level key would otherwise reach `_require_ir_section`'s raise — and the conductor
        would hand the compile leaf a traceback as its repair findings. The key is ignored for the
        read (no canonical document forbids it), and the IR validates as it otherwise would.
        """
        for stray in ("schema_version", "algorithm"):
            with self.subTest(stray=stray), tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                self._fixture_ir(repo_root)
                (repo_root / MOCK_TESTS_REF).parent.mkdir(parents=True, exist_ok=True)
                (repo_root / MOCK_TESTS_REF).write_text("### 1-1. `t1`\n", encoding="utf-8")
                ir_path = repo_root / _FIXTURE_IR_REL
                doc = json.loads(ir_path.read_text(encoding="utf-8"))
                doc["algorithm"][stray] = "1.0"
                doc["io_contract"]["test_evidence_requirements"] = [
                    {"test_id": "t1", "required_raw_variables": ["h"]}
                ]
                ir_path.write_text(json.dumps(doc), encoding="utf-8")
                violations = validate_compile_stage(  # must not raise
                    repo_root, "workspace", str(Path(_FIXTURE_IR_REL).parent)
                )
                self.assertFalse(
                    [v for v in violations if "algorithm" in v and "must be" in v], violations
                )

    def test_over_long_tests_ref_is_a_violation_not_a_traceback(self) -> None:
        """`Path.is_file()` is not total — an over-long path (ENAMETOOLONG) raises out of the probe
        itself, so probing an LLM-authored ref with it would crash the validator before the leaf
        gets its finding."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._fixture_ir(repo_root)
            # The parent must EXIST, or the probe short-circuits on ENOENT and never reaches the
            # over-long component — which is what ENAMETOOLONG needs to surface.
            (repo_root / MOCK_SPEC_DIR).mkdir(parents=True, exist_ok=True)
            ir_path = repo_root / _FIXTURE_IR_REL
            doc = json.loads(ir_path.read_text(encoding="utf-8"))
            doc["meta"]["source_refs"]["tests"] = f"{MOCK_SPEC_DIR}/{'a' * 300}.md"
            ir_path.write_text(json.dumps(doc), encoding="utf-8")
            violations = validate_compile_stage(  # must not raise
                repo_root, "workspace", str(Path(_FIXTURE_IR_REL).parent)
            )
        self.assertTrue(
            [v for v in violations if "meta.source_refs.tests" in v and "unresolvable" in v],
            violations,
        )

    def test_non_utf8_ir_is_a_violation_not_a_traceback(self) -> None:
        """A leaf-authored artifact need not be valid UTF-8. `_read_yaml` decoded strictly, so a
        `UnicodeDecodeError` escaped every caller (they guard `yaml.YAMLError` only) and reached the
        leaf as a traceback instead of a finding."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._fixture_ir(repo_root)
            (repo_root / _FIXTURE_IR_REL).write_bytes(b"\xff\xfeschema_version: '1.0'\n")
            violations = validate_compile_stage(  # must not raise
                repo_root, "workspace", str(Path(_FIXTURE_IR_REL).parent)
            )
        self.assertTrue(violations)

    def test_non_utf8_ir_is_not_silently_sanitized(self) -> None:
        """The trap in the fix above: decoding leniently (`errors="ignore"`) DELETES the offending
        bytes, so an IR whose invalid byte sits in a comment sanitizes into a clean document and
        CERTIFIES. An artifact that is not valid UTF-8 must fail, not be silently rewritten."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._fixture_ir(repo_root)
            (repo_root / MOCK_TESTS_REF).parent.mkdir(parents=True, exist_ok=True)
            (repo_root / MOCK_TESTS_REF).write_text("### 1-1. `t1`\n", encoding="utf-8")
            ir_path = repo_root / _FIXTURE_IR_REL
            doc = json.loads(ir_path.read_text(encoding="utf-8"))
            doc["io_contract"]["test_evidence_requirements"] = [
                {"test_id": "t1", "required_raw_variables": ["h"]}
            ]
            clean = yaml.safe_dump(doc, sort_keys=False).encode("utf-8")
            self.assertFalse(
                [v for v in self._compile(repo_root, ir_path, clean) if "invalid json" in v],
                "the fixture must certify while it is valid UTF-8",
            )
            # the SAME document, plus one invalid byte inside a YAML comment
            dirty = b"# note: \xff invalid byte in a comment\n" + clean
            violations = self._compile(repo_root, ir_path, dirty)
        self.assertTrue([v for v in violations if "invalid json" in v], violations)

    def _compile(self, repo_root: Path, ir_path: Path, body: bytes) -> list[str]:
        ir_path.write_bytes(body)
        return validate_compile_stage(
            repo_root, "workspace", str(Path(_FIXTURE_IR_REL).parent)
        )

    def test_non_utf8_json_evidence_is_a_violation_not_a_traceback(self) -> None:
        """`UnicodeDecodeError` is a `ValueError`, not a `json.JSONDecodeError`, so a non-UTF-8
        artifact escaped every caller — all of which guard `JSONDecodeError` and report
        `invalid json` as the finding the leaf repairs. The runner that writes these files is
        leaf-authored Fortran, so this is a shape the workflow can actually produce."""
        from tools.validate_pipeline_semantics import _read_json

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "metrics_basis.json"
            bad.write_bytes(b"\xff{}")
            with self.assertRaises(json.JSONDecodeError):  # not UnicodeDecodeError
                _read_json(bad)

    def test_deeply_nested_ir_is_a_violation_not_a_traceback(self) -> None:
        """A pathologically nested document raises `RecursionError`, which `yaml.YAMLError` does not
        cover, so it escaped the gates that guard the IR read."""
        from tools.validate_pipeline_semantics import _read_yaml

        with tempfile.TemporaryDirectory() as tmp:
            bomb = Path(tmp) / "spec.ir.yaml"
            bomb.write_text("a: " + "[" * 50000, encoding="utf-8")
            with self.assertRaises(yaml.YAMLError):  # not RecursionError
                _read_yaml(bomb)

    def test_malformed_ir_does_not_raise_out_of_the_document_reader(self) -> None:
        """`_ir_document_for_execution` feeds the post_execute case/test mappings. It read the IR
        unguarded, so a malformed `spec.ir.yaml` raised `yaml.ParserError` out of the gate loop —
        while the contract-file gate reports the same IR as invalid at the same stage."""
        from tools.validate_pipeline_semantics import _ir_document_for_execution

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._fixture_ir(repo_root)
            (repo_root / _FIXTURE_IR_REL).write_text("a: [unterminated\n", encoding="utf-8")
            pipeline_dir = (
                repo_root / "workspace/pipelines/problem__shallow_water2d__0.3.0"
                / "shallow-water2d_20260415_001"
            )
            execution = vps.NodeExecution(
                node_key="problem/shallow_water2d@0.3.0",
                node_dir=pipeline_dir,
                exec_dir=pipeline_dir,
                pipeline_dir=pipeline_dir,
            )
            self.assertIsNone(_ir_document_for_execution(repo_root, execution))  # must not raise

    def test_non_utf8_command_log_does_not_raise(self) -> None:
        """`command_log.jsonl` is in the generate/verify leaf's `allowed_output_paths`, so a stray
        byte in it is leaf-authored content: the readers must degrade to "record not found" (itself
        a violation) rather than raise."""
        from tools.validate_pipeline_semantics import _find_command_log_record

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            log_ref = "workspace/pipelines/n/p/source/s/src/command_log.jsonl"
            log = repo_root / log_ref
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_bytes(
                b'{"command_id": "cmd_ok", "tool": "run_linter"}\n\xff\xfe{"command_id": "x"}\n'
            )
            found = _find_command_log_record(repo_root, "cmd_ok", log_ref)  # must not raise
            self.assertIsNotNone(found)
            self.assertIsNone(_find_command_log_record(repo_root, "cmd_absent", log_ref))

            # `_validate_trial_meta` scans the same log through its own reader.
            node_dir = repo_root / "node"
            node_dir.mkdir()
            _write_json(
                node_dir / "trial_meta.json",
                {
                    "source_command_ref": [
                        {"command_id": "cmd_ok", "command_log_ref": log_ref},
                    ]
                },
            )
            execution = vps.NodeExecution(
                node_key="component/n@0.1.0",
                node_dir=node_dir,
                exec_dir=node_dir,
                pipeline_dir=node_dir,
            )
            violations: list[str] = []
            vps._validate_trial_meta(repo_root, execution, violations)  # must not raise
        self.assertFalse(
            [v for v in violations if "command_id" in v and "not found" in v], violations
        )

    def test_over_long_controlled_spec_ref_is_a_violation_not_a_traceback(self) -> None:
        """`meta.source_refs.controlled_spec` is LLM-authored like the tests ref, and the two
        infrastructure gates probe it. Without the total probe an over-long ref raises ENAMETOOLONG
        out of the gate."""
        from tools.validate_pipeline_semantics import (
            _validate_infrastructure_generated_signatures,
            _validate_infrastructure_public_api,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._fixture_ir(repo_root)
            (repo_root / MOCK_SPEC_DIR).mkdir(parents=True, exist_ok=True)
            ir_dir = repo_root / Path(_FIXTURE_IR_REL).parent
            doc = json.loads((ir_dir / "spec.ir.yaml").read_text(encoding="utf-8"))
            doc["meta"]["spec_kind"] = "infrastructure"
            doc["meta"]["spec_id"] = "harness_x"
            doc["meta"]["source_refs"]["controlled_spec"] = (
                f"{MOCK_SPEC_DIR}/{'a' * 300}.md"
            )
            (ir_dir / "spec.ir.yaml").write_text(json.dumps(doc), encoding="utf-8")

            for gate in (
                _validate_infrastructure_public_api,
                _validate_infrastructure_generated_signatures,
            ):
                violations: list[str] = []
                try:
                    gate(repo_root, ir_dir, violations)  # must not raise
                except TypeError:
                    # signature gate takes the source dir; drive it through the compile stage below
                    continue
            compile_violations = validate_compile_stage(
                repo_root, "workspace", str(Path(_FIXTURE_IR_REL).parent)
            )
        self.assertTrue(
            [v for v in compile_violations if "controlled_spec" in v], compile_violations
        )

    def test_unreadable_controlled_spec_degrades_to_an_empty_parse(self) -> None:
        """The controlled_spec ref is LLM-authored too: its probe is total, but the READ can still
        fail (a file that stats but does not open). Both §5 parsers absorb it — the callers already
        report an empty parse as a fail-closed violation."""
        from tools.validate_pipeline_semantics import (
            _parse_canonical_interface_from_controlled_spec,
            _parse_public_api_from_controlled_spec,
        )

        with tempfile.TemporaryDirectory() as tmp:
            unreadable = Path(tmp)  # a directory: stats fine, does not open
            self.assertEqual(
                _parse_public_api_from_controlled_spec(unreadable, "spec_x"), (set(), set())
            )
            _, _, err = _parse_canonical_interface_from_controlled_spec(unreadable)
            self.assertIsNotNone(err)

    def test_unreadable_tests_md_degrades_to_no_test_ids(self) -> None:
        """A file readable at probe time but not at read time is what the probe cannot cover, so the
        parser absorbs it rather than raising into the gate."""
        from tools.validate_pipeline_semantics import _parse_test_ids_from_tests_md

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_parse_test_ids_from_tests_md(Path(tmp)), [])

    def test_ir_document_cannot_be_passed_where_a_section_is_expected(self) -> None:
        """The class-kill: handing the whole document to a section-level reader now raises."""
        with tempfile.TemporaryDirectory() as tmp:
            doc = self._fixture_ir(Path(tmp))
        with self.assertRaises(ValueError):
            _algorithm_state_contract(doc)
        with self.assertRaises(ValueError):
            _require_ir_section(doc, "io_contract")
        # The section itself passes through untouched.
        self.assertIs(_require_ir_section(doc["algorithm"], "algorithm"), doc["algorithm"])
        self.assertIsNotNone(_algorithm_state_contract(doc["algorithm"]))


class PureLaunchRecordSweepTest(unittest.TestCase):
    """M-B: the launch-record sweep's Z2 pure-leaf checks — request/prompt agreement, the
    reduced pure marker set, the ABSENCE of an output manifest, and the pure_readonly / empty
    write_roots capability shape. Built by converting the minimal orchestration tree's
    generate.generate substep into a pure record, then mutating each invariant."""

    _ORCH = "orch_test_001"
    _ARID = "substep_run_generate_generate_001"
    _NODE = "problem/shallow_water2d@0.3.0"

    def _build_tree(self, repo_root: Path) -> None:
        _seed_shape_expr_schema_into(repo_root)
        model_text = (
            "module shallow_water2d_model\n"
            "use dynamics_shallow_water_flux_2d_rusanov_p0_model\n"
            "implicit none\ncontains\nsubroutine solve(flag)\n"
            "  logical, intent(out) :: flag\n"
            "  call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(flag)\n"
            "end subroutine solve\nend module shallow_water2d_model\n"
        )
        runner_text = ("program shallow_water2d_runner\nimplicit none\n"
                       "write(*,*) 'ok'\nend program shallow_water2d_runner\n")
        _create_minimal_execution_tree(
            repo_root,
            dep_spec_id="dynamics_shallow_water_flux_2d_rusanov_p0",
            model_text=model_text,
            runner_text=runner_text,
            run_command=["./simulate", "workspace/spec.ir.yaml", "workspace/outdir"],
        )
        _create_minimal_orchestration_tree(repo_root)

    def _orch_root(self, repo_root: Path) -> Path:
        return repo_root / "workspace" / "orchestrations" / self._ORCH

    def _make_pure(self, repo_root: Path, *, capability_mode: str = "pure_readonly",
                   pure_prompt: bool = True) -> None:
        """Convert the generate.generate substep launch record into a pure record."""
        from tools.orchestration_runtime import (
            prepare_launch_request_payload, render_launch_prompt_text)
        from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION
        orch_root = self._orch_root(repo_root)
        req_path = orch_root / "launches" / f"{self._ARID}.request.json"
        req = json.loads(req_path.read_text(encoding="utf-8"))
        req["leaf_mode"] = "pure"
        # A real pure launch request carries the contract version; the audit re-checks its value.
        req["prompt_contract_version"] = PURE_PROMPT_CONTRACT_VERSION
        req_path.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
        if pure_prompt:
            pure_render_req = prepare_launch_request_payload({
                "leaf_mode": "pure", "agent_model": "opus", "agent_role": "substep",
                "node_key": self._NODE, "step": "generate", "substep": "generate",
                "orchestration_id": self._ORCH, "agent_run_id": self._ARID,
                "parent_agent_run_id": "orch_run_001",
                "ir_ref": "workspace/ir/problem__shallow_water2d__0.3.0/x_001",
                "pipeline_ref": "workspace/pipelines/problem__shallow_water2d__0.3.0/x_001",
                "dependency_ref": "workspace/ir/problem__shallow_water2d__0.3.0/x_001/spec.ir.yaml",
                "source_id": "src_20260415_001",
                "prompt_contract_version": PURE_PROMPT_CONTRACT_VERSION,
                "allowed_output_paths": [],
                "pure_context": {"harness_capabilities": "{}", "target_profile": "t",
                                 "ir_document": "i", "tests_document": "t"},
            })
            prompt = render_launch_prompt_text(pure_render_req)
            (orch_root / "launches" / f"{self._ARID}.prompt.txt").write_text(
                prompt, encoding="utf-8")
        # A pure launch's zero-authority capability.
        cap_dir = orch_root / "capabilities"
        cap_dir.mkdir(parents=True, exist_ok=True)
        (cap_dir / f"{self._ARID}.json").write_text(
            json.dumps({"agent_run_id": self._ARID, "mode": capability_mode,
                        "write_roots": [], "mcp_permissions": [],
                        "step": "generate", "substep": "generate"}),
            encoding="utf-8")
        # Deny-all read manifest + read-only sandbox profile a real pure launch persists.
        rman_dir = orch_root / "read_manifests"
        rman_dir.mkdir(parents=True, exist_ok=True)
        (rman_dir / f"{self._ARID}.json").write_text(
            json.dumps({"agent_run_id": self._ARID, "allowed_read_roots": [],
                        "denied_read_roots": ["./"]}),
            encoding="utf-8")
        sbx_dir = orch_root / "sandbox_profiles"
        sbx_dir.mkdir(parents=True, exist_ok=True)
        (sbx_dir / f"{self._ARID}.json").write_text(
            json.dumps({"agent_run_id": self._ARID, "readonly": True, "write_roots": [],
                        "read_roots": []}),
            encoding="utf-8")

    def _pure_violations(self, repo_root: Path) -> list[str]:
        violations = validate(repo_root=repo_root, workspace_root="workspace",
                              require_orchestration=True)
        return [v for v in violations
                if "pure launch" in v or "pure-launch mismatch" in v]

    def test_consistent_pure_record_has_no_pure_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._build_tree(repo_root)
            self._make_pure(repo_root)
            self.assertEqual(self._pure_violations(repo_root), [])

    def test_pure_record_with_output_manifest_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._build_tree(repo_root)
            self._make_pure(repo_root)
            om_dir = self._orch_root(repo_root) / "output_manifests"
            om_dir.mkdir(parents=True, exist_ok=True)
            (om_dir / f"{self._ARID}.json").write_text("{}", encoding="utf-8")
            self.assertTrue(
                any("must NOT have an output manifest" in v
                    for v in self._pure_violations(repo_root)))

    def test_pure_record_wrong_capability_mode_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._build_tree(repo_root)
            self._make_pure(repo_root, capability_mode="readwrite")
            self.assertTrue(
                any("capability mode must be 'pure_readonly'" in v
                    for v in self._pure_violations(repo_root)))

    def test_pure_request_with_nonpure_prompt_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._build_tree(repo_root)
            # Request declares pure but the prompt stays the original (non-pure) full body.
            self._make_pure(repo_root, pure_prompt=False)
            self.assertTrue(
                any("pure-launch mismatch" in v
                    for v in self._pure_violations(repo_root)))

    def test_pure_record_nonempty_capability_write_roots_flagged(self) -> None:
        # Positive test for the sweep's `write_roots must be []` clause (otherwise it could be
        # deleted and stay green — _make_pure always writes []).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._build_tree(repo_root)
            self._make_pure(repo_root)
            cap_path = self._orch_root(repo_root) / "capabilities" / f"{self._ARID}.json"
            cap = json.loads(cap_path.read_text(encoding="utf-8"))
            cap["write_roots"] = ["workspace/pipelines/x/source/s/src/"]
            cap_path.write_text(json.dumps(cap), encoding="utf-8")
            self.assertTrue(
                any("write_roots must be []" in v
                    for v in self._pure_violations(repo_root)))

    def test_pure_record_nonempty_capability_mcp_permissions_flagged(self) -> None:
        # Positive test for the sweep's `mcp_permissions must be []` tripwire — a pure leaf
        # invokes no gate/MCP, so a populated list is a zero-authority-record violation.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._build_tree(repo_root)
            self._make_pure(repo_root)
            cap_path = self._orch_root(repo_root) / "capabilities" / f"{self._ARID}.json"
            cap = json.loads(cap_path.read_text(encoding="utf-8"))
            cap["mcp_permissions"] = ["build-runtime"]
            cap_path.write_text(json.dumps(cap), encoding="utf-8")
            self.assertTrue(
                any("mcp_permissions must be []" in v
                    for v in self._pure_violations(repo_root)))

    def test_pure_record_obsolete_contract_version_flagged(self) -> None:
        # The audit re-checks the prompt_contract_version VALUE (request + prompt), not just the
        # marker name — an obsolete/forged version in a persisted record must be caught.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._build_tree(repo_root)
            self._make_pure(repo_root)
            root = self._orch_root(repo_root)
            req_path = root / "launches" / f"{self._ARID}.request.json"
            req = json.loads(req_path.read_text(encoding="utf-8"))
            req["prompt_contract_version"] = "pure-OBSOLETE"
            req_path.write_text(json.dumps(req), encoding="utf-8")
            prompt_path = root / "launches" / f"{self._ARID}.prompt.txt"
            prompt_path.write_text(
                prompt_path.read_text(encoding="utf-8").replace(
                    "prompt_contract_version: pure-1",
                    "prompt_contract_version: pure-OBSOLETE"),
                encoding="utf-8")
            self.assertTrue(
                any("prompt_contract_version" in v for v in self._pure_violations(repo_root)))

    def test_pure_record_omitted_mcp_permissions_flagged(self) -> None:
        # An absent mcp_permissions (truncated/hand-crafted capability) must be flagged, not
        # defaulted to [] — the producer always emits an explicit empty list.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._build_tree(repo_root)
            self._make_pure(repo_root)
            cap_path = self._orch_root(repo_root) / "capabilities" / f"{self._ARID}.json"
            cap = json.loads(cap_path.read_text(encoding="utf-8"))
            cap.pop("mcp_permissions", None)
            cap_path.write_text(json.dumps(cap), encoding="utf-8")
            self.assertTrue(
                any("mcp_permissions must be []" in v
                    for v in self._pure_violations(repo_root)))

    def test_pure_record_non_denyall_read_manifest_flagged(self) -> None:
        # A pure launch mistakenly provisioned with a non-empty read manifest must be caught.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._build_tree(repo_root)
            self._make_pure(repo_root)
            rman_path = self._orch_root(repo_root) / "read_manifests" / f"{self._ARID}.json"
            rman = json.loads(rman_path.read_text(encoding="utf-8"))
            rman["allowed_read_roots"] = ["workspace/ir/"]
            rman_path.write_text(json.dumps(rman), encoding="utf-8")
            self.assertTrue(
                any("read manifest allowed_read_roots must be []" in v
                    for v in self._pure_violations(repo_root)))

    def test_pure_record_writable_sandbox_profile_flagged(self) -> None:
        # A pure launch provisioned through the generic (writable/non-readonly) sandbox path
        # must be caught.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._build_tree(repo_root)
            self._make_pure(repo_root)
            sbx_path = self._orch_root(repo_root) / "sandbox_profiles" / f"{self._ARID}.json"
            sbx = json.loads(sbx_path.read_text(encoding="utf-8"))
            sbx["readonly"] = False
            sbx["write_roots"] = ["workspace/pipelines/x/source/s/src/"]
            sbx_path.write_text(json.dumps(sbx), encoding="utf-8")
            violations = self._pure_violations(repo_root)
            self.assertTrue(any("sandbox profile must be readonly" in v for v in violations))
            self.assertTrue(any("sandbox profile write_roots must be []" in v for v in violations))

    def test_pure_record_missing_marker_flagged(self) -> None:
        # Positive test for the sweep's pure marker set: strip a required NON-sentinel marker
        # (keep the sentinel so the record still classifies as pure) and confirm the generic
        # marker violation fires for the pure record.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._build_tree(repo_root)
            self._make_pure(repo_root)
            prompt_path = self._orch_root(repo_root) / "launches" / f"{self._ARID}.prompt.txt"
            lines = prompt_path.read_text(encoding="utf-8").splitlines()
            kept = [ln for ln in lines if not ln.startswith("prompt_contract_version:")]
            prompt_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            violations = validate(repo_root=repo_root, workspace_root="workspace",
                                  require_orchestration=True)
            # The stripped marker is the pure-only `prompt_contract_version:`, so the violation
            # naming it is unambiguously from the pure record.
            self.assertTrue(
                any("missing launch-prompt template markers" in v
                    and "prompt_contract_version:" in v
                    for v in violations))


if __name__ == "__main__":
    unittest.main()
