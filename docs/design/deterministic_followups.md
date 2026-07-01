# Deterministic build/execute migration — follow-up issues to address

The Build / Validate.execute in-process migration is complete and the Codex review
findings are fixed. While chasing a fully-green end-to-end run, three classes of
follow-up surfaced — all **orthogonal to the migration mechanics** (the root trigger
is generator/IR nondeterminism). This document enumerates the concrete fixes.

Priority key: **P1** blocks auto-repair; **P2** data/robustness; **P3** latent.

## Follow-up: deterministic `src/Makefile` (2026-06-24)

The `src/Makefile` is a pure function of known inputs (pinned `<spec_id>_model/runner.f90`
names, the fixed `use`-graph, structured `impl_defaults.toolchain`/`target`), yet the LLM
authored it and a large static validator rejected deviations (regenerate-loop cost). It is now
authored deterministically host-side, mirroring `lineage.json`.

**Part 1 — leaf nodes (implemented, default-on).** `Conductor._write_makefile(refs)` emits the
fixed runner→model Makefile (`BIN ?= <spec_id>_runner`, FFLAGS from `toolchain.standard` +
`target.backend`); called from `run_phase` at generate start, originally gated to
`_is_leaf_node` + make/fortran (**Part 2 below dropped the leaf gate**: authorship is now
make/fortran for leaf OR dependency nodes; c/cpp/mixed keep LLM authoring). The Makefile is
dropped from the node's write-authorization at all four sites (`build_launch_request`
generate/verify `allowed_output_paths`, `phase_required_outputs`, and orchestration_runtime
`_mandatory_file_tool_pins_for_launch` via `_resolved_makefile_host_authored`). The
post_generate validators stay as the safety net (the template passes all three by
construction). Docs/SKILLs note the conductor authorship.

**Part 2 — dependency nodes (Model B, IMPLEMENTED; E2E-UNVERIFIED).** The dependency build was
unimplemented/contradictory (no `.o`/`.mod` staging; `phase_02 §41` forbids copying dep sources
into `src/`, but the only historically-working build copied them in). Chosen + now-implemented
model: **Model B — transient source staging.** The conductor stages each closure `<dep>_model.f90`
into the per-run build tmp `$(OBJDIR)` (NOT canonical `src/`) and the deterministic Makefile
compiles + links the closure (`_write_makefile` non-leaf branch: deepest-first
`$(OBJDIR)/<dep>_model.o` rules + `DEP_OBJS`, derived from the union of
`dependency.direct_deps` + `dependency.transitive_deps`, ordered by `all_nodes[].topo_level`,
via `_dependency_closure`). Rationale over Model A (prebuilt `.o`/`.mod` reuse): no gfortran `.mod`
ABI coupling, reuses the already-durable dep source, single-toolchain build, canonical `src/` stays
pristine.

**What shipped:** `_conductor_authors_makefile` (and the runtime mirror
`_resolved_makefile_host_authored`) now author the Makefile host-side for **every** make∧fortran
node — leaf or dependency — so `run_phase` wires the non-leaf branch live and the generate leaf is
dropped from the Makefile write-authorization at all sites. `_build_inproc` stages the closure via
`_stage_dependency_sources` (resolves each dep's `<dep>_model.f90` from the **certified binary**'s
`binary_meta.source_source_id` — the same binary `_verify_dep_stage` certifies readiness against,
NOT the pipeline `lineage.json` which tracks the latest *generated* source and may have advanced
past the validated binary; a missing dep → transport `fail_closed`, not a content retry). Staging self-gates on make∧fortran — it is a no-op for
c/cpp/mixed dependency nodes (the LLM-authored Makefile owns their dependency build). The closure
is the **union** of `direct_deps` + `transitive_deps` (per the compile §V4 contract; a one-hop dep
has an empty `transitive_deps`, so a transitive-only read would wrongly fail-close it).
`_execute_inproc` needs no staging (`make test` only runs the already-built binary). Reconciliation:
`phase_02 §41` carve-out + §47 authorship updated; phase_03 `dependency_violation` targets `src/`
mixing only, so unchanged. Covered by synthetic-IR unit tests (`test_workflow_conductor.py`: closure
order, direct-only one-hop closure, dependency-Makefile rules, staging copy / leaf no-op /
non-fortran no-op / unbuilt-dep + malformed-IR fail-closed; conductor↔runtime authorship agreement).

**Still UNVERIFIED end-to-end:** the wired path has never run through a real
`compile→generate→build→validate`. A minimal 2-node dependency spec is now authored (the
`demo_dep_base`/`demo_dep_top` chain — see D1 below), but running
`run_workflow.py <ref> validate --llm claude --with-deps` to `meta=pass` +
`aggregate_verdict=pass` (billed, long) is the only way to confirm and remains outstanding.

## Follow-up: deterministic binary name (2026-06-24)

B1 made the recorded binary path robust to whatever `BIN` the generator chose, but the
binary name itself still dropped `_runner` (generator commonly emits `BIN=<spec_id>`),
leaving it inconsistent with the `<spec_id>_runner.f90` source / `<spec_id>_runner`
program. Rather than keep adapting (or re-add the removed `BIN must be <spec_id>_runner`
value gate, which churned), the binary name is now **imposed deterministically**:

- `Conductor._resolve_exe_name` returns the constant `<spec_id>_runner`.
- Build passes `BIN=<spec_id>_runner` on the make command line; `Validate.execute`
  imposes the same value via the `make test` environment.
- Because the execute override travels through the environment (which overrides a `?=`
  assignment only), the Makefile must declare `BIN ?=`. A new structural post_generate
  check (`_validate_makefile_bin_overridable`) requires the overridable `?=` form — NOT a
  specific value (the conductor imposes the value), so it does not re-introduce the
  churn-prone value gate. Mirrors the `OBJDIR/BINDIR/RUNDIR ?=` parameterization.
- Contracts updated: `phase_02_generate.md`, `phase_03_build.md`, `phase_04_validate.md`,
  and both generate SKILLs. Tests: `MakefileBinNotPinnedTest` (now asserts `?=` required,
  value free), `test_build_inproc_imposes_canonical_bin_override`.

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

---

# Known limitations & deferred work (recorded 2026-06-25)

These surfaced while implementing the deterministic-Makefile + transport/resume fixes. The
in-scope bugs are all fixed (suite green; Codex review clean). The items below are
**deliberately deferred** — pick them up in a future session. A ready-to-paste starter
prompt lives at `docs/design/dependency_build_followup_prompt.md`.

## D1 (PRIMARY) — dependency-node build: code IMPLEMENTED (Model B); E2E verification remains
The Model B dependency build is now wired live (see "Part 2 — dependency nodes" above for the
full implementation note). What was the gap, and what closed it:
- The contract was self-contradictory: `phase_02_generate.md:41` (encapsulation) forbids
  copying dep sources into `src/`, yet the only historically-working dependency build
  (`workspace_20260319/.../problem__shallow_water2d__0.3.0/.../Makefile`) did exactly that.
  Resolved: §41 carve-out (transient `$(OBJDIR)` staging ≠ canonical-tree copy) + §47.
- **Done:** `_conductor_authors_makefile`/`_resolved_makefile_host_authored` author the Makefile
  for every make∧fortran node (leaf or dependency), so `run_phase` drives the `_write_makefile`
  non-leaf branch live; `_stage_dependency_sources` (called from `_build_inproc`) stages each
  closure `<dep>_model.f90` into `$(OBJDIR)` from the dep's ready pipeline lineage; synthetic-IR
  unit tests cover closure order, Makefile rules, staging, and conductor↔runtime agreement.
- **E2E run (2026-06-25): dependency build VERIFIED.** Ran
  `run_workflow.py spec/component/demo/dep_chain/demo_dep_top validate --llm claude --with-deps`
  (orchestrations `orch_20260625T025619Z_3d1917e0` base / `…_9a123e7d` top). The dependency node
  `demo_dep_base` completed `compile→validate` with `aggregate_verdict=pass`. For `demo_dep_top`
  the conductor authored the dependency Makefile (`DEP_OBJS = $(OBJDIR)/demo_dep_base_model.o` +
  the staged `$(OBJDIR)/demo_dep_base_model.o: $(OBJDIR)/demo_dep_base_model.f90` rule),
  `_stage_dependency_sources` staged the certified `demo_dep_base_model.f90` into the per-run
  `$(OBJDIR)`, and **real gfortran compiled + linked the closure** into `demo_dep_top_runner`
  (`binary_meta: status=pass, dependency_check.resolved=match`; build phase passed). That is
  conclusive proof Model B works end-to-end.
