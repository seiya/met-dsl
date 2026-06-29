# Overall workflow common contract: Spec -> Compile -> Generate -> Build -> Validate

This document defines the workflow's phase sequence, inter-phase input/output contract, and workflow common norms. For terms, refer to `GLOSSARY.md`.

## Purpose
- Define the workflow as the 5 phases `Spec -> Compile -> Generate -> Build -> Validate`.
- The phase boundary is cut by **the hierarchy of observable primary producers**. Each phase produces exactly one kind of primary artifact.
- The execution order between `node` is determined from the `spec` dependency declarations, and within each `node` execution proceeds in the order `Compile -> Generate -> Build -> Validate`.
- Uniquely define each phase's `execution input`, `verification input`, and `output`.
- Limit the parallel execution of independent `node` to cases with an explicit instruction; by default execute sequentially.

## Scope
- Workflow execution in `spec`-origin mode and `resolved`-origin mode
- `Compile` / `Generate` / `Build` / `Validate`
- Per-`node` workflow, dependency `DAG` expansion, artifacts under `workspace/`

## Document responsibility
- This document (`WORKFLOW_CORE.md`) defines, as the canonical source, the workflow common invariants, phase sequence, per-`phase` I/O contract list, artifact layout rules, and completion criteria. The detailed contract of each `phase` uses the files under [phases/](phases/) as the canonical source.
- `ORCHESTRATION.md` defines the workflow's agent hierarchical execution conventions as the canonical source.
- `SPEC.md` defines the overall policy, `spec` management requirements, and registry requirements as the canonical source.
- The execution procedure, retry procedure, tool-call order, and on-failure operations of each phase use the corresponding `SKILL.md` as the canonical source.
- The contracts of the optional flows (`Tune` / `Promote`) are handled in a separate plan. They are not included in the core workflow.

## term rules
- `phase` refers to the logical unit that composes the workflow, including `Spec` / `Compile` / `Generate` / `Build` / `Validate`.
- `step` is treated as the orchestration-level execution unit corresponding to one phase.
- `substep` refers to a lower execution unit decomposed from a `step`.
  - `Compile` has the 2 substeps `generate` and `verify`.
  - `Generate` has the 3 substeps `generate`, `lint`, and `verify` — `lint` is a deterministic conductor-run substep (no leaf) between `generate` and `verify`; a lint finding warm-resumes `generate`.
  - `Validate` has the 2 substeps `execute` and `judge`.
  - `Build` is a single step that has no standard substep.
- `stage` is used only as existing field names such as `generated_by_stage`, `<stage>_meta.json`, and `write_scope_baseline.json.stage`. It must not be used as a synonym for `phase` or `step` in the body text.

## Workflow overview
### phase sequence
0. `Spec` (manual): create `controlled_spec.md`, `tests.md`, and `deps.yaml`.
1. `Compile`: integrate the natural-language specification + dependency resolution into a **single structural IR** (`spec.ir.yaml`).
2. `Generate`: take the IR as input and generate the source of `model` and `runner`.
3. `Build`: deterministically turn the generated source into a binary with a standard build tool.
4. `Validate`: run the binary, recompute the judgment metrics from the primary evidence, and finalize the `verdict`.

### primary deliverable
| phase | primary deliverable | nature |
|-------|---------------------|------|
| Spec | `controlled_spec.md` / `tests.md` / `deps.yaml` | natural language (manual) |
| Compile | `spec.ir.yaml` | structured (LLM) |
| Generate | code under `source/<source_id>/` | source (LLM) |
| Build | `binary/<binary_id>/bin/` | binary (deterministic) |
| Validate | `verdict.json` / `aggregate_verdict.json` | judgment (execution + LLM) |

