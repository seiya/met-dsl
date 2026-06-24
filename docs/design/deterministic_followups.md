# Deterministic build/execute migration — follow-up issues to address

The Build / Validate.execute in-process migration is complete and the Codex review
findings are fixed. While chasing a fully-green end-to-end run, three classes of
follow-up surfaced — all **orthogonal to the migration mechanics** (the root trigger
is generator/IR nondeterminism). This document enumerates the concrete fixes.

Priority key: **P1** blocks auto-repair; **P2** data/robustness; **P3** latent.

## Status (2026-06-24)

All five follow-ups are implemented with unit tests:

- **A — done.** `_build_step_agents_missing_step_result` now skips reopen-superseded
  run_ids (test: `test_missing_step_result_skips_superseded_build_agent`).
- **C1 — done.** The compile io_contract gate (`_validate_io_contract_file`) rejects a
  non-scalar snapshot `time_shape_expr`; `phase_01_compile.md` updated to require
  `scalar` (test: `test_snapshot_time_shape_expr_must_be_scalar`). Confirmed on a real
  IR via `--stage compile`: `[1]` → FAIL (only violation), `scalar` → PASS.
- **C2 — done.** `Conductor._validate_execute_fail_count` escalates a recurring
  execute structural failure to a Compile reopen after 2 fails; the counter resets both
  when escalating to Compile (the reopen regenerates the IR, so the next execute failure
  gets its own Generate-retry-first cycle) and when validate advances (test:
  `test_recurring_execute_failure_escalates_to_compile`).
- **B1 — done.** `phase_required_outputs` takes a resolved `exe_name` for build; the
  `run_phase` call site passes `_resolve_exe_name(...)` (test:
  `test_build_required_outputs_use_resolved_exe_name`).
- **B2 — cosmetic only.** Re-analysis: the runner/model source names are already pinned
  by generate's write-authorization (any other name fails as `unauthorized_write`), so
  no variability risk exists. The only inconsistency was the validator's looser
  `*_runner.f90` glob; `_validate_runner_source_files` now asserts the
  `<spec_id>_runner.f90` basename (the model side already enforced this via
  `_model_files_in_src_dir`). No functional change (test:
  `test_runner_source_name_must_match_spec_id`).

---

## A. reopen + build relaunch guard — false positive (P1)

**Symptom (live):** after a `Validate → Generate` retry loop, the next build
`record-launch` raises:
```
record-launch: prior build agent(s) for <node>/build finished without a
step_result (<arids>); ... write it with `write-step-result --backfill` ...
```

**Root cause (precise):**
- `reopen_phase` (tools/orchestration_runtime.py:~13992) **archives** each affected
  phase's `step_result.json` → `step_result.superseded.<reopen_seq>.json`, and records
  the invalidated agents in `superseded_agent_run_ids` (`_load_superseded_run_ids`).
- The relaunch guard `_build_step_agents_missing_step_result`
  (tools/orchestration_runtime.py:2900) scans `agent_runs.jsonl` for terminal build
  `step` agents and checks for the **canonical** `step_result.json` only. It does
  **not** consult `_load_superseded_run_ids` (other pass-completion checks do, e.g.
  :8815). So a build agent whose step_result was archived by reopen is wrongly flagged
  "finished without a step_result", blocking the next build launch.

**Scope:** pre-existing & general (fires for any reopen that crosses `build` —
`validate→generate`, `validate→compile`, `build→generate`). Newly reachable because the
deterministic `execute→Generate` routing (added for Codex finding 1) plus repeated
generator nondeterminism (C) loops `validate→generate→build` several times.

**Fix:** in `_build_step_agents_missing_step_result`, exclude `run_id`s present in
`_load_superseded_run_ids(...)` (or accept a sibling `step_result.superseded.*.json` as
satisfying the invariant). Add a unit test driving reopen→relaunch.

---

## B. Generator binary / source-file name variability — residual hardcodes (P2/P3)

