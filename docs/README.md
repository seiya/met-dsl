# Docs Index

This document set is organized so that "the reading order = the way to proceed".

## Shortest reading order
1. `SPEC.md` (invariant principles, final goal, `spec_kind` 3-layer design)
2. `CONTROLLED_SPEC.md` (format and required items for `problem` / `component` / `profile`)
3. `TESTS.md` (format and required items for `tests`)
4. `PHYSICAL_VALIDATION.md` (requirements for physical-validity judgment)
5. `GLOSSARY.md` (`Artifacts` / `terms`)
6. `WORKFLOW.md` (entry point for the 5-phase `Spec → Compile → Generate → Build → Validate`; the body is split into `workflow/WORKFLOW_CORE.md` and `workflow/phases/`)
7. `ORCHESTRATION.md` (execution conventions for `orchestration agent -> substep agent` and `orchestration agent -> step agent`)
8. `RUNBOOK.md` (minimal operational procedures for running trials)
9. `IMPL_PLAN_SPEC.md` (default-value rules for the `spec.ir.yaml.impl_defaults` section)
10. `PERFORMANCE_DIAGNOSTICS.md` (`perf.json` specification)
11. `TUNING_WORKFLOW.md` (optional flow: operational guidance for performance exploration)

## Role-based Structure
### Core (direction / contracts)
- `SPEC.md`
- `CONTROLLED_SPEC.md`
- `TESTS.md`
- `PHYSICAL_VALIDATION.md`
- `GLOSSARY.md`

### Loop (running trials, core workflow)
- `WORKFLOW.md` / `workflow/WORKFLOW_CORE.md` / `workflow/phases/`
- `ORCHESTRATION.md`
- `RUNBOOK.md`

### Execution / Performance (implementation and performance)
- `IMPL_PLAN_SPEC.md`
- `PERFORMANCE_DIAGNOSTICS.md`

### Optional flows (optional flows, outside the core workflow)
- `TUNING_WORKFLOW.md` (Tune: implementation-discretion variant exploration / Promote: promotion to the official version)

## Operations Rules
- When in doubt, return to the "invariant principles" in `SPEC.md`.
- To add or change a specification, update `SPEC.md`, `CONTROLLED_SPEC.md`, `TESTS.md`, and the target `spec`'s `tests.md`.
- For a change that crosses the responsibility boundary of `problem` / `component` / `profile`, update the related `spec` in the same change.
- Regardless of language, the generated code separates `model` (physics computation) and `runner` (execution / judgment coordination).
- A stage that uses the `LLM` performs `generate -> verify -> regenerate` inside the stage, and saves only the final accepted artifact.
- A stage that uses the `LLM` produces each stage's `<stage>_meta.json` as a required output, and in standard operation (`debug_mode=false`) does not save failed-attempt artifacts.
- `workflow` execution follows the conventions in `ORCHESTRATION.md` and starts from the `orchestration agent`.
- As soon as the trial procedure is settled, proceed with automation on the premise of `RUNBOOK.md`.
- Run `compile` / `run` / `quality check` through the `MCP` server (`mcp_servers/build_runtime_server.py`).
