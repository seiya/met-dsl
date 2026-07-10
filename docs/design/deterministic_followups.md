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

- **C2 (design note; the `restart` strategy is superseded by B1 for recognized structural
  categories, the `Compile` backstop below is not):** the deterministic `execute→Generate`
  routing (Codex finding 1) regenerates the **runner**, which fixes *code* defects but **not IR**
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

**D5.1 — surface each dummy's rank/type/intent, not only argument order (2026-07-06).**
The original D5 pinned the argument *order* but `_extract_subroutine_interface` parsed only the
subroutine *header line* (bare dummy names), so the consumer learned the order yet still guessed
each dummy's **rank/shape**. Symptom (orch `…065033Z_4be45da7`, `shallow_water2d`): the consumer
passed its full rank-3 state `u_ext(ncomp,:,:)` to the certified boundary op declared
`real(dp), intent(inout) :: U(:,:)` (rank-2, a single field) → `Error: Rank mismatch in argument
'u' (rank-2 and rank-3)` → Build fail → dev `dev_phase_rollback` fail_closed. **Fix (same
variance-reduction posture — no new gate):** `_extract_subroutine_interface` now also parses the
body declarations (new `_parse_fortran_dummy_declarations` + `_parse_one_declaration` /
`_rank_of_shape` helpers) and returns an additive `arguments:[{name,type,intent,rank,dimension}]`
aligned 1:1 with `argument_order`. `_published_operations_lines` renders a per-dummy line
(`U: real(dp), intent(inout), rank-2 (:,:)`) and the block header now instructs Generate to MATCH
each dummy's rank/shape — looping over components and passing lower-rank slices when the dummy is
lower-rank than the full state array. Omit-on-doubt: an unparseable/conflicting declaration renders
an explicit "declaration not resolved" marker, never a guessed rank; the datum is orientation-only,
never a gate; Build's compiler stays the deterministic backstop. Docs: `phase_02_generate.md`
§ author/§ verify, generate/verify SKILLs (ceilings bumped). Tests: extended
`ExtractSubroutineInterfaceTests` (assumed-shape/explicit/assumed-size/`dimension`-attr/multi-entity/
`character(len=*)`/`double precision`/`real*8`/coarray/not-found/conflict), `ResolveDependencyFactsTests`
(`test_published_operations_carry_rank_for_boundary_op`), `DependencyFactsRenderTests`,
`WriteLineageTest.test_persists_published_operations_for_fortran_consumer`. **Residual:** billed E2E
re-run confirming Build passes on attempt 1 (operator-gated).

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

## G4 — split `Validate.judge` into `pre_judge / execute / judge / post_judge` substeps + severity-classify the post_judge gate (IMPLEMENTED 2026-07-01)

**Motivation.** G3's initial billed E2E (orch `…76cd1743`) failed `fail_closed` because the judge
leaf wrote `semantic_review.json` with `review_method: "llm_semantic_recompute"` but the gate requires
the literal `"llm_semantic_review"` (the value lived only in gate code). Under G3 that trivial,
leaf-authored conformance error was **unrecoverable**: the pre_judge gate has no severity dimension
(any violation fails), and the judge `pre_phase_complete` hook forbids a `fail` step_result atop a
`pass` semantic_review, so a minor typo terminalizes exactly like a genuine record-integrity breach.

**Change.** Promote G3's two conductor-owned gates into explicit deterministic substeps so the
validate phase is `("pre_judge", "execute", "judge", "post_judge")`, then classify post_judge
violations by severity and warm-resume the judge for the recoverable class:
- `pre_judge` (`Conductor._pre_judge_inproc`, index 0): the pre-spawn dependency-DAG readiness check
  (was a run_phase pre-loop branch), authoring `pre_judge_meta.json`. A not-ready closure fails
  `fail_closed` (integrity blocker; never warm-resumed — no judge has run).
- `judge` (LLM leaf, index 2): unchanged semantic pass, minus the inline post-gate. `determine_substep_
  status` reduces its pass condition to `aggregate_verdict ∈ {pass,xfail}` (the gate AND moved out).
- `post_judge` (`Conductor._post_judge_inproc`, index 3): runs `validate_pipeline_semantics --stage
  pre_judge` and CLASSIFIES the free-text violations via `classify_post_judge_violations`, authoring
  `post_judge_meta.json` with a `disposition`. **NOTE the naming**: the substep is `post_judge` (runs
  after the judge) but the validator STAGE is still literally `pre_judge` (before pass-certification).
- **Severity classifier** (`classify_post_judge_violations`, precedence unrecoverable > unknown >
  recoverable): keys on the leading artifact-path token of each violation. The JUDGE's own
  deliverables `semantic_review.json` / `verdict.json` / `aggregate_verdict.json` / `summary.json` /
  `validate_meta.json` → **recoverable** (judge-authored → warm_resume). `agent_graph.json` /
  `step_result.json` / `lineage.json` / an `orchestrations/` path / the literal `copy_based_artifact_
  reuse detected` / `dependency DAG incomplete` → **unrecoverable** (fail_closed). Anything else —
  including execute-authored evidence (`diagnostics.json` / `perf.json` / `trial_meta.json` / `raw/`),
  which the judge re-run cannot rewrite — → **unknown** → conservatively **fail_closed** (an
  escalate-LLM adjudicator is a deferred follow-up).
- **Warm-resume mini-loop** (`Conductor._maybe_warm_resume_post_judge`): on a `warm_resume`
  disposition, tombstone the judge+post_judge attempt, warm-`--resume` the judge with a slim
  findings-only prompt (reusing the G1-slim `_resolve_reuse_resume` machinery — it re-authors
  `semantic_review.json`), then re-run the deterministic post_judge gate. Bounded by
  `MAX_ATTEMPTS_PER_PHASE`. Self-contained — it drives `run_substep("validate","judge", repair=…)`
  directly, so the "repair reaches only substep index 0" rule and the compile/generate-only
  same-phase reopen (`conduct` / `reopen_phase`, which crashes for a validate trigger) are never
  touched. This is a `post_judge → judge` in-phase substep retry, which dev-mode F1 already permits.

