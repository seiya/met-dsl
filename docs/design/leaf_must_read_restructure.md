# Leaf must-read restructuring plan

Status: **implemented** — 2026-06-26 (full `tools/tests` pytest green, the one
pre-existing env-dependent `test_spawn_leaf_wraps_in_bwrap` codex-hooks failure
aside; billed E2E attempt-1 check pending, see §9).

Implementation notes (what shipped vs the proposal):
- `WORKFLOW_CORE.md` dropped from **all** LLM-leaf must-reads; its leaf-actionable
  invariants + `<stage>_meta.json` keys + the command_log placement one-liner now
  live in `AGENT_CONTRACT.md` (new "Workflow behavioral invariants" / "Stage meta
  keys" / "MCP command_log placement" sections).
- `RUNNER_OUTPUT_CONTRACT.md` created and made the must-read for
  `generate.generate` / `generate.verify` / `validate.judge`;
  `generate.generate` dropped `phase_03` + `MCP_COMMAND_LOG_PLACEMENT` +
  `PERFORMANCE_DIAGNOSTICS` (the Makefile contract stays inline in the
  generate-generate SKILL).
- **Deviation honored from the §4 reconciliation:** `Compile` still force-reads
  `phase_01` (its IR schema is the contract the compile SKILL defers to). All
  other phase docs are not force-read.
- Key enabler confirmed in code: the access policy grants the **whole `docs/`
  tree** as readable (`orchestration_runtime.py:build_access_policy_payload`,
  `allowed_read_roots` includes `docs/`), so every demoted doc stays reachable —
  dropping it from must-read removes forced reading only, never reachability.
- **Contract-doc policy unified to one helper (Option C).** The leaf contract-doc
  policy is defined once in `orchestration_runtime.leaf_contract_doc_refs(step)`
  and called by BOTH must-read assembly paths: the conductor's
  `build_launch_request` (full launch intent) and record-launch's
  `_workflow_contract_refs_for_launch` (security-boundary normalization). The two
  *layers* are intentionally kept (conductor knows node-specific spec paths;
  record-launch guarantees the contract + verify refs regardless of caller, fail-
  closed) — only the *policy derivation* is shared, so a policy change is a one-
  function edit. `test_reproduces_every_real_substep_payload` (conductor output ==
  real launched payload) structurally guards against the two paths drifting.
  No circular import: `orchestration_runtime` is the lower layer; the conductor
  lazy-imports the helper.
- Touched (final, post Option-C unification): `workflow_conductor.py`
  (removed `_DOC_CORE` / `_PHASE_DOC`; `build_launch_request` now calls
  `leaf_contract_doc_refs` and appends only node-specific spec artifacts),
  `orchestration_runtime.py` (new `leaf_contract_doc_refs` single-source policy;
  `_workflow_contract_refs_for_launch` delegates to it; `RUNNER_OUTPUT_CONTRACT_REF`;
  `WORKFLOW_PHASE_DOC_BY_STEP` retained, only `compile` consumed; dead
  `WORKFLOW_CORE_REF` removed), `AGENT_CONTRACT.md`, `RUNNER_OUTPUT_CONTRACT.md` (new),
  `AGENT_SKILLS.md`, `WORKFLOW_CORE.md` §scope, pointer banners in
  `phase_02`/`phase_04`/`PERF`, the generate-generate / generate-verify /
  validate-judge SKILL citations (+ inlined C003 placement + 63-char limit in
  generate-generate), conductor fixtures, `LeafContractDocPolicyTests`, and
  doc-size ceilings.
- **Launch-prompt template reconciled** (`skills/workflow-orchestration/references/launch_prompts.md`):
  the static "MCP command-log & program-output placement" boilerplate line used to
  instruct Generate leaves to *read* `MCP_COMMAND_LOG_PLACEMENT.md` — which would
  re-force the demoted doc at render time regardless of `skill_must_read_refs`
  (Codex review caught this; the must-read code change alone is insufficient). It
  now states the actionable minimum inline, points runner output at
  `RUNNER_OUTPUT_CONTRACT.md` (force-read) and the command_log rule at
  `AGENT_CONTRACT.md`, demotes the MCP doc to optional reference, and is
  **phase-scoped** (clauses prefixed "If your substep …") so `Compile.*` /
  `Validate.judge` leaves are not misdirected about contracts they don't have.
- **Doc-size ceiling policy** (`ChildContextDocSizeTests._CEILINGS`): the size
  guard now applies **only to docs the LLM leaf force-reads** (AGENT_CONTRACT,
  RUNNER_OUTPUT_CONTRACT, phase_01_compile) — those land in cold-start context so
  their size is a real cost. Docs that left the leaf must-read set (WORKFLOW_CORE,
  phase_02/03/04, PERFORMANCE_DIAGNOSTICS, MCP_COMMAND_LOG_PLACEMENT) are no longer
  ceiling-guarded (their size no longer affects leaf context). The 5 per-substep
  LLM SKILLs are also leaf-read and are now ceiling-guarded too; Build /
  Validate.execute are deterministic (no SKILL) so are not guarded. The exact
  ceiling values are the single source of truth in
  `ChildContextDocSizeTests._CEILINGS` (not duplicated here, to avoid drift).

---

Original proposal (pre-implementation) — 2026-06-26.
Goal: cut the cold-start reading load each LLM leaf pays, without losing any
accuracy-bearing rule, by (1) consolidating the "every-leaf" content into one
contract doc, (2) removing operator/orchestration-only docs from the leaf
must-read set, and (3) eliminating the rule duplication that today spans four
layers (launch prompt ↔ AGENT_CONTRACT ↔ WORKFLOW_CORE invariants ↔ SKILL
boilerplate).

This plan is the canonical reference for the change. Implementation happens
only after review.

## 1. Why (measured)

A clean, retry-free run of the demo dependency chain (orch
`…053040Z_b05abe39` base PASS 18.4 min, `…053040Z_9afac161` top 20.1 min):

- ~97% of wall time is the leaf `claude -p` execution; orchestration overhead is
  negligible. `build` / `validate.execute` are already deterministic
  (conductor in-process, ~8 s each) — nothing to win there.
- Inside a leaf (`generate.generate`, 390 s, from its transcript): ~48 s is
  cold-start orientation — **14 sequential `Read` round-trips** of must-read
  docs — before any authoring; the rest is irreducible code generation
  (single Write block +168 s) plus a lint repair loop.
- The `generate.generate` leaf is told to read **7 canonical docs in full**
  (~790 lines / ~100 KB) every cold start. Audit finding: **~half is content
  the leaf does not act on** (orchestration/operator material or verbatim
  duplication).

The win targeted here is the orientation/reading slice (~45 s/leaf × 5 LLM
leaves ≈ 3–4 min/node, ~20%), at **zero accuracy risk**, by not forcing the
leaf to read what it does not use. Token/context pressure drops correspondingly.

## 2. Current leaf must-read composition (source: `workflow_conductor.py:build_launch_request`)

`_DOC_CORE = (WORKFLOW_CORE.md, AGENT_CONTRACT.md)` + `skills/<skill>/SKILL.md`
+ `_PHASE_DOC[step]` are forced on every LLM leaf, plus per-substep additions.

| substep | canonical docs forced (excl. node spec/ir/tests) |
|---|---|
| compile.generate | WORKFLOW_CORE, AGENT_CONTRACT, SKILL, phase_01 |
| compile.verify | WORKFLOW_CORE, AGENT_CONTRACT, SKILL, phase_01 |
| generate.generate | WORKFLOW_CORE, AGENT_CONTRACT, SKILL, phase_02, **phase_03**, **MCP_COMMAND_LOG_PLACEMENT**, **PERFORMANCE_DIAGNOSTICS** |
| generate.verify | WORKFLOW_CORE, AGENT_CONTRACT, SKILL, phase_02 |
| validate.judge | WORKFLOW_CORE, AGENT_CONTRACT, SKILL, phase_04 |

(`build`, `validate.execute` are deterministic — no leaf, no must-read.)

## 3. Audit: what is actually leaf-actionable

- **WORKFLOW_CORE.md (315 lines)** — only invariants 1–9 + 31–33 (behavioral
  norms), the `<stage>_meta.json` key rules (§4 / lines 92–100), and the leaf's
  own phase I/O are leaf-actionable (~40 lines). The remaining ~275 lines are
  orchestration/operator canonical: full artifact tree, ID-minting rules,
  preflight/launch, completion criteria, CI, re-execution, dependency-coverage,
  write-scope baseline mechanics. The leaf *reads* but does not *act on* them.
- **AGENT_CONTRACT.md (81 lines)** — genuinely the every-leaf contract; nearly
  all leaf-actionable, but internally restates the write/gate/tmp rules 3–4×.
- **Phase docs** — `phase_03_build` is ~85% Build-orchestration (MCP calls,
  retry classification, `binary_meta.json` routing); `generate.generate` needs
  only its Makefile contract (~8 lines). `phase_04_validate` has a 42-line
  retry **decision table** that is orchestration-facing, not judge-facing. Every
  phase doc re-states the `<stage>_meta.json` keys already in WORKFLOW_CORE.
- **The runner output contract** (`diagnostics.json` / `perf.json` / `raw/` /
  per-case snapshot naming / Fortran JSON-descriptor rules) is **defined twice**
  — in `phase_02_generate` and `phase_04_validate` — and is the binding subset
  of `PERFORMANCE_DIAGNOSTICS.md` §2/§6 and of `MCP_COMMAND_LOG_PLACEMENT.md`.
- **SKILLs (5 files, 343 lines total)** — ~28% is boilerplate repeated 5×
  (direct-Write rules, workspace-root rule, meta keys, canonical-input rule,
  dev-mode fail), each block duplicating AGENT_CONTRACT/WORKFLOW_CORE. This
  already violates AGENT_SKILLS.md's own rule ("do not duplicate the phase's
  I/O contract / artifact format / numerical canonical requirements" in SKILL).

## 4. Constraints that shape the target (discovered, must be honored)

1. **AGENT_SKILLS.md responsibility-decision flow (§1–6)** deliberately
   separates *contract* (validity/audit/reproducibility → `docs/workflow/`) from
   *procedure* (tool-call order, regeneration, on-failure → `SKILL.md`) from
   *common child contract* (`AGENT_CONTRACT.md`). The restructure must **relocate
   boundaries, not blur them**, and update AGENT_SKILLS in lockstep.
2. **Build and Validate.execute have no SKILL** (conductor in-process); their
   canonical contract *is* `phase_03_build.md` / `phase_04_validate.md`. These
   phase docs therefore **cannot be deleted** — they stay as
   orchestration/deterministic canonical, merely dropped from the LLM-leaf
   must-read set.
3. **Multi-backend**: Codex / Gemini / Claude all read the same SKILL as the
   procedure source — keep SKILL backend-independent.
4. **Launch-prompt guard** (`_required_launch_prompt_constraint_lines` in
   `orchestration_runtime.py`) requires a fixed small set of
   security-critical lines to stay **inline in the prompt** (apply_patch ban,
   `output_manifests/`, `/capabilities/`, the capability_token fail rule, direct
   `Edit`/`Write`). This set is small and unaffected; the rest of the prompt's
   re-inlined contract prose can shrink to a pointer.
5. **must_read vs read_manifest**: dropping a doc from *must_read* (forced
   reading — the time cost) does **not** require removing it from
   *read_manifest* (allowed reading). Keep demoted docs in the read_manifest so
   a leaf can still gate-read the full contract if a SKILL slice proves
   insufficient. This de-risks accuracy: nothing becomes unreachable, it just
   stops being force-read.

## 5. Target architecture

### 5.1 Leaf-facing canonical docs (the only forced reads)

- **`docs/AGENT_CONTRACT.md` → the single every-leaf contract.** Absorb the
  leaf-actionable slice of WORKFLOW_CORE (behavioral invariants 1–9 / 31–33 and
  the `<stage>_meta.json` key rules) and internally de-duplicate its own
  write/gate/tmp restatements. This is the one doc every leaf reads.
  (Filename kept to avoid rename churn across guards/tests/AGENT_SKILLS; an
  optional rename to `LEAF_CONTRACT.md` is noted in §8.)
- **`skills/<skill>/SKILL.md` → the single per-substep leaf doc.** Each LLM
  substep's SKILL absorbs the **leaf-actionable I/O slice** of its phase doc
  (e.g. `generate-generate` absorbs phase_02's generate contract + phase_03's
  Makefile contract). SKILL boilerplate that duplicates AGENT_CONTRACT is
  stripped and replaced by a one-line pointer.
- **`docs/workflow/RUNNER_OUTPUT_CONTRACT.md` (new, shared).** The runner output
  contract defined once: `diagnostics.json` / `perf.json` (PERF §2 fields) /
  `raw/` evidence / per-case snapshot naming / Fortran JSON-descriptor rules
  (PERF §6) / the "runner must not write verdict/aggregate/summary/trial_meta"
  rule. Read only by the substeps that need it (generate.generate,
  generate.verify, validate.judge). One file, zero duplication — replaces the
  twin definitions in phase_02/phase_04 and the leaf-binding subset of PERF +
  MCP docs.

### 5.2 Orchestration/operator canonical (retained, dropped from leaf must-read)

- `WORKFLOW_CORE.md` — workflow contract; referenced by the conductor and by
  AGENT_SKILLS; not force-read by leaves (its leaf slice now lives in
  AGENT_CONTRACT).
- `phase_01..04` — orchestration + deterministic-step contract; phase_03/04 stay
  the canonical for the no-SKILL deterministic steps. Their leaf-actionable
  slices have moved into the SKILLs.
- `PERFORMANCE_DIAGNOSTICS.md`, `MCP_COMMAND_LOG_PLACEMENT.md` — full reference
  canonical; their leaf-binding subset now lives in RUNNER_OUTPUT_CONTRACT.

### 5.3 Resulting leaf must-read (canonical docs; node spec/ir/tests unchanged)

| substep | canonical docs forced | count (was) |
|---|---|---|
| compile.generate | AGENT_CONTRACT, SKILL | 2 (was 4) |
| compile.verify | AGENT_CONTRACT, SKILL | 2 (was 4) |
| generate.generate | AGENT_CONTRACT, SKILL, RUNNER_OUTPUT_CONTRACT | 3 (was 7) |
| generate.verify | AGENT_CONTRACT, SKILL, RUNNER_OUTPUT_CONTRACT | 3 (was 4) |
| validate.judge | AGENT_CONTRACT, SKILL, RUNNER_OUTPUT_CONTRACT | 3 (was 4) |

## 6. Content move-map (line-level intent; exact lines confirmed at edit time)

| content | from | to |
|---|---|---|
| behavioral invariants 1–9, 31–33 | WORKFLOW_CORE §3 | AGENT_CONTRACT (Common contract) |
| `<stage>_meta.json` common + per-phase keys | WORKFLOW_CORE §4 (92–100) + each phase doc's "required keys" | AGENT_CONTRACT (one meta-keys section) |
| write/gate/tmp restatements (deduped) | AGENT_CONTRACT 22–35, 43–80 (internal repeats) | AGENT_CONTRACT (single statement each) |
| SKILL boilerplate (write/storage/meta/canonical-input/dev-mode) | each of 5 SKILLs | deleted; pointer to AGENT_CONTRACT |
| phase_02 generate I/O + code-gen contract slice | phase_02 §2-1 | SKILL generate-generate |
| phase_02 verify checklist G1–G7 | phase_02 §2-2 | SKILL generate-verify |
| Makefile contract (BIN ?=, object rules, OBJDIR/BINDIR/RUNDIR, no-relink) | phase_03 (Makefile parts only) | SKILL generate-generate |
| phase_04 judge criteria slice | phase_04 §4-2 | SKILL validate-judge |
| runner output contract (diagnostics/perf/raw/snapshot/JSON-descriptor) | phase_02 §2-1 + phase_04 §4-1 + PERF §2/§6 + MCP leaf subset | RUNNER_OUTPUT_CONTRACT.md (new) |
| command_log placement (generic leaf rule) | MCP_COMMAND_LOG_PLACEMENT | AGENT_CONTRACT one-liner (exact path already in manifest/task-card) |
| phase_01 compile schema slice | phase_01 §3/§5 | SKILL compile-generate; verify invariants V1–V7 → SKILL compile-verify |

Phase docs keep: orchestration retry/decision tables, deterministic-step
contracts (phase_03 MCP/binary_meta, phase_04 execute), design rationale.

## 7. Code / test / convention changes (implementation checklist)

1. `tools/workflow_conductor.py`
   - `_DOC_CORE = (AGENT_CONTRACT,)` — drop WORKFLOW_CORE.
   - Drop `_PHASE_DOC[step]` from leaf `must_read` (phase docs no longer
     force-read by LLM leaves). Keep them in the **read_manifest** (allowed).
   - `generate.generate`: replace phase_03 + MCP + PERF must-reads with
     `RUNNER_OUTPUT_CONTRACT.md`; `generate.verify` / `validate.judge`: add
     `RUNNER_OUTPUT_CONTRACT.md`.
2. `tools/tests/test_workflow_conductor.py` (+ any
   `test_orchestration_runtime` / `test_validate_pipeline_semantics` that assert
   must_read composition) — update expected must-read sets.
3. `skills/workflow-orchestration/references/launch_prompts.md` — shrink the
   re-inlined contract prose to a pointer to AGENT_CONTRACT, keeping the
   guard-required inline constraint lines (§4.4) intact.
4. `docs/AGENT_SKILLS.md` — update the responsibility-decision flow and the
   "don't duplicate I/O contract in SKILL" rule to the new boundary: SKILL is
   the leaf-facing single source for its substep's I/O slice; `phase_*.md` is
   the orchestration/deterministic contract; add RUNNER_OUTPUT_CONTRACT and the
   "WORKFLOW_CORE/phase docs are not LLM-leaf must-reads" note.
5. `docs/workflow/WORKFLOW_CORE.md` "Agent reference scope" (§ lines 298–301) —
   update to reflect the new leaf must-read set.
6. Read-manifest assembly (`build_skill_must_read_refs` in
   `orchestration_runtime.py`) — verify demoted docs remain readable
   (read_manifest) though no longer must-read.
7. Grep for stale cross-references to moved content across `docs/`, `skills/`,
   `CLAUDE.md`, `AGENTS.md`.

## 8. Open options (decide during implementation)

- **Rename** `AGENT_CONTRACT.md` → `LEAF_CONTRACT.md` for clarity vs. keep the
  name to avoid churn across the launch-prompt guard, AGENT_SKILLS, tests, and
  read_manifest logic. Default: **keep the name**.
- **RUNNER_OUTPUT_CONTRACT** as a standalone file (chosen: no duplication, only
  the 3 substeps that need it read it) vs. folding it into AGENT_CONTRACT (every
  leaf incl. compile over-reads ~60 runner-specific lines). Default: **standalone**.

## 9. Accuracy-preservation rules for the edit

- **Move, never drop.** Every accuracy-bearing rule lands in exactly one new
  home; nothing is deleted outright. Demoted docs stay in read_manifest.
- After edits, diff the **union** of leaf-reachable rules (must_read +
  read_manifest) before/after to prove no rule became unreachable.
- Re-run the full suite (`validate_workspace_root` + `validate_pipeline_semantics`
  + pytest) and one billed `--with-deps` E2E to confirm leaves still pass
  attempt-1 with the slimmer reads (the real accuracy check).

## 10. Expected outcome

- `generate.generate` forced reads: 7 docs (~100 KB) → 3 (~AGENT_CONTRACT +
  slim SKILL + RUNNER_OUTPUT_CONTRACT). Orientation `Read` round-trips roughly
  halve.
- No content duplication across launch prompt / AGENT_CONTRACT / WORKFLOW_CORE /
  SKILL.
- Estimated ~3–4 min/node wall-time reduction, zero accuracy-bearing rule lost.