## Workflow common invariants
1. Forbid `dummy` output for the purpose of passing `tests` or advancing the workflow.
2. Generate `diagnostics.json` and `perf.json` only as the execution result of the target `runner`. Forbid hand-writing, fixed-value embedding, and external post-editing.
3. `verdict.json` and `aggregate_verdict.json` must be derived from `tests.md` and the execution artifacts of the same `run_id`.
4. When a phase input is insufficient, stop the relevant phase with `fail`, and forbid guessed completion.
5. On a phase failure, an artifact file must not be artificially generated for the purpose of satisfying a downstream phase's start condition.
6. Without an explicit specification, forbid referencing the content of existing workflow output (past `ir_id` / `pipeline_id` / `source_id` / `binary_id` / `run_id`). For an orchestration where `resume_enabled=true` is recorded in `orchestration_meta.json`, referencing the artifacts of completed steps recorded in `orchestration_checkpoint.json` is permitted.
7. Even when past artifacts exist under `workspace/`, forbid viewing their content and referencing them as input.
8. Workflow execution uses, as input, only the repository-managed `spec` canonical source and the preceding artifacts generated in the relevant trial.
9. Do not extract and complete a requirement, judgment rule, or input/output contract not defined in `docs/`, `spec/`, or the relevant trial's artifacts from the implementation under `tools/`, verification scripts, test code, or validator code.
10. Workflow execution must execute each phase (`Compile` / `Generate` / `Validate`) with the `LLM`. `Build` is a deterministic process and is executed by a build-command call via the MCP server.
11. For workflow execution, a script that proxies multiple phases at once must not be newly generated or executed. Phase execution allows only `orchestration agent -> step agent` or `orchestration agent -> substep agent`.
12. The storage root for workflow artifacts allows only `workspace/`. If `workspace/` does not exist, create it directly under the repository root.
13. During workflow execution, the artifacts under `workspace/ir` and `workspace/pipelines` of the target `DAG` must not be deleted.
14. `quality check` uses the comparison of `diagnostics.json` and `verdict.json` as the canonical source, and must not finalize pass/fail by `stdout` diff alone.
15. The artifact reference paths of `lineage.json` and `trial_meta.json` must be recorded relative to `workspace/`.
16. `trial_meta.json` requires recording `generated_by_stage`, `source_source_id`, `source_binary_id`, `source_command_ref`, and `source_artifact_hash` (`run_id` is canonically encoded by the `runs/<run_id>/` directory path itself where the trial_meta is placed, and a separate `source_run_id` field is not recorded — because it is self-referential / circular). Each entry of `source_command_ref` declares a `tool_name` (`run_program` or `run_quality_checks`), and must match the `tool_name` of the corresponding MCP `command_log` record. The trial_meta of the execute part of `Validate` must have at least 1 entry with `tool_name='run_program'`. The `source_meta.json` that `source_source_id` points to must have `verification_status=pass`. The `<pipeline>/binary/<source_binary_id>/bin/` that `source_binary_id` points to must exist, and the executable of the `run_program` log record must resolve under that bin/.
17. Across different `pipeline_id`, the artifact body must not be reused by changing only the `id`-family metadata. When detected, treat it as `copy_based_artifact_reuse` and mark it `invalid`.
18. A violation of these norms is a workflow specification violation, and marks the relevant `pipeline` `invalid`.
19. All phases of the core workflow must not write outside of `workspace/`. The exception for the optional flow (`Promote`) is defined in a separate plan.
20. Before all phases start, capture a `baseline` of the file set under the repository root, and perform a diff comparison before the relevant phase completes.
21. The diff comparison must detect an `add` / `modify` / `delete` outside of `workspace/` as a violation.
22. When `python` execution is used in the workflow path, a setting in which `__pycache__` is not generated outside of `workspace/` is required. Use `PYTHONDONTWRITEBYTECODE=1` or `PYTHONPYCACHEPREFIX=workspace/.pycache/<pipeline_id>/`.
23. A phase that detected a write-scope violation is `fail`, and a downstream phase must not start. The violation content must be recorded in metadata under `workspace/`.
24. A `pipeline` that detected a write-scope violation is `invalid`. The same trial must not continue without resolving the violation state.
25. The workflow's hierarchical execution contract, and the requirements of `preflight`, `agent_runs.jsonl`, `agent_graph.json`, and `step_result.json` must be applied using `ORCHESTRATION.md` as the canonical source.
26. The canonical entrypoint for starting the workflow is `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|claude>]`. `<until_phase>` specifies one of `compile` / `generate` / `build` / `validate`.
27. When `preflight` is `fail`, the `orchestration agent` must not launch a child `agent`. The workflow must stop with `fail`.
28. `preflight.json` must not be manually edited or post-edited to make it `pass`.
29. Just before launching a child `agent`, re-run the execution platform's live check, and confirm the satisfaction of `multi_agent=true` and the launchability of the child `agent`.
30. The phase artifacts of `workspace/ir/` and `workspace/pipelines/` must not be generated by anything other than a legitimate child `agent` capability. The `orchestration agent` can generate only reservation artifacts.
31. The requirement definition for the output format, input/output contract, and judgment conditions must reference only `controlled_spec.md`, `tests.md`, `deps.yaml`, `spec.ir.yaml`, and `docs/` canonical-source documents.
32. The verification python scripts, quality-check implementations, and verify implementations under `tools/` are treated as input dedicated to validity confirmation, and must not be referenced as input for the requirement definition or output-format definition.
33. When a requirement definition is insufficient, forbid back-deriving completion from the verification implementation, and stop the relevant phase with `fail`.
34. The preset-compatible quality path needed for `quality check` execution must be established by the official output of `Generate` alone. Forbid the operation of having a downstream phase additionally generate test source, harness, auxiliary scripts, or a temporary Makefile under `workspace/` to establish it.
35. The `quality check` execution method must be consistent with `impl_defaults.toolchain.language` and `impl_defaults.toolchain.build_system` of `spec.ir.yaml`. With `toolchain.build_system=make` and `toolchain.language=fortran` / `c` / `cpp` / `mixed` families, use `make_test` or `make_check`, and forbid substitution by `pytest`.
36. Even for `node` that are independent in terms of dependencies, the workflow must execute sequentially unless an explicit parallel-execution instruction exists.