**In-flight scoping.** post_judge runs the gate AFTER the judge returns, so post_judge's OWN
agent_graph edge is the dangling in-flight one (not the judge's, which is already recorded). The gate's
in-flight filter (`_validate_orchestration_hierarchy`) now accepts substep ∈ {judge, post_judge}, and
`_post_judge_inproc` declares both the judge arid (`_pending_judge_arid`) and its own `child_arid` via
repeated `--in-flight-agent-run-id`.

**fail_closed posture preserved.** A terminal pre_judge / post_judge failure terminalizes `fail_closed`
WITHOUT a routeable step_result (skip-write + tombstone), reading `pre_judge_meta` / `post_judge_meta`
in run_phase. A physics/evidence judge fail breaks the loop BEFORE post_judge runs (structural skip of
the gate that G3 achieved with a verdict-conditional), and routes via `classify_failure`
(index-based over `SUBSTEPS["validate"]`).

**Recording layer unchanged** (as in G3): `PHASE_VALIDATION_STAGE["validate"]` and
`STEP_REQUIRED_VALIDATION_STAGES["validate"]` stay `pre_judge`; `post_judge` is not a stage token. The
pass step_result's `launch_request_ref` points at the JUDGE substep (not the trailing post_judge) so
the judge completion hook still enforces `semantic_review`.

**Deferred.** The escalate-LLM adjudicator for `unknown` violations — **implemented in G5 below**
(a post_judge `unknown` now routes to the unified escalate LLM in prod). Still deferred: hoisting the
deterministic parts inside the judge leaf (structural start-condition checks / raw recompute) out to
the conductor.

**Files.** `tools/workflow_conductor.py` (SUBSTEPS, `_is_deterministic_substep`, `_pre_judge_inproc` /
`_post_judge_inproc` / `_maybe_warm_resume_post_judge`, `classify_post_judge_violations`,
`determine_substep_status`, `run_phase`, `classify_failure`); `tools/orchestration_runtime.py`
(deterministic allow-list, `ALLOWED_VALIDATE_PIPELINE_STAGES`, leaf-non-writable meta exclusions);
`tools/validate_pipeline_semantics.py` (in-flight filter accepts post_judge); doc fix in
`skills/workflow-validate-judge/SKILL.md` + `docs/workflow/phases/phase_04_validate.md` (pin the
`review_method` literal). Tests: `PostJudgeClassifierTest`, `G3JudgeGateSubstepTest`, updated
`ConductHappyPathTest` / `TransportFailureTest` (incl. the new warm-resume recovery test).

## G5 — unify the escalate LLM: severity-aware reuse/discard + wire the post_judge `unknown` (IMPLEMENTED 2026-07-01)

**Motivation.** The orchestrator already has ONE escalate LLM — the **diagnostician** (`Conductor.escalate()`),
a one-shot read-only separate-persona leaf every escalate site funnels through (`conduct` dispatch). It
already jointly decides **how far to roll back** (`target_phase`) and **reuse-vs-discard**
(`repair_strategy`: `reuse`=warm keep vs `restart`=cold supersede+fresh id). Three gaps: it output no
**severity level**; the G4 post_judge `unknown` disposition was a blind `fail_closed` (never reached the
diagnostician); and its persona was a fully-inline prompt, not a SKILL.

**Change.** Make the diagnostician the single severity-aware adjudicator used at every escalate site,
including the post_judge `unknown`:
- **Directive gains `severity ∈ {minor, major, critical}`** (`_DIRECTIVE_SCHEMA` / `_parse_directive`;
  absent → default `major` for back-compat). `RouteDecision` gains an optional `severity`.
- **Severity governs reuse-vs-discard** via `resolve_severity_directive` (called in `escalate()` after
  parse): `minor` → `reuse` (forced); `major` → `reuse` by default, honors an explicit LLM `restart`
  (escalate-to-discard); `critical` → `restart`/discard (forced). `re_execute` passes through
  (orthogonal). **`target_phase` is NOT clamped** by severity — the LLM's rollback distance is honored
  as-is (`conduct`'s `dev_phase_rollback` gate still catches a dev cross-phase reopen). "Discard" is the
  existing `restart` (id-rotation + `reopen_phase` supersede; nothing is deleted) — no new primitive.
- **post_judge `unknown` → escalate.** `_post_judge_inproc`'s disposition dict maps `unknown` →
  `"escalate"` (was `fail_closed`); `recoverable` → `warm_resume` and `unrecoverable` → `fail_closed`
  unchanged. `run_phase`'s G4 branch forks on the `escalate` disposition: **prod** returns
  `RouteDecision("escalate", reason="validate_post_judge_unknown")` (tombstoning the attempt's orphan
  arids, skip-write posture) → `conduct` reuses its existing escalate dispatch → the resolved directive
  reopens generate/compile (reuse/discard) or `fail_closed`s; **dev** keeps the fail-fast `fail_closed`
  (no billed escalate leaf). The judge warm-resume stays exclusively the deterministic `recoverable`
  path (a `validate` same-phase target terminalizes — the LLM cannot certify a validate pass).
- **`_gather_failure_context`** now embeds `post_judge_meta.json` + `pre_judge_meta.json` (so the
  read-only leaf reasons over the violation list / disposition without reading the FS).
- **SKILL.** New `skills/workflow-escalate/SKILL.md` is the canonical persona + directive + severity
  policy. It is **conductor-consumed, host-rendered** (Option A): `_diagnosis_prompt` reads the SKILL
  body host-side (memoized, frontmatter stripped) and embeds it, keeping the read-only leaf pure (reads
  nothing). Falls back to a minimal inline persona if the file is missing. `_DIRECTIVE_SCHEMA` stays the
  machine-checkable final-line contract; SKILL prose and `_DIRECTIVE_SCHEMA` are kept in lockstep.

**Decisions (operator sign-off).** (1) reuse/discard is derived from severity with a bounded LLM
override; (2) no rollback clamp; (3) dev keeps `fail_closed` for post_judge `unknown`, only prod
escalates; (4) the `classify_verify_severity` pre-escalate gate is kept (dev major/critical still
fail-fast before any escalate) — the LLM's severity is authoritative only for the sites with no
upstream severity.

**No new enforcement surface.** `VALID_ISSUE_SEVERITIES` already = `{none,minor,major,critical}`; the
policy emits only `reuse`/`restart` into repair payloads (`VALID_REPAIR_STRATEGIES`; `re_execute` is
conductor-internal, never in a launch payload); the `validate_post_judge_unknown` reason funnels into
the allowlisted `conductor_phase_fail_closed`; `MAX_ATTEMPTS_PER_PHASE` stays the sole loop bound (the
one-shot escalate leaf can never re-emit `escalate`).

**Deferred.** Migrating `classify_verify_severity` into the LLM (decision #4); a rollback clamp
(decision #2); fully merging the post_judge free-text taxonomy with minor/major/critical.

**Files.** `tools/workflow_conductor.py` (directive schema/parser, `resolve_severity_directive` +
`_SEVERITY_FORCED_STRATEGY`, post_judge disposition + run_phase G4 fork, `_gather_failure_context`,
`_diagnosis_prompt` + `_load_escalate_persona`); new `skills/workflow-escalate/SKILL.md`; docs
(`AGENT_SKILLS.md`, `LAUNCH_PROMPT_REFERENCE.md`). Tests: extended `DiagnosticianTest`, new
`resolve_severity_directive` + post_judge-escalate wiring tests.

## G6 — hoist the deterministically-derivable Validate artifacts out of the judge LLM leaf (IMPLEMENTED 2026-07-01)

**Motivation.** After G4 split `Validate` into `(pre_judge, execute, judge, post_judge)`, the LLM `judge`
still authored **5** artifacts: `verdict.json`, `semantic_review.json`, `aggregate_verdict.json`,
`summary.json`, `validate_meta.json`. Three of those are **100% deterministically derivable** from the
LLM's `verdict.json#per_test` plus the dependency set's `aggregate_verdict`s. Two problems: (a) the LLM
wastes output and injects nondeterminism authoring them; (b) **a silent correctness hole** — the
composition of `aggregate_verdict.json` (the transitive fold + the `blocked` DAG logic) was **never
structurally validated** by any gate; only its top-level `aggregate_verdict` field is read downstream
(dependency readiness), so an LLM mistake in the fold/`blocked` logic silently corrupted downstream
state. (This is "proposal 2", deferred from G4.)

**Change.** The conductor now authors `aggregate_verdict.json` / `summary.json` / `validate_meta.json`
correct-by-construction; the judge authors only `verdict.json` + `semantic_review.json`.
- **Option B (no 5th substep).** A new `_author_derived_validate_artifacts(refs)` is called at the TOP of
  the existing `_post_judge_inproc`, BEFORE the `--stage pre_judge` gate subprocess — so the gate then
  re-validates the conductor's own `summary.counts` vs `verdict.per_test`
  (`_validate_tests_verdict_summary_consistency`): correct-by-construction AND gate-verified. Folding into
  `post_judge` avoids the index-fragile `outcomes[-2]==judge` warm-resume + judge-idx `launch_request_ref`
  invariants a 5th substep would perturb. Idempotent on a warm-resume re-run (re-derived from the
  re-authored `verdict.json`).
- **Derivation.** `self_verdict` = reduce `per_test` (`fail` if any entry is `fail` or `blocked`; else
  `xfail` if every non-skipped entry is `xfail`; else `pass`; `blocked` is a legal per-test outcome that
  must be counted and non-certifying). `blocked` rule: a direct dep that is **not built+validated in its
  own pipeline** (`validate_pipeline_semantics._closure_node_validated_in_own_pipeline` — the SAME
  readiness predicate `pre_judge` and the `--stage pre_judge` gate use) → `aggregate_verdict="blocked"` +
  `blocking_direct_deps`; else the transitive fold (precedence `blocked > fail > xfail > pass`) over
  `{self_verdict}` + each ready dep's readiness-consistent contribution (`xfail`/`pass`, never
  `fail`/`blocked`). Using the readiness predicate — not the dep's *latest* verdict from
  `orchestration_runtime._resolve_dependency_facts` (which supplies display verdict + pipeline/run refs
  only, NEVER the blocking signal) — guarantees the derived `aggregate_verdict` can never contradict a
  readiness gate that already passed. `summary.json` carries `self_summary` + `dependency_summary` + the
  gate-checked `counts` (including `blocked`).
- **Judge pass criterion (`determine_substep_status`).** `aggregate_verdict.json` no longer exists at
  judge-completion, so the judge branch now passes iff `verdict.json#per_test` is a non-empty list with no
  `fail` entry AND `semantic_review.json#decision == "pass"`. A per_test `fail` or a `decision=="fail"`
  breaks `run_phase` before `post_judge`; `classify_failure` still routes on `verdict.failure_class` +
  `semantic_review.findings[0].attribution` (routing preserved; both stay LLM-authored). An all-`xfail`
  node still passes. Because the criterion now fails the judge on `decision=="fail"` even when the
  mechanical `per_test` is clean (a fabrication finding on passing tests), `classify_failure`'s judge
  branch special-cases a `decision=="fail"` with a `pass`/missing `failure_class`: it escalates
  (`judge_semantic_review_fail`) to the diagnostician instead of letting `classify_validate_judge` treat
  `failure_class=="pass"` as `advance` and silently drop the finding (Codex P2).
- **Contract / non-writable wiring.** The judge's `allowed_output_paths` + `_matches_phase_contract`
  `allowed_files` shrink to `{verdict.json, semantic_review.json}`; `post_judge` accepts
  `{post_judge_meta.json, aggregate_verdict.json, summary.json, validate_meta.json}` (pre_judge stays
  exact-match on its own meta). The three derived artifacts are added to the leaf-non-writable exclusions
  (scoped to `/runs/`) in both `_allowed_file_tool_paths_for_launch` branches.

**Decisions.** (1) **Option B** — fold into `_post_judge_inproc`, no 5th substep. (2) **Conservative SKILL
slim** — remove only the now-conductor-owned aggregate/summary *authoring* instructions; **keep** the
SKILL's structural coverage checks (metrics_basis / diagnostics_contract) as belt-and-suspenders with the
gates.

**Deferred.** The spec-general per-contract **recompute engine stays in the LLM** (io_contract acceptance
is arbitrary per-spec named diagnostics with no fixed metric; recompute from `raw/` + reconcile with
`diagnostics.json`, and `verdict.per_test[].status` / `failure_class` / `semantic_review`
fabrication+attribution, all remain LLM). Only the deterministically-derivable *artifacts* move. A future
pass could remove the kept SKILL structural checks once gate confidence is established.

**Files.** `tools/workflow_conductor.py` (`_author_derived_validate_artifacts` + its call, the judge pass
criterion, `_judge_attempt_count`, judge/post_judge `allowed_output_paths`); `tools/orchestration_runtime.py`
(`_matches_phase_contract` judge + pre/post_judge, leaf-non-writable exclusions in both branches); docs
(`phase_04_validate.md`, `LAUNCH_PROMPT_REFERENCE.md`, `skills/workflow-validate-judge/SKILL.md`). Tests:
`_author_derived_validate_artifacts` (single-node / deps-present / `blocked`), the G6 judge criterion, the
judge+post_judge contract split, and the conductor-derived `summary.counts` gate consistency (+ a mutated
negative).


## G7 — host-author the derived dependency graph (`dependency_graph.json` sidecar) (IMPLEMENTED 2026-07-01)

**Motivation (the host-author sibling of G2/G3, applied to the dependency graph).** The IR's
`dependency` section mixed two very different fields: the low-mutation **directly-read** edge
(`node_key` + `direct_deps[]`, straight from `deps.yaml`) and the **derived closure/topo graph**
(`all_nodes` with `topo_level`, `transitive_deps` with `via`). The derived graph is a pure
function of `deps.yaml` + `spec/registry/spec_catalog.yaml` — the SAME closure
`run_workflow.py --with-deps` resolves — yet `compile.generate` (the LLM) authored it by
"guessing" the transitive closure and topological order, carrying mutation risk (wrong
`topo_level`, dropped `transitive` edge, closure ≠ `deps.yaml`). Worse, the V4 consistency
invariants (closure == all_nodes / expected_node_set == all_nodes / topo) were checked by the
LLM `Compile.verify` ONLY — `--stage compile` never looked at `dependency`.

**Change (partial hoist — graph structure only).** The conductor authors the derived graph
host-side, mirroring the `lineage.json` / `Makefile` / D5 published-interface precedents.

- **Sidecar, not IR rewrite (operator decision).** The conductor writes a separate file
  `<ir_ref>/dependency_graph.json` (`{node_key, all_nodes:[{node_key, topo_level}],
  transitive_deps:[{node_key, via:[...]}], generated_by:"conductor"}`, no `operations`) at
  Compile phase start (`workflow_conductor._write_dependency_graph`, before any substep's
  record-launch baseline). The IR keeps only `node_key` + `direct_deps[]` (with the semantic
  `operations`, which has no host data source and stays LLM-authored). The host directly-required
  set is recoverable as `{all_nodes} − {self} − {transitive_deps}`.
- **Builder** `tools/dependency_graph.build_dependency_graph` reuses the runtime closure helpers
  (`_read_deps_yaml` / `_parse_dep_entries` / `_matching_dep_versions` / `resolve_spec_ref_for` /
  `_load_spec_catalog`); a post-order DFS records child edges → `topo_level` = height (leaf 0),
  `via` = the lexicographically-smallest intermediate path (byte-reproducible, re-authored every
  compile). Error taxonomy is identical to `_resolve_dependency_closure` (cycle / unresolvable /
  version_conflict / identity_conflict / deps_unreadable / deps_malformed / spec_ref_unresolved /
  spec_catalog_corrupt); fail-closed, no partial sidecar. The L6 diamond guard stays in
  `_dependency_closure_nodes` (a Model-B staging concern, not a graph-structure concern).
- **Compile fail_closed** on a builder error (deps.yaml/catalog structurally broken — not
  LLM-fixable content), same contract as the closure driver.
- **Write authorization (3-layer exclusion, the `compile_static_meta.json` pattern — a write_root
  path that is host-authored):** absent from `compile.generate`'s `allowed_output_paths`; rejected
  by `_matches_phase_contract`'s exact-set `compile_required`; and excluded (auto-derive `and not
  path.endswith("/dependency_graph.json")` + explicit-list `raise ValueError`) in
  `_allowed_file_tool_paths_for_launch`. No FS-diff exemption needed (host-write at phase start,
  inside `workspace/ir/...` write_roots, in no substep window).
- **Consumer switch** to the shared reader
  `validate_pipeline_semantics._read_dependency_graph_sidecar`:
  `_dependency_closure_nodes` (Build/Model-B), `_judge_pre_spawn_dag_block` (G3 pre-spawn),
  `_author_derived_validate_artifacts`'s `dependency_set` (G6), and the run-stage
  `_dependency_resolved_for_execution` + lineage-loop (which now merge the sidecar's `all_nodes`
  over the IR block). `_is_leaf_node` / `_resolve_dependency_facts` / `_write_lineage` are
  unchanged (they read `direct_deps`, which stays in the IR).
- **New deterministic gate** `_validate_compile_dependency_consistency` in `--stage compile`
  (`compile.static`): sidecar present + internally consistent (self ∈ all_nodes, transitive ⊆
  all_nodes, non-negative int topo_level), and the IR's `direct_deps` `(kind, spec_id)` set ==
  `{all_nodes} − {self} − {transitive}`. A mismatch routes to `compile.generate` (warm reopen).
  Version drift is soft (gfortran/link backstop). This closes the V4a/V4b/topo determinism gap —
  the LLM `Compile.verify` now checks only **V4c** (`operations ⊆ published`).

**Migration (clean-cut, no IR fallback).** An IR fallback would re-trust LLM `all_nodes` and break
correct-by-construction, so it was not kept. The one on-disk IR
(`component__demo_dep_base__0.1.0/.../spec.ir.yaml`, a leaf, E2E-untested) was trimmed
(`all_nodes` / `transitive_deps` dropped, `direct_deps: []` kept) and its leaf sidecar authored;
future compiles author the sidecar automatically.

**Files.** `tools/dependency_graph.py` (new builder + `tools/tests/test_dependency_graph.py`);
`tools/workflow_conductor.py` (`_write_dependency_graph` + the compile branch in `run_phase`,
consumer switches); `tools/orchestration_runtime.py` (3-layer file-tool exclusion + comments);
`tools/validate_pipeline_semantics.py` (`_read_dependency_graph_sidecar`,
`_validate_compile_dependency_consistency`, run-stage sidecar merge); docs
(`phase_01_compile.md`, `AGENT_CONTRACT.md`, compile-generate/verify SKILLs, and the reference
docs that name `all_nodes` / `topo_level`). Tests: builder taxonomy, the consistency gate
(match/mismatch/leaf/version-drift/missing-sidecar/self-absent), the conductor sidecar author +
fail-closed, the file-tool exclusion, and consumer-switch fixture splits (IR = node_key +
direct_deps / sidecar = all_nodes + transitive). **Real verification is a billed E2E run**
(single-node: sidecar `all_nodes:[self@0]` + direct_deps gate; `--with-deps demo_dep_top`:
multi-node closure/topo, build order from sidecar topo_level, pre_judge DAG reads sidecar
all_nodes, `aggregate_verdict=pass`).

**Known limitation (accepted, deferred to multi-version support).** The sidecar pins each
dependency node to the HIGHEST catalog version satisfying the consumer constraint
(`matched[0]`), matching the closure driver's `node_label` (`run_workflow.py`
`spec_versions[0]`). Three EXISTING version conventions are mutually inconsistent when a spec
has >1 catalog version: `resolve_node` builds the dependency under the FIRST catalog entry
(ignoring the constraint), `_dependency_node_ready` accepts ANY matching version, and
`node_label`/sidecar/`_stage_dependency_sources` use the highest. For a spec with a single
catalog version (all 12 today) these coincide and the pin is exact. If they ever diverge,
`_stage_dependency_sources` FAILS CLOSED ("no ready pipeline") rather than substitute a sibling
version (which could link stale/constraint-incompatible code) — the same fail-closed stance as
the L6 diamond guard. This host-author change did NOT introduce the asymmetry (pre-change,
LLM-authored `all_nodes` fed staging identically); reconciling it is part of the deferred
multi-version effort (unify `resolve_node`/readiness/staging into one constraint-aware version
selection), not this change. (Codex review P1, accepted as documented 2026-07-02.)

## G8 — deterministic per-test verdict: host-author `verdict.json` at `execute` from IR predicates (R2) (IMPLEMENTED 2026-07-06)

Canonical design: `docs/design/workflow_scaling_redesign.md` §R2 (first tranche;
implementation plan in the sleepy-snacking-kitten plan). This is the last leg of the
G6/G7 trajectory: the per-test pass/fail was the remaining LLM-authored, nondeterministic
Validate artifact (`xfail_verdict_contract_gap` / per_test schema-fabrication classes).

**Change.** Each `tests.md` test's `pass_when` / `judgment` prose is formalized at Compile as a
machine-evaluable predicate in `io_contract.test_predicates`
(`{test_id, expected_outcome, target_cases, pass_when.all:[{ref, op, value, per_case?, na_allowed?}]}`;
op ∈ eq/ne/le/ge/lt/gt/includes). `Validate.execute` evaluates the predicates against the
runner's `diagnostics.json` (pure module `tools/verdict_evaluator.py`, imported by both the
conductor and the validator — same shape as `dependency_graph.py`) and authors `verdict.json`
(`per_test[].status` + machine `basis` + `failure_class`) in-process, right after the
`post_execute` structural gate + quality-check pass. A per-test predicate `fail`
(`self_verdict=fail`) fails the execute substep so the judge leaf is **not spawned** (the R2
cost lever); `classify_failure` routes it on `verdict.json#failure_class` — `physics_fail` /
`structural_violation` → escalate diagnostician (prod, for attribution) / `fail_closed` (dev,
F1). The judge leaf now authors **only** `semantic_review.json` (its `determine_substep_status`
is `decision == pass`); it reconciles `diagnostics.json` vs `raw/` and catches fabrication /
semantic-intent mismatch the mechanical predicate cannot. `verdict.json` moved out of the judge
`allowed_output_paths` into execute's, and from `_POST_JUDGE_RECOVERABLE_BASENAMES` to
unrecoverable (host-authored → a gate violation naming it is a conductor derivation defect, not
judge-fixable).

**Evaluation point moved from the design's `post_judge` to `execute`** (deliberate, to align with
the measured cost baseline — execute already reads `diagnostics.json`, and evaluating there lets a
predicate fail short-circuit the judge spawn entirely).

**Compile-stage gate.** `validate_pipeline_semantics --stage compile` gains
`_validate_test_predicates` (→ `verdict_evaluator.validate_predicate_schema`): presence +
schema (op/outcome enums, non-empty `pass_when.all`), `target_cases ⊆ case.test_case_set`, `ref`
resolution against the declared diagnostics vocabulary (`verdict.<field>` / `checks.<id>` / a
per-case metric address pinned in the new optional `diagnostics_contract.metrics`), and
`test_id` set == `tests.md` (backstopped by the pre_judge `_validate_tests_verdict_summary_consistency`).
`Compile.verify` V3 owns the SEMANTIC check that each predicate faithfully translates the prose
(right op/threshold direction) — the judge-time variance becomes a reviewable compile-time artifact.

**Files.** `tools/verdict_evaluator.py` (new + `tools/tests/test_verdict_evaluator.py`, incl. the
12-spec DSL-expressibility proof: component boolean, profile per-case map, problem nx-dependent
thresholds + convergence order + N/A rules, inverted-guard xfail); `tools/workflow_conductor.py`
(`_author_execute_verdict` + execute gate restructure, judge `determine_substep_status`
simplification, recoverable-basename move, `classify_failure` execute physics branch, judge
allowed_output_paths); `tools/validate_pipeline_semantics.py` (`_validate_test_predicates`); docs
(`phase_01_compile.md` schema + V3, `phase_04_validate.md`, compile-generate + validate-judge
SKILLs, `LAUNCH_PROMPT_REFERENCE.md`, `AGENT_CONTRACT.md`); the two conductor launch-request golden
fixtures. **Real verification is a billed E2E** (single component node: judge runs on
`semantic_review` alone, verdict host-authored, `aggregate_verdict=pass`; timing-audit for the
validate-cost delta). Unit suite green (1942).

## R5 / M2 — certified exemplar injection into generate.generate (IMPLEMENTED 2026-07-06)

Canonical design: `docs/design/workflow_scaling_redesign.md` §R5 (first tranche, M2). The
per-node re-derivation of cross-node-identical runner plumbing is the dominant thinking cost
(measured baseline); R5 trades cheap, cacheable input tokens for that expensive thinking by
showing the authoring leaf a known-good sibling implementation.

**Change.** A new host-side selector `orchestration_runtime._resolve_exemplar_source(repo_root,
ir_ref)` resolves, for the target node, a previously-certified SIBLING node's source in the same
`(family, spec_kind, language)` — family/spec_kind from `spec_catalog.yaml`
(`_catalog_family_index`), language from the IR — EXCLUDING the target itself. Discovery mirrors
the dependency certified-source selection (`_verify_dep_stage` / `_certified_model_source`):
latest pipeline → latest binary → the bound `aggregate_verdict ∈ {pass, xfail}` (a genuinely
certified node, not merely built); across siblings the globally most-recent certified pipeline
(by canonical `(date, seq)`) wins. Pre-R1 the injected sources are the certified
`<spec_id>_model.f90` + `<spec_id>_runner.f90`; post-R1 (harness node) this becomes model +
checks. The corpus is self-bootstrapping (every certified node exemplifies its siblings).
Best-effort — the selector NEVER raises, and any miss simply omits the exemplar.

The conductor resolves it in `run_substep` ONLY for `generate.generate` (the sole authoring
leaf, not on a warm-resume slim repair) and threads it via `build_launch_request(exemplar=...)`
onto `request_payload["exemplar"]`. `_build_exemplar` renders the `<exemplar>` template
placeholder (new, in `step_agent.txt` / `substep_agent.txt`, next to `<dependency_facts>`;
registered in `_template_placeholder_values`) as a data-fenced `Certified exemplar` block —
guard-safe like `<dependency_facts>`, empty for any non-`generate.generate` request. It is PRIOR
ART, never a gate and never the node's own past source (self is excluded): the leaf uses it as a
structural reference for the plumbing and authors THIS node's physics from its own spec.

**Docs.** `AGENT_CONTRACT.md` — the past-artifact-reference prohibition gains the
conductor-injected-exemplar exception (host-selected, not a spontaneous leaf read).
`skills/workflow-generate-generate/SKILL.md` — the exemplar-usage rule (structural reference,
do not copy physics, do not self-read other nodes' sources).

**Files.** `tools/orchestration_runtime.py` (`_resolve_exemplar_source`, `_catalog_family_index`,
`_build_exemplar`, `_template_placeholder_values`); `tools/workflow_conductor.py`
(`_resolve_exemplar`, `build_launch_request` exemplar param + `run_substep` wiring);
`tools/prompt_templates/{step,substep}_agent.txt`; docs + ceilings. Tests: selector unit
(certified sibling / self-exclude / uncertified / family+kind mismatch / non-fortran /
most-recent-across-siblings), `_build_exemplar` render gating, and the build_launch_request
attach scope. **Real verification is a billed E2E** (a node in a family with a certified sibling;
attempt-1 pass rate + thinking-token delta via the timing-audit — ride-along with M1's E2E).
Unit suite green (1972).

## R1 / M3d — recovery: spec-input spec_id bound, heuristic deletion, node-aware runner-contract narrowing (IMPLEMENTED 2026-07-08)

Canonical plan: `~/.claude/plans/dapper-baking-thompson.md` (M3d). With the harness host-render path
proven (M3c-β / billed E2E #3), M3d recovers the now-obsolete leaf-authored-runner scaffolding and
adds the mass-opt-in prerequisite gate. Five parts:

1. **spec_id ≤ 55 spec-input gate.** New `runner_renderer.spec_id_length_violation(spec_id)` (reuses
   `MAX_SPEC_ID_LEN`), called from `workflow_conductor.resolve_node` BEFORE the catalog read, so a
   too-long spec_id fails at spec-input for the explicit target AND every `--with-deps` closure member
   (each runs through `run_conductor → resolve_node`), with a **closure-build mirror** in
   `run_workflow._resolve_dependency_closure`'s `visit()` so an already-ready dependency — skipped
   before it would reach `_run_node`/`resolve_node` — cannot slip the gate. This is the canonical capture point for the ONE
   node-IDENTITY render precondition the compile.static hoist deliberately excludes (a re-author cannot
   shorten a spec_id); the renderer's `_check_identifier_lengths` keeps the same bound as a
   defense-in-depth backstop. Deliberately spec-input (pre-IR) ⇒ language/phase-agnostic; the 55 bound
   reflects the f2008 limit of the only current backend (fortran). The known 61-char catalog offender
   `dynamics_advection_diffusion_profile_1d_upwind_center2_euler1` is thus blocked from harness adoption
   until renamed (it is excluded from the mass-add below).
2. **Deleted the two LLM-fabrication heuristics** (`_validate_problem_runner_diagnostics_dependency`,
   `_validate_problem_runner_nonphysical_casepath_input`) + their now-dead private helpers +
   `skip_llm_heuristics` threading + 4 tests. They were unreliable `problem/`-scoped guesses with a
   known false-positive history; the cheap deterministic backstops (name / forbidden-output /
   json-serialization / snapshot-filename) stay for every runner. Not a full no-op: the one remaining
   legacy no-harness `problem/` node (`advdiff1d_linear`) now relies on LLM verify/judge + backstops
   for fabrication, not these heuristics.
3. **Node-aware runner-output-contract narrowing.** `leaf_contract_doc_refs(step, *, is_m3c_physics)`
   drops `RUNNER_OUTPUT_CONTRACT.md` from an M3c PHYSICS generate leaf (authors model+checks, runner
   host-rendered) but KEEPS it for `Validate.judge` and for a NON-M3c runner-authoring generate leaf
   (the `infrastructure` harness self-test — its §3 cites §4 — and legacy no-harness nodes that
   hand-roll JSON). The conductor stamps `runner_host_authored` into the request payload; the
   record-launch security-boundary path reads it via `_payload_is_m3c_physics`, so both must-read
   assembly paths compute the identical set (the stamp is load-bearing against drift). This is exactly
   the plan's "exclude from the physics-node must-read", not a blanket generate removal (which would
   strand the certified infra harness leaf on its next re-cert).
4. **Doc retargeting.** Scope notes added to `RUNNER_OUTPUT_CONTRACT.md`, `AGENT_SKILLS.md`,
   `phase_02_generate.md` §2-1, `WORKFLOW_CORE.md`, `PERFORMANCE_DIAGNOSTICS.md` §6, and the
   generate-verify SKILL — all node-aware (host-rendered on M3c; leaf-authored + contract-read on
   non-M3c). The architectural "the system produces a runner" statements (SPEC/CONTROLLED_SPEC/GLOSSARY/
   RUNBOOK) stay true (a runner is still produced, just host-rendered for physics) and were left.
5. **deps.yaml mass-add (E2E #4 prep).** The `infrastructure: harness_fortran_cpu >=0.2.0 <1.0.0`
   dep was added to the 4 `shallow_water2d`-closure nodes lacking it (boundary got it in M3c-β) — the
   exact set billed E2E #4 re-certifies. The advection_diffusion family is deferred: its closure
   (`advdiff1d_linear`) pulls in the 61-char offender, which the new spec-input gate would reject, so
   that family opts in only after the offender is renamed.

Unit suite green (2134). Two independent adversarial reviews (general + Codex) converged — no confirmed
correctness bug. The general pass drove rationale/caveat fixes (accurate `advdiff1d_linear` coverage
note, mono-fortran-backend caveat) and an end-to-end two-path consistency test; the Codex pass drove two
robustness hardenings: (a) `_payload_is_m3c_physics` now reads the flag strictly (`is True`, not a
truthy `bool(...)`) so a malformed non-boolean falls back to the SAFE superset (keep RUNNER); and (b)
the closure-build `spec_id` mirror above (an over-length ALREADY-READY dep could otherwise skip the
per-node gate). **Real verification is billed E2E #4** (operator-run: `shallow_water2d --with-deps`
full-closure re-certification + timing-audit before/after).

## R1 / M3c-α — infrastructure published surface as a signature-level contract (IMPLEMENTED 2026-07-07)

Canonical plan: `~/.claude/plans/dapper-baking-thompson.md` (M3c-α). M3b-cert certified the
harness but exposed that the certified implementation had DRIFTED from its `controlled_spec` §3
prose (string-carrying `h_named`, flat `write_diagnostics`), and the `public_api` gate pinned only
NAMES, so "the spec is the contract" did not hold at the signature level. M3c-α makes an
`infrastructure` node's published Fortran surface a binding, machine-checked, signature-level
contract — the cheapest moment to do it (zero consumers yet).

**Spec (v2, `spec/infrastructure/infra/harness/harness_fortran_cpu/`, `spec_version 0.2.0`).** §3
was rewritten to (a) ACKNOWLEDGE the certified signatures byte-for-byte where the implementation
was right (`h_named{name,json}`, non-generic `__box(name,json)`, the `__emit_*` family,
`__parse_cases`, `__write_snapshot`, the 8-arg `__write_perf`) and (b) RESPEC the two writers
record-driven (`__write_diagnostics(results, n)` / `__write_metrics_basis(entries, n)`) with four
new published derived types (`h_check`, `h_metric`, `h_case_result`, `h_mb_entry`), so the JSON
envelope assembly + verdict fold live only inside the certified operations (a per-language glue
renderer holds zero serialization knowledge — M3c-β). A new **§5.1 canonical interface block** (a
fenced Fortran interface: every published type + operation signature) is the machine-readable
source of truth. `tests.md`/catalog bumped to 0.2.0 (in-place respec — no consumers).

**Gates (`tools/validate_pipeline_semantics.py`).** A §5.1 parser
(`_parse_canonical_interface_from_controlled_spec` → per-symbol *stanzas*; `_parse_interface_stanzas`,
`_fortran_logical_lines`, `_normalize_fortran_line`, `_strip_fortran_comment`) normalizes away
comments, `&` continuations (spanning interleaved blank/comment lines — the §5.1 `write_perf`
header is >132 cols and MUST wrap), case, and whitespace. Three deterministic pins:
- **Compile** (`_validate_infrastructure_public_api` + `_validate_ir_signatures_against_section51`):
  §5-prose name set == §5.1 symbol set, AND the IR's `public_api.signatures` (`{symbol, interface}`,
  authored by Compile.generate transcribing §5.1) == §5.1. A derived type's component layout is
  compared ORDERED (positional-construction ABI); a procedure's dummy decls as a SET (the header
  already pins call order). Malformed / mislabeled / duplicated entries are fail-closed.
- **Generate.static** (`_validate_infrastructure_generated_signatures`): the generated
  `<spec_id>_model.f90` must publish every §5.1 signature — checked PER-SYMBOL (a drift in one
  procedure cannot be masked by an identical decl in another), types ORDERED, procs by membership,
  plus the §5.1 module `parameter` declarations (`dp` / `case_id_len`) pinned by value. Infra-only;
  fail-closed when a node is infrastructure (by `node_key`) but its IR/§5.1 cannot be resolved.

`public_api.signatures` exists because `Generate.generate` is walled off from `controlled_spec.md`
(phase_02 §2-1): the IR is the leaf's ONLY carrier of the signatures it must publish. This moves
signature-exactness off the ~17-min `Generate.verify` leaf (and the Build link error) to two cheap
deterministic checks.

**Docs.** `phase_01_compile.md` (V8 + `public_api.signatures` schema + §1-1), `phase_02_generate.md`
(the new Generate.static signature gate), `skills/workflow-{compile-generate,generate-generate,
generate-verify}` (author/transcribe §5.1 verbatim; verify no longer re-audits signatures). Ceilings
bumped with justifications.

**Review.** Multiple independent adversarial passes (general sub-agents + Codex), iterated to
convergence (the last two rounds found no actionable defect). Fixed across rounds: the
whole-file-line-set false-accept (a drifted decl masked by an identical decl elsewhere → per-symbol
scoping); component-order false-accept (set→ordered-list for types) and an inserted-extra-component
false-accept (subsequence→exact-list); combined-declarator false-reject (`integer, intent(in) :: a,
b` → per-entity `_declaration_atoms` splitting on top-level commas); no-space `endfunction`/`endtype`
and bare `end type`/bare `end` (→ `end\s*` + `_canonicalize_end_line` dropping the trailing name +
stanza loops terminating on the next header so a bare `end` can't swallow the following symbol —
while a derived type missing its mandatory `end type` still fails closed, since a bare `end` does
not close a type);
`&`-continuation join across interleaved comment/blank lines (the `write_perf` header is >132 cols
and must wrap); unpinned §5.1 parameters; duplicate-stanza overwrite; unvalidated `signatures`
`symbol`; infra fail-closed on an unresolvable/malformed/non-infra IR; and scoping the fence to the
`### 5.1` subsection (so an unrelated §5 fence can't brick certification). The comparison unit is a
per-entity, end-canonicalized **atom** so every semantics-preserving Fortran formatting difference
compares equal while a genuine name/type/rank/intent/component/order drift still fails. Remaining
false-reject-only limitations (interface-block dummies, type-prefixed `real(dp) function` headers,
`pure`/`elemental` prefixes, `character(4)` vs `character(len=4)`) are unreachable by the harness
§5.1 as written and self-correcting under the verbatim-transcription instruction. Two further Codex
passes found (a) a residual fail-open — a §5.1 type missing `end type` before the next header was
silently accepted — now fixed (fail-closed while still bounding the cascade); and (b) a spec-wording
error: §3/§5.1 called `dp`/`case_id_len` "public", but the certified harness keeps them module-
private (its runner hardcodes `character(len=64)`), so the wording was corrected to "internal,
value-pinned, not exported" rather than adding a visibility check that would false-reject a correct
harness. **Real verification is a billed E2E #2′** (operator-run: harness single-node re-certification at 0.2.0,
`aggregate_verdict=pass`, the strengthened gates forcing correct signatures from attempt 1). Unit
suite green (2034). M3c-β (physical-node narrowing + host-rendered glue) and M3d (recovery) follow.

## B1 — structural `Validate.execute` failure: warm `generate` reuse carrying the gate findings (IMPLEMENTED 2026-07-09)

Canonical plan: `~/.claude/plans/floofy-cuddling-octopus.md` (Part B / B1). An execute failure that
authors no `verdict.json` (case (b) of `classify_failure`'s execute branch) routed uniformly to
`("generate", "restart")` — a **cold** re-roll that discards the text explaining why the run failed —
while the judge's equivalent class, `("structural_violation", "code")`, routes to
`("generate", "reuse")` (warm). The two differ only in whether the defect was caught by the
deterministic `post_execute` gate or by the judge, not in how repairable it is, so the cold restart was
a misclassification. Its cost is concrete: a `raw/metrics_basis.json` shaped
`{"test_id": ..., "values": {...}}` fails the `required_raw_variables` check, and a blind restart cannot
learn that (Part A removes the ambiguity that produces the wrong shape; this part makes the repair turn
informed regardless).

1. **On-disk discriminator.** The two no-verdict kinds are distinguishable without a new file:
   the runner runtime-error branch of `_execute_inproc` returns **before** any `trial_meta.json` is
   written, while the structural branch writes one. That branch now also records
   `failure_category` (`post_execute_violation` / `snapshot_deliverable_gap` /
   `quality_check_mismatch`, in that precedence — a gate report is the most specific) and
   `failure_excerpt` (the `[execute fail]` block, tail 50 lines as in `binary_meta` / `lint_meta`,
   **and** tail 4000 characters: unlike compiler stderr a `post_execute` violation is not
   line-shaped — it interpolates whole dict payloads into one line — so the line cap alone leaves
   the prompt-rendered excerpt unbounded). `trial_meta.json` is already an execute `allowed_output_paths`
   entry and `_validate_trial_meta` checks only required fields, so no authorization or gate
   wiring changes. Because `trial_meta.json` is now routing-critical, it joins `verdict.json` in the
   R2 stale-artifact guard at the top of `_execute_inproc`: both are unlinked before the run, so
   `<file> present ⟺ THIS execute authored it` holds without relying on the external run-id-rotation
   invariant. The field contract (values, classification convention, `repair_strategy` mapping) is
   canonical in `docs/workflow/phases/phase_04_validate.md`, presented exactly as `binary_meta.json`'s
   is in `phase_03_build.md`.
2. **Routing table.** `VALIDATE_EXECUTE_FAILURE_ROUTING` maps each category to
   `("generate", "reuse")`, with `VALIDATE_EXECUTE_REASON_PREFIX` composing the route reason
   `validate_execute_<category>`. Consumers must match on the **category suffix** (a table key),
   never the prefix: the cold-restart `validate_execute_fail` and the per-test predicate reasons
   `validate_execute_physics_fail` / `validate_execute_structural_violation` share it.
3. **`classify_failure` case (b).** The C2 counter increment and the threshold-2 Compile reopen run
   **first and unchanged** (a Generate repair that already failed to fix the failure still attributes
   the defect to the IR). Below the threshold, a `status == "fail"` trial_meta whose category is in
   the table yields `RouteDecision("retry", "generate", "reuse", reason=validate_execute_<category>)`.
   A missing trial_meta (runtime error) or an unrecognized category keeps the cold restart.
4. **Findings channel.** `_read_repair_findings` gains a branch reading the failed run's
   `trial_meta.json#failure_excerpt`, and `conduct`'s cross-phase reopen now reads the findings
   **before** `reopen_phase` (while `refs` still names the failed run — reopen rotates the run id)
   and threads them into `_repair_payload`, mirroring the same-phase branch. Every other cross-phase
   reason yields `None`, so the change is upward compatible. Nothing else is new: `reuse` + findings +
   a resumable producer session is exactly the existing **G1-slim** path — `_resolve_reuse_resume`
   warm-`--resume`s the `generate.generate` producer and the slim prompt renders the findings inside
   its untrusted fence. No new substep, no new run-node file, no new prompt form, and no marker-parity
   change (the slim branch is payload-flag driven, not reason driven). A garbage-collected session
   degrades to the cold full prompt, exactly as a lint/static repair does today.
5. **dev is unchanged (F1).** A cross-phase rollback still fail_closes on the first occurrence with
   `reason_code=dev_phase_rollback`; the category now rides in `reason_detail` as
   `validate_execute_<category>`, which is what the B2 dev `--resume` directive keys on.

Unit suite green (2195), including: the routing split (category present / absent / unrecognized), the
preserved C2 ordering, one producer test per `failure_category` plus the precedence between them
(mutation-checked: each category literal and each precedence edge fails a distinct test), the
runtime-error branch writing no trial_meta and the stale-trial_meta guard, the prefix-collision guard in
`_read_repair_findings`, and a `conduct`-level pass asserting the prod warm reopen (findings read before
reopen with `refs` still naming the failed run, `warm_resume` + emptied must-read on the repair launch)
and the dev fail_closed detail. The route fires only on a real structural execute failure, so its live
exercise is opportunistic; B2 (dev `--resume` → generate + findings injection) follows.

## B2 — dev `--resume` after a structural `Validate.execute` failure (IMPLEMENTED 2026-07-09)

Canonical plan: `~/.claude/plans/floofy-cuddling-octopus.md` (Part B / B2). In dev, B1's
`("generate", "reuse")` route is a cross-phase backward rollback, so the F1 guard fail_closes it as
`dev_phase_rollback` on the first occurrence. The operator's `--resume` then skipped the checkpointed
`Compile` / `Generate` / `Build` phases and re-ran `Validate` against the **same binary**, so the
deterministic gate failed identically — a deadlock the operator could only escape with a fresh full
run. B2 makes that resume reopen `Generate` and hand it the violation text, i.e. the operator-initiated
equivalent of prod's automatic B1 retry. **F1 itself is unchanged**: an in-run automatic rollback still
fail_closes; the directive fires only on `--resume`.

1. **Deriver.** `_derive_dev_validate_execute_resume_directive` (`tools/orchestration_runtime.py`) gates
   on `reason_code == "dev_phase_rollback"` **and** a `reason_detail` of `validate_execute_<category>`
   whose **category suffix** is one of B1's reuse-routed keys — never the prefix alone, so the
   cold-restart `validate_execute_fail` and the per-test predicate reasons keep the plain resume. The
   trigger and `node_key` come from `failure_analysis.json#failed_agent_run` (fallback: the newest
   non-pass `steps/*/validate/*/step_result.json#failed_substeps[-1]`), re-validated against
   `agent_runs.jsonl` as a terminal non-pass `validate.execute` substep that a prior reopen has **not**
   already superseded — the same record `reopen_phase` will accept as a trigger, and a consumed one
   would make it a `noop` (reopening nothing while the conductor skips the still-checkpointed Generate
   and drops the repair). The findings are the failed run's `trial_meta.json#failure_excerpt`, whose
   path is recovered from that run's `launches/<arid>.request.json#allowed_output_paths` (the run node
   dir is not derivable from the orchestration root); the fallback is the `[execute fail]` block of
   `agents/<arid>/dialogs/deterministic.stderr.log`, bounded to 4000 characters. **Nothing new is
   persisted** — the directive is a derived cache on `meta["resume_directive"]`, like the two derivers
   beside it. `cmd_init --resume-from-checkpoint` wires it third in the `terminal_reset` chain (after
   the `_ir` and unauthorized-write derivers), capturing `reason_detail` before the archive loop moves
   it to `resumed_from_reason_detail`; a resume whose terminal failure does not match drops the stale
   directive, as before.
2. **Consumer.** `Conductor._consume_resume_directive`, called at the top of `conduct()`, is the
   **first consumer** of `resume_directive` (the earlier two are honored by the operator/agent
   following `RUNBOOK.md` §3-1). It acts only on `source == "dev_validate_execute_structural"` with a
   matching `node_key` and `generate` in scope. The producer arid is recovered from the checkpointed
   step_result **before** `reopen_phase`, which drops the entry it is read from. It then calls
   `reopen_phase(from_phase="generate", trigger_arid=<failed execute arid>)` — the anti-abuse gate
   accepts it because the trigger is a recorded terminal non-pass substep strictly downstream of
   `generate` — and returns a `pending_repair["generate"]` of
   `{issue_severity: major, repair_strategy: reuse, repair_target_agent_run_id: <producer>,
   repair_reason: validate_execute_structural_resume, repair_findings: <excerpt>}` for `conduct` to
   merge. A `Generate` that is **not** checkpointed-complete is a no-op (the plain resume re-runs it
   anyway, and reopening would archive the in-progress attempt); a rejected reopen emits
   `resume_directive_reopen_failed` and degrades to the plain resume rather than crashing; a `noop`
   reopen — the superseded-trigger case the deriver already rejects — seeds no repair, so a dropped
   repair can never masquerade as an applied one.
3. **No new channel.** The repair payload flows through the existing G1-slim mechanism exactly as B1's
   does: `run_phase(repair=...)` → `_resolve_reuse_resume` warm-`--resume`s the `generate.generate`
   producer → the slim prompt renders the findings inside its untrusted fence. A garbage-collected
   session degrades to the cold full prompt (findings dropped), as every other `reuse` repair does.
4. **Cross-module literals.** `_DEV_VALIDATE_EXECUTE_REUSE_CATEGORIES` /
   `_DEV_VALIDATE_EXECUTE_REASON_PREFIX` / `_DEV_RESUME_FINDINGS_MAX_CHARS` duplicate the conductor's
   `VALIDATE_EXECUTE_FAILURE_ROUTING` / `VALIDATE_EXECUTE_REASON_PREFIX` / `_EXECUTE_EXCERPT_MAX_CHARS`
   because `workflow_conductor` imports `orchestration_runtime` and the dependency cannot be inverted.
   A parity test pins the copies, following the `SLIM_REPAIR_FINDINGS_HEADER` precedent.

Unit suite green (2212), including: the deriver's reason/category gate (each non-firing reason and each
firing category), a passing execute run and a superseded one rejected as triggers, both fallbacks
(step_result → trigger, stderr log → findings), the 4000-character bound, the directive-without-findings
reopen, the `cmd_init` set/drop integration, the routing-table parity, and — conductor-side — the reopen
call with its repair payload, the four no-op gates (wrong source / wrong node / no trigger / `generate`
out of scope), the incomplete-`Generate` no-op, the `noop`-reopen no-op, and the reopen-exception
degradation. Like B1 the route fires only on a
real structural execute failure, so a billed E2E exercises it only opportunistically: when E2E #4 passes
on the clarified spec (Part A) the directive never fires, and when it fails structurally B2 replaces the
deadlock.

## B3 — node-aware execute routing + the per-case snapshot scope gate (IMPLEMENTED 2026-07-09)

Found by billed dev E2E #4 (orch `…075057Z_89f9f59a`, `dynamics_shallow_water_flux_2d_rusanov_p0`). Part A
held — the harness re-certified at `0.2.1` and emitted a flat `metrics_basis` — but the consumer node
fail_closed with `dev_phase_rollback` / `validate_execute_post_execute_violation`. Two distinct defects.

1. **`post_execute` false-rejected a conformant runner.** The `state_snapshots` bullet of
   `phase_04_validate.md` states the gate "scopes required variables per the snapshot's case", and a
   host-rendered (M3c) runner emits, per case, the union of `required_raw_variables` over the tests
   targeting it (`runner_renderer._per_case_vars`). The gate tried to resolve that scope from
   `case.test_case_set[].test_id` (`_case_id_to_test_id`), from an in-file `test_id`, or from the file
   stem. An M3c IR declares none of the three: `test_id` is not a required `test_case_set` field, the
   harness's `__write_snapshot` writes no `test_id` key, and the stem is a `case_id` (`case_dry_state`),
   not a `test_id` (`l0_invalid_dry_state_xfail`). So the gate fell through to "require every declared
   variable" and reported `F_star`/`G_star`/`a_x`/`guard_fired` missing from cases that legitimately omit
   them. Fix: resolve the case's tests through `io_contract.test_predicates[].target_cases` — the one
   case→test mapping every IR carries, and the exact field the renderer reads — as an anchor between the
   in-file `test_id` and the legacy ones (`_case_id_to_test_ids`). A case ranged over by several tests
   takes the **union**, which is what the renderer emits and has no single `test_id`. The anchor order is
   load-bearing: a snapshot naming its own `test_id` is a per-test file and must scope to that one test,
   never to the wider union. A case that is **declared** in `case.test_case_set[]` but that no predicate
   ranges over takes the EMPTY union — `validate_predicate_schema` checks each `target_cases` entry is a
   declared case, never that every declared case is targeted, and `_per_case_vars` renders such a case as
   an empty-state snapshot (`allocate(vals(0))`), so demanding every declared variable of it is the same
   false-reject. That relaxation is ordered last (a legacy per-case mapping still wins) and is gated on
   the case being declared, so a snapshot whose `case_id` matches no declared case still gets the strict
   set. The all-declared fallback therefore fires whenever no anchor resolves — an IR with no
   `test_evidence_requirements`, or an unknown case token — and is not disabled merely by the presence of
   predicates. `_io_contract_for_execution` flattens `io_contract` through a key whitelist that dropped
   `test_predicates`; adding it there is what makes the anchor reachable. Re-running the real gate over
   the failed run's artifacts turns `FAIL` into `PASS`, and the already-passing harness node stays `PASS`.
2. **B1's routing table ignores which phase authored the runner.** It sends every structural category to
   `("generate","reuse")`, which assumes the defect is in leaf-authored code. On an M3c node the leaf
   authors only `<spec_id>_model.f90` + `<spec_id>_checks.f90`; the runner is host-rendered. The renderer
   boxes that case's required variables **unconditionally** — `get_r1('F_star', …)` looks the value up in
   a leaf registry and the found-flag is discarded — so the snapshot key set and shapes are fixed by the
   IR while every value comes from the leaf. Therefore: `post_execute_violation` and
   `quality_check_mismatch` remain leaf-repairable in the value domain (a trivial all-zero basis, a NaN, a
   wrong metric) and keep the Generate route, but a `snapshot_deliverable_gap` — a missing per-case
   `<case_id>.json`, which the rendered runner writes for every `case.test_case_set[].case_id` — can never
   be fixed by regenerating model/checks. `classify_failure` now re-attributes exactly that category on a
   `_conductor_authors_runner` node to the IR and reopens Compile, with the `_ir` reason suffix the C2
   backstop already uses (`HOST_RENDERED_RUNNER_UNREPAIRABLE`). The suffix is not a routing-table key, so
   it also keeps the case out of the reuse set that `_read_repair_findings` and the B2 dev resume
   directive gate on — neither threads findings into a Generate repair that could not apply them. Like the
   C2 threshold branch, this reopen **resets the C2 counter**: it regenerates the IR and everything
   downstream, so the next execute failure is against fresh artifacts and must get its own
   Generate-retry-first cycle. Without the reset the stale count sends the very next failure —
   typically a leaf-repairable value defect in the regenerated checks module — to the findings-less C2
   reopen, skipping the warm repair B1 exists for. A leaf `error stop` mid-loop cannot
   reach this branch: it exits non-zero, so `_execute_inproc` writes no `trial_meta` and the cold
   `validate_execute_fail` restart stands.

**Dev limitation (unchanged by this section).** In dev the new Compile reopen is a cross-phase rollback,
so F1 fail_closes it as `dev_phase_rollback` and no deriver claims the `_ir` reason detail — `--resume` is
therefore a plain resume that reproduces the failure, exactly as the pre-existing `validate_execute_fail_ir`
C2 reopen already behaves in dev. The operator runs `reopen-phase --from-phase compile` manually. This is not
a regression: before this change dev reopened Generate, which cannot repair a host-rendered snapshot gap and
therefore reproduced the failure after spending a Generate attempt.

Suite green (2227). Coverage: the gate's anchors (per-case pass, multi-test union, union member omitted
still flagged, in-file `test_id` outranking the union, the union outranking the legacy single-test map,
the legacy map outranking the empty union, a declared-but-untargeted case requiring nothing, an
undeclared case token keeping the strict requirement,
the empty-union relaxation confined to predicate-carrying IRs, a targeted test with no evidence
requirement, the un-flattened `io_contract` shape, malformed / duplicate / whitespace-padded predicate
entries) — mutation-checked, with the whitelist entry removed, each anchor disabled in turn, the union
replaced by an intersection, each adjacent anchor pair swapped, the empty-union mirror removed, its
declared-case guard and its `case_to_tests` guard dropped, the `t in per_test_required` filter removed,
and the dedup / strip dropped all failing distinct tests; the routing split (M3c snapshot gap → compile `_ir`, M3c `post_execute_violation` /
`quality_check_mismatch` → generate/reuse as literals, non-M3c snapshot gap → generate/reuse), with
`HOST_RENDERED_RUNNER_UNREPAIRABLE` pinned as a literal so an over-inclusive edit cannot make the
value-domain test vacuous; the C2 counter reset (dropping it sends the next value defect to the
findings-less reopen); and the B2 deriver rejecting the new `_ir` reason.

