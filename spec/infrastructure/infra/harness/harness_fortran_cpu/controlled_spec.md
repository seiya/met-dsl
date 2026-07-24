# Controlled Spec: Fortran/CPU runner harness (infrastructure spec)

## 0. Meta information
- `spec_id`: `harness_fortran_cpu`
- `spec_version`: `0.7.0`
- `status`: `controlled_draft`
- `spec_kind`: `infrastructure`
- `domain`: `infra`
- `family`: `harness`

## 1. Responsibility and scope
This `infrastructure` node (R1 harness) is responsible for the shared **runner plumbing** that every Fortran/CPU physics node's runner is built against: argv / `--cases` parsing, the case-set loop driver, the JSON emission machinery (numeric / integer / boolean / rank-1..4 real-array tokens), and the standard runner-output writers (`raw/state_snapshots/<case_id>.json`, `raw/metrics_basis.json`, `diagnostics.json`, `perf.json`). It carries **no physics**: the per-case kernel and the per-test check logic are supplied by the consuming physics node (a `case_run` / `checks_compute` callback in the physics `*_checks.f90`), never here. It targets `(language=fortran, hardware=cpu)`; a different `(language, hardware)` target is a separate harness node.

The node's own generated code is a `harness_fortran_cpu_model.f90` publishing the plumbing operations plus a self-test `harness_fortran_cpu_runner.f90` that exercises them and emits the standard runner outputs (so the harness is verified through the exact same Compile→Generate→Build→Validate path as any node; it is self-hosting — the self-test writes its evidence using its own emitters).

The published surface is a **binding, signature-level contract**: §5.1 gives the canonical language-neutral structured interface block (every public type and every operation signature) in a machine-readable fenced code block, from which the language backend renders the Fortran surface. The generated `harness_fortran_cpu_model.f90` must publish exactly those signatures; a consuming physics node's host-rendered runner glue is written against them and holds no serialization knowledge of its own (the JSON envelope assembly and the verdict fold live only inside these certified operations).

## 2. input/output contract
Input (to the self-test runner): the standard runner argv `--cases <spec.ir.yaml> <case_id>...` — the spec path is taken positionally and need not be read; the trailing tokens are the `case_id`s to run, one per `case.test_case_set[]`. The main program marshals the process argv into a token array and calls `harness_fortran_cpu__parse_cases` on it. Each `case_id` selects, by dispatching on the `case_id`, the plumbing aspect that case verifies. Every `case_id` is also the `test_id` of the single-target test that names it, but the converse does not hold: a **multi-target** test ranges over several existing cases and declares no case of its own (`l0_multi_case_evidence_pass` in `tests.md`). A missing `--cases` flag (no cases) makes `__parse_cases` return `ok = false` (the input guard); the guard case verifies this by calling `__parse_cases` on a length-0 token array.

