---
name: workflow-tune-generate
description: Use this when running the generate of the Tune stage and generating knob-layer override candidates of `impl_defaults` while keeping the structure of the `spec.ir.yaml` finalized in the core workflow invariant. As an optional flow, it applies to the work of expanding trial candidates from the performance-exploration `tuning.spec`.
---

# Workflow Tune Generate

## Purpose
Fix the candidate-generation responsibility of the Tune stage, and create performance-exploration candidates under fixed physics conditions. This flow is an **optional flow** separated from the core workflow, and treats `spec.ir.yaml` as an invariant premise.

## Scope
- the work of generating knob-layer override variants of `spec.ir.yaml.impl_defaults` from `tuning.spec`
- the work of generating exploration-space expansion candidates with `LLM` assistance

## Requirements
- Fix the `case` / `algorithm` / `io_contract` / `dependency` sections of `spec.ir.yaml`, and do not change the physics algorithm.
- Generate candidates by changing **only the knob layer** of `spec.ir.yaml.impl_defaults` (`abstract.*` / `backend_overrides.*`). Crossing into the fixed layer (`target.*` / `toolchain.*` / `selected.*`) is forbidden (canonical boundary: the "fixed / knob boundary of impl_defaults" section of `docs/workflow/phases/phase_01_compile.md`).
- When `tuning.spec` includes an entry that overrides a fixed sub-key, do not launch Tune and stop with `fail_closed`.
- Prioritize safe knobs such as `tile`, `fuse`, `vectorize`, and `layout`.
- When proposing a new implementation pattern, record the basis for adding it to the `search_space` of `tuning.spec`.
- When using the `LLM`, apply the `LLM` conventions of `SPEC.md` and output `<stage>_meta.json`.

## Operations Rules
1. Issue an `impl_hash` per candidate, and do not re-run a duplicate candidate. `impl_hash` is computed from the final value of `spec.ir.yaml.impl_defaults` (after the knob override).
2. After candidate generation, save the variant `spec.ir.yaml` to a separate path, and run the same `Generate` / `Build` / `Validate` as the core workflow with the same `case`.
3. Make `debug_mode=false` the standard, and do not save failed-attempt artifacts.
4. On a candidate-generation failure, update `last_fail_reason` and hand it to verify.

## Decision Criteria
- All candidates satisfy the `case` / `algorithm` / `io_contract` / `dependency` fixed conditions of `spec.ir.yaml`.
- The candidate diff is limited to the knob layer of `impl_defaults` (`abstract.*` / `backend_overrides.*`).
- The metadata holds the information needed for re-execution.