## B4 — the dev resume directive must not inject stale findings (IMPLEMENTED 2026-07-10)

Found by the first live firing of B2, during billed dev E2E #4 (orch `…075057Z_89f9f59a`). The B3 gate
fix landed as `852493a`; the operator then resumed the orchestration that had fail_closed under
`81b9a63`. B2 behaved exactly as designed — it derived the directive, reopened Generate, warm-resumed
the producer session, and rendered the failing gate's `failure_excerpt` into the slim repair prompt.
The excerpt was the OLD gate's violation text. Reasoning from it as ground truth, the leaf concluded
"Validate.execute's deliverable gate pins each snapshot against the declared `schema.variables` set, so
no per-case snapshot can ever satisfy it … unsatisfiable in the leaf's write scope", declined to fabricate
a repair, and attributed the defect to the IR. `generate.verify` graded that `critical`, F1 fail_closed
as `dev_verify_critical`, and one Generate cycle was spent on a defect that no longer existed. Every
component was correct; the input was false.

The failure mode is general: a repair leaf treats injected findings as ground truth, so findings that
outlive the source that produced them are worse than no findings at all — a blind restart re-derives
reality, while a misinformed warm repair confidently does not.

1. **Stamp.** `_execute_inproc` records `repo_revision` (`{commit, dirty}`, via the existing
   `_capture_repo_revision`) into `trial_meta.json`, beside the `failure_excerpt` B1 already writes there.
   No new file — a field on an artifact the execute substep already owns and `_validate_trial_meta`
   already tolerates (it checks required fields, not an exact key set).
