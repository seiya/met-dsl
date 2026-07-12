---
name: spec-input-check
description: Use this before starting the workflow to check whether a spec's input set (`controlled_spec.md` / `deps.yaml` / `tests.md`) is sufficient and self-consistent to run `Spec -> Compile -> Generate -> Build -> Validate`. It detects missing required items, cross-file contradictions, ambiguous statements, and unresolvable dependencies, and reports them as proposals. It never modifies the spec.
---

# Spec Input Check

## Purpose
Inspect the input set of a single `spec` (`controlled_spec.md`, `deps.yaml`, `tests.md`) and report whether the input is sufficient and self-consistent to start the core workflow, before any workflow phase runs. The output is a proposal that points out problems; this skill does not fix, complete, or rewrite the spec.

This skill is a pre-`Compile` advisory check. It is not a workflow phase, does not run inside the `orchestration agent`, and produces no workflow artifact (`spec.ir.yaml` / `*_meta.json` / `verdict.json`). It applies the principle "treat a shortage as an error and forbid implicit completion" (`docs/CONTROLLED_SPEC.md`) at the input boundary, so that a deficiency is surfaced before it stops a phase mid-run.

## Scope
- the three input files of one target `spec` directory:
  - `spec/<spec_kind>/<domain>/<family>/<spec_id>/controlled_spec.md`
  - `spec/<spec_kind>/<domain>/<family>/<spec_id>/deps.yaml`
  - `spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md`
- the registry `spec/registry/spec_catalog.yaml`, read-only, for registration and dependency-resolution checks.
- the `deps.yaml` of each declared dependency `spec`, read-only, for resolvability checks.

Out of scope:
- modifying, completing, or reformatting any file (proposal only).
- judging physical / scientific validity (that is delegated to the `Validate` execution result).
- the optional flows `Tune` / `Promote`.

## Canonical sources for the rules
This skill does not restate the spec format. The judgment rules are owned by:
- `docs/CONTROLLED_SPEC.md` — required meta and required sections of `controlled_spec.md` per `spec_kind`, and the ambiguity-elimination rules.
- `docs/TESTS.md` — required meta, required sections, and coverage rules of `tests.md` per `spec_kind`.
- `docs/SPEC.md` — `spec` hierarchy, naming rules (including the `spec_id` length bound), `deps.yaml` declaration rules, and registry consistency.
- `docs/GLOSSARY.md` — the allowed `domain` / `family` classification vocabulary, and the `deps.yaml` declaration vocabulary (`component_id` / `profile_id` / `infrastructure_id`).
- `docs/workflow/phases/phase_01_compile.md` — the node-**identity** preconditions `Compile` cannot repair by re-authoring, so they must be caught here: `spec_id` ≤ 55 characters, and **exactly one** `infrastructure` direct dependency. Also the `infrastructure` §5 / §5.1 public-API pin.

When a check below and a canonical source disagree, the canonical source wins; report the divergence rather than guessing.

## Input
- The target is given as a `spec_ref`: either a spec directory path (`spec/<spec_kind>/<domain>/<family>/<spec_id>/`), a path to one of its files, or a bare `spec_id` that is resolved against `spec/registry/spec_catalog.yaml`.
- When the `spec_ref` cannot be resolved to exactly one spec directory, stop and report that as the first finding (do not proceed to other checks against a guessed directory).

## Requirements (checks to perform)
Group findings by severity:
- `blocker` — the workflow cannot start or a phase will stop with `fail` (missing file, missing required meta/section, cross-file contradiction, unresolvable dependency).
- `warning` — the input is likely to cause a downstream `fail` or an ambiguous result (subjective wording, placeholder, missing unit/threshold, coverage gap).
- `info` — a non-blocking observation worth the author's attention.

### A. File presence and structure
1. All three files (`controlled_spec.md`, `deps.yaml`, `tests.md`) exist at the resolved spec directory. A missing file is a `blocker`.
2. `deps.yaml` parses as YAML; `controlled_spec.md` / `tests.md` parse as Markdown with a readable meta block.
3. Exactly one `tests.md` exists for the spec (`docs/SPEC.md`: only 1 file per spec).

