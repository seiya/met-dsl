---
name: workflow-build
description: Use this when running the Build stage and building a `source` artifact with `compile_project` via the `MCP` server to create a `binary_id` artifact. It applies to the work of observing the standard-build-tool constraint of `fortran` / `c` / `cpp` / `mixed` families.
---

# Workflow Build

> **Execution mode.** This procedure runs either as a leaf `step agent` (default) or, when `METDSL_CONDUCTOR_DETERMINISTIC_BUILD` is set, **in-process by the conductor** (no leaf). The I/O contract — `compile_project` via MCP, out-of-source `OBJDIR`/`BINDIR` overrides (no `BIN`), `binary_meta.json`, `command_log.jsonl`, and the `post_build` gate — is identical in both modes; the conductor path additionally writes full `compile.stdout.log` / `compile.stderr.log`. See [`docs/workflow/phases/phase_03_build.md`](../../docs/workflow/phases/phase_03_build.md) "Deterministic conductor execution".

## Purpose
Fix the execution responsibility of the Build stage, and generate a reproducible build artifact.

## Scope
- the work of generating `workspace/pipelines/<pipeline_id>/binary/<binary_id>/`
- the work of building an artifact whose `source_meta.json` has `verification_status=pass`

## Requirements
- The validator gates this phase can launch use the "substep ↔ allowed validator gate correspondence table" of `skills/workflow-orchestration/references/launch_prompts.md` as the canonical source.
- `compile` uses the `MCP` server's `compile_project`.
- For `fortran` / `c` / `cpp` / `mixed` families, only the standard build tools `make` / `cmake` / `meson` / `ninja` are allowed.
- A one-off build of `gcc` / `clang` / `gfortran` is forbidden.
- With `spec.ir.yaml.impl_defaults.toolchain.build_system=make`, the input `src/Makefile` makes explicit the language-dependent compile-order dependencies as target prerequisites, and requires that its success/failure does not change with `make -j`.
- **out-of-source override (`build_system=make`):** the in-source Make passes `OBJDIR=<abs>/workspace/tmp/<agent_run_id>/build` and `BINDIR=<abs>/<pipeline>/binary/<binary_id>/bin` via the `extra_args` of `compile_project` (make variable overrides appended after `make -j<jobs> <target>`). **Override only these two — do not pass `BIN`.** The binary basename is owned by the Makefile's `BIN` default (`<spec_id>_runner`); `Validate.execute`'s `make test` resolves the same default with no `BIN` in its env, so renaming the binary at build (e.g. `BIN=<slug>`) desyncs the two and makes the execute guard fail (or, with a relinking guard, an `unauthorized_write_violation` → `fail_closed`). This outputs object/`.mod` to a per-run tmp (auto-authorize + auto-clean on success) and the execution binary to `binary/<binary_id>/bin/` under the Makefile-default name, writing nothing other than the cross-phase audit log to `src/`. Enumerate the execution binary `binary/<binary_id>/bin/<exe>` in the launch's `allowed_output_paths` in **file form** (it enters `allowed_file_tool_paths` by auto-derive and is authorized in terminal validation. `allowed_file_tool_paths` is usually not made explicit and is left to auto-derive).
- A `node` that has dependencies must verify at `Build` time that the resolution target of the dependency `operation` matches `spec.ir.yaml.dependency`. On mismatch, it is a `Build fail`.
- Record `build_system`, `compiler`, `build_log_ref`, `status`, and `source_source_id` in `binary_meta.json`. `source_source_id` requires recording the id of `<pipeline>/source/<source_source_id>/` that this build used as input (`Validate.execute` uses it for the lineage verification of the cross-phase MCP audit log).
- `binary_meta.json#binary_artifact_ref` points to the canonical placement of the execution binary `<pipeline>/binary/<binary_id>/bin/<exe>` (the out-of-source `BINDIR` output. It must not point under `src/`. The `run_program` input verification of `Validate.execute` requires resolution under `binary/<binary_id>/bin/`).
- On failure, record `failure_category` / `failure_source_refs[]` / `failure_excerpt` in `binary_meta.json` as required (the "retry trigger (no LLM involvement)" section of `docs/workflow/phases/phase_03_build.md` is the canonical source). `failure_category` is one of `compile_error` / `link_error` / `make_error` / `dependency_violation` / `validate_post_build_violation`.
- The MCP `command_log` output of `compile_project` allows only the following 2 canonical placements:
  - In-source build (Make for Fortran/C/cpp/mixed): `<pipeline>/source/<source_id>/src/command_log.jsonl` (cross-phase, project_dir=`<src>/src/`). Always include `source_id` in the launch request and pass it through record_launch (a failed/stale source is rejected by record_launch's verification_status check).
  - Out-of-source build (CMake/Meson/Ninja): `<pipeline>/binary/<binary_id>/command_log.jsonl` (in-phase, project_dir=`<binary_id>/`).
  A composition where the log lands in a non-canonical placement (e.g. `<binary_id>/bin/command_log.jsonl`) becomes an `unauthorized_write_violation` in terminal validation.
- The output `bin/` has a relative placement that `Validate.execute` can reference.
- The storage root for workflow artifacts allows only `workspace/`, and the workflow-root judgment targets only `workspace/`.

## Operations Rules
1. Issue a `binary_id`, and fix the output destination to `workspace/pipelines/<pipeline_id>/binary/<binary_id>/`.
2. Make `source_meta.json`'s `verification_status=pass` the start condition.
3. On a build failure, record the retry-trigger information such as `failure_category` in `binary_meta.json`, and go back to `Generate` (the deterministic mapping uses `docs/workflow/phases/phase_03_build.md` as the canonical source).
4. A rebuild of the same `source_id` is operated append-only with a different `binary_id`.
5. When the output destination is not `workspace/`, it is a `Build fail`.
6. When `workspace/` does not exist before workflow execution starts, create `workspace/` directly under the repository root.
7. Before start and before completion, run `python3 tools/validate_workspace_root.py`, and on `fail` it is a `Build fail`.
8. Before completion, run `python3 tools/validate_pipeline_semantics.py --stage post_build --pipeline-root workspace/pipelines/<node_key_safe>/<pipeline_id>/`, and add `--source-id <source_id>` as needed. `exit code 0` is required, and on `fail` it is a `Build fail`.

## Decision Criteria
- The build means is only `MCP compile_project`.
- The required items of `binary_meta.json` are not missing (on failure, `failure_category` etc. also cannot be missing).
- The `workspace` placement convention matches `docs/workflow/WORKFLOW_CORE.md` and `docs/workflow/phases/phase_03_build.md`.