2. **Gate.** `_derive_dev_validate_execute_resume_directive` reads the failing run's `trial_meta.json`
   (resolved through its `launches/<arid>.request.json#allowed_output_paths`, as before) and emits the
   directive only when that stamp equals the revision at resume time. An unreadable trial_meta, an
   unstamped one (a pre-B4 run), or a repo that is not a git checkout all leave freshness unprovable, so
   all decline.
3. **The trigger must be the NEWEST execute attempt, not the one `failure_analysis.json` names.** The
   canonical analysis is written once (`_atomic_write_json_exclusive`) at the first failure and preserved
   across resumes, so `failed_agent_run` names the FIRST failing run forever. The deriver previously read
   it first, which would have compared that run's stale stamp on every later resume and declined
   permanently — converting the deadlock-breaker into the deadlock, precisely in the case this directive
   exists for (a structural failure that still needs a Generate reopen after the source change).
   `_dev_execute_failure_arid` therefore scans `agent_runs.jsonl` in append order and takes the newest
   `validate.execute` substep run, declining when it passed (nothing to repair) or was already superseded
   by a reopen. That record is also what `reopen_phase` validates the trigger against.
4. **Why the stamp lives on the failing run, and why that makes the guard self-correcting.**
   `orchestration_meta.repo_revision` is captured at the orchestration's FIRST start and deliberately
   preserved across resumes (provenance), so comparing against it would diverge permanently after any
   commit lands mid-run. The per-run stamp plus the newest-run trigger give the guard its liveness
   property: when it declines, the plain resume re-runs the deterministic Validate.execute under the
   current source, which either passes (the fix worked — and it costs no Generate cycle at all) or fails
   again and appends a freshly-stamped attempt, so the NEXT resume's directive fires with truthful
   findings. This is also what migrates an in-flight pre-B4 run, whose first `trial_meta` carries no stamp
   at all: one deterministic re-run and the directive is live again.