## Common conventions
### `LLM`-using phases
- Apply the "Handling of the `LLM` (overall principles)" of `SPEC.md` to all phases that use the `LLM`.
- An `LLM`-using phase produces each phase's `<stage>_meta.json` (`ir_meta.json` for `Compile`, `source_meta.json` for `Generate`, `validate_meta.json` for `Validate`) as a required output.
- The common required keys of `<stage>_meta.json` are `attempt_count`, `verification_status`, `last_fail_reason`, `debug_mode`, and `context_isolated`.
- When `context_isolated=false`, `constraint_reason` is required.
- `ir_meta.json` requires only the common keys above.
- `source_meta.json` requires the common keys above. Lint is no longer recorded in `source_meta.lint_command_ref` — it is the deterministic conductor-run `Generate.lint` substep, certified by `post_generate` against the host-authored `<pipeline_root>/lint_evidence/<source_id>.json`.
- `validate_meta.json` requires the common keys above, and only when `verification_status=pass` requires the evidence of the LLM semantic check in `judge_command_ref`.
- With `debug_mode=false`, do not save failed-attempt artifacts. When saved with `debug_mode=true`, record the saved count and storage location in metadata.
- The execution mode at workflow start is specified by `--mode` of `tools/run_workflow.py`, with a default of `dev`.
- In `dev` mode, the `verify substep` can continue treating only `issue_severity=minor` as a minor problem, and must treat `major` / `critical` as `fail`.
- When the workflow `fail` in `dev` mode, it must generate `workspace/orchestrations/<orchestration_id>/failure_analysis.json` and record the failure reason, the related `agent_run`, the related `step_result`, and an auxiliary log summary.

### Agent hierarchical execution
- Apply `ORCHESTRATION.md` for the workflow's hierarchical execution contract, parent-child relationships, launch order, stop conditions, and execution-record format.
- This document, as the canonical source for the phase contract the `orchestration agent` passes to the child `agent`, defines each phase's `execution input`, `verification input`, and `output`.

### artifact layout rules
#### Root structure
The storage location for workflow artifacts uses `workspace/` as the canonical source, and requires the following structure.

