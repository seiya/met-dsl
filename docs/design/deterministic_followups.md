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
- **Remaining (the reason this stays open):** the wired path has never run end-to-end. A
  minimal 2-node dependency spec is now **authored** (`spec/component/demo/dep_chain/demo_dep_base`
  leaf + `demo_dep_top` which `use`s it; both registered in `spec/registry/spec_catalog.yaml`;
  closure resolution verified offline via `_resolve_dependency_closure` → `[demo_dep_base@0.1.0]`).
  The remaining operator-gated step is the **billed, long** run:
  `python3 tools/run_workflow.py spec/component/demo/dep_chain/demo_dep_top validate --llm claude --with-deps`
  confirmed to `meta=pass` + `aggregate_verdict=pass`. That is the only outstanding D1 work.

## D2 — `--with-deps` closure wires build artifacts via staging (resolved with D1)
Building node N now consumes its dependency nodes' built sources through
`_stage_dependency_sources` (each dep's `<dep>_model.f90` from its ready pipeline). Closed with
D1's implementation; E2E verification is the same outstanding item as D1.

## L (latent / low severity — fix opportunistically)
- **L1** Generated Makefile emits a harmless `make` warning `target '.' given more than once`
  for the `$(OBJDIR) $(BINDIR):` rule when `OBJDIR==BINDIR=="."` (local in-source `make`
  only; exit 0; not on Build/Validate which pass distinct dirs). Cosmetic; in the conductor
  template (`_write_makefile`) and prior LLM templates alike. Could split into two rules.
- **L2** The C1 scalar gate assumes the per-snapshot time index is always scalar; a future
  spec needing a vector per-file time dimension would need a carve-out
  (`validate_pipeline_semantics._validate_io_contract_file`).
- **L3** C2 escalation threshold is hardcoded `2` (`workflow_conductor.classify_failure`);
  tune if it proves too eager/lazy in practice.
- **L4** `_impl_is_leaf_node` disagrees with the YAML parser only for **invalid** YAML
  (tab-indented `direct_deps`) — benign/unreachable (fails compile), not worth fixing.
- **L5** A judge session/usage-limit now ends as a clean resumable `fail_closed`
  (`leaf_transport_error`) but still requires a **manual `--resume`** after the quota
  resets — no auto-retry/scheduling. By design; revisit if it becomes operationally painful.
- **L6** The dependency build (Model B) keys staged source filenames and Makefile object
  rules on the bare `spec_id_of(node_key)` (`_dependency_closure` / `_stage_dependency_sources`),
  dropping `kind` and `@version`. A closure containing two deps that share a `spec_id` but
  differ in version or kind (e.g. `component/foo@1.0.0` + `component/foo@2.0.0`, a diamond) would
  collide on `foo_model.f90` (last-write-wins stage + duplicate `$(OBJDIR)/foo_model.o` rules).
  The version-pinned *pipeline* path stays unambiguous (node_key carries `@version`); only the
  in-`$(OBJDIR)` basename is not. Not reachable by the minimal 2-node verification spec. Guard
  (or version-qualify the object/staged basenames) before allowing multi-version/diamond
  closures.

## T1 — testing gap (minor)
The transport+resume path is covered by two unit layers (conductor routing in
`test_workflow_conductor.py::TransportFailureTest`; runtime helper + completion exemption in
`test_orchestration_runtime.py::TransportOrphanCompletionTest`). There is **no single
end-to-end fault-injection integration test** driving the real conductor → runtime CLI →
completion check; the conductor→CLI seam is covered only by a smoke check. Add one if the
seam changes.
