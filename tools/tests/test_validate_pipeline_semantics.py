#!/usr/bin/env python3
"""Regression tests for pipeline semantic validation anti-cheat rules."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.validate_pipeline_semantics import validate


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _create_minimal_execution_tree(
    repo_root: Path,
    *,
    dep_spec_id: str,
    model_text: str,
    runner_text: str,
    run_command: list[str],
    extra_sources: dict[str, str] | None = None,
    makefile_text: str | None = None,
    derived_contract: dict[str, object] | None = None,
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
            "plan_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_test",
            "dependency_ref": "workspace/plans/problem__shallow_water2d__0.3.0/plan_test/dependency.resolved.yaml",
        },
    )
    _write_json(
        workspace / "plans" / "problem__shallow_water2d__0.3.0" / "plan_test" / "dependency.resolved.yaml",
        {
            "node_key": "problem/shallow_water2d@0.3.0",
            "direct_deps": [f"component/{dep_spec_id}@0.1.0"],
            "transitive_deps": [f"component/{dep_spec_id}@0.1.0"],
            "topo_level": 1,
        },
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
                            "state_variables": ["h", "hu", "hv"],
                            "time_variable": "time",
                        },
                    },
                ]
            },
        }
    _write_json(
        workspace / "plans" / "problem__shallow_water2d__0.3.0" / "plan_test" / "derived_contract.json",
        derived_contract,
    )

    _write_json(node_dir / "diagnostics.json", {"metric": 1.0})
    _write_json(node_dir / "perf.json", {"runtime_sec": 0.01})
    _write_json(raw_dir / "metrics_basis.json", {"basis": 2.0})
    _write_json(raw_dir / "execution_trace.json", {"trace": ["step1", "step2"]})
    _write_json(
        snapshots_dir / "snapshot_schema.json",
        {"state_variables": ["h", "hu", "hv"], "time_variable": "time"},
    )
    _write_json(snapshots_dir / "snapshot000.json", {"h": 1.0, "hu": 0.0, "hv": 0.0, "time": 0.0})
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


class ValidatePipelineSemanticsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