5. **Equality, not cleanliness.** A same-commit dirty-to-dirty resume still fires. Declining on `dirty`
   alone could never self-correct — the re-run would re-stamp `dirty` again and decline forever, restoring
   the deadlock B2 exists to break. The residual gap is a working tree edited between the failure and the
   resume without a commit: `{commit, dirty}` compares equal and the findings are injected anyway. Closing
   it would need a content digest of the dirty tree, which `_capture_repo_revision` does not compute.
   Commit the fix before resuming when the findings must be invalidated.

Replaying the incident against the guarded deriver returns `None` where it previously returned a directive
carrying 1334 characters of stale findings. Suite green (2234); mutation-checked — removing the guard,
removing the stamp, excluding `dirty` from the comparison, resolving the trigger from the FIRST execute
run instead of the newest (the frozen-analysis bug), and dropping the newest-run-passed check each fail a
distinct test.

## R3-core — multi-target test evidence: metrics-basis keyed by (test_id, case_id) (IMPLEMENTED 2026-07-10)

**The wedge.** `shallow_water2d`'s `tests.md` v0.2.0 declares three intrinsically multi-target tests — two
grid-refinement convergence sweeps over `nx ∈ {32, 64, 128}` and a translation-equivariance test over a
base/shifted case pair. The M3c-β renderer recorded each test's `raw/metrics_basis.json` evidence from that
test's **first** target case and explicitly fail-closed on any test targeting more than one
(`runner_renderer.py`, "Refusing to emit partial evidence"). So a faithful IR was rejected by the gate and a
gate-passing IR violated the Compile V3 faithfulness invariant: **no IR was authorable**, and `compile.generate`
correctly reported `Unrepresentable contract`. The leaf was right; the contract was wrong.