- **Caveat — target node did not reach `aggregate_verdict=pass`.** `demo_dep_top` `fail_closed`
  with `retry_budget_exhausted` at `validate.execute` — **NOT a dependency-build issue** (build
  linked the closure correctly). Two E2E runs pinned the real blocker to the **validate
  `post_execute` dependency-DAG scope check** that the `--with-deps` cross-orchestration model does
  not satisfy — see **D2** below for the full root-cause + fix options. (A first-pass hypothesis
  blamed the demo `tests.md` xfail wording; clarifying it did make the runner emit a clean
  `verdict.overall=pass`, but Validate still failed on the DAG-scope check, so the spec was not the
  blocker.)

## D2 — `--with-deps` closure: build wired via staging; **validate-scope DAG check still open**
Building node N now consumes its dependency nodes' built sources through
`_stage_dependency_sources` (each dep's `<dep>_model.f90` from its ready pipeline). The BUILD path
is closed + E2E-verified (D1).

**Open (found by the 2026-06-25 E2E re-runs):** the **validate `post_execute` dependency-DAG
scope check** is incompatible with the `--with-deps` cross-orchestration model and blocks a
dependency node from completing Validate. `_validate_impl`
(`tools/validate_pipeline_semantics.py:8150-8215`) requires every closure node in `all_nodes` to be
present in the *validation scope* (the executions/lineages gathered from the passed
`--pipeline-root`/`--run-id`), unless the dependency block carries a `resolved_at` token (compiled
IRs here carry none). But the conductor runs the gate with only the node's OWN pipeline/run
(`_execute_inproc`: `--stage post_execute --pipeline-root <self> --run-id <self>`), so a dependency
that ran as a SEPARATE `--with-deps` orchestration (its own pipeline) is never in scope →
`dependency DAG incomplete for validation scope; missing node workflows [<dep>]` (+ "node plans /
pipelines not issued"). This fails `validate.execute` on every attempt → `generate exceeded 3`
fail_closed, even though the dependency build linked correctly (`binary_meta.resolved=match`) and
the runner's diagnostics are clean (`verdict.overall=pass`). Confirmed on
`orch_20260625T045636Z_9f9a00cb` (demo_dep_top): base ready+skipped, build pass, diagnostics pass,
yet post_execute fails the DAG-scope check.

**Fix — IMPLEMENTED (validator-side, 2026-06-25).** A conductor-only fix (pass dep pipeline-roots
to the post_execute gate) would be **incomplete**: the `validate.judge` leaf runs its own
`pre_judge` gate, which routes through the SAME `_validate_impl` DAG check
(`validate_pipeline_semantics.py:8419-8435` — both `post_execute` and `pre_judge` call `validate()`),
and the judge's scope is not conductor-controlled. So the fix is validator-side and covers every
caller at once: `_closure_node_validated_in_own_pipeline(repo_root, <kind>/<spec_id>)` — only in the
token-less ("validation scope") DAG branch — excuses a `missing` closure node when it has its OWN
**fully built+validated** pipeline elsewhere (`workspace/pipelines/<kind>__<spec_id>__*/<pipe>` with
a `binary/*/binary_meta.json` `verification_status=pass` AND a `runs/**/aggregate_verdict.json`
`pass`/`xfail` whose sibling `trial_meta.source_binary_id` binds it to that SAME passing binary),
which is exactly the `--with-deps` shape. Requiring the full chain (not a bare `binary_meta` field)
means a stray/forged one-key JSON or a half-built leftover cannot excuse a node; the binary↔verdict
binding prevents combining a passing binary from one attempt with an unrelated verdict from another
(the cross-run mixing the readiness gate rejects via `bound_to_binary_id`, Codex round 24).
The strict `resolved_at` per-token branch is untouched; a genuinely-incomplete dependency is still
flagged; path tokens are regex-guarded against traversal; version/freshness binding of the SPECIFIC
resolved dep is enforced separately at launch by the readiness gate
(`_verify_dependency_readiness`). **Verified read-only against the real failed-run artifacts**
(`orch_20260625T045636Z_9f9a00cb` run_20260625_003): the previously-failing `post_execute` gate now
returns `PASS`. Unit tests added (`test_validate_pipeline_semantics.py`: built-dep excused /
unbuilt-dep still flagged / failed-binary not excused / traversal guard); suite green (1571).

**Not yet done:** a billed `--with-deps` re-run to confirm the target reaches `aggregate_verdict=pass`
end-to-end (deferred by operator choice — the fix is verified against the captured failing artifacts,
which is the same gate the live run invokes).

Note: the demo `tests.md` xfail wording was ALSO clarified (2026-06-25) so the runner emits a clean
`verdict.overall=pass` with `input_guard` as a passing guard — a real robustness improvement, but it
was NOT the blocker (this DAG-scope check is). Both E2E runs failed here.

## D3 — `post_execute` snapshot completeness over-strict for guard-rejection cases (FIXED 2026-06-25)
**Found by the billed dev `--with-deps` E2E re-run (2026-06-25, orch `orch_20260625T141819Z_2692801d`).**
With the D2 validate-scope fix in place, the run reached further but `fail_closed` at the **leaf
dependency node `demo_dep_base`** (so the target `demo_dep_top` never ran): `validate.execute` failed,
and the dev F1 gate correctly stopped on the first cross-phase rollback (`reason_code=dev_phase_rollback`,
`reason_detail=validate_execute_fail`, zero budget burned). The runner itself was clean
(`diagnostics.json verdict.overall=pass`); the blocker was the deterministic `post_execute` snapshot
gate:
```
raw/state_snapshots: declared state_variables missing in snapshot files ({'c_l0_invalid_length.json': ['y']})
```
**Root cause:** `_validate_raw_evidence` (`tools/validate_pipeline_semantics.py`) required the **global
union** of declared schema `variables[]` (`{x, y}`) in **every** snapshot file. But the input-guard
rejection case (`n <= 0`) produces no output state, so its snapshot legitimately carries only the
rejected input `x` and omits the output `y`. The IR was already correct and self-consistent — its
`io_contract.test_evidence_requirements` scopes `l0_invalid_length_xfail` to `required_raw_variables:
[x]` (judged on `input_guard` diagnostics) vs `[x, y]` for the valid case. The gate ignored that
per-case scoping (the `metrics_basis` gate at `:~5904` already honored it; this gate did not). A
C-class cross-phase robustness gap surfacing as a gate-vs-contract disagreement, NOT a
dependency-build/migration bug — and the dependency-build path (D1/D2) was not even exercised this run.

**Fix — IMPLEMENTED (validator-side, deterministic).** `_validate_raw_evidence` now scopes each
snapshot's required state variables to its case's test: it builds `_case_id_to_test_id` (from
`case.test_case_set`, via `_algorithm_contract_for_execution`) and intersects the per-test
`_contract_test_evidence_requirements` with the declared schema variables. A snapshot tagged with a
`case_id` (in-file field, else filename stem) is only required to carry that case's test's
`required_raw_variables`. Falls back to the prior strict union when no per-test contract / case mapping
is resolvable (backward compatible); strictness is preserved for any case whose test *does* require the
variable. phase_04_validate.md §43 reconciled. **Verified read-only against the captured failing
artifacts** (`orch_20260625T141819Z_2692801d` → pipeline `demo-dep-base_20260625_001` run
`run_20260625_001`): the previously-failing `post_execute` gate returns **PASS**. Unit tests added; suite
green.

**Hardened for a second IR shape (2026-06-26).** A second billed dev `--with-deps` re-run
(`orch_20260625T150418Z_6571ad31`, on the committed first fix) `fail_closed` at `demo_dep_base`
validate.execute with the *same* error class but a *different* Compile/runner output shape — C-class IR
nondeterminism: the snapshot was named `l0_invalid_length_xfail_0000.json`, carried an in-file
**`test_id`** field, and `case.test_case_set[].test_id` was **null** (so `_case_id_to_test_id` returned
an empty map). The first fix keyed only on the case_id→test_id map and so fell back to the strict union,
again wrongly failing the guard case. **Hardened resolution:** the scope now anchors on whatever
authoritative identity the snapshot self-declares, trying in order — (1) the snapshot's in-file
`test_id`, (2) `case_id` mapped via `case.test_case_set`, (3) `case_id`/filename-stem used directly as a
test_id — and uses the first that is a key in `test_evidence_requirements`; only then falls back to the
strict union (the `if per_test_required and case_to_test` guard was relaxed to `if per_test_required`,
since an in-file `test_id` no longer needs the map). **Verified read-only:** post_execute now PASS on
BOTH shapes (`demo-dep-base_20260625_001` run-1 case_id-only, and `demo-dep-base_20260625_002` run-2
in-file-`test_id`/empty-map). Tests:
`test_snapshot_state_variables_scoped_to_per_case_evidence` (shape 1),
`test_snapshot_scope_resolves_via_in_file_test_id_when_case_map_empty` (shape 2),
`test_snapshot_completeness_falls_back_to_strict_union_without_per_test_contract` (strict fallback);
suite green (1594).

**Third dev run (2026-06-26, orch base `…234756Z_a89f1956` / top `…234756Z_9c50988f`, on committed
`e992b60`): D1/D2 dependency BUILD path PROVEN end-to-end; base node fully passed.** `demo_dep_base`
completed compile→generate→build→**validate all pass** (`workflow_status=pass`) — the D3 hardening
cleared the snapshot gate that fail-fasted the two prior runs. `demo_dep_top` then went
compile✅→generate✅→**build✅**: the conductor authored the dependency Makefile, `_stage_dependency_sources`
staged the certified `demo_dep_base_model`, and **real gfortran compiled+linked the closure** into
`demo_dep_top_runner` in a single live `--with-deps` run. That is the conclusive D1/D2 build verification
(previously only the same-session two-orchestration evidence existed). **The target did NOT reach
`aggregate_verdict=pass`, but the blocker is unrelated to dependencies — see D4.**

## D4 — runner snapshot filename off the per-case `<case_id>.json` contract (FIXED + E2E-CONFIRMED 2026-06-26)
`demo_dep_top` `fail_closed` at `validate.execute` (dev `dev_phase_rollback`) **despite**
`trial_meta.status=pass`, clean `diagnostics.json` (`verdict.overall=pass`), and the post_execute
**semantic** gate passing (verified standalone PASS, both legacy and orchestration-context). Root cause is
a THIRD facet of the runner snapshot-filename nondeterminism (same family as D3), this time at the
**conductor's deliverable layer**, not the validator:
- `build_launch_request` (`workflow_conductor.py:538-541`) derives `allowed_output_paths` for
  validate.execute as one snapshot file **per case_id**: `raw/state_snapshots/{case_id}.json`.
- `_classify_substep` (`:1234-1241`) gates execute pass on `trial_meta.status=="pass"` **AND**
  `_fresh_deliverables_written(allowed_output_paths)` — i.e. every `{case_id}.json` must exist.
- The `demo_dep_top` runner wrote a single combined `snapshot_0001.json` (schema `samples=['snapshot_0001.json']`),
  so the expected `l0_shift_scaled_identity_pass.json` / `l0_invalid_length_xfail.json` were absent →
  `_fresh_deliverables_written` False → execute fail. The deterministic logs are empty and there are no
  write violations, because the failure is the deliverable presence check, not the runner or the gate.
- Observed runner naming across runs (all valid per contract, but only some match `{case_id}.json`):
  base run-1 `c_l0_invalid_length.json` (matched), run-2 `l0_invalid_length_xfail_0000.json` (didn't, but
  failed on the semantic gate first), run-3 `l0_invalid_length_xfail.json` (matched → passed), top
  `snapshot_0001.json` (matched nothing → failed). So the conductor passes only when the LLM happens to
  name snapshots exactly `{case_id}.json`.
- **The canonical contract does NOT require per-case filenames.** `phase_04_validate.md` §43/§44 require only
  `snapshot_schema.json` + ≥`min_samples` data files (any names, listed in `schema.samples`); the
  post_execute semantic gate enforces exactly that. The conductor's `{case_id}.json` requirement is an
  over-specification stricter than both. **Judge recomputation reads `raw/metrics_basis.json` (per-test
  index), NOT per-case snapshot files**, so per-case snapshot filenames are not needed downstream.
- **Fix — IMPLEMENTED (2026-06-26; operator direction: keep the conductor strict, teach the runner the
  canonical name, detect early).** The report-only "relax the conductor" proposal below was NOT taken;
  instead the canonical `{case_id}.json` requirement is kept (it IS the contract now) and the rest of the
  system is made to agree + catch a wrong name early. Two prongs:
  1. **Teach the generator.** The runner already receives the exact case_id strings on argv
     (`--cases <spec> *case_ids`, `workflow_conductor.py:1780`), so it can name files `<case_id>.json`
     directly. Contracts/SKILLs updated to mandate **one snapshot per case at
     `raw/state_snapshots/<case_id>.json`, built from the received `case_id`** (never a fixed/sequential
     literal): `phase_02_generate.md` (runner-output rules), `phase_04_validate.md` §43,
     `skills/workflow-generate-generate/SKILL.md`, `skills/workflow-generate-verify/SKILL.md`. This matches
     the already-agreed `CLI_REFERENCE.md` `output_refs` name. (Doc-size ceilings for phase_02/04 bumped
     with justification in `test_orchestration_runtime.py::ChildContextDocSizeTests`.)
  2. **Detect early (best-effort static) + a deterministic backstop.**
     - `post_generate`: `_validate_runner_snapshot_filenames` (`validate_pipeline_semantics.py`, wired into
       `_validate_runner_source_files` ← `_validate_generate_outputs_for_generation`) flags a hardcoded
       whole-path `state_snapshots/<name>.json` string literal not built from the `case_id`
       (`snapshot_0001.json`). IR-aware via `_case_ids_for_execution`: a literal whose stem IS a declared
       `case_id` is NOT flagged (it satisfies the deliverable gate — no false positive); `snapshot_schema.json`
       is exempt. Best-effort (runtime-built names this parse can't resolve fall to the backstop).
     - `validate.execute` backstop: `Conductor._snapshot_deliverable_gap` (`workflow_conductor.py`, called in
       `_execute_inproc` after evidence promotion) compares the expected `{case_id}.json` set against what the
       runner actually wrote and, on a gap, records an actionable diagnostic (`expected=…; runner wrote=…;
       missing=…`) into the execute failure and sets `trial_meta.status=fail` (rc 0 → routes to Generate with a
       clear cause, instead of the opaque `_fresh_deliverables_written` presence fail).
  Authorization stays permissive (promotion still globs `*.json`); only the expected NAME is enforced. Unit
  tests: `test_runner_snapshot_filename_must_be_per_case` (static check: literal flagged / case_id-built ok /
  matching-case_id literal not a false positive / schema exempt / continuation-merge), `SnapshotDeliverableGapTest`
  (backstop diagnostic). Suite green (1598). Committed `c69f9bc`.
- **E2E-CONFIRMED (2026-06-26, orch `orch_20260626T020724Z_0d7b9e28`, dev `--with-deps`).** Reusing the already-ready
  `demo_dep_base` (skipped, `status=ready`), `demo_dep_top` ran compile✅→generate✅→build✅→validate✅ all on
  **attempt 1** and reached **`aggregate_verdict=pass`** (`workflow_status=pass`). Decisive because **dev mode
  fail-fasts on the first cross-phase rollback** (F1): a clean attempt-1 pass means the taught generator emitted the
  canonical per-case names on the first try — the runner wrote `l0_shift_scaled_identity_pass.json` +
  `l0_invalid_length_xfail.json` (exactly the two declared `case_id`s), where the prior failing run wrote a single
  combined `snapshot_0001.json`. The conductor deliverable gate passed without the backstop firing. This closes the
  LAST known blocker — `demo_dep_top` now reaches `aggregate_verdict=pass` end-to-end.

- **Proposed fix (NOT implemented — superseded by the IMPLEMENTED fix above):** align the conductor's execute
  snapshot-deliverable check with the canonical contract — require `snapshot_schema.json` + ≥`min_samples`
  fresh snapshot data files in `raw/state_snapshots/` (derive-from-ground-truth, the `_fresh_deliverables_written`
  smell test), instead of hardcoding `{case_id}.json`. Keep authorization permissive (the deterministic
  execute already promotes tmp snapshots by glob, so any runner name is accepted into the canonical tree).
  This is the D3-style robustness fix one layer down; it is the LAST known blocker to `demo_dep_top`
  reaching `aggregate_verdict=pass`. Alternatively, a prod re-run's retry budget would also absorb it.

**Net status of the original D1/D2 verification:** the dependency BUILD path is E2E-proven in a live
`--with-deps` run, and as of 2026-06-26 the dependency TARGET also reaches full **`aggregate_verdict=pass`**
end-to-end (orch `orch_20260626T020724Z_0d7b9e28`): `demo_dep_top` reused the ready `demo_dep_base`
(skipped) and passed all phases on attempt 1 in dev mode. The D4 snapshot-naming blocker is fixed and
confirmed. No known blockers remain for the demo dependency chain.

## D5 — dependency call-site argument order surfaced to the consumer (IMPLEMENTED 2026-06-26)

**Symptom.** With D1–D4 closed, the demo chain's `--with-deps` E2E was still not
deterministic across runs: a consumer (`demo_dep_top`) emits `call <dep>__<op>(...)` to a
dependency subroutine (`demo_dep_base__scale`) and **guessed the Fortran argument order**.
A wrong guess (`(n,x,y)` for the certified `(x,n,y)` interface) compiles the consumer against
a type/rank mismatch and **fails Build**. This is a variance-prone C-core inference: IR
`dependency.direct_deps[].operations` carries only operation *names*, and at Generate time the
dependency source is not staged into the consumer's `$(OBJDIR)` (only at Build, Model B / D1),
so the agent had nothing authoritative to read.

**Fix (host-side interface surfacing — no IR schema change, no guessing).** Under
`--with-deps` the closure runs deepest-first, so the dependency's **certified** source already
exists when the consumer generates. Surface its real signature host-side and inject it into the
existing `<dependency_facts>` launch-prompt block:
- New `orchestration_runtime._certified_model_source(pipe_dir, spec_id) -> Path|None`:
  the single-sourced selection (latest `binary/*/binary_meta.json` → `source_source_id` →
  `source/<id>/src/<spec_id>_model.f90`) that **both** the Generate-time hint
  (`_resolve_dependency_facts`) and Build staging (`_stage_dependency_sources`, refactored onto
  it) use — so the interface SHOWN equals the source Build COMPILES (the two had already drifted
  once). Pure/never-raises; Build re-raises its fail-closed precondition on `None`, the hint
  skips that dep.
- New `_extract_subroutine_interface(source_text, op_name)`: robust to `&` continuations
  (the generate SKILL forces wrapping for fortitude S001), `!` comments, case, prefixes
  (`pure`/`elemental`/`recursive`/`module`), and multiple subroutines (selects by name). Returns
  `{interface, argument_order}`, the load-bearing datum being the positional order.
- `_resolve_dependency_facts` (gated on the **consumer** being Fortran; only **direct** deps —
  the consumer call-sites only those) adds `published_operations:[{operation, interface,
  argument_order}]`; `_build_dependency_facts` renders a role-aware "Published dependency
  operations" sub-block instructing Generate to call with EXACTLY that order.
- Docs/SKILLs: `phase_02_generate.md` §47 (authoring) + §G7 (verify), generate/verify SKILLs.

**Not a new gate.** A deterministic argument-order check is infeasible (Fortran is positional;
the consumer uses its own local names), so this is *variance reduction* — give Generate the
correct order so it gets it right the first try. Build's compiler remains the deterministic
backstop (a mismatch fails Build → routed back to Generate); the verify SKILL adds an LLM check.
Cross-ref: D1 (Model B staging), L6 (spec_id basename keying). Tests:
`ExtractSubroutineInterfaceTests`, `CertifiedModelSourceTests`, extended
`ResolveDependencyFactsTests` / `DependencyFactsRenderTests`. **Residual:** billed `--with-deps`
E2E re-run to confirm the consumer emits the correct order on attempt 1 (operator-gated).

## L (latent / low severity — fix opportunistically)
- **L1 — DONE (2026-06-25).** Generated Makefile emitted a harmless `make` warning
  `target '.' given more than once` for the `$(OBJDIR) $(BINDIR):` rule when `OBJDIR==BINDIR=="."`.
  Fixed in `_write_makefile` by wrapping the target list in GNU make `$(sort $(OBJDIR) $(BINDIR)):`
  which dedups (collapses to one target when equal; two when distinct) — no warning, single rule.
  Test: `test_authors_makefile_for_leaf_node`.
- **L2** The C1 scalar gate assumes the per-snapshot time index is always scalar; a future
  spec needing a vector per-file time dimension would need a carve-out
  (`validate_pipeline_semantics._validate_io_contract_file`).
- **L3 — DONE (2026-06-25).** C2 escalation threshold was the bare literal `2` in
  `workflow_conductor.classify_failure`; extracted to the named module constant
  `C2_EXECUTE_FAIL_ESCALATION_THRESHOLD = 2` (near `MAX_ATTEMPTS_PER_PHASE`) for tunability.
  Behavior unchanged; covered by `test_recurring_execute_failure_escalates_to_compile`.
- **L4** `_impl_is_leaf_node` disagrees with the YAML parser only for **invalid** YAML
  (tab-indented `direct_deps`) — benign/unreachable (fails compile), not worth fixing.
- **L5** A judge session/usage-limit now ends as a clean resumable `fail_closed`
  (`leaf_transport_error`) but still requires a **manual `--resume`** after the quota
  resets — no auto-retry/scheduling. By design; revisit if it becomes operationally painful.
- **L6 — GUARDED (2026-06-25).** The dependency build (Model B) keys staged source filenames and
  Makefile object rules on the bare `spec_id_of(node_key)` (`_dependency_closure` /
  `_stage_dependency_sources`), dropping `kind` and `@version`. A closure containing two deps that
  share a `spec_id` but differ in version or kind (e.g. `component/foo@1.0.0` + `component/foo@2.0.0`,
  a diamond) would collide on `foo_model.f90` (last-write-wins stage + duplicate
  `$(OBJDIR)/foo_model.o` rules + a duplicate `module foo_model`). The version-pinned *pipeline*
  path stays unambiguous (node_key carries `@version`); only the in-`$(OBJDIR)` basename is not.
  Not reachable by the minimal 2-node verification spec. **Fix:** `_dependency_closure_nodes` now
  raises a clear `RuntimeError` ("spec_id basename collision …") when two distinct closure
  node_keys map to the same spec_id, so both consumers (`_dependency_closure` Makefile rules and
  `_stage_dependency_sources` staging) inherit the guard before any clobber. A guard, not
  version-qualification, because qualifying the staged/object basename alone would not fix the
  `module <spec_id>_model` name clash — proper multi-version support needs module renaming (a
  larger change, deferred until a multi-version/diamond closure is actually required). Test:
  `test_dependency_closure_raises_on_spec_id_basename_collision`.

## T1 — testing gap (PARTIALLY CLOSED 2026-06-25)
The transport+resume path is covered by two unit layers (conductor routing in
`test_workflow_conductor.py::TransportFailureTest`; runtime helper + completion exemption in
`test_orchestration_runtime.py::TransportOrphanCompletionTest`). **Added
`test_workflow_conductor.py::TransportTombstoneRealCliTest`** — drives the **real**
`Conductor.runtime()` subprocess (symlinks the real `tools/` into a temp repo) calling the actual
`add-superseded-runs` CLI and asserts the persisted superseded set via `_load_superseded_run_ids`
(the exact reader the completion check consults) + idempotency. This closes the conductor→CLI seam
that was previously only smoke-tested. Deliberately narrowed to the tombstone seam: the full
conduct→judge→resume loop (seeding a node to `validate.judge`) is still covered only by the unit
layers — extend to the full loop if that seam changes.

## F1 — dev-mode retry scoping: stop on phase rollback, auto-retry only within a phase (DONE 2026-06-25)
> Ready-to-paste starter prompt (begins in plan mode): `docs/design/dev_mode_retry_scoping_followup_prompt.md`.

**Implemented (2026-06-25).** `Conductor.conduct` (`workflow_conductor.py`) now gates the
cross-phase router: in dev mode, a backward rollback — the `target_idx < idx` case, the only
routing that actually reopens an already-passed upstream phase — sets `fail_closed` with the
dedicated reason_code `dev_phase_rollback` (allowlisted in
`orchestration_runtime.FAIL_CLOSED_REASON_CODES`), carrying the routing reason in `reason_detail`,
instead of reopening. `target_idx < idx` already covers every real reopen (they all target
compile) and every earlier-phase retry; a same-phase/forward (malformed) reopen is not a backward
rollback and falls through to the existing `target_idx >= idx` terminal-fail branch (plain `fail`,
as in prod) rather than being mislabeled a rollback. The gate sits before the attempts-budget
increment (so the rollback never consumes budget). The intra-phase substep loop
(`run_phase`/`run_substep`, generate `verify→regenerate`) is untouched — that is the within-phase
retry dev keeps. The C2 backstop (`classify_failure` execute-no-verdict → reopen compile) is
unchanged code but is now intercepted by the dev gate (no-op in dev, live in prod). prod keeps the
bounded cross-phase reopen/retry. Tests: `DevPhaseRollbackTest` (dev validate→generate /
execute-no-verdict / reopen → first-occurrence `fail_closed`+no reopen; prod parity reopens;
dev intra-phase same-phase route stays plain `fail`, not rollback); the existing prod reopen tests
(`test_reopen_compile_on_ir_then_succeed`, `test_reopen_budget_exhausts_to_fail_closed`,
`test_conduct_escalates_then_reopens`) pin `workflow_mode="prod"`; `dev_phase_rollback` added to the
allowlist coherence test. Suite green.

**Decision (operator):** in **dev** mode, a **cross-phase rollback** — any routing that goes back
to an earlier phase (`build→compile`, `build→generate`, `validate→generate`, `validate→compile`,
including the `validate.execute`-no-verdict → `generate`/`compile` routes and every `reopen`) — must
**`fail_closed` immediately** (surface to the operator), NOT auto-retry. Dev-mode auto-retry is
confined to **within a single phase** — the substep-level iteration (e.g. `generate.generate →
generate.verify → regenerate` inside the `generate` phase). **prod** mode keeps today's full
cross-phase reopen/retry behaviour (bounded by `MAX_ATTEMPTS_PER_PHASE`).

**Why.** Dev is for fast feedback. Cross-phase regeneration loops frequently cannot fix the
underlying problem (the C1/C2/D2 "structural mismatch that regenerating one side can't fix" pattern)
and burn the entire attempt budget before surfacing. Example that motivated this: the D2
`validate.execute` DAG-scope failure was (mis)classified as a code defect and routed
`generate→build→validate→(compile reopen)→…` until `generate exceeded 3` → `fail_closed:
retry_budget_exhausted`, ~90 min of billed churn for an infra/structural issue an operator should
have seen on the first rollback. Under this decision dev would have stopped at the first
`validate.execute→generate` rollback.

**Scope / relation to current behaviour.** This GENERALIZES the existing dev-only gate
`classify_verify_severity` (`workflow_conductor.py:193-202`: dev + verify/judge `major|critical` →
`fail_closed`) from "verify/judge severity only" to "any cross-phase rollback regardless of failure
classification". Intra-phase iteration is unchanged.

**Where it was implemented** (original plan; the authoritative shipped form is the "Implemented
(2026-06-25)" note at the top of this section — read that for the final gate condition).
- `Conductor.conduct` (`workflow_conductor.py`) is the cross-phase router. It already computes
  `target_idx = phases.index(target)` vs the current `idx`. The shipped gate fires when
  `self.workflow_mode == "dev"` AND `target_idx < idx` (the backward-rollback predicate). The
  originally-sketched extra `action == "reopen"` arm was dropped: every real reopen targets compile
  (upstream) so `target_idx < idx` already covers it, and a same-phase/forward (malformed) reopen is
  NOT a backward rollback — it falls through to the existing `target_idx >= idx` terminal-fail branch
  (plain `fail`, as in prod) rather than being mislabeled `dev_phase_rollback`. Sets `fail_closed`
  with `reason_code="dev_phase_rollback"` (allowlisted in `FAIL_CLOSED_REASON_CODES`), before the
  attempts-budget increment. Forward `advance` and same-phase in-place handling are untouched.
- Intra-phase substep retries (the `run_phase`/`run_substep` substep loop and the generate
  verify→regenerate cycle) are unaffected — those are the "within a phase" retries dev keeps.
- The C2 backstop (`classify_failure` `validate_execute_fail_count` → reopen compile) becomes a
  no-op in dev (it routes a cross-phase reopen, which the dev gate now stops on) — kept for prod.
- Tests: dev `validate→generate` / `validate→compile` / execute-no-verdict rollback → `fail_closed`
  on first occurrence; same in prod → reopen/retry as today; dev intra-phase generate verify loop
  still retries (`DevPhaseRollbackTest`).

## G1 — deterministic `Generate.static` substep: post_generate + workspace_root before LLM verify (IMPLEMENTED 2026-06-29)

**Problem.** The purely-static post_generate gate
(`validate_pipeline_semantics --stage post_generate` + `validate_workspace_root.py`) ran
*inside* the LLM `Generate.verify` leaf as a SKILL responsibility. A full, cold,
separate-persona verify pass (G1–G7 semantic checks) ran first and was thrown away whenever a
purely structural defect (63-char identifier, `F0`/`L` descriptor, hardcoded snapshot name,
forbidden runner output, makefile/naming/io_contract violation) tripped the gate at completion —
wasted tokens with zero accuracy benefit.

**Shape (nested loop the design realizes).**

```
loop1 (outer):
  loop2 (inner): generate.generate (LLM, warm resume) -> lint -> static   # deterministic gates
  generate.verify (pure LLM semantic G1-G7)
```

This is produced by the existing conductor machinery, not a new literal loop: the `run_phase`
substep loop **breaks on the first non-pass substep**, and a `lint`/`static` finding routes to a
**same-phase warm-resume reopen** of `generate.generate`. So `verify` is reached only when every
deterministic gate is clean, and the producer leaf stays warm across inner iterations.

**Decision — new `static` substep (not folded into `lint`).** `SUBSTEPS["generate"]` becomes
`("generate", "lint", "static", "verify")`. A distinct substep keeps a distinct
`failure_category` (`post_generate_violation` / `workspace_root_violation`) and routing reason
(`static_*`) — mirroring how Build keeps `validate_post_build_violation` distinct from
`compile_error` — and is low-risk because `classify_failure` maps the failed substep **by name**,
not index. It also makes the three substep-aware phases symmetric:
`compile→post_build`, `generate→post_generate`, `validate→post_execute`.

**Where implemented (`tools/workflow_conductor.py`).**
- `SUBSTEPS["generate"]` + `STATIC_FAILURE_ROUTING` + `classify_static_failure` (mirrors the lint
  table/classifier).
- `_is_deterministic_substep` and `_run_deterministic_substep` dispatch `generate.static` →
  new `_static_inproc`, which runs the two validators via `subprocess.run` (same idiom as the
  post_build gate in `_build_inproc`) and writes `static_meta.json` (`status` +
  `failure_category` + `failure_excerpt`). A violation is a CONTENT failure (rc 0) routed via the
  table; only an unexpected exception becomes a transport `fail_closed`.
- `determine_substep_status` and `classify_failure` gain `generate.static` branches reading
  `static_meta.json`.
- The same-phase warm-reopen guard in `conduct` widened from `.startswith("lint_")` to
  `.startswith(("lint_", "static_"))`. (Later superseded — see the "Same-phase producer reopen is
  now a first-class routing outcome" note below: the guard is now the structural condition
  same-phase target + concrete `repair_strategy`, no route-reason prefix and no flag.)
- `build_launch_request` gains a `static` arm with `allowed_output_paths=[<src>/static_meta.json]`.
  `static_meta.json` is intentionally NOT a `phase_required_outputs` entry (parity with
  `lint_meta.json`); the substep freshness gate covers it.

**Ownership transfer (`tools/orchestration_runtime.py`).**
`ALLOWED_VALIDATE_PIPELINE_STAGES[("generate","verify")]` set to `frozenset()`;
`("generate","static"): frozenset()` added (keeps the table total). `_render_runbook`'s
`generate.verify` branch removed → it returns `""`. `generate.verify` therefore launches **no**
validator gate and is a pure LLM semantic pass.

**SKILL/doc updates.** `skills/workflow-generate-verify/SKILL.md` drops the post_generate +
workspace_root leaf responsibility (old Operations Rules 6/7). `phase_02_generate.md`,
`WORKFLOW_CORE.md`, `ORCHESTRATION.md` updated to `generate → lint → static → verify`.

**Non-regression notes.**
- The verify leaf invoked `validate_workspace_root.py` **without** `--write-scope-baseline`; the
  baseline branch was never reached. `_static_inproc` reproduces the exact bare invocation — no
  dropped argument. (Do not "correct" this by adding a baseline.)
- `post_generate` is purely static and does **not** read `source_meta.verification_status`, so
  running it before verify is acyclic. It certifies `lint_evidence`, which the conductor wrote in
  `_lint_inproc` earlier in the same attempt — i.e. it certifies conductor-owned evidence.
- `static_meta.json` lives under `source/<id>/` (the substep's own write_root), so unlike the
  pipeline-root `lint_evidence` certificate it needs no `record-agent-run` write exemption.

### G1-slim — slim warm-resume repair turn (findings-only prompt)

**Context.** The `lint`/`static` finding reopen re-runs `generate.generate` with
`repair_strategy=reuse`, which (claude) warm-`--resume`s the
producer leaf's session so its context is intact. Empirically that already roughly halves the
generate substep wall-time (one observed node: 544s cold → 225s warm). But the warm turn still
re-sent the **full ~11.5KB cold-start prompt** and did **not** include the findings: `lint` runs
in-process **after** the generate leaf finishes, so the resumed leaf never saw its own findings and
fixed them by re-reading its source and *guessing* (a real correctness risk — e.g. the subtle C061
case-insensitive `u_L`≡`U_L` collision). It also `find`s the rotated new source dir because its
warm context holds the **stale** old paths.

**Decision.** When a warm resume actually fires, send a **slim** repair turn instead of the full
prompt: inject the `failure_excerpt` and EVERY rotated per-agent path the resumed context now
holds stale — `agent_run_id`/`source_id`/`allowed_output_paths`/`output_manifest_path`/
`capability_doc_path`/`read_manifest_path` (the capability file is per-arid: the leaf must read its
`capability_token` fresh from the NEW path or `run-gate` fails with a capability mismatch) — and drop
the SKILL boilerplate, must-read header,
dependency facts and gate runbook (the resumed leaf already holds them). The win is **correctness
+ orientation** (fix the exact reported lines; no stale-path `find`), not primarily wall-time —
the warm leaf already skips re-reading the must-read docs on its own. Token cost drops ~11.5KB→
~1–2KB as a secondary benefit.

**Gate.** Always-on — the former opt-in env flags `METDSL_CONDUCTOR_REUSE_SLIM_PROMPT` and
`METDSL_CONDUCTOR_REUSE_RESUME` were removed (warm resume + slim are now the default). Slim applies
**only** when a warm resume is actually resolved (session resumable) AND a findings excerpt is
present — i.e. the `Generate.lint`/`Generate.static`/`Compile.static` deterministic-gate reopens;
a warm reuse without findings (e.g. a cross-phase code repair) or a cold fallback keeps the full
prompt unchanged. The warm/cold selection itself stays driven by `repair_strategy`
(`reuse`→warm, `restart`→cold).

**Where implemented.**
- `tools/workflow_conductor.py`: `_resolve_reuse_resume` (extracted from `run_substep` so the
  resume decision is made **before** `build_launch_request` — the slim/full choice, the
  `record_launch`-persisted prompt and the `spawn_leaf` args must all agree); `_read_repair_findings`
  (reads the failed source's `{lint,static,compile_static}_meta.json` `failure_excerpt` at the reopen
  point, before id rotation); `_repair_payload(..., findings=...)` → `repair_findings`;
  `build_launch_request` sets `req["warm_resume"]` (when a warm resume resolved AND findings present)
  and empties `skill_must_read_refs`. (Update 2026-06-30: warm resume + slim are now ALWAYS-ON — the
  former opt-in env flags and their `_reuse_resume_enabled` / `_reuse_slim_prompt_enabled` gates were
  removed; `run_substep` sets `warm_resume = resume_session_id is not None`.)
- `tools/orchestration_runtime.py`: `_is_slim_repair_request` + `SLIM_REPAIR_PROMPT_SENTINEL` +
  `_render_slim_repair_launch_prompt` (built directly like the deterministic prompt, branched in
  `_render_launch_prompt_template`); `prepare_launch_request_payload` empties `skill_must_read_refs`
  for slim (**both** must-read assembly paths must agree or `_validate_launch_prompt_text` rejects the
  persisted prompt); `_required_launch_prompt_markers` / `_required_launch_prompt_constraint_lines`
  gain slim branches.

**Non-regression notes.**
- Emptying `skill_must_read_refs` does **not** lose read access: `build_access_policy_payload`'s base
  `allowed_read_roots` already blanket-grants `docs/`, `spec/`, `ir_ref/` and `pipeline_ref/` (the
  source), plus `skill_ref`. Must-read only adds a redundant force-read list.
- `repair_findings` is threaded into `pending_repair` regardless of `warm_resume`; only the
  `warm_resume` flag + emptied must-read are gated on an actually-resolved resume, so a cold fallback
  still carries the (unused) findings without changing the full prompt.
- The slim deliverables block lists the **leaf-writable** paths from
  `_allowed_file_tool_paths_for_launch` — NOT the raw `allowed_output_paths`. The raw set includes
  MCP-owned `command_log.jsonl` (integrity-protected) and the conductor-authored in-process
  `lint_meta.json` / `static_meta.json`; listing those under "re-write the deliverables below" would
  have the resumed leaf Edit/Write the command log and trip the write guard / corrupt the MCP audit
  artifact. The full prompt already derives the same file-tool subset, so slim matches its posture.
- The gate-allowlist lint (`_lint_launch_prompt_gate_allowlist`) scans only the conductor-authored
  prefix for a slim turn (`_gate_allowlist_scan_text` fences out the findings region at
  `SLIM_REPAIR_FINDINGS_HEADER`): the injected `failure_excerpt` is uncontrolled, quoted gate output
  (DATA, not a leaf instruction), so a `validate_pipeline_semantics` string inside it must not
  fail-close the launch under the empty `(generate,generate)` allow-set.
- Prompt-injection hardening: the `failure_excerpt` is the ONE untrusted span in the slim prompt
  (it quotes the leaf's own source, which the leaf authored), so it is wrapped in a data-only fence
  (`SLIM_REPAIR_FINDINGS_WARNING` + `SLIM_REPAIR_FINDINGS_FENCE_BEGIN`/`_END`) that tells the resumed
  LLM to treat everything between the markers strictly as data to fix and never as instructions to
  follow.

## G2 — deterministic `Compile.static` substep: hoist `--stage compile` out of the LLM `Compile.verify` leaf (IMPLEMENTED 2026-06-30)

**Problem (the G1 pattern, one phase up).** The purely-static IR gate
(`validate_pipeline_semantics --stage compile` + `check_artifact_syntax` + `validate_workspace_root`)
ran *inside* the LLM `Compile.verify` leaf. A full, cold, separate-persona verify pass ran first and
was thrown away whenever a purely structural IR defect (forbidden `shape_expr` form `vector(N)`,
non-scalar `time_shape_expr`, undefined `steps[]` token binding, null knob, malformed `ir_meta`)
tripped the gate at completion — wasted tokens with zero accuracy benefit. This is exactly the
generate.verify→generate.static situation (G1), one phase up.

**Decision — new `static` substep.** `SUBSTEPS["compile"]` becomes `("generate", "static", "verify")`.
The conductor's `Compile.static` (`_compile_static_inproc`) runs the three gates the old
`compile.verify` runbook emitted — `validate_workspace_root`, `check_artifact_syntax` on
`spec.ir.yaml` + `ir_meta.json`, and `validate_pipeline_semantics --stage compile` — and authors
`compile_static_meta.json` under the IR dir. A violation is a CONTENT failure (rc 0) routed via
`COMPILE_STATIC_FAILURE_ROUTING` (`compile_static_violation → ("compile","reuse")`) as a SAME-PHASE
warm reopen of `Compile.generate`; only an unexpected exception is a transport `fail_closed`.

**`Compile.verify` is NOT eliminated.** `--stage compile` reads no `controlled_spec.md` and parses
`tests.md` only for a regex test_id-set check, so it covers the internal-consistency / shape-grammar
invariants only. The genuinely-semantic spec-cross-reference invariants stay LLM: V1 case substance,
V3 recompute-sufficiency + `tests.md §3` diagnostics coverage (`_validate_diagnostics_contract`
explicitly defers §3 coverage to the LLM), V5 impl_defaults. So `compile.verify` becomes a pure LLM
semantic pass (like generate.verify post-G1) that launches no validator gate.

**Prerequisite — `io_contract` authorship moved to `Compile.generate` (review-driven correction).**
The first cut mirrored G1 mechanically and was WRONG: unlike `generate.verify` (a pure judge),
`compile.verify` was a *producer* — the compile SKILLs had it AUTHOR the `io_contract` section of
`spec.ir.yaml`. `--stage compile` hard-requires a structurally-complete `io_contract`
(`_validate_io_contract_file` is unconditional), so running it in a pre-verify `Compile.static`
validated an IR whose `io_contract` did not exist yet → every fresh compile node would `fail_closed`
(the mock-stubbed tests were blind to it). Fix (the only one that makes a pre-verify static gate
coherent AND delivers the fail-fast value): **`Compile.generate` now authors all 5 sections including
`io_contract`** (this already matched `phase_01_compile.md` §1-1; only the two compile SKILLs +
GLOSSARY dissented — they were realigned), and `Compile.verify` only CHECKS `io_contract` (V3),
writing nothing but `ir_meta.json`. Structurally this makes `compile.generate`→`compile.static`→
`compile.verify` a true analog of `generate.generate`→`generate.static`→`generate.verify`: producer →
deterministic structural gate → pure semantic judge. (Reconciled: `skills/workflow-compile-generate`
gains the authoring rules, `skills/workflow-compile-verify` drops them, `docs/GLOSSARY.md`
`diagnostics_contract` provenance, compile-generate SKILL ceiling 10800→11500.)

**`compile.verify` freshness gate (Codex P2 follow-up).** Because the `--stage compile` gate moved
OUT of `compile.verify` (to `compile.static`), the verify leaf lost the implicit "must do work"
enforcement its own end-of-substep gate used to provide. Its status check
(`determine_substep_status`) read `ir_meta.verification_status == "pass"` with NO freshness gate, so a
no-op verify (exit 0 without re-authoring `ir_meta.json`) would pass by reading a stale
`verification_status=pass` that `Compile.generate` — now the IR author — may have left (the `--stage
compile` gate only requires a non-empty string, not a specific value). Fix: the compile.verify branch
now ANDs `_fresh_deliverables_written(allowed_output_paths)` (allowed_output_paths == `[ir_meta.json]`
after the verify write-restriction), enforcing the SKILL's "an inspect-only verify that writes nothing
cannot terminate pass". A compliant verify re-authors `ir_meta.json` (refreshing `verify_attempts`) so
it passes; a no-op verify fails. Test: `test_compile_verify_requires_fresh_ir_meta`. The symmetric
`generate.verify` branch had the same latent gap post-G1 and is fixed the same way, but **scoped to
`source_meta.json` only** (`_fresh_deliverables_written([f"{source_dir}/source_meta.json"])`) — its
`allowed_output_paths` also lists the producer sources (model/runner.f90) it does NOT rewrite, so a
whole-set freshness check would false-fail a verify that legitimately only re-authors `source_meta.json`.
Test: `test_generate_verify_requires_fresh_source_meta_scoped` (asserts a STALE source does not
false-fail when source_meta is fresh).

**Where implemented.**
- `tools/workflow_conductor.py`: `SUBSTEPS["compile"]`; `COMPILE_STATIC_FAILURE_ROUTING` +
  `classify_compile_static_failure`; `_compile_static_inproc`; `build_launch_request` compile branch
  (scope must-read to non-deterministic substeps + a `static` arm = `[<ir_ref>/compile_static_meta.json]`,
  widen the `deterministic` predicate); `determine_substep_status` / `_is_deterministic_substep` /
  `_run_deterministic_substep` / `classify_failure` compile.static branches.
- **Same-phase reopen generalized.** The conduct() warm-reopen guard was hardcoded to
  `target=="generate"` + a route-reason prefix match. It was generalized to `target == phase` with
  `reopen_phase(from_phase=phase)` and a phase-aware `_read_repair_findings`. (The signal went
  through a brief `RouteDecision.same_phase_reopen` bool, then was finalized as the structural
  condition `same-phase target + repair_strategy ∈ {reuse, restart}` — see the verify-minor section
  near the end of this doc; the bool was removed.)
- `tools/orchestration_runtime.py` enforcement sites (the G1 checklist, compile analog): CP-1
  `_allowed_output_paths_for_launch` authorizes `compile_static_meta.json` for `substep=="static"`
  ONLY (the compile contract is an exact-set match, so this is load-bearing — without it record-launch
  fail-closes the real flow while mock tests stay green); CP-7 `_validate_launch_request_payload`
  deterministic-flag allowlist adds `compile.static`; CP-6 `ALLOWED_VALIDATE_PIPELINE_STAGES`
  `("compile","verify")→frozenset()` + add `("compile","static"): frozenset()`; CP-5 `_build_gate_runbook`
  drops the compile.verify branch (now empty → `""`); CP-8 `reopen_phase` same-phase carve-out adds the
  compile.static trigger; CP-2 `_allowed_file_tool_paths_for_launch` excludes `compile_static_meta.json`
  (defense-in-depth). No write-exemption (CP-4) needed: `--stage compile` is read-only and the meta lands
  inside the substep's own `ir_ref` write_root (authorized by containment), unlike generate's pipeline-root
  `lint_evidence`.

**Non-regression notes.**
- `compile.static` is deterministic (no leaf) → minimal deterministic launch prompt (phase-agnostic
  renderer keyed on the `deterministic` flag), empty `skill_must_read_refs`, no `_build_gate_runbook`.
- `_ensure_fresh_producer_id` already rotates `ir_id` for a `compile` re-run, so a same-phase reopen
  writes a fresh IR dir (the failed `compile_static_meta.json` is read for findings BEFORE rotation).
- Doc-size: `phase_01_compile.md` ceiling bumped 17000→18200 (it is force-read by compile.generate/verify;
  the Compile.static documentation + the "verify is now pure semantic" note add ~1.3KB).
- Default-on (no env flag), mirroring G1.static. Real verification is a billed E2E run.

## Verify-minor → warm same-phase repair; warm-resume/slim always-on (2026-06-30)

Two operator-directed changes to the repair loop, on top of G2:

**Warm-resume + slim are now always-on (env flags removed).** The opt-in env flags
`METDSL_CONDUCTOR_REUSE_RESUME` / `METDSL_CONDUCTOR_REUSE_SLIM_PROMPT` and their
`_reuse_resume_enabled` / `_reuse_slim_prompt_enabled` gates were deleted. `_resolve_reuse_resume`
warm-`--resume`s (forks) the producer session for ANY `repair_strategy=reuse` repair (claude,
session resumable); `restart` stays cold (anchoring avoidance) — so warm/cold is driven purely by
`repair_strategy`. `run_substep` sets `warm_resume = resume_session_id is not None`, and the slim
findings-only turn is rendered whenever a warm resume fires AND a findings excerpt is present.

**A verify finding is no longer tolerated — `minor` warm-repairs the producer.** Previously a
`minor` verify finding was a non-blocking note (the leaf could pass with it; "minor exception
allowed" in prod), deferring real correctness to Validate (hybrid-verification). Per operator
decision that is reversed: a verify finding ALWAYS sets `verification_status=fail`, and
`classify_verify_severity` routes by severity —
- `minor` → `RouteDecision(retry, repair_strategy=reuse)`: a SAME-PHASE warm reopen of the phase's
  producer (`compile.generate` / `generate.generate`) with the finding injected (slim).
  `_read_repair_findings` gained a `verify_*` branch reading the phase's verify meta
  `last_fail_reason` (compile→`ir_meta.json`, generate→`source_meta.json`).
- `major`/`critical` → `dev`: `fail_closed` (fast operator feedback); `prod`: `escalate` → the
  diagnostician decides how far back to go — including the SAME phase's own producer (see below).

**Same-phase producer reopen is now a first-class routing outcome (not deterministic-gate-only).**
The conductor's same-phase branch was generalized: a decision that targets the current phase
(`compile`/`generate` only — the phases with a re-runnable LLM producer) with `action ∈ {retry,
reopen}` and `repair_strategy ∈ {reuse, restart}` re-runs the phase's producer (rotating its id;
`reuse`→warm, `restart`→cold), instead of terminalizing. This covers the deterministic gates and
verify-minor (`reuse`→warm) AND the escalate **diagnostician** when it judges the right recovery
level is "this phase's own producer" (e.g. a major IR defect regenerated from scratch). The previous
"a same-phase decision terminalizes" rule was a vestige of the era before the same-phase reopen
mechanism existed (it predated lint/static); the escalate path was never updated, so a diagnostician
that wanted to re-run `compile.generate` terminalized.

Two subtleties made this actually reachable + crash-safe (found in review):
- The diagnostician's parsed directive carries NO `repair_strategy` (`_parse_directive` omits it for
  `reopen` and the schema doesn't require it for `retry`), so a same-phase diagnostician decision
  would not satisfy the branch's `repair_strategy ∈ {reuse, restart}` guard. Fix: after `escalate`,
  conduct **normalizes** a same-phase (`compile`/`generate`) `retry`/`reopen` decision with no
  concrete strategy to `restart` (cold — an escalated failure is significant; an explicit
  reuse/restart/re_execute is preserved, and `_parse_directive` now keeps the strategy for `reopen`).
- The branch is **scoped to `compile`/`generate`**. Without that scope, a diagnostician same-phase
  `validate`/`build` decision (e.g. `retry/validate/restart`) would fire the branch and call
  `reopen_phase` with a phase its carve-out doesn't support → `RuntimeError` (uncaught) → conductor
  crash. Scoped out, such a decision terminalizes (build/validate have no producer-reopen).

The redundant `RouteDecision.same_phase_reopen` bool (briefly added) was **removed** — the structural
condition (same-phase compile/generate target + concrete `repair_strategy`) is the signal; a
same-phase decision with NO `repair_strategy` that did NOT come from `escalate` (a malformed/unflagged
retry) still terminalizes.

**Anti-abuse guard narrowly relaxed.** `reopen_phase`'s same-phase carve-out previously forbade a
`verify` trigger (only `lint`/`static`/`compile_static`). It now allows a `compile.verify` /
`generate.verify` trigger too — but ONLY a terminal NON-PASS one (the existing pass-check still
rejects a passing trigger), so a passing pipeline still can never be erased; only a verify that
failed can reopen its producer. Docs reconciled: `WORKFLOW_CORE.md` §100, `AGENT_CONTRACT.md`,
`ORCHESTRATION.md`, both verify SKILLs (minor no longer tolerated). Tests:
`test_dev_verify_severity_gate`, `test_verify_minor_finding_warm_reopens_same_phase`,
`_read_repair_findings` verify branch, `reopen_phase` accept-verify-fail / reject-verify-pass,
the dev same-phase-no-repair_strategy terminalize boundary tests.

## G3 — hoist `--stage pre_judge` out of the LLM `Validate.judge` leaf into the conductor (IMPLEMENTED 2026-07-01)

**Problem (the G1/G2 pattern, one more phase down).** The purely-structural `--stage pre_judge` gate
(orchestration-record integrity + the cross-pipeline dependency DAG) ran *inside* the LLM
`Validate.judge` leaf as its final step. This is the same "LLM leaf owns a deterministic gate"
shape G1/G2 removed one and two phases up (`ALLOWED_VALIDATE_PIPELINE_STAGES[(validate, judge)]`
was `{"pre_judge"}`). Two costs: (a) a full, cold, separate-persona judge ran before a purely
structural blocker was even checked; (b) in a `--with-deps` run a dependent whose dependency
closure was not yet built+validated still spawned a cold judge that could only fail.

**Decision — NOT a new deterministic substep; a conductor-owned two-sided gate.** Unlike
compile/generate, the judge's independent LLM semantic check is essential, so `judge` stays an LLM
leaf. Only the *gate* is hoisted:
- **Pre-spawn (`Conductor._judge_pre_spawn_dag_block`).** Before spawning the cold judge — and
  before running `Validate.execute` (the check is placed at the top of `run_phase` for `validate`)
  — verify every `spec.ir.yaml.dependency.all_nodes` closure node is built+validated in its own
  pipeline. It derives the closure from the SAME source and normalization the post-gate uses
  (`dependency.all_nodes` → normalized `<kind>/<spec_id>` tokens via
  `validate_pipeline_semantics._dependency_expected_node_keys`, self excluded) and consults the SAME
  cross-pipeline predicate (`_closure_node_validated_in_own_pipeline`) the `pre_judge` DAG check uses
  for a closure node absent from the current validation scope. A missing node fails the phase
  `fail_closed` immediately, with **no record-launch** — so the judge `pre_phase_complete` hook
  (which would demand a `semantic_review` the leaf never wrote) is never reached. Empty closure
  (single-node run) → skip, zero overhead. Because it shares both the node-set source and the
  predicate, it is a rigorous STRICT SUBSET of the post-gate: anything it blocks would also fail
  `pre_judge`, so it never fails a run the post-gate would pass; it only saves the wasted
  execute+judge cost. (Reading `all_nodes` directly — not `_dependency_closure_nodes` — also skips
  that helper's L6 diamond guard, a Build/Model-B staging concern irrelevant to DAG readiness that
  would otherwise mis-raise for a c/cpp/mixed node at validate.)
- **Post-return (`Conductor._run_judge_pre_judge_gate`).** After the judge leaf returns its verdict
  (in `run_substep`, guarded on a clean rc 0 **AND an `aggregate_verdict` of `pass`/`xfail`**), run
  `validate_pipeline_semantics --stage pre_judge` scoped to this run (`--orchestration-id` /
  `--in-flight-agent-run-id <judge_arid>` / `--pipeline-root` / `--run-id`, the same scoping the leaf
  used) — the `_build_inproc` post_build idiom — and author `judge_gate_meta.json` under the run-node
  dir recording the verdict. **The pass/xfail scope is load-bearing (Codex P1):** `pre_judge`'s
  `_validate_llm_semantic_review` treats `semantic_review.decision != "pass"` as a violation, so
  running it on a legitimate physics/evidence `fail` verdict would fail the gate and (via the
  terminalize below) convert a *routeable* Validate.judge failure into a terminal `fail_closed`,
  robbing `classify_failure` of the retry/attribution routing. A non-pass verdict therefore skips the
  gate (no `judge_gate_meta` written) and flows to the decision tables exactly as in the leaf era,
  where the completion `pre_judge` ran only on a judge terminating `pass`. The structural gate matters
  only when a node is about to be CERTIFIED `pass`.

**Reconciliation — a `pre_judge` violation is a `fail_closed` integrity blocker, NOT a routeable
`fail` (deviation from the initial plan, driven by the judge integrity hook).** The plan first
proposed routing a `pre_judge_violation` to `("generate","reuse")` via a normal `fail`
step_result. That fights `_pre_phase_complete_judge_checks`: for a launch whose substep is `judge`
it REQUIRES a `semantic_review.json` and **forbids a `fail` step_result atop a `pass`
`semantic_review`** (the common case — the judge passed on physics but tripped a structural
integrity gate). A structural `pre_judge` violation is exactly the documented non-physics `blocked`
posture ("could not be CERTIFIED due to a non-physics blocker, unrecoverable in the current run"),
so the coherent terminal is `fail_closed`. Wiring:
- `determine_substep_status` (validate.judge branch) ANDs `judge_gate_meta.status == "pass"` into
  the pass condition (verdict pass/xfail AND gate pass), so a gate `fail` on an otherwise-passing
  judge fails the substep; a missing meta reads as fail too (harmless for the two cases it arises: a
  crashed judge, or a non-pass verdict that intentionally skipped the gate — both already fail on the
  verdict check).
- `run_phase` (after the transport branch, before `write_step_result`) detects
  `judge_gate_meta.status == "fail"` and terminalizes `fail_closed` WITHOUT writing a step_result
  (the transport branch's skip-write + tombstone shape) — so `classify_failure` is bypassed and the
  judge hook is never reached. The pre-spawn block is terminalized `fail_closed` even earlier (no
  substep runs at all).

**Where implemented.**
- `tools/workflow_conductor.py`: `_judge_pre_spawn_dag_block` / `_write_judge_gate_meta` /
  `_run_judge_pre_judge_gate`; the `run_phase` pre-spawn early-return + post-gate `fail_closed`
  terminalize; the `run_substep` post-gate call; the `determine_substep_status` judge branch.
- `tools/orchestration_runtime.py`: CP-6 `ALLOWED_VALIDATE_PIPELINE_STAGES[(validate, judge)]` →
  `frozenset()`; CP-5 `_build_gate_runbook` drops the judge branch (now `""` — mirrors
  compile.verify / generate.verify); the `_build_dependency_facts` orientation prose updated (the
  conductor runs pre_judge). **Recording layer kept unchanged** (intentional): `write-step-result`'s
  `validation_stage` allow-set still lists `pre_judge`, `PHASE_VALIDATION_STAGE["validate"]` stays
  `pre_judge`, and the `validate_pre_judge_step_result_executor_integrity` / repair machinery is
  untouched — the conductor now RUNS the gate, but the step_result still records the stage. Timing
  is preserved: the leaf used to run pre_judge as its LAST step (before the conductor writes the
  step_result), and the conductor now runs it at that same point (judge child recorded + returned,
  not yet finalized → `--in-flight-agent-run-id` scoping unchanged).

**Non-regression notes.**
- Default-on (no env flag), mirroring G1/G2. Real verification is a billed E2E run: a single-node
  spec (empty closure → pre-spawn skip, post-gate pass → `aggregate_verdict=pass`) and the
  `demo_dep_top` `--with-deps` chain (multi-node closure → pre-spawn readiness holds → pass).
- Tests: `G3JudgePreJudgeGateTest` (determine_substep_status verdict∧gate; post-gate pass/fail meta
  + scoped args; pre-spawn single-node skip / multi-node ready / multi-node incomplete block),
  `TransportFailureTest.test_pre_spawn_dag_incomplete_fails_closed_without_spawning` /
  `test_post_gate_pre_judge_violation_fails_closed_and_tombstones`, and the runtime
  `test_runbook_judge_emits_no_gate` + ALLOWED-table coherence.
