# Overall workflow: Spec -> Compile -> Generate -> Build -> Validate

The ultimate goal of the `workflow` is to generate executable code (`model` + `runner`) from the natural-language `Controlled Spec`, and to confirm via the execution result that it satisfies the behavior required by `tests`. For this goal, this workflow consists of 5 phases, and each phase, as an **observable primary producer**, produces exactly one kind of primary artifact.

The contract body of the workflow is split and placed under `docs/workflow/`. This file is its entry point. For terms, refer to `GLOSSARY.md`.

## Phase order and primary artifacts

| # | phase | role | primary artifact |
|---|-------|------|----------|
| 0 | Spec | manual writing of the natural-language specification | `controlled_spec.md` / `tests.md` / `deps.yaml` |
| 1 | Compile | natural-language specification → structured IR | `spec.ir.yaml` |
| 2 | Generate | IR → source code | `source/<source_id>/` |
| 3 | Build | source → binary (deterministic) | `binary/<binary_id>/bin/` |
| 4 | Validate | execution + pass/fail judgment | `verdict.json` / `aggregate_verdict.json` |

The phase boundary is cut by **"the hierarchy of observable primary artifacts"**. The feedback direction on failure (e.g. Build failure → Generate re-run) is not a criterion for the phase boundary.

## Optional flows

Optimization (`Tune`) and promotion (`Promote`) are removed from the required path of the core workflow and treated as independent optional flows. The core workflow does not mix structural IR and implementation discretion; Tune explores this as variants. Details are handled in a separate plan.

## Common part (canonical source)

- [workflow/WORKFLOW_CORE.md](workflow/WORKFLOW_CORE.md): phase sequence, workflow common invariants, the per-`phase` I/O contract list, artifact layout rules, completion criteria, agent reference scope

## Phase contract details (canonical source)

- [workflow/phases/phase_00_spec.md](workflow/phases/phase_00_spec.md): 0 Spec (manual)
- [workflow/phases/phase_01_compile.md](workflow/phases/phase_01_compile.md): 1 Compile
- [workflow/phases/phase_02_generate.md](workflow/phases/phase_02_generate.md): 2 Generate
- [workflow/phases/phase_03_build.md](workflow/phases/phase_03_build.md): 3 Build
- [workflow/phases/phase_04_validate.md](workflow/phases/phase_04_validate.md): 4 Validate

## Cross-cutting conventions (canonical source)

- [ORCHESTRATION.md](ORCHESTRATION.md): agent hierarchical execution contract
- [SPEC.md](SPEC.md): overall policy, `spec` management requirements, registry requirements