### B. Meta information
1. `controlled_spec.md §0` states all of `spec_id`, `spec_version`, `status`, `spec_kind`, `domain`, `family`. A missing field is a `blocker`.
2. `deps.yaml` states `spec_id` and `spec_kind`.
3. `tests.md §0` states `test_profile_id`, `test_profile_version`, `status`, and the `spec_ref` fields (`spec_kind`, `spec_id`, `spec_version`, `controlled_spec_path`).
4. `spec_id` matches the form `^[a-z][a-z0-9_]{2,63}$` **and is at most 55 characters** (`docs/SPEC.md` req. 4). An over-length `spec_id` is a `blocker`, and one worth reporting first: the bound is enforced at spec-input, so the workflow rejects the target — and every member of a `--with-deps` closure — before any phase runs, and no re-authoring of the `IR` or the source can repair it. Only a rename can, which also touches the directory, `spec_catalog.yaml`, and every dependent's `deps.yaml`. For a `component` spec, also check the recommended form `<domain>_<family>_<operator>_<dim>d_<scheme>` (recommendation → `info`, not a `blocker`).
5. `spec_kind` is one of `problem` / `component` / `profile` / `infrastructure` (`infrastructure` = the R1 harness node kind).
6. `domain` / `family` match the classification vocabulary in `docs/GLOSSARY.md`.

### C. Cross-file consistency (contradictions)
1. `spec_id` is identical across `controlled_spec.md §0`, `deps.yaml`, `tests.md` (`spec_ref.spec_id`).
2. `spec_kind` is identical across the three files.
3. `tests.md spec_ref.spec_version` equals `controlled_spec.md §0 spec_version`. A mismatch is a `blocker` (the test profile targets a different spec version).
4. `tests.md spec_ref.controlled_spec_path` resolves to the actual `controlled_spec.md` of this spec directory.
5. `spec/registry/spec_catalog.yaml` registers this spec, and its `spec_kind` / `spec_version` / `domain` / `family` / `controlled_spec_path` / `tests_path` / `deps_path` agree with the files. An unregistered spec, or any field mismatch, is a `blocker` (`docs/SPEC.md`: unregistered dependencies are not allowed).
6. The directory path encodes the same `spec_kind` / `domain` / `family` / `spec_id` as the meta declares.

### D. controlled_spec.md required sections (per spec_kind)
Confirm every required section for the declared `spec_kind` exists and is non-empty, per `docs/CONTROLLED_SPEC.md`:
- `problem`: sections 1–10 (Problem definition; Variables and coordinates; Domain and boundary-condition types; Dependent `component` and adopted `profile`; Integration algorithm; Model parameters and runtime input contract; Prohibitions; Traceability; tests reference; AD preparation information).
- `component`: sections 1–9 (Responsibility and scope; input/output contract; Operation definition; Failure conditions and constraints; Public API and compatibility; Prohibitions; Traceability; tests reference; AD preparation information).
- `profile`: sections 1–6 (Target `component` and compatibility range; Selection rules; Parameter constraints; Fallback rules; Traceability; tests reference).
- `infrastructure` (R1 harness): sections 1–8 — the `component` shape minus section 9 (AD preparation information), since a harness carries no physics: Responsibility and scope; input/output contract (including the `diagnostics.json` / `perf.json` / `raw/*` shapes it emits); Operation definition (the `<spec_id>__*` operations physics-node runners call); Failure conditions and constraints; Public API and compatibility; Prohibitions; Traceability; tests reference.

A missing required section is a `blocker`. A section present but empty or a stub is a `warning`.