```text
workspace/
  orchestrations/
    <orchestration_id>/
      orchestration_meta.json
      preflight.json
      phase_state.json
      phase_state_log.jsonl
      orchestration_checkpoint.json
      agent_graph.json
      agent_runs.jsonl
      launches/
        <agent_run_id>.request.json
        <agent_run_id>.response.json
        <agent_run_id>.prompt.txt
        <agent_run_id>.reply.txt
      agents/
        <agent_run_id>/
          dialogs/
            child.request.json
            child.response.json
            child.prompt.txt
            child.reply.txt
            agent.result.json
            agent.summary.txt
      access_policies/
      access_logs/
      capabilities/
      gates/
      violations/
      steps/
        <node_key_safe>/
          <step>/
            <agent_run_id>/
              step_result.json
  ir/
    <node_key_safe>/
      <ir_id>/
        spec.ir.yaml
        ir_meta.json
  pipelines/
    <node_key_safe>/
      <pipeline_id>/
        lineage.json
        source/
          <source_id>/
            src/
            source_meta.json
            attempts/  # optional: only when debug_mode=true
        binary/
          <binary_id>/
            bin/
            binary_meta.json
        runs/
          <run_id>/
            <node_key_safe>/
              diagnostics.json
              perf.json
              quality_check.json
              raw/
                state_snapshots/
                metrics_basis.json
                execution_trace.json
              verdict.json
              aggregate_verdict.json
              summary.json
              semantic_review.json
              trial_meta.json
              validate_meta.json
              stdout.log
              stderr.log
  index/
    ir_index.json
    pipeline_index.json
```

#### `ID` and invariants

##### `node_key` format
- `node_key` is of the form `<spec_kind>/<spec_id>@<spec_version>`.
  - `spec_kind`: the value of the `spec_kind` field of `deps.yaml` (e.g. `component`, `problem`, `profile`)
  - `spec_id`: the value of the `spec_id` field of `deps.yaml`
  - `spec_version`: the value of the `spec_version` field of `controlled_spec.md`
- `node_key_safe` is the storage notation of `node_key`, of the form `<spec_kind>__<spec_id>__<spec_version>`.
  - Regex: `^[a-z][a-z0-9_]*__[a-z0-9][a-z0-9_]*__[0-9][0-9A-Za-z._-]*$`

##### `ID` naming rules
- `orchestration_id` is the `ID` that identifies one entire workflow.
- `ir_id` is the `ID` that identifies the `spec.ir.yaml` per `node`.
  - Format: `<slug>_<YYYYMMDD>_<seq3>`
  - `slug` is a short readable token derived from `spec_id` (hyphen-separated, alphanumeric).
  - Regex: `^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$`
- `pipeline_id` is the `ID` that identifies one `Generate -> Build -> Validate` series per `node`. Same format and regex as `ir_id`.
- `source_id` / `binary_id` / `run_id` are per-trial `ID` of each stage, with a recommended form of `<prefix>_<date>_<seq3>`. `prefix` uses `src` / `bin` / `run`.
- The workflow runs independently each time, and must newly issue `ir_id` / `pipeline_id` / `source_id` / `binary_id` / `run_id` each time.
- `agent_run_id` is the per-execution `ID` of a `step agent` / `substep agent` / `orchestration agent`, and `step` / `substep` require recording `parent_agent_run_id`.
- The `step` / `substep` roles of `agent_runs.jsonl` require recording `agent_backend`, `agent_model`, `context_id`, and `context_isolated`.
- The terminal-status rows (`pass` / `fail` / `blocked` / `timeout` / `cancel`) of `agent_runs.jsonl` require recording `finished_at`.
- The `context_id` of the `step` / `substep` roles must be unique within the `orchestration_id`.
- The judgment unit of `Validate` is `node_key`. When multiple `node_key` are handled under a `run_id`, per-`node_key` artifact separation is required.
- The `spec.ir.yaml` under an `ir_id` is `immutable`, and on update a new `ir_id` is issued.
- Under a `pipeline_id` is `append-only`, and overwriting an existing `run_id` is forbidden.