Output artifacts (produced by the writers, into the run node dir relative to `cwd=RUNDIR`):
- **`diagnostics.json`** — a JSON object with a top-level `checks` object holding one entry per `io_contract.diagnostics_contract.checks[].id` (each `{ "status": "pass"|"fail" }`), a top-level `verdict` object `{ "overall": "pass"|"fail", "failed_checks": [<check_id>...] }`, and a `per_case` map `{ <case_id>: { "checks": {...}, "verdict": { "overall", "failed_checks" }, "metrics": {...} } }` giving each case's own result. The assembly is done entirely inside `__write_diagnostics` from the caller-supplied per-case result records — the harness performs the fold, the caller supplies only the honest per-case check/metric data and each case's `expected_xfail` flag (§3). Top-level aggregation rule: `verdict.overall == fail` iff some case with `expected_xfail == false` has a failing per-case verdict; a per-case failure of a case with `expected_xfail == true` (the `input_guard` firing on the guard case) is EXCLUDED from the top-level `failed_checks`, so a run where the only failure is the expected guard reports top-level `{ "overall": "pass", "failed_checks": [] }` with `checks.input_guard.status == pass` (the guard behaved as expected). The per-case `input_guard` failure is confined to `per_case.<guard_case>`. The per-case `metrics` object holds one leaf per `h_metric` the caller supplied for that case (dotted-address key ⇒ numeric value; a `is_na` metric is written as `"<address>": null` plus a sibling `"<address>_reason_na": "<reason>"`). Exactly one self-test case supplies metrics: `l0_metric_leaf_pass` supplies the two sentinel `h_metric` records of §3, so its per-case object is `"metrics": { "selftest.metric_leaf": 0.25, "selftest.metric_na": null, "selftest.metric_na_reason_na": "not_computed" }` (the numeric value is written as the round-trip-lossless token of the serialization rule below, abbreviated here for readability); every other case supplies a length-0 `metrics` array, so its per-case `metrics` is `{}`.
- **`perf.json`** — one object with `case_id`, `target` (`"cpu"`), `walltime_sec`, `steps`, `cells_updated`, `throughput_cells_per_sec` (`= cells_updated / walltime_sec`), a `parallelism` object (`mpi_ranks`, `threads_per_rank`, `gpu_devices`, `parallel_degree_total = mpi_ranks*threads_per_rank*max(gpu_devices,1)`), and `timestamp_utc` (ISO-8601).
- **`raw/state_snapshots/<case_id>.json`** — exactly one per case, named at runtime as `'raw/state_snapshots/'//trim(case_id)//'.json'` (never a hardcoded/sequential literal). Each holds every variable in *that case's* `io_contract.test_evidence_requirements.required_raw_variables` plus the declared scalar `time_variable` `t` (value `0.0`; the self-test has a single `steps=1` step). The snapshot state variables (declared in `snapshot_schema.json` with their `shape_expr`) are, per case:
  - `l0_numeric_roundtrip_pass`: `x_in` (rank-1, `[3]` — the sentinel reals), `x_out` (rank-1, `[3]` — the values re-parsed from `__emit_real`'s tokens), `max_abs_deviation` (scalar).
  - `l0_boolean_literal_pass`: `bool_match` (scalar, `1.0` iff a `true` and a `false` boolean emitted the exact literals `true`/`false`).
  - `l0_array_emit_pass`: `a1` (rank-1, `[2]`), `a2` (rank-2, `[2,2]`), `a3` (rank-3, `[2,2,2]`), `a4` (rank-4, `[2,2,2,2]`) — the inputs to `__emit_array_r1..r4` — and `max_abs_deviation` (scalar — max component deviation of the re-parsed arrays).
  - `l0_case_fanout_pass`: `case_index` (scalar — this case's ordinal in the run).
  - `l0_perf_derived_pass`: `throughput_residual` (scalar — `|throughput_cells_per_sec - cells_updated/walltime_sec|`).
  - `l0_metric_leaf_pass`: `metric_count` (scalar — the number of `h_metric` records this case supplied to `__write_diagnostics`, `2.0`).
  - `l0_missing_cases_xfail`: `guard_fired` (scalar, `1.0` when `__parse_cases` on a length-0 token array returned `ok = false`). A guard case still emits its snapshot, shape-valid.
  Each case's `required_raw_variables` is exactly its listed variables above; a variable is declared once in `snapshot_schema.json` and emitted only by the cases that require it.
- **`raw/metrics_basis.json`** — a `{ "per_test": [ <entry>, ... ] }` index holding only primary evidence (never a copy of `diagnostics.json`). Assembled inside `__write_metrics_basis` from the caller-supplied entry records (§3). There is exactly **one entry per (`test_id`, target `case_id`) pair**: a test's primary evidence is the evidence of every case its predicate ranges over, so a single-target test contributes one entry and a multi-target test one entry per targeted case. `(test_id, case_id)` is the unique entry key, and a single-target test carries its `case_id` too — there is no special case. Each entry is a **flat JSON object**: the `test_id` key, the `case_id` key, and every name in that test's `io_contract.test_evidence_requirements.required_raw_variables` as a **direct sibling key of `test_id`**, valued from that entry's own case. Wrapping the required variables under any nested object is forbidden — in particular under the literal key `values`, which is the Fortran component name of `harness_fortran_cpu__h_mb_entry` (§3.1) and never a JSON key. The `post_execute` gate rejects an entry that nests them under the unrecognized key `values` as `missing required_raw_variables`, and rejects an entry that omits `case_id`. One entry, with its numeric tokens abbreviated for readability (the serialization rule below governs their emitted form):

  ```json
  {
    "test_id": "l0_array_emit_pass",
    "case_id": "l0_array_emit_pass",
    "a1": [1.0, 2.0],
    "a2": [[1.0, 2.0], [3.0, 4.0]],
    "a3": [[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]],
    "a4": [[[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]], [[[9.0, 10.0], [11.0, 12.0]], [[13.0, 14.0], [15.0, 16.0]]]],
    "max_abs_deviation": 0.0
  }
  ```

  The wrapped form `{ "test_id": "l0_array_emit_pass", "values": { "a1": [1.0, 2.0], … } }` is rejected. The multi-target test `l0_multi_case_evidence_pass` contributes two entries, `{ "test_id": "l0_multi_case_evidence_pass", "case_id": "l0_array_emit_pass", "max_abs_deviation": 0.0 }` and the same with `"case_id": "l0_numeric_roundtrip_pass"`, so the self-test's index holds nine entries for its eight tests.

Numeric serialization follows the abstract runner-output contract: a real as a round-trip-lossless exponential token, an integer as its minimal decimal, a boolean as the literal token `true`/`false` — a truncating form (one that may drop a leading digit) and any language-specific boolean token are forbidden. The target-language realization (for Fortran: reals via `ES24.16E3` then `trim(adjustl())`, or a bounded `Fw.d`, never `F0`/`F0.d`; integers via `I0`; booleans by branching to the literal, never an `L`-family descriptor) is canonical in `docs/workflow/RUNNER_OUTPUT_CONTRACT.md §4`.

## 3. Operation definition
The published operations (all under module `harness_fortran_cpu_model`, prefix `harness_fortran_cpu__`) are the plumbing surface a physics-node runner reuses. The canonical machine-readable interface (every signature and public type, verbatim) is §5.1; the prose below states each operation's contract. The module declares two module-level integer parameters the signatures reference: `dp` (the double-precision real kind token; §5.1 value `float64` = IEEE-754 binary64) and `case_id_len = 64` (the fixed storage width of a parsed case id, known to the caller, because an assumed-length `intent(out)` string dummy is disallowed). These are internal parameters (not part of the public export list); a consuming runner passes matching double-precision-real actuals and declares its own fixed-width (`case_id_len`) case-id buffer. Their VALUES are pinned (the gate rejects a drifted `case_id_len`), because a consumer's hardcoded length must match. The Fortran binding of these tokens (`dp = real64`, `character(len=64)`) is produced by the language backend, not authored here.

### 3.1 Published derived types
The module publishes five derived types (each named by its fully-qualified `harness_fortran_cpu__<name>`):
- `harness_fortran_cpu__h_named` `{ name: string (dynamic length), json: string (dynamic length) }` — a **boxed named value**: a JSON key `name` and its already-serialized JSON value `json`. It lets heterogeneous-rank / ragged snapshot and metrics-basis variables travel through one homogeneous rank-1 dynamic array of `harness_fortran_cpu__h_named` (a homogeneous array argument cannot carry differing ranks — this box, holding the pre-serialized token, is the workaround). The caller `__box`es a value into it before serialization is complete. (Types and lengths use the §5.1 neutral vocabulary; the Fortran binding is the language backend's.)
- `harness_fortran_cpu__h_check` `{ id: string (dynamic length), status: string (length 4) }` — one per-case check result: its `id` and honest `status` (`'pass'` / `'fail'` / `'na  '`, right-padded to width 4). The caller computes `status`; the harness does not judge.
- `harness_fortran_cpu__h_metric` `{ name: string (dynamic length), value: real (kind dp), is_na: boolean, reason_na: string (dynamic length) }` — one per-case metric leaf: its dotted-address `name`, numeric `value`, an `is_na` flag, and a `reason_na` string used only when `is_na` is true.
- `harness_fortran_cpu__h_case_result` `{ case_id: string (dynamic length), expected_xfail: boolean, checks: rank-1 dynamic array of h_check, metrics: rank-1 dynamic array of h_metric }` — one case's complete result: its `case_id`, whether its failure is expected (`expected_xfail`, driving the top-level xfail exclusion), and its `checks` and `metrics` arrays. This is the data-driven record `__write_diagnostics` folds; it embeds no harness-side judgment.
- `harness_fortran_cpu__h_mb_entry` `{ test_id: string (dynamic length), case_id: string (dynamic length), values: rank-1 dynamic array of h_named }` — one metrics-basis entry: the `test_id` it is evidence for, the `case_id` that evidence was taken from, and the caller-boxed (ragged, any-rank) `values` that form its primary-evidence body. A multi-target test contributes one entry per targeted case, so `(test_id, case_id)` — not `test_id` alone — is unique across the `entries` array handed to `__write_metrics_basis`.

### 3.2 Published operations
- `harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, ncases, ok)` — parse a supplied argv **token list** (NOT the process argv directly, so the guard is exercisable with a synthetic list): find `--cases` in `tokens`, skip the following token (the positional, unread spec path), collect the remaining tokens as `case_ids` (each stored in a fixed-width `case_id_len` slot); return `ok = false` when `--cases` is absent from `tokens` or no `case_id` follows it (the input guard), else `ok = true`. The self-test's main program marshals the real process argv (via the language-standard argv accessors) into `tokens` for the normal cases, and passes a length-0 `tokens` for the guard case.
- `harness_fortran_cpu__emit_real(x) result(s)` — format a `real (kind dp)` scalar as a JSON numeric token (round-trip-lossless exponential form; §2).
- `harness_fortran_cpu__emit_int(i) result(s)` — format an integer as a JSON numeric token (minimal decimal; §2).
- `harness_fortran_cpu__emit_bool(b) result(s)` — format a `boolean` as the JSON literal `true`/`false`.
- `harness_fortran_cpu__emit_array_r1(a) result(s)` … `__emit_array_r4(a) result(s)` — format an assumed-shape rank-1..4 `real (kind dp)` array as a nested JSON array (`[ ... ]`, row-major over the leading index), reusing `__emit_real` per element.
- `harness_fortran_cpu__box(name, json) result(nv)` — pack a JSON key `name` and an already-serialized JSON value `json` into a `harness_fortran_cpu__h_named`. A consuming runner emits each of a case's snapshot variables (via the matching `__emit_*`), boxes each with `__box`, and hands the resulting `values` array to `__write_snapshot` in one call; in a physics node the host-rendered glue emits this `__box` list mechanically from `snapshot_schema.json` (the harness stays stateless — it holds no snapshot registry, and does no serialization of the boxed value beyond copying the caller's token).
- `harness_fortran_cpu__write_snapshot(case_id, values, time)` — write the per-case `raw/state_snapshots/<case_id>.json` (runtime-built filename) holding the boxed state variables in `values` (a rank-1 array of `harness_fortran_cpu__h_named`) plus the scalar time variable. The emitted object is **flat**: each boxed variable is written as a **top-level key of the snapshot object**, keyed by its `name` with its already-serialized `json` as the value, sibling to the time variable's key. `values` is the dummy-argument name of the boxed array; neither it nor any other wrapper key appears in the emitted JSON.
- `harness_fortran_cpu__write_metrics_basis(entries, n)` — write `raw/metrics_basis.json` as the `per_test` index from `entries(1:n)` (a rank-1 array of `harness_fortran_cpu__h_mb_entry`); the harness assembles the `{ "per_test": [ ... ] }` envelope and, per entry, a **flat body**: the entry's `test_id` key, its `case_id` key, followed by one key per element of that entry's `values` array, each written under its `name` with its already-serialized `json` as a **direct sibling key of `test_id`**. `values` is the component name of `harness_fortran_cpu__h_mb_entry` (§3.1); neither it nor any other wrapper key appears in the emitted JSON (§2 gives the literal entry shape and the rejected shape). The writer emits one JSON entry per supplied record, in the caller's order, and neither deduplicates nor reorders: the caller owns the `(test_id, case_id)` product. **Data-driven plumbing**: the caller supplies the boxed evidence; the harness owns the JSON envelope.
- `harness_fortran_cpu__write_diagnostics(results, n)` — write `diagnostics.json` from `results(1:n)` (a rank-1 array of `harness_fortran_cpu__h_case_result`). The harness computes every derived value: each case's per-case verdict (`overall == fail` iff any of that case's `checks` has `status == 'fail'`; `failed_checks` = those check ids), the top-level `checks` object (one entry per distinct check id; `status == fail` iff that id fails in some case with `expected_xfail == false`), and the top-level `verdict` (xfail-excluded fold per §2). It emits the full JSON: top-level `checks` / `verdict` and the `per_case` map (each case's `checks`, `verdict`, and `metrics` leaf object, with an `is_na` metric encoded as `null` + a `_reason_na` sibling). **Data-driven plumbing, not judgment**: the harness folds and serializes; the per-case check statuses, metric values, and each case's `expected_xfail` are computed by the (self-test or physics) caller and passed in. It embeds no per-test pass/fail decision of its own.
- `harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, walltime_sec, mpi_ranks, threads_per_rank, gpu_devices)` — write `perf.json` with all required fields incl. the derived `throughput_cells_per_sec` and the `parallelism` object (`parallel_degree_total = mpi_ranks*threads_per_rank*max(gpu_devices,1)`).

The self-test `harness_fortran_cpu_runner.f90` calls `__parse_cases`, then for each `case_id` runs (dispatching on the `case_id`) the plumbing check that case names — verifying the emitter round-trips, the case fan-out, and the input guard — and builds that case's `h_case_result` (its `checks`, its `metrics`, and `expected_xfail` from the case's expected outcome). Only `l0_metric_leaf_pass` supplies metrics, so that the metric fold of `__write_diagnostics` is exercised inside this node rather than only by a consuming physics node: that case builds exactly two `harness_fortran_cpu__h_metric` records — `{ name = 'selftest.metric_leaf', value = 0.25, is_na = false }` (`0.25` is exactly representable in binary floating point, so the emitted token round-trips without deviation) and `{ name = 'selftest.metric_na', value = -1.0, is_na = true, reason_na = 'not_computed' }` (`-1.0` is an out-of-band value a correct writer never serializes, because an `is_na` metric is written as `null`) — records its `metric_leaf` check as `pass` when it supplied both records, and emits `metric_count = 2.0` into its snapshot. Every other case supplies a length-0 `metrics` array. It then builds the `h_mb_entry` array over the `(test_id, case_id)` product of §2 (one entry per case each test targets, so a multi-target test yields several) and finally calls the four writers. Because the writers use the emitters, a correct emitter is necessary for a correct output; the checks are the harness's own verification that its plumbing is faithful.

## 4. Failure conditions and constraints
A missing `--cases` flag (or no `case_id` after it) is a hard input error — `__parse_cases` returns `ok = false`, and the self-test's `l0_missing_cases_xfail` case exercises this guard by calling `__parse_cases` on a synthesized empty token list and confirming `ok = false` (recorded as the `input_guard` check firing, with the case's `h_case_result` carrying `expected_xfail = true`). A JSON emitter whose re-parsed token does not reproduce its input within an absolute tolerance of `1e-12` is a failure of the corresponding check.