**The fix — the evidence index is a matrix, not a list.** `raw/metrics_basis.json` now carries one entry per
(`test_id`, target `case_id`) pair, `case_id` being a direct sibling key of `test_id` in every entry (single-target
tests included — no special case). The product is taken over `io_contract.test_predicates[].target_cases`, which
was already the anchor the host-rendered runner emits from (`_per_case_vars`, the per-case snapshot scope gate of
B3) and is the only case→test mapping every IR carries. Both sides therefore read ONE field:

- `runner_renderer.render_runner` emits `Σ len(target_cases)` `h_mb_entry` records instead of one per test;
- `validate_pipeline_semantics._validate_metrics_basis_per_test` pins the entry set against the same product,
  in both directions (missing row and unknown row alike), via the new `_test_id_to_case_ids` — the exact reverse
  sibling of `_case_id_to_test_ids`.

The harness respec (`harness_fortran_cpu` `0.2.1` → `0.3.0`) adds the `case_id` component to
`harness_fortran_cpu__h_mb_entry` (§3.1 / §5.1, component order `test_id, case_id, values` — the §5.1 type stanza
is compared line-for-line) and teaches `__write_metrics_basis` to emit it. Its self-test grew a seventh test,
`l0_multi_case_evidence_pass`, which declares no case of its own and ranges over the two existing cases that
already emit `max_abs_deviation` — so the multi-entry writer path is exercised by the harness's own certification
with no new snapshot variable and no new check id.