The **section numbers are load-bearing for an `infrastructure` spec** — the `Compile` gate reads the published surface out of `## 5.` and `### 5.1` by number — so check them literally, not just by title:
- `## 5.` (Public API and compatibility) lists the published `operation_id`s **exhaustively** ("the published `operation_id`s are exactly: ...") and the published derived types, each as a backtick span carrying the `<spec_id>__` prefix. `Compile` pins the `IR`'s `public_api` to this list by set equality, so a helper emitter that no test exercises but a consuming runner still calls must be listed here. An operation named in section 3 but absent from section 5 (or the reverse) is a `blocker`.
- `### 5.1` (Canonical interface block) exists as a subsection of section 5 and contains **exactly one** fenced code block, giving every published type and operation signature (argument names, order, types, ranks, `intent`s, `result` names, derived-type component layouts) plus the module-level `parameter` declarations the signatures reference. A missing `### 5.1`, a missing fence, or more than one fence inside `### 5.1` is a `blocker` — each fails `Compile` closed.
- The symbol set of `### 5.1` equals the name lists of `## 5.`. A mismatch is a `blocker`.
- Section 5.1 is what carries the signatures into the `IR`, because `Generate.generate` cannot read `controlled_spec.md`; treat a signature stated only in prose (section 3) and absent from the `### 5.1` fence as a `blocker`, not a stylistic gap.
- Section 2 states which record component carries the `case_id`, since the `metrics_basis.json` index is keyed by (`test_id`, `case_id`). Absence is a `warning`.

### E. tests.md required content (per spec_kind)
1. The required sections of `docs/TESTS.md §Description format` (0–8) exist; an unnecessary section states `N/A` with a reason rather than being omitted.
2. At least one `L0` test is defined (`docs/SPEC.md` req. 13, `docs/TESTS.md`). Absence is a `blocker`.
3. Each test's judgment condition is stated per `node_key` and does not implicitly reference a dependency `node`'s state.
4. Coverage rules per `spec_kind`:
   - `component`: each published `operation` has at least one normal case and one guard case (`fail` / `xfail`).
   - `profile`: tests cover the selection-establishment, exclusion, and fallback-prohibition conditions, plus a guard case for out-of-compatibility input.
   - `problem`: execution control, case expansion, judgment expressions, and pass/fail aggregation rules are defined; a non-applicable validity item defines `N/A` and `reason_na`.
   - `infrastructure` (R1 harness): each published harness operation has at least one normal case and one guard case (`fail` / `xfail`) — e.g. numeric-round-trip, boolean-literal, case fan-out → per-case snapshot naming, missing-`--cases` guard, per-test index completeness.
5. Every `xfail` defines both `xfail_condition` and `pass_when`.
6. Where `tests.md` names case identifiers literally (rather than deriving them from a sweep rule), each must survive the `Compile` case gate: at most 64 characters, drawn from `[A-Za-z0-9._-]`, containing no `..`, and pairwise distinct (`docs/workflow/phases/phase_01_compile.md`). A `case_id` is concatenated into the per-case snapshot path and rendered as a Fortran `select case` label, so an over-long one is truncated until no label can ever match and every run aborts despite compiling cleanly. Report a violation as a `warning`, since `Compile` — not `tests.md` — fixes the final `case_id` set; state that the check applies only to the literally-named ids.