#### Origin modes
- `spec`-origin mode: resolve the dependency `DAG` from the `spec`, issue a new `ir_id` per `node`, and start the `pipeline`.
- `ir`-origin mode: specify an existing `ir_id` and execute only from `Generate` onward.
- `lineage.json` requires recording `spec_ref`, `ir_ref`, each stage `id`, `dependency_ref`, `node_key`, and `direct_dependency_status`.
- The stage ids — `pipeline_id`, `source_id`, `binary_id`, `run_id` — must be recorded as **top-level keys** of `lineage.json` (not nested under a sub-object). `pipeline_id` must be present and match the `<pipeline_id>` directory name (`<slug>_<YYYYMMDD>_<seq3>`) already at `Generate` time; the remaining stage ids are filled in as their phases run. Both `post_generate` and `post_execute` enforce the top-level `pipeline_id` schema (`validate_pipeline_semantics.py:_validate_pipeline_lineage_presence`), so a malformed or nested `pipeline_id` fails at `Generate` rather than at `Validate`.

#### Re-execution rules
- `Generate` may be run multiple times with the same `ir_id`. Each trial is a different `source_id`.
- `Build` may be run multiple times with the same `source_id`. Each trial is a different `binary_id`.
- `Validate` may be run multiple times with the same `binary_id`. Each trial is a different `run_id`. This applies to both a full Validate retry (`execute` re-run + `judge` re-evaluation) and a judge-only re-evaluation (reusing the `execute` output and re-running only `judge`): in either case, issue a new `run_id`, and do not overwrite the existing `runs/<run_id>/` directory. Because running `judge` more than once under the same `run_id` would overwrite `verdict.json` / `aggregate_verdict.json` / `summary.json` / `semantic_review.json` (the canonical output of judge) and lose the previous judgment basis, reusing the same `run_id` is forbidden. When a judge-only re-evaluation reuses the `execute`'s `raw/` / `diagnostics.json` / `perf.json` / `quality_check.json` / `trial_meta.json` as-is, the orchestration agent copies them under the new `run_id`, and the `source_source_id` / `source_binary_id` of `trial_meta.json` must match the original `run_id` (to maintain the provenance of the source and build that generated the binary). Furthermore, as a survival condition of the judgment basis, the `<pipeline_ref>/source/<source_source_id>/` directory that `trial_meta.json.source_source_id` points to and the `<pipeline_ref>/binary/<source_binary_id>/` directory that `trial_meta.json.source_binary_id` points to must not be deleted as long as all `run_id` that hold the relevant `trial_meta.json` (the original + all runs duplicated in re-evaluation) exist (because judge depends on the `source_meta.json` check and the semantic check of `source/<source_id>/src/`, a dangling provenance chain makes re-evaluation impossible).
- The start condition for `Build` is that the target `source_id`'s `source_meta.json` has `verification_status=pass`.
- A `Generate` with `debug_mode=false` must not generate `attempts/`.
- The `Validate` input is always the artifacts under the same `run_id`, and mixing with other `run_id` is forbidden.
- On each phase `fail`, forbid the after-the-fact generation of files for the purpose of satisfying a downstream phase's start condition.
- The re-submission strategy (`repair_strategy=reuse` / `restart`) and recording requirements for a phase that has `substep` are applied using `ORCHESTRATION.md` as the canonical source.

#### Reference rules
- When referencing a `step` / `substep` execution from the `orchestration`, use `orchestration_id + agent_run_id`, and do not track it by full-text search of the log body alone.
- The `step` completion judgment uses `workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/step_result.json` as the canonical source.
- When referencing an `ir` from a `pipeline`, use `node_key_safe + ir_id`, and forbid a direct relative-file-path reference.
- The reproduction of `Validate` must be possible with `lineage.json` and `trial_meta.json` alone.
- `trial_meta.json` requires recording `runner_command`, `process_trace_ref`, and `raw_artifact_refs`.
- `index/ir_index.json` and `index/pipeline_index.json` are search-only, and must not be used as the canonical source for judgment logic.
- `aggregate_verdict.json` is always consistent with the `dependency` section of `spec.ir.yaml`, and forbids omission of the dependency set.