**Cross-case reductions stay inside the runner.** The DSL invariant "the runner reduces, the predicate compares"
is untouched. A convergence order or a symmetry residual is accumulated by the checks module's module-level
accumulator pattern (`CHECKS_MODULE_CONTRACT.md` §3, already contractual) and emitted through `metric_compute` as
a **per-case metric of the case where it first becomes computable** — the `n064` case carries
`convergence.order_n032_to_n064`, the `n128` case carries `order_n064_to_n128` (zero-padded, so the ordering rule
below holds). A sparse per-case metric (present in
some cases, absent in others) passes every existing gate; there is no metric-coverage gate to satisfy.

To read such a metric a predicate needs to name ONE case, which the DSL could not express: `per_case: true` means
"in every target case", and predicates are one-per-test (`test_id` duplication is rejected), so the scope had to be
a **condition-level** selector. `verdict_evaluator` gained `case: <case_id>`, which resolves `ref` inside that one
case's `_case_slice`. Schema: non-empty string, a member of the predicate's own `target_cases`, and mutually
exclusive with `per_case` (the evaluator raises `PredicateError` on the pair as its own mirror). The evaluated
basis is the same `{"case": cid, ...}` shape `per_case` already produced, so the conductor's
`_verdict_failure_report` renders it unchanged.

**Ordering is lexicographic, and that is a contract.** `read_case_ids` sorts, and both execution paths (the
`Makefile` and `validate.execute`) pass the sorted list, so an accumulator may depend only on cases whose `case_id`
sorts **before** the emitting case's. Zero-padded resolutions (`n032 < n064 < n128`), zero-padded shifts, and
suffix-extended derivatives satisfy this naturally; the trap — documented in `CHECKS_MODULE_CONTRACT.md` §2 — is a
derived case whose id sorts ahead of its base (`..._dts050` before `..._dts100`). `test_case_set` declaration order
is never the execution order.

**Why no new render precondition.** An earlier draft added a gate for "a target case that does not emit the test's
`required_raw_variables`". It cannot fire: `_per_case_vars` *defines* a case's emitted set as the union of
`required_raw_variables` over the tests targeting it, and already `RenderError`s when one is absent from the
snapshot schema. Adding the check would have been dead code asserting its own precondition.

**Why the tools change forces the harness recert.** `runner_renderer._HARNESS_V3_INTERFACE` (renamed from `_V2`)
carries the new `h_mb_entry` verbatim, and `assert_harness_pin` three-way-compares it against the certified harness
IR's `public_api.signatures` and the certified model source. Until the harness re-certifies at 0.3.0, every
consumer's render fails closed with the pin-drift hint. The harness's own recert does not pass through the pin
(`_conductor_authors_runner` is False for `infrastructure`; `_ir_is_m3c_physics` no-ops), so the ordering
tools → harness recert → dependent recert is *structurally* enforced, not merely documented. The dependent recert is
then automatic — see R6-lite.

The `tests` **object** container form is still parsed but deprecated: being keyed by `test_id` it cannot hold the
several rows a multi-target test owes, so `_validate_metrics_basis_per_test` rejects that combination with an
actionable message naming the `per_test` list rather than reporting the rows as merely missing.

**Two adjacent holes review closed on the way.** Both are the same shape as the wedge above — something compiles and
then fails at runtime, in a host-authored file no leaf can repair:

1. **`case_id` longer than the harness's `case_id_len` (64).** `__parse_cases` stores each parsed id in a fixed-width
   `character(len=case_id_len)` slot (an assumed-length `intent(out)` character dummy is disallowed), truncating a
   longer id — while the `select case` labels and `find_case_index` literals the renderer emits carry the full id. So
   `trim(case_ids(ci))` never matches, `case default` yields an empty snapshot, and the run `error stop`s. The
   100-column lint guard does not catch it: a bare `case ('<id>')` label only reaches column 100 at ~87 chars, leaving
   a 65–87-char window that renders, compiles, and always fails. `_case_ids` now bounds it with an `identity=False`
   `RenderError`, so `compile.static` hoists it to a `compile.generate` re-author.
2. **The renderer's copy of `case_id_len` was pinned to nothing.** `assert_harness_pin` compares interface *stanzas*,
   which name the SYMBOL `case_id_len` and never its value; the §5.1↔source parameter gate pins the harness against
   its own spec, not the renderer against the harness. A recert lowering the width to 32 therefore left every gate
   green while the glue kept passing a 64-wide actual to a 32-wide `intent(out)` dummy. `_HARNESS_V3_PARAMETERS` now
   pins both module parameter VALUES (`dp`, `case_id_len`) against the certified source, using the same per-entity
   atom normalization the §5.1 gate uses — so `CASE_ID_LEN`, the width the runner declares, and the harness's own
   parameter are one constant that cannot drift apart.
3. **A non-ASCII name reopened (1) through the units the bound is measured in.** Fortran's default character kind
   counts BYTES; `len()` and the 100-column guard count Python code points. A 64-code-point, 68-byte `case_id`
   therefore passed the new bound, was truncated into the `character(len=64)` slot, and `error stop`ped on every run
   — reproduced end-to-end through gfortran. `_flit`, the single choke point every embedded IR-sourced name (case_id,
   snapshot variable, metric address, test_id, target class) passes through, now rejects anything outside printable
   ASCII. One check, one class of bug, no per-name bound to keep in sync.
4. **A `case_id` is also a PATH, and printable ASCII includes `/` and `..`.** The harness builds each per-case
   snapshot filename by concatenating the runtime case_id — `raw/state_snapshots/'//trim(case_id)//'.json'` — so a
   case_id of `../../pwned` traverses out of the run directory and the cleanly-compiling, cleanly-running runner
   writes an arbitrary file (reproduced through gfortran: it wrote `pwned.json` a directory above the run node). The
   compile gates only required a case_id to be non-empty, and `_flit`/`CASE_ID_LEN` pass anything printable and short.
   `_case_ids` now restricts a case_id to `[A-Za-z0-9._-]` with no `..` — the same safe-token grammar the dependency
   layer (`orchestration_runtime._is_safe_path_token`) uses for the path segments it interpolates. All 20 existing
   spec case_ids already satisfy it. An apostrophe is thereby also barred from a case_id (it is a filename), so
   `_flit`'s apostrophe-doubling now only ever fires on the non-path names (test_id, metric address, snapshot
   variable) that legitimately reach a Fortran literal without becoming a path.

**And one in R6-lite: `all_nodes` is not a faithful closure signature.** `topo_level` is a node's *height*, so the
shapes `a→b, a→c, b→c` and `a→b→c` produce identical `(node_key, topo_level)` pairs for every node. They differ only
in whether `c` is a direct or a transitive dependency of `a` — precisely the `deps.yaml` edit that must re-certify
`a`, since its IR's `direct_deps` are gated against the sidecar at Compile. `_closure_signature` now compares the
`transitive_deps` membership as well (which pins the direct set too, as `all_nodes − {self} − transitive`). The
`via` PATHS stay excluded and uncomputed: they are derived from the same edges, and their enumeration is the
exponential part. So the builder's flag became `include_via=False` — membership is a set difference, and only the
path enumeration is skipped.

## R6-lite — dependency-freshness readiness: a dependency spec update regenerates its dependents (IMPLEMENTED 2026-07-10)

**The problem R3-core surfaced.** Bumping `harness_fortran_cpu` to 0.3.0 must re-certify the five nodes that depend
on it. The obvious lever — bump each dependent's `spec_version` so its artifacts miss — was rejected: their content
did not change, and a version bump that means nothing re-authenticates nothing. The freshness signal belongs in the
workflow, not in the specs.

**The mechanism.** A certified node already records the dependency closure it was built against: the G7
conductor-authored sidecar `<ir_ref>/dependency_graph.json`, whose `all_nodes[]` entries are
`kind/spec_id@version` node_keys. `_dependency_resolution_freshness` re-derives that closure from the current
`deps.yaml` + `spec_catalog.yaml` using the **same pure builder** (`tools/dependency_graph.py`) and compares. A
mismatch is *stale* — a distinct condition from *unbuilt*, with a distinct remedy. No new persisted file: the
recorded side is an artifact that already exists.

Enforcement is at the two — and only two — readiness evaluators, because the invariant leaks the moment one path
is missed:

- `_verify_dep_stage`, anchored on the `ir_ref` stage (every caller requires it, and the cumulative chains in
  `_verify_dependency_readiness` short-circuit on it). This covers `run_workflow._dependency_node_ready`, so
  `--with-deps` re-runs a stale dependency instead of skipping it as ready.
- `_certify_and_collect_dep_artifacts`, which the launch gate's own recomputation
  (`_compute_dep_readiness_and_fingerprint` → `_dependency_ready`) uses and which does **not** route through
  `_verify_dep_stage`. Staleness demotes the version to level 0, exactly as an `ir_ref` failure would. Without this
  a stale dependency would still pass `workflow-launch-check` on a single-node run.

On the reject path `_dependency_ready` calls `_stale_dependency_details` to turn the opaque
`direct_dependency_*_readiness_not_pass` into a message naming the drifted node, the resolution it was certified
against, the one derived now, and the remedy (`--with-deps`). Only a dependency whose `ir_meta.json` actually passes
can be reported stale; an unbuilt one is merely not ready.

**The leaf carve-out.** A node whose derived closure is only itself (a leaf — the harness) has no recorded resolution
that could drift, so it is fresh by construction and needs no sidecar. Without this every leaf would demand a sidecar
it has no reason to have.

**When the closure does not build, the reason decides.** The two halves are not symmetric, and conflating them was
the review's one real finding:

- The registry could not be **read** (`_UNREADABLE_CLOSURE_REASONS`: unreadable/malformed `deps.yaml`, unresolved
  spec_ref, corrupt catalog — plus a `RecursionError` from the builder's recursive DFS, which readiness must not turn
  into a crash now that it builds a graph where it previously built none). This says nothing about the recorded
  resolution. Freshness is a *comparison*; manufacturing staleness from a missing right-hand side would mask the real
  defect, which its own gates (`_resolve_dependency_closure`, `_validate_compile_dependency_consistency`) already
  surface. Treated as fresh.