## 5. Public API and compatibility
The published `operation_id`s are exactly: `harness_fortran_cpu__parse_cases`, `harness_fortran_cpu__emit_real`, `harness_fortran_cpu__emit_int`, `harness_fortran_cpu__emit_bool`, `harness_fortran_cpu__emit_array_r1`, `harness_fortran_cpu__emit_array_r2`, `harness_fortran_cpu__emit_array_r3`, `harness_fortran_cpu__emit_array_r4`, `harness_fortran_cpu__box`, `harness_fortran_cpu__write_snapshot`, `harness_fortran_cpu__write_metrics_basis`, `harness_fortran_cpu__write_diagnostics`, `harness_fortran_cpu__write_perf`. The module also publishes the derived types `harness_fortran_cpu__h_named`, `harness_fortran_cpu__h_check`, `harness_fortran_cpu__h_metric`, `harness_fortran_cpu__h_case_result`, and `harness_fortran_cpu__h_mb_entry`.

A change breaking compatibility of any signature (or of a published derived type's component layout) is a **breaking change released under a new `spec_version`**, not a rename: `0.2.1` → `0.3.0` added the `case_id` component to `harness_fortran_cpu__h_mb_entry`. A change to how the published surface is CARRIED — the §5.1 / `IR public_api.signatures` REPRESENTATION — is likewise released under a new `spec_version` even when the ABI is byte-identical, because dependency freshness invalidates a stale certified `IR` only via its version: `0.3.0` → `0.4.0` moved §5.1 and `public_api.signatures` from a Fortran interface block to the language-neutral structured form (`{symbol, signature}`); the published operations, argument types/ranks/`intent`s, and component layouts are unchanged. `0.4.0` → `0.5.0` began transcribing §5.1's value-pinned `module_parameters` (the `dp` / `case_id_len` values) into the `IR`'s `public_api.module_parameters` (a new carrier the `--stage compile` gate pins == §5.1 by value); the §5.1 block, the published operations/types, and the generated ABI are byte-identical — only the IR representation gained the field, so freshness must re-certify a stale `0.4.0` IR that lacks it. `0.5.0` → `0.6.0` made the §5.1 / `IR` leaf vocabulary fully language-neutral: a string length is `deferred` / `assumed` (not the Fortran `:` / `*`) and a kind value is `float64` / `float32` (not `real64` / `real32`); the language backend lowers these tokens to their Fortran spelling, so the published operations, argument types/ranks/`intent`s, component layouts, and the generated ABI are byte-identical — only the leaf-facing representation changed, so freshness must re-certify a stale `0.5.0` IR carrying the old tokens. `0.6.0` → `0.7.0` extends the self-test to exercise the per-case metric fold: the new case `l0_metric_leaf_pass` supplies two sentinel `h_metric` records and the new test of the same name asserts their serialized addresses, so a `__write_diagnostics` that drops the caller-supplied metrics is rejected inside this node instead of only in a consuming physics node. The §5.1 block, the published operations and types, the component layouts, and the generated ABI are unchanged; the self-test behavior and the test profile changed, so freshness must re-certify a stale `0.6.0` IR whose predicates and diagnostics contract lack the new case. Dependent nodes are not migrated by hand and need no content-free version bump of their own. The workflow enforces the skew mechanically at two points:

- **Regeneration** — a node's certified dependency resolution is recorded in its `dependency_graph.json` sidecar; when the catalog moves the harness to a new version, every dependent's recorded resolution stops matching the one `deps.yaml` + `spec_catalog.yaml` derive, so the dependency-freshness readiness check reports it stale and `run_workflow.py --with-deps` re-certifies the closure bottom-up.
- **Skew fail-close** — a consumer that would nonetheless render its runner glue against a drifted interface is stopped before Build by the renderer's signature pin (`tools/runner_renderer.assert_harness_pin`), which compares this §5.1 block against the certified harness IR's `public_api.signatures` and its generated model source.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable **language-neutral** signature block (`module_parameters` / `types` / `procedures`). It describes each published type and operation abstractly — for every argument, result, and derived-type component: its `name`, neutral `type` (`real` / `integer` / `logical` / `string` / `derived`), `rank`, and (for an argument) `intent`; plus the value-pinned module parameters the signatures reference. The vocabulary is neutral throughout: a `string` length is a neutral token — `deferred` (dynamic length) / `assumed` (caller-known length) / a fixed decimal width / a symbol — never the Fortran `:` / `*`; a module-parameter kind value is `float64` / `float32`, never the Fortran `real64` / `real32`. The target language's binding (here Fortran: `real(dp)`, `character(len=:)` / `character(len=*)`, `type(...)`, assumed-shape `(:)` ranks, `integer, parameter :: dp = real64`, the `<spec_id>__` names) is produced by the language backend (`tools/lang_backend_fortran`), not authored here — so this contract is not tied to Fortran. The generated `harness_fortran_cpu_model.f90` must publish every symbol below with the signature this block describes (formatting/continuations/comments may differ; names, argument order, types, ranks, `intent`s, `result` names, and component layout may not). The deterministic gates render this block to the target language and pin it: the `--stage compile` gate cross-checks its symbol set against §5, and the `Generate.static` gate pins the generated model source against these signatures (normalized: comments stripped, continuations joined, case-folded, whitespace-insensitive).

```yaml
module_parameters:
- name: dp
  value: float64
- name: case_id_len
  value: '64'
types:
- name: harness_fortran_cpu__h_named
  components:
  - name: name
    spec:
      type: string
      len: deferred
      alloc: true
  - name: json
    spec:
      type: string
      len: deferred
      alloc: true
- name: harness_fortran_cpu__h_check
  components:
  - name: id
    spec:
      type: string
      len: deferred
      alloc: true
  - name: status
    spec:
      type: string
      len: '4'
- name: harness_fortran_cpu__h_metric
  components:
  - name: name
    spec:
      type: string
      len: deferred
      alloc: true
  - name: value
    spec:
      type: real
      kind: dp
  - name: is_na
    spec:
      type: logical
  - name: reason_na
    spec:
      type: string
      len: deferred
      alloc: true
- name: harness_fortran_cpu__h_case_result
  components:
  - name: case_id
    spec:
      type: string
      len: deferred
      alloc: true
  - name: expected_xfail
    spec:
      type: logical
  - name: checks
    rank: 1
    spec:
      type: derived
      name: harness_fortran_cpu__h_check
      alloc: true
  - name: metrics
    rank: 1
    spec:
      type: derived
      name: harness_fortran_cpu__h_metric
      alloc: true
- name: harness_fortran_cpu__h_mb_entry
  components:
  - name: test_id
    spec:
      type: string
      len: deferred
      alloc: true
  - name: case_id
    spec:
      type: string
      len: deferred
      alloc: true
  - name: values
    rank: 1
    spec:
      type: derived
      name: harness_fortran_cpu__h_named
      alloc: true
procedures:
- kind: subroutine
  name: harness_fortran_cpu__parse_cases
  args:
  - name: tokens
    rank: 1
    intent: in
    spec:
      type: string
      len: assumed
  - name: ntokens
    intent: in
    spec:
      type: integer
  - name: case_ids
    rank: 1
    intent: out
    spec:
      type: string
      len: case_id_len
  - name: ncases
    intent: out
    spec:
      type: integer
  - name: ok
    intent: out
    spec:
      type: logical
- kind: function
  name: harness_fortran_cpu__emit_real
  args:
  - name: x
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu__emit_int
  args:
  - name: i
    intent: in
    spec:
      type: integer
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu__emit_bool
  args:
  - name: b
    intent: in
    spec:
      type: logical
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu__emit_array_r1
  args:
  - name: a
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu__emit_array_r2
  args:
  - name: a
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu__emit_array_r3
  args:
  - name: a
    rank: 3
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu__emit_array_r4
  args:
  - name: a
    rank: 4
    intent: in
    spec:
      type: real
      kind: dp
  result:
    name: s
    spec:
      type: string
      len: deferred
      alloc: true
- kind: function
  name: harness_fortran_cpu__box
  args:
  - name: name
    intent: in
    spec:
      type: string
      len: assumed
  - name: json
    intent: in
    spec:
      type: string
      len: assumed
  result:
    name: nv
    spec:
      type: derived
      name: harness_fortran_cpu__h_named
- kind: subroutine
  name: harness_fortran_cpu__write_snapshot
  args:
  - name: case_id
    intent: in
    spec:
      type: string
      len: assumed
  - name: values
    rank: 1
    intent: in
    spec:
      type: derived
      name: harness_fortran_cpu__h_named
  - name: time
    intent: in
    spec:
      type: real
      kind: dp
- kind: subroutine
  name: harness_fortran_cpu__write_metrics_basis
  args:
  - name: entries
    rank: 1
    intent: in
    spec:
      type: derived
      name: harness_fortran_cpu__h_mb_entry
  - name: n
    intent: in
    spec:
      type: integer
- kind: subroutine
  name: harness_fortran_cpu__write_diagnostics
  args:
  - name: results
    rank: 1
    intent: in
    spec:
      type: derived
      name: harness_fortran_cpu__h_case_result
  - name: n
    intent: in
    spec:
      type: integer
- kind: subroutine
  name: harness_fortran_cpu__write_perf
  args:
  - name: case_id
    intent: in
    spec:
      type: string
      len: assumed
  - name: target
    intent: in
    spec:
      type: string
      len: assumed
  - name: steps
    intent: in
    spec:
      type: integer
  - name: cells_updated
    intent: in
    spec:
      type: integer
  - name: walltime_sec
    intent: in
    spec:
      type: real
      kind: dp
  - name: mpi_ranks
    intent: in
    spec:
      type: integer
  - name: threads_per_rank
    intent: in
    spec:
      type: integer
  - name: gpu_devices
    intent: in
    spec:
      type: integer
```

## 6. Prohibitions
- No physics: the harness must embed no per-case kernel or per-test judgment logic; those are the consuming physics node's `case_run` / `checks_compute` callbacks. `__write_diagnostics` folds caller-supplied statuses and each case's `expected_xfail`; it never decides pass/fail itself.
- No truncating or language-specific serialization anywhere in the generated source: never a numeric form that may drop a leading digit, never a language-specific boolean token in place of the literal `true`/`false` (branch to the literal instead). The forbidden Fortran realizations (`F0` / `F0.d` numeric, an `L`-family logical descriptor) are enumerated in `docs/workflow/RUNNER_OUTPUT_CONTRACT.md §4`.
- Never write `verdict.json`, `aggregate_verdict.json`, `summary.json`, or `trial_meta.json` — not even as a literal filename inside a comment or example string.
- No launch of an external interpreter (`python` / `bash` / `sh` / `node`).
- All output paths are written relatively (so `cd $(RUNDIR)` redirects them); no hardcoded / sequential snapshot filename literal.

## 7. Traceability
Record the harness adoption in `component_catalog.yaml` (as the `(language, hardware)` = `(fortran, cpu)` runner harness) and the resolved harness version each dependent physics node was certified against.

## 8. tests reference
The corresponding `tests.md` is `spec/infrastructure/infra/harness/harness_fortran_cpu/tests.md`, with `test_profile_version` of `0.4.0`.