The deterministic bodies were made robust to the binary name (Build/Validate.execute now
derive `<exe>` from the Makefile's `BIN` via `Conductor._resolve_exe_name`). Residual
`<spec_id>_runner` assumptions remain:

- **B1 (P2):** `phase_required_outputs(refs, "build")`
  (tools/workflow_conductor.py:1952) still hardcodes `bin/<spec_id>_runner`. This is
  written into the build `step_result.required_outputs`, so for a `BIN=<spec_id>` build
  it records a non-existent binary path. (Build still passes because required_outputs is
  not strictly existence-gated, but the recorded data is wrong.) **Fix:** thread the
  resolved exe name into `phase_required_outputs` (same as `build_launch_request`'s
  `exe_name`).

- **B2 (P3):** the runner/model **source** file names are hardcoded
  `<spec_id>_model.f90` / `<spec_id>_runner.f90` in generate's `allowed_output_paths`
  and `phase_required_outputs` (tools/workflow_conductor.py:467-468, 482-483,
  1944-1945), while the validator finds them by glob `*_runner.f90` / `*_model.f90`
  (tolerant, e.g. tools/validate_pipeline_semantics.py:3665, 6043). If the generator
  ever varies the prefix, generate's existence check fails with a non-obvious error.
  Reliable so far (unlike BIN), so latent. **Fix (optional):** resolve the actual
  `*_model.f90` / `*_runner.f90` basenames by glob for the generate deliverable set, or
  enforce the `<spec_id>_*` names with a clear post_generate check.

---

## C. Compile/Generate cross-phase contract disagreement (LLM quality) (P2 + design)

`Compile` (IR) and `Generate` (runner) are independent LLM phases that must agree on
many cross-phase contracts. When they disagree, a downstream gate correctly rejects it,
but auto-repair may not converge.

- **C1 (P2, observed):** for `state_snapshots`, `Compile` sometimes declares
  `io_contract.raw_requirements.required_evidence.schema.time_shape_expr: "[1]"` for the
  per-snapshot time index `snapshot_index`, while the runner writes it as a **scalar**
  (`snapshot_index: 0`). `post_execute` flags `snapshot_index shape [] does not match
  declared time_shape_expr [1]` (tools/validate_pipeline_semantics.py:~3170). The
  per-snapshot time index is canonically a **scalar**; `[1]` is a Compile mis-declaration
  (a prior green run chose `scalar`). **Fix:** pin `time_shape_expr: scalar` for the
  snapshot time variable in the Compile contract/prompt, and add a post_compile (or
  post_generate) check that the snapshot `time_variable` shape is scalar. (Analog of the
  BIN robustness fix, on the Compile side.)

- **C2 (design note):** the deterministic `execute→Generate restart` routing (Codex
  finding 1) regenerates the **runner**, which fixes *code* defects but **not IR**
  defects. For C1 the IR's `[1]` is the actual error, so regenerating the runner to keep
  emitting scalar never matches → the loop relies on the attempt budget, not convergence.
  A structural mismatch attributed to the IR should route to `Compile` (reopen), but the
  non-LLM execute substep can't determine code-vs-ir attribution. Mitigation: prefer
  making Compile robust (C1) so the disagreement never arises; optionally, on a recurring
  execute structural failure (≥N attempts) escalate to `Compile` instead of `Generate`.

**General pattern:** make cross-phase agreement robust by either (a) deriving from
ground truth (binary name ← Makefile BIN), or (b) pinning a canonical value + a gate
(snapshot time index ← scalar). Routing failures to regenerate one side cannot fix a
disagreement rooted in the other side.

---

## Suggested order

1. **A** (P1) — unblocks the auto-repair loop; small, self-contained guard fix + test.
2. **C1** (P2) — removes the recurring trigger that exposes A; Compile contract + gate.
3. **B1** (P2) — step_result data correctness; thread resolved exe name.
4. **B2 / C2** (P3 / design) — latent hardening; address if/when they bite.