#### Dependency workflow coverage check
- The `dependency.all_nodes` set of `spec.ir.yaml` and the `node_key_safe` set of `workspace/ir/*/<ir_id>/` must match one-to-one.
- The `dependency.all_nodes` set of `spec.ir.yaml` and the `node_key` set of `workspace/pipelines/*/<pipeline_id>/lineage.json` must match one-to-one.
- When the code hash of `source/<source_id>/src/` generated under different `node_key` matches, except for a file explicitly stated as a common library, it must be marked `copy_based_artifact_reuse` and `invalid`.
- Before the completion declaration of a workflow execution, the `workspace/ir` / `workspace/pipelines` artifacts of the target dependency `DAG` must not be deleted.

#### Write-scope guard
- At the start of each phase, save `write_scope_baseline.json` under `workspace/`, and fix the `baseline` to be compared.
- `write_scope_baseline.json` must hold at least `stage`, `node_key`, `pipeline_id`, `captured_at`, `tracked_diff`, and `untracked_files`.
- Before each phase completes, compute the diff against `write_scope_baseline.json`, and must judge a change outside of `workspace/` as a `write_scope_violation`.
- When no violation is detected, `write_scope_check.status=pass` must be recorded in the phase metadata.
- When a violation is detected, output `write_scope_violation.json` under `workspace/`, and must record `violation_paths`, `stage`, `node_key`, `pipeline_id`, and `detected_at`.
- When a `write_scope_violation` is detected, the relevant phase is `fail`, and the `aggregate_verdict` finalization of the relevant `pipeline` is forbidden.

## Per-phase input/output contract list
In this section, the input of each phase is described separately as `execution input` and `verification input`. When the two roles overlap, the same artifact may be listed in both.

### 0. Spec (manual)
- execution input: the requirements, physics requirements, and dependency-selection policy given outside the workflow
- verification input: none
- output: `spec/<spec_kind>/<domain>/<family>/<spec_id>/controlled_spec.md`, `spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md`, `spec/<spec_kind>/<domain>/<family>/<spec_id>/deps.yaml`

### 1. Compile
- execution input: `controlled_spec.md`, `tests.md`, `deps.yaml`, `spec/registry/spec_catalog.yaml`
- verification input: `controlled_spec.md`, `tests.md`, `deps.yaml`, `spec/registry/spec_catalog.yaml`, the generated `spec.ir.yaml`
- output: `spec.ir.yaml`, `ir_meta.json`

### 2. Generate
- execution input: `spec.ir.yaml`
- verification input: `spec.ir.yaml`, `controlled_spec.md` (verify-only, requirement-fidelity cross-check), the generated `source/<source_id>/src/`
- output: `source/<source_id>/src/`, `source_meta.json`
- Before `Generate` completes, the conductor's deterministic `Generate.lint` substep runs the MCP `run_linter` consistent with `impl_defaults.toolchain.language` of `spec.ir.yaml` and writes the host-authored lint evidence (`<pipeline_root>/lint_evidence/<source_id>.json`, each element with `command_id`, `command_log_ref`, and `preset`); `post_generate` certifies it. The leaf does not run `run_linter`.
- `Generate` must include in its official output a preset-compatible quality path with which `Validate.execute` can run using only the `preset` of `run_quality_checks`. On shortage, it is `Generate fail`.
- With `toolchain.build_system=make` and `toolchain.language=fortran` / `c` / `cpp` / `mixed` families, a `test` or `check` target must be defined in `source/<source_id>/src/Makefile`. On absence, it is `Generate fail`.

### 3. Build
- execution input: `source/<source_id>/src/`, the `impl_defaults` of `spec.ir.yaml`
- verification input: `spec.ir.yaml`, `source_meta.json`
- output: `binary/<binary_id>/bin/`, `binary_meta.json`, the `command_id` and `command_log_ref` of `compile_project`