### F. deps.yaml and dependency resolvability
1. `deps.yaml` declares the `dependencies` block, whose keys are drawn from exactly `components` / `profiles` / `infrastructure`. `components` and `profiles` are **required in every `deps.yaml`**, an `infrastructure` spec's own included (there both are empty lists); empty lists are likewise valid for a leaf `component`. `infrastructure` is optional. Any other key under `dependencies` is a `blocker` — the closure build rejects the whole `deps.yaml` as malformed, so a typo like `infrastructures:` does not degrade to "no harness dependency", it stops the run.
2. A `problem` spec declares its dependent `component`s and its adopted `profile`(s) (`docs/SPEC.md` req. 10). A `problem` with an empty `components` list is a `blocker`.
3. Each declared `component_id` / `profile_id` / `infrastructure_id` carries a `version_constraint`.
4. Each declared dependency — `infrastructure` included — is registered in `spec/registry/spec_catalog.yaml` under the matching `spec_kind`, and its files exist on disk. An unregistered or missing dependency is a `blocker` (`docs/SPEC.md` req. 11).
5. Each dependency's catalog `spec_version` satisfies the declared `version_constraint`. A violation is a `blocker`. Check this for the `infrastructure` entry too: the harness publishes a `spec_version` per interface generation, and a dependent pinned below the certified harness version resolves against an interface that no longer exists.
6. **At most one `infrastructure` entry.** Declaring one promotes the node to an `M3c` node — its runner becomes host-rendered glue over the certified harness plumbing plus a leaf-authored `<spec_id>_checks` module — and declaring none leaves the node to author its own runner. Both are valid, so report zero as `info`, not a finding. But **more than one is a `blocker`**: a node with two `infrastructure` dependencies is not an `M3c` node, so its runner is silently never host-rendered — the failure is a quiet loss of the harness path, not an error message (`docs/workflow/phases/phase_01_compile.md`).
7. `controlled_spec.md §4` (for a `problem`) names the same `component_id`s / `profile_id`s that `deps.yaml` declares — neither file references an `id` the other omits. A discrepancy is a `blocker` (contradiction between the prose contract and the machine-readable dependency set).
8. For a `profile` spec, the `target component_id` in `controlled_spec.md §1` is declared in `deps.yaml`, and the adopted profile of any referencing `problem` is compatible with the components that `problem` declares (cross-spec consistency → report what is checkable from the given files; mark unverifiable items `info`).
9. No direct path reference / relative `import` is used in place of a `deps.yaml` declaration (`docs/SPEC.md` req. 9).

### G. Ambiguity and insufficiency (forbid implicit completion)
1. Flag subjective expressions that the spec format forbids: "appropriate", "sufficiently small", "as needed", "etc." used where a value or rule is required (`docs/CONTROLLED_SPEC.md §Ambiguity-elimination rules`). → `warning`.
2. Flag unresolved placeholders: `TBD`, `TODO`, `<...>`, `???`, blank required values. → `blocker` when in a required field, else `warning`.
3. Flag parameters stated without a unit, a numeric value, or an allowed range where the format requires them (model parameters, thresholds, default values). → `warning`.
4. Flag a judgment threshold referenced in `tests.md` that is not numerically defined. → `blocker`.
5. Flag any place that offers multiple options without a stated selection rule (priority or fixed value). → `warning`.

## Operations Rules
1. Read-only operation. Use the `Read` tool, `grep`, and YAML/Markdown parsing for inspection only. Do not call `Edit` / `Write` on any spec file, registry, or workspace artifact. Do not run `tools/run_workflow.py` or any workflow phase.
2. Resolve the target spec directory first (see Input). If resolution is ambiguous or fails, report only that and stop.
3. Run the checks A–G in order. Continue through all groups even after the first finding, so the author gets a complete list in one pass.
4. This check is heuristic for the Markdown-prose parts (sections D, E, G): a section or rule expressed in unusual wording may be miscounted. State assumptions, and prefer reporting a `warning` "could not confirm X" over silently passing or silently failing.
5. Do not invent or assume default values for a missing item. A missing required item is reported as a deficiency, never completed.
6. The output is advisory. Do not claim the spec "passed the workflow" or "is ready to merge"; state only that the input check found / did not find the listed problems.

## Output format
Produce a single report with:
1. A one-line summary: the resolved spec (`spec_id`, `spec_kind`, path) and the count of findings by severity.
2. A `blocker` list, then a `warning` list, then an `info` list. Each finding states: the file and location (section / line / key), what is missing or contradictory, the canonical-source rule it relates to, and a concrete proposed fix for the author to apply.
3. A closing note that this is a proposal only and no file was modified.

When no findings exist, state that the input set passed all A–G checks and is structurally sufficient to start the workflow, while noting that physical validity is still decided at `Validate`.

## Decision Criteria
- A `blocker` count of zero means the input set is structurally sufficient and self-consistent to start `Compile`; it does not assert physical correctness.
- Any `blocker` means the workflow should not be started until the author resolves it.
- Every finding cites the file location and the canonical-source rule, so the author can verify the basis independently.
- No spec, registry, or workspace file was written or modified during the check.