- The registry **was** read and yields no valid closure (`dependency_unresolvable` / `dependency_version_conflict` /
  `dependency_identity_conflict` / `dependency_cycle`, or any reason the builder grows later). That is a definitive
  statement that the recorded resolution is not reproducible today — i.e. staleness. Fail closed. Reporting it routes
  the node to a re-run whose own closure resolution names the registry defect precisely, instead of letting a
  consumer link against a dependency that can no longer be re-derived.

Freshness passes `include_transitive=False` to the builder: it compares `all_nodes` only, and the sidecar author's
`via_for` enumerates every simple path (exponential on a wide diamond). `all_nodes` is byte-identical either way, so
the comparison still matches what the sidecar recorded.

**Scope: version granularity.** A content change within one `spec_version` is invisible here, which the respec
discipline (content change ⇒ `spec_version` bump) makes sufficient. Content-hash chaining is R6 proper.

**Effect on the harness bump.** Registering `harness_fortran_cpu` 0.3.0 in the catalog (with the five consumers'
`version_constraint` tightened to `>=0.3.0`) makes every dependent's recorded resolution stop matching, so a single
`--with-deps` run re-certifies the whole closure bottom-up. The constraint tightening is load-bearing rather than
decorative: under the old `>=0.2.0` range the harness node itself would still resolve to a ready 0.2.1 and be skipped,
while its consumers went stale — and Build stages dependency sources by resolved version.

### R6-lite follow-up — an ambiguous catalog entry must be stale, not fresh (review round 6)

`_dependency_resolution_freshness` resolves the subject node's spec directory before comparing closures.
`resolve_spec_ref_for` returns `None` for TWO different conditions — the catalog resolves the node to zero
directories (absence: a version-only entry with no `deps_path`), or to more than one (ambiguity: two entries for one
`(kind, spec_id)` pointing at different dirs). The original code treated both as "no comparison possible → fresh".

That masked a real defect. An ambiguous catalog was read fine and is a definitive statement that the recorded
resolution cannot be reproduced — exactly the condition `_resolve_dependency_closure` fail-closes on with
`dependency_spec_ref_unresolved`. So `--with-deps` would refuse to run while `_verify_dependency_readiness` reported
the dependency ready, and a single-node consumer built against it. Symmetric with the round-5 unreadable-vs-
irreconcilable split, ambiguity belongs on the stale side.

The two conditions had to be told apart, because marking absence stale false-fail-closes (24 tests, and any valid
path-less entry). `_spec_ref_candidates` was extracted from `resolve_spec_ref_for` to expose the candidate SET:
`len > 1` → ambiguity → stale; `len == 0` → absence → fresh; `len == 1` → the healthy path. The transitive-depth
case (a deeper node ambiguous inside `build_dependency_graph`) stays on the fresh side, because the builder's error
string conflates absence and ambiguity and cannot distinguish them — but every closure node is a freshness SUBJECT
in turn under `--with-deps`, where the precise `_spec_ref_candidates` check catches it.

### R3-core follow-up — the path-traversal case_id was gated only for M3c nodes (review round 6)

Bug (4) above put a case_id safe-token gate in `runner_renderer._case_ids`, but that runs only for M3c host-rendered
nodes. A **non-M3c** physics node has a leaf-authored runner (contractually building `raw/state_snapshots/'//trim(
case_id)//'.json'` from the argv), and NOTHING gated its case_id grammar — so an IR-declared `../../evil` survived
Compile, reached the conductor's `read_case_ids` → the runner argv, and the honest, cleanly-compiling runner wrote
outside the run directory (reproduced: a two-level `..` dropped a `.json` a directory above the run node; the execute
is a plain `run_program` subprocess, not bwrap-confined). Same class as (4), a layer up.

The canonical fix is a spec-input gate, not a renderer one: `_validate_case_ids` (compile stage, ALL node kinds)
rejects any `case.test_case_set[].case_id` outside `[A-Za-z0-9._-]`/no-`..`, routing to `compile.generate` before any
build. It reuses the renderer's `_CASE_ID_TOKEN_RE` as the single grammar. `read_case_ids` — the shared runtime argv
boundary for M3c and non-M3c alike — additionally DROPS any unsafe token, so even a hand-crafted IR that bypassed
Compile can never place a traversal string on the argv (the dropped case then fails its own in-directory deliverable
gate, a bounded failure, rather than writing out of bounds). The M3c renderer gate remains as defense-in-depth. All 20
existing spec case_ids satisfy the grammar. (A related NIT the same review found: `_validate_test_predicates` built
its case_id set unstripped while every runtime reader strips — fixed, so a whitespace-padded case_id no longer desyncs
predicate membership from the runtime identity.)

## Harness pin — resolve the certified IR structurally, not from `source_meta.ir_ref` (IMPLEMENTED 2026-07-11)

Canonical plan: `~/.claude/plans/sprightly-wibbling-turtle.md`. E2E #4 (`shallow_water2d --with-deps`) fail-closed
the consumer `dynamics_shallow_water_flux_2d_rusanov_p0` at generate start with `generate_runner_render_failed`:
"certified harness IR public_api.signatures omits `harness_fortran_cpu__parse_cases` … recert drift". **The
message was a misdiagnosis** — the harness 0.3.0 certified IR carried all 18 signatures. The real cause:
`_write_runner` resolved the certified harness IR for the signature pin (`assert_harness_pin`) via the certified
harness SOURCE's `source_meta.json` `ir_ref` field. But `ir_ref` is a leaf-authored **optional** field — absent
from `required_meta_keys_for_step("generate")`, the generate SKILL, and the docs. A 0.3.0 leaf wrote a
contract-minimal `source_meta` with no `ir_ref` → `harness_signatures=None` → empty `ir_iface` → the pin misfired
on the first symbol with the drift message → conductor render fail-close killed the workflow. 0.2.1-era leaves
happened to write a richer `source_meta`, so E2E #2′/#3 passed by luck; M3c-α (commit `a740f0b`) introduced the
hidden dependency on a non-required field.

**Fix.** (1) `_write_runner` now resolves the harness IR **structurally** and **bound to the linked source's
lineage**. `_build_inproc` stamps `binary_meta.source_ir_id` (host-authored) — the `ir_id` the certified binary's
source was generated from — and `_write_runner` reads it from the same certified binary snapshot the source comes
from, pinning against THAT IR. The binary is selected ONCE (`_certified_binary_meta` → both `source_source_id` and
`source_ir_id`, with `_certified_model_source` refactored to delegate to it), so a binary published between two
latest-binary selections cannot pair a source with a mismatched IR lineage (a TOCTOU a split source-vs-provenance
lookup would allow). This is not the globally-latest passing IR: a same-version **compile reopen**
re-numbers `ir_id` under the SAME `pipeline_id` (`_ensure_fresh_producer_id`), so the latest certified IR can
advance past the certified binary, and pinning it would raise a *false* interface drift even though source+binary
are internally consistent (the P1 Codex round caught this — the `ir_ref` and `pipeline_ref` readiness stages are
evaluated independently, `orchestration_runtime.py:1136-1162`, so a consumer can reach `_write_runner` in that
mixed state). Binaries predating the field (`source_ir_id` ABSENT) fall back to `_certified_ir_dir` (latest certified IR), which
equals the source's origin IR whenever no reopen skew exists — so the pending E2E-recovery harness (no
`source_ir_id`) still resolves correctly without a re-cert. Only ABSENCE falls back: a PRESENT-but-unresolvable
`source_ir_id` (unsafe token / dangling dir) is corrupt lineage and fails closed with `RuntimeError`, never masked
behind the latest-IR fallback (which would reintroduce the skew).

**Same-version signature invariant — the legacy fallback's safety basis.** At a fixed spec version the
controlled_spec §5.1 canonical interface is fixed, and the IR validator (`_validate_ir_signatures_against_section51`)
pins EVERY certified IR's `public_api.signatures` == §5.1. So all passing certified IRs at the same
`(kind, id, version)` carry IDENTICAL signatures, and the pin compares those signatures against the renderer's
embedded interface — hence WHICH same-version passing IR the fallback picks cannot change the pin verdict. A
signature divergence between two same-version passing IRs can arise ONLY from a §5.1 edit without a version bump
(a version-discipline contract violation R6-lite freshness governs), not normal operation. The `source_ir_id`
binding is exact-provenance defense-in-depth ON TOP of this invariant, not a substitute for it; the invariant is
why the earlier P1 concern (a compile-reopen advancing the latest IR past the certified binary) is a false-drift
only in that contract-violating window, and why the legacy fallback is safe. This is also the reason the fix does
NOT reintroduce a read of the leaf-authored `source_meta.json` `ir_ref` for legacy binaries (the Codex round-3
suggestion): it would re-add the very leaf dependency whose absence was the original bug, would not help the
0.3.0 recovery harness (which lacks that field), and buys nothing the invariant does not already guarantee. As a
belt-and-suspenders operator aid, a pin failure taken via the legacy fallback appends a hint naming the fallback
and pointing at `--with-deps` (rebuild → stamp `source_ir_id`), so even a contract-violation-window failure reads
as actionable rather than as a misdiagnosed interface drift. `source_ir_id` is a genuine host-authored structural link (a source→IR
binding not otherwise recoverable), so it does not fall under [feedback: no redundant persistence] (which bans
persisting values recoverable from existing artifacts) — that same feedback is why the earlier leaf-`ir_ref`-stamp
idea was rejected. The IR is never read from the leaf-authored OPTIONAL `source_meta.json` `ir_ref`. No certified
IR / no *usable* `public_api.signatures` (missing / empty / all-malformed list) now raises a `RuntimeError` (a
build precondition — "run `--with-deps` first"), distinctly routed from a `RenderError` drift;
`assert_harness_pin`'s own empty-signatures guard remains defense-in-depth for any caller. "Usable" mirrors BOTH
the pin's `ir_iface` build AND the IR validator's non-empty-field rule
(`_validate_ir_signatures_against_section51`): an entry needs a NON-BLANK str `symbol` and a NON-BLANK str
`interface`. A blank-field-only list (`{"symbol": " ", "interface": ""}`) — which that validator rejects — must
therefore route to the missing-artifact path, not seed a `""` key that `assert_harness_pin` would later surface as
a bogus per-symbol "omits … recert drift" (the same misclassification the whole fix removes).
(2) `assert_harness_pin` gained an early guard: no usable signatures
at all (None / `[]` / non-list / all entries malformed) fails closed as "no usable public_api.signatures — missing
artifact, NOT interface drift", so the per-symbol "omits … recert drift" message only fires when real signatures
ARE present but one specific symbol is missing (a true drift). `ir_id != pipeline_id` (Compile reopen re-numbers
ir_id independently), so deriving the IR dir from the pipeline dir name is unsound — the structural resolver keys
on `(kind, id, version)` and picks the latest by parsed canonical `(date, seq)`. Lesson (shared with the metric
address fix): host deterministic code silently depending on a leaf-authored non-required field surfaces as a
failure under LLM output variance; resolve ground truth structurally / host-authored.