### 4. Validate
- execution input: `binary/<binary_id>/bin/`, `spec.ir.yaml`, `tests.md`
- verification input: `spec.ir.yaml`, `source/<source_id>/`, the `raw/` / `diagnostics.json` / `perf.json` / `quality_check.json` under the same `run_id`
- output: `diagnostics.json`, `perf.json`, `quality_check.json`, `raw/`, `stdout.log`, `stderr.log`, `semantic_review.json`, `verdict.json`, `aggregate_verdict.json`, `summary.json`, `trial_meta.json`, `validate_meta.json`, the `command_id` and `command_log_ref` of `run_program`
- `Validate` has the `execute` substep and the `judge` substep. The `execute` substep calls `run_program` via MCP to generate execution evidence, and the `judge` substep finalizes the `verdict` by an LLM semantic check and judgment-metric recomputation.

## Phase details (reference)

In this section, `Compile` / `Generate` / `Validate` are described as phases that have substeps, and `Build` is described as a single step.

The contract details per phase use the files under [phases/](phases/) as the canonical source.

| phase | file | substep |
|-------|----------|---------|
| 0 Spec (manual) | [phases/phase_00_spec.md](phases/phase_00_spec.md) | - |
| 1 Compile | [phases/phase_01_compile.md](phases/phase_01_compile.md) | generate / verify |
| 2 Generate | [phases/phase_02_generate.md](phases/phase_02_generate.md) | generate / lint / verify |
| 3 Build | [phases/phase_03_build.md](phases/phase_03_build.md) | - |
| 4 Validate | [phases/phase_04_validate.md](phases/phase_04_validate.md) | execute / judge |

## Agent reference scope

- The `skill_must_read_refs` (forced reading) of a child `step agent` / `substep agent` is assembled by `build_skill_must_read_refs` of `tools/orchestration_runtime.py` (and `build_launch_request` of `tools/workflow_conductor.py`). The whole `docs/` tree stays **readable** via the access policy regardless — `skill_must_read_refs` only controls what is *force-read* at cold start.
- The single common leaf contract is `docs/AGENT_CONTRACT.md` (it carries the leaf-actionable invariants — the subset of this document's §"Workflow common invariants" — plus the `<stage>_meta.json` key rules). **This document (`WORKFLOW_CORE.md`) and `docs/ORCHESTRATION.md` are NOT leaf must-reads** (they are orchestration/operator canonical; no leaf acts on the bulk of them).
- The phase doc (`docs/workflow/phases/phase_*.md`) is force-read **only for `Compile`** (`phase_01`'s IR schema is the contract the compile SKILL defers to). The `Generate` / `Validate` SKILLs are self-sufficient and cite their phase doc as canonical without force-reading it; `Generate.generate` / `Generate.verify` / `Validate.judge` instead force-read `docs/workflow/RUNNER_OUTPUT_CONTRACT.md` (the consolidated runner-output contract). Canonical rationale: `docs/design/leaf_must_read_restructure.md`. `docs/WORKFLOW.md` is the entry point to the specification.

## Completion criteria
- The workflow completion condition is that `orchestration_meta.json`, `agent_graph.json`, and `agent_runs.jsonl` exist under the target workflow's `orchestration_id`.
- The workflow completion condition is that, for the `dependency.all_nodes` set of `spec.ir.yaml`, `workspace/ir/<node_key_safe>/<ir_id>/` and `workspace/pipelines/<node_key_safe>/<pipeline_id>/` exist, and the `node_key` and `dependency_ref` of `lineage.json` match.
- The workflow completion declaration is permitted only when the `dependency workflow` coverage check, the `trial_meta` integrity check, and the non-detection of `copy_based_artifact_reuse` are simultaneously satisfied.
- The workflow completion declaration is permitted only when, simultaneously, there is no embedding of a dependency `node` implementation in the `src/` of an upper `node`.
- The workflow completion declaration is permitted only when, simultaneously, no `write_scope_violation` is detected in all phases.
- `CI` treats the execution result of `python3 tools/validate_workspace_root.py` and `python3 tools/validate_pipeline_semantics.py` (`--stage full` or omitted) as a `pass` condition.

## Reference documents
- `ORCHESTRATION.md`
- `PERFORMANCE_DIAGNOSTICS.md`
- `SPEC.md`
