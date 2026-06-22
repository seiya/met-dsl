# Agent Skills Mapping

This document defines the reference conventions for the `skills` used in the project.

## Purpose
- Use the same phase definitions on `Codex` / `Gemini` / `Claude Code`.

## Scope
- The conductor (`tools/workflow_conductor.py`) that drives the whole `workflow`
- The core workflow phases `Compile` / `Generate` / `Build` / `Validate`
- The `skills/<skill_name>/SKILL.md` referenced in each phase
- The SKILL of the optional flows `Tune` / `Promote` is handled separately from the core workflow.

## Requirements
- The agent identifies the target phase, then reads the corresponding `SKILL.md`.
- A phase that has `generate -> verify -> regenerate` must apply the 2 `SKILL`, one for `generate` and one for `verify`, separately.
- The workflow common invariants (the ban on referencing past artifacts, the `dummy` prohibition, verification-contract derivation, the `workspace/` root constraint, the `quality check` judgment axis) use `docs/workflow/WORKFLOW_CORE.md` as the canonical source. The detailed contract of each `phase` uses the files under `docs/workflow/phases/` as the canonical source. The entry point to the specification is `docs/WORKFLOW.md`.
- The execution contract of the agent hierarchy (`orchestration -> step` and `orchestration -> substep`) uses `ORCHESTRATION.md` as the canonical source.
- The common contract that every child `step agent` / `substep agent` must apply (capability_token usage, read/write permission guards, the direct `Write` / `Edit` artifact-write procedure, tmp-area rules, the no-inline-write / no-cross-agent-read constraints, dev-mode fail rules) uses `docs/AGENT_CONTRACT.md` as the canonical source. It is child-readable (under `docs/`, in every child's `read_manifest`) and is referenced by the rendered launch prompt rather than inlined per child; the security-critical constraint lines that `record-launch`'s `_required_launch_prompt_constraint_lines` guard requires are additionally kept inline in the launch prompt (`skills/workflow-orchestration/references/launch_prompts.md`).
- The overall policy and `spec` management requirements (`spec_kind` / registry / official-version placement / naming rules) use `SPEC.md` as the canonical source.
- `Build` / `Validate.execute` / `quality check` run via the `MCP` server, and simultaneously apply the `MCP execution rules` of `AGENTS.md` and the contract of the corresponding `SKILL.md`.
- Each phase must not drop the required outputs defined in the corresponding `SKILL.md` (e.g. `ir_meta.json`, `source_meta.json`, `binary_meta.json`, `verdict.json`, `validate_meta.json`).
- Write into `SKILL.md` the execution procedure and the procedures specific to that `SKILL`, and do not duplicate the `phase`'s I/O contract / artifact format / numerical canonical requirements in a form that contradicts `docs/workflow/WORKFLOW_CORE.md` or `docs/workflow/phases/phase_*.md`.
- The hook implementation separates backend-independent `common validation` from backend-specific adapters, and uses `tools/hooks/common.py` as the canonical source for `common validation` and `tools/hooks/adapters/` for the backend adapters.
- On the `codex` backend, use `.codex/hooks.json` as the canonical source for the hook invocation definitions, and require `feature_states.hooks=true` in the `preflight` decision.

## Responsibility-decision flow
1. Judge whether the rule to add/change directly affects the validity of a workflow artifact.
2. When it directly affects validity, write it into `docs/workflow/WORKFLOW_CORE.md` or the relevant `docs/workflow/phases/phase_*.md`.
3. When it defines an overall policy such as `spec` registry / naming / placement / promotion, rather than a workflow common norm, write it into `SPEC.md`.
4. When the rule is a detail of the execution method such as the tool-call procedure, input-collection order, regeneration procedure, or on-failure operations, write it into the corresponding `SKILL.md`.
5. Agent-specific execution conveniences (e.g. prompt order, log-organization procedure) are limited to `SKILL.md`, and are not mixed into `docs/workflow/WORKFLOW_CORE.md` or `docs/workflow/phases/`.
6. When the decision is hard, use as the decision axis whether the impact of a rule violation extends to the destruction of auditability / reproducibility / judgment consistency. When it destroys, choose the contract documents under `docs/workflow/`; when it does not, choose `SKILL.md`.

## phase-to-Skill correspondence table (core workflow)
- `Compile generate`: `skills/workflow-compile-generate/SKILL.md`
- `Compile verify`: `skills/workflow-compile-verify/SKILL.md`
- `Generate generate`: `skills/workflow-generate-generate/SKILL.md`
- `Generate verify`: `skills/workflow-generate-verify/SKILL.md`
- `Build`: `skills/workflow-build/SKILL.md`
- `Validate execute`: `skills/workflow-validate-execute/SKILL.md`
- `Validate judge`: `skills/workflow-validate-judge/SKILL.md`

## phase-to-Skill correspondence table (optional flows)
- `Tune generate`: `skills/workflow-tune-generate/SKILL.md`
- `Tune verify`: `skills/workflow-tune-verify/SKILL.md`
- `Promote`: `skills/workflow-promote/SKILL.md`

## Auxiliary Skills
- `Workflow audit (Codex)`: `skills/workflow-audit-codex/SKILL.md`
- `Workflow audit (Claude Code)`: `skills/workflow-audit-claude/SKILL.md`
- `Spec input check`: `skills/spec-input-check/SKILL.md` (pre-`Compile` advisory check of `controlled_spec.md` / `deps.yaml` / `tests.md`; proposal only, does not modify the spec)

## Operations Rules
1. When handling multiple phases in one piece of work, switch the corresponding `SKILL` per phase.
2. When `verify` fails, go back to the `generate` of the same phase, and re-verify after regeneration.
3. Record the loop state and the failure reason in the metadata of the relevant phase.
4. When the `SKILL` definition is changed, update this correspondence table in the same change.
5. When changing a workflow contract, first update `docs/workflow/WORKFLOW_CORE.md` or the relevant `docs/workflow/phases/phase_*.md`, and update `SKILL.md` following that change.
6. Write a change to the workflow common norms into `docs/workflow/WORKFLOW_CORE.md`, a change to each `phase`'s detailed contract into `docs/workflow/phases/`, a change to the hierarchical execution contract into `ORCHESTRATION.md`, and a change to the phase procedure into the corresponding `SKILL.md`.
7. Do not restate the rule body in `AGENT_SKILLS.md`; write the reference target and the responsibility decision.

## Decision Criteria
- The `SKILL` path used in the target phase can be explained.
- The generated artifacts and judgment artifacts match the contract of the corresponding `SKILL`.
- The phase choice for the same input is consistent across agents.
- The reference target for the workflow common norms, the hierarchical execution contract, and the phase procedure is uniquely determined.
- The same rule is not duplicated/restated across `docs/workflow/WORKFLOW_CORE.md` or `docs/workflow/phases/`, `ORCHESTRATION.md`, and `SKILL.md`.
