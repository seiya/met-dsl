# Tests: Fortran/CPU runner harness (L0 plumbing self-test)

## 0. Meta information
- `test_profile_id`: `harness_fortran_cpu_l0`
- `test_profile_version`: `0.4.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `infrastructure`
- `spec_ref.spec_id`: `harness_fortran_cpu`
- `spec_ref.spec_version`: `0.7.0`
- `spec_ref.controlled_spec_path`: `spec/infrastructure/infra/harness/harness_fortran_cpu/controlled_spec.md`

## 1. Test purpose
This suite verifies the published runner-plumbing operations of `harness_fortran_cpu` at `L0`: numeric / boolean / rank-1..4 array JSON emission round-trips, the case-set fan-out (one `<case_id>.json` snapshot per case), the derived `perf` throughput, the `--cases` input guard, the per-case metric fold (a supplied numeric leaf and a supplied `N/A` leaf reaching `diagnostics.json` at their declared addresses), and the `(test_id, case_id)` shape of the metrics-basis index. The self-test runner exercises each operation and records a per-case check in `diagnostics.json`; because the writers use the emitters, a faithful emitter is a precondition for a correct output.

## 2. Input-defaulting rules
- Each normal case supplies a small fixed set of sentinel values to the emitter under test (including a negative, a subnormal-scale, and a large-magnitude real for the numeric case).
- The metric case (`l0_metric_leaf_pass`) supplies exactly two fixed sentinel `h_metric` records and no other sentinel payload. It carries the same `grid` / `time` / `boundary` inputs as every other case, and — like every case — its own dispatch entry naming the plumbing aspect it verifies, here the diagnostics fold (`harness_fortran_cpu__write_diagnostics`). The two records, whose field values are the case's input data, are:
  - `{ name = 'selftest.metric_leaf', value = 0.25, is_na = false, reason_na = '' }`
  - `{ name = 'selftest.metric_na', value = -1.0, is_na = true, reason_na = 'not_computed' }`
  There is no third record: `selftest.metric_na_reason_na` is a key the writer derives from the second record's `is_na` / `reason_na` (§5), never a supplied `h_metric`.
- The abnormal case (`l0_missing_cases_xfail`) synthesizes an empty argv-token list (no `--cases`) and drives `harness_fortran_cpu__parse_cases` to exercise the guard.

## 3. Execution-control rules
`N/A`: the harness self-test defines no time-stepping. Each case runs its named plumbing check once (`steps = 1`).

## 4. Case-expansion rules
The suite defines seven `case`s and eight `test_id`s (no sweep). Seven tests are **single-target**: each names exactly one case, and that case's `case_id` equals the `test_id` — the seven `case.test_case_set[].case_id` are exactly those seven `test_id`s. The eighth, `l0_multi_case_evidence_pass`, is **multi-target**: it declares no case of its own and ranges over the two existing cases `l0_array_emit_pass` and `l0_numeric_roundtrip_pass`, both of which already emit `max_abs_deviation`. Each `case_id` selects the plumbing aspect that case verifies. The multi-case run itself is the `case_fanout` evidence: each case writes exactly one `raw/state_snapshots/<case_id>.json`. Each snapshot holds that case's `required_raw_variables` (the union over the tests targeting it, listed per test in §6) plus the scalar time variable `t` (value `0.0`; a single `steps=1` step).

Because `raw/metrics_basis.json` carries one entry per (`test_id`, target `case_id`) pair, the eight tests produce **nine** entries: one for each single-target test plus two for `l0_multi_case_evidence_pass`.

## 5. Diagnostics contract
- Require outputting, in `diagnostics.json`, a top-level `checks` object holding `checks.numeric_roundtrip`, `checks.boolean_literal`, `checks.array_emit`, `checks.case_fanout`, `checks.perf_derived`, `checks.metric_leaf`, and `checks.input_guard` (each `{ "status": "pass"|"fail" }`), a top-level `verdict` object with `overall` and `failed_checks`, and a `per_case` map giving each `case_id` its own `{ checks, verdict, metrics }`. The whole `diagnostics.json` is assembled inside `__write_diagnostics` from the caller-supplied `h_case_result` records (each carrying its `expected_xfail` flag); the caller supplies only honest per-case data, and the harness performs every fold.
- The per-case `metrics` object is a **fold over that case's supplied `h_metric` array**: `harness_fortran_cpu__write_diagnostics` iterates the array and writes exactly one leaf per record — a record with `is_na = false` as the single key `"<name>": <value>` (no sibling), a record with `is_na = true` as `"<name>": null` plus the one sibling `"<name>_reason_na": "<reason_na>"`. The object is decided by the supplied records alone: a body that does not iterate the supplied array — one selected by branching on `case_id`, or one emitting a fixed key set — is forbidden even when it reproduces the expected keys for this suite, because the consuming physics nodes supply different records. A length-0 array legitimately yields the empty object `{}`.
- Exactly one case supplies metrics: `l0_metric_leaf_pass` supplies the two records of §2, so its `metrics` object carries the three addresses below and every other case supplies a length-0 array and therefore gets the empty object `{}`. The required per-case metric addresses — declared in the IR as `io_contract.diagnostics_contract.metrics` and each emitted verbatim as a key of `per_case.l0_metric_leaf_pass.metrics` — are:
  - `selftest.metric_leaf` — the numeric leaf of the first supplied record, value `0.25`. `0.25` is exactly representable in binary floating point, so the round-trip through the emitted JSON token introduces no deviation.
  - `selftest.metric_na` — the honest-`N/A` leaf of the second supplied record, written as `null`. Its supplied `value = -1.0` is out-of-band and a correct writer never serializes it, because an `is_na` record is written as `null`.
  - `selftest.metric_na_reason_na` — the `N/A` sibling the writer derives from the second record's `reason_na`, value `"not_computed"`. It is declared here so the predicate `ref` of §6 resolves; it is not a supplied `h_metric`, and no case computes it.
- The invalid-input case (`l0_missing_cases_xfail`) is reported as a **failing** guard at the PER-CASE level only: `per_case.l0_missing_cases_xfail.verdict.overall == fail` with `input_guard` in its `failed_checks` (the guard correctly firing on a missing `--cases`). This expected `xfail` failure is EXCLUDED from the top-level aggregation: the top-level `verdict.overall` stays `pass`, top-level `failed_checks` is `[]`, and top-level `checks.input_guard.status == pass` (the guard behaved as expected). Only a NON-`xfail` case failure would set the top-level `overall` to `fail`.

## 6. Test definitions
- `test_id`: `l0_numeric_roundtrip_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__emit_real`
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `x_in` (rank-1, `[3]`), `x_out` (rank-1, `[3]`), `max_abs_deviation` (scalar)
  - `judgment`: the sentinel reals `x_in` (a negative, a `1e-30`-scale, and a `1e+30`-scale value) emitted via `harness_fortran_cpu__emit_real` and re-parsed into `x_out` reproduce the inputs within an absolute tolerance of `1e-12` (`max_abs_deviation = max|x_out - x_in| <= 1e-12`), and `checks.numeric_roundtrip.status == pass`.
- `test_id`: `l0_boolean_literal_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__emit_bool`
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `bool_match` (scalar)
  - `judgment`: a `true` and a `false` boolean emit the exact JSON literals `true` / `false` (no language-specific boolean token), so `bool_match == 1.0`, and `checks.boolean_literal.status == pass`.
- `test_id`: `l0_array_emit_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__emit_array_r2` (representative; the case exercises `__emit_array_r1..r4`)
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `a1` (rank-1, `[2]`), `a2` (rank-2, `[2,2]`), `a3` (rank-3, `[2,2,2]`), `a4` (rank-4, `[2,2,2,2]`), `max_abs_deviation` (scalar)
  - `judgment`: the rank-1..4 real arrays `a1..a4` emitted via `harness_fortran_cpu__emit_array_r1..r4` produce well-formed nested JSON arrays whose parsed shape and element values match the inputs within `1e-12` (`max_abs_deviation <= 1e-12`), and `checks.array_emit.status == pass`.
- `test_id`: `l0_case_fanout_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__write_snapshot`
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `case_index` (scalar)
  - `judgment`: the case loop writes exactly one `raw/state_snapshots/<case_id>.json` per case (runtime-built name), each carrying its `required_raw_variables`, and `checks.case_fanout.status == pass`.
- `test_id`: `l0_perf_derived_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__write_perf`
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `throughput_residual` (scalar)
  - `judgment`: `perf.json` carries all required fields and `throughput_cells_per_sec == cells_updated / walltime_sec` within a relative tolerance of `1e-9` (`throughput_residual <= 1e-9 * throughput`), and `checks.perf_derived.status == pass`.
- `test_id`: `l0_metric_leaf_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__write_diagnostics`
  - `expected_outcome`: `pass`
  - `required_raw_variables`: `metric_count` (scalar)
  - `judgment`: the case supplies the two `h_metric` records of §2 to `harness_fortran_cpu__write_diagnostics` and records `checks.metric_leaf.status == pass` when it supplied both, and the writer folds them into that case's `metrics` object: `selftest.metric_leaf` is present as a number equal to `0.25` within `1e-10`, `selftest.metric_na_reason_na` is present with the value `"not_computed"`, and `selftest.metric_na` is either `null` (the honest-`N/A` encoding) or a number `>= 0.0` (never satisfied by the supplied out-of-band `-1.0`). A writer that drops the supplied metrics leaves `metrics` empty, so the `selftest.metric_leaf` and `selftest.metric_na_reason_na` addresses are absent and those conditions fail structurally; the `selftest.metric_na` condition is `na_allowed` and therefore carries no structural detection of its own — it fails only when a number is written where `null` is required. The evidence variable `metric_count` (`= 2.0`, the number of records supplied) is snapshot evidence of the supply, not a `diagnostics.json` address: it is carried in `raw/state_snapshots/l0_metric_leaf_pass.json` and `raw/metrics_basis.json`, and no `pass_when` condition references it.
  - `predicate_conditions` (the verbatim `pass_when.all` conditions, transcribed into the IR as this test's `io_contract.test_predicates[].pass_when.all`; the metric conditions are scoped `per_case: true` over this test's single target case, because a metric address exists only inside a `per_case` slice):
    - `ref`: `selftest.metric_leaf`, `op`: `ge`, `value`: `0.2499999999`, `per_case`: `true`
    - `ref`: `selftest.metric_leaf`, `op`: `le`, `value`: `0.2500000001`, `per_case`: `true`
    - `ref`: `selftest.metric_na_reason_na`, `op`: `eq`, `value`: `not_computed`, `per_case`: `true`
    - `ref`: `selftest.metric_na`, `op`: `ge`, `value`: `0.0`, `per_case`: `true`, `na_allowed`: `true`
    - `ref`: `checks.metric_leaf.status`, `op`: `eq`, `value`: `pass`
- `test_id`: `l0_missing_cases_xfail`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__parse_cases`
  - `expected_outcome`: `xfail`
  - `required_raw_variables`: `guard_fired` (scalar)
  - `xfail_condition`: a `--cases` flag is absent from the token list passed to `__parse_cases`
  - `pass_when`: `per_case.l0_missing_cases_xfail.verdict.overall == fail` and `per_case.l0_missing_cases_xfail.verdict.failed_checks includes 'input_guard'` (calling `__parse_cases` on a length-0 token array returns `ok = false`, so `guard_fired == 1.0` and the guard fires).
- `test_id`: `l0_multi_case_evidence_pass`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__write_metrics_basis`
  - `expected_outcome`: `pass`
  - `target_cases`: `l0_array_emit_pass`, `l0_numeric_roundtrip_pass` (this test declares no case of its own — it is the suite's multi-target test)
  - `required_raw_variables`: `max_abs_deviation` (scalar)
  - `judgment`: `__write_metrics_basis` records this test's primary evidence for **every** case it targets — `raw/metrics_basis.json` carries one `per_test` entry per (`test_id`, target `case_id`) pair, so this test contributes two entries, each keyed by its own `case_id` and holding that case's `max_abs_deviation` — and both target cases round-trip within the `1e-12` tolerance, i.e. `per_case.l0_array_emit_pass.verdict.overall == pass` and `per_case.l0_numeric_roundtrip_pass.verdict.overall == pass`.

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied. For the multi-target test the judgment must hold in EVERY target case.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied (the input guard fires on a missing `--cases`).
- `per_test.evidence_rule`: `raw/metrics_basis.json` holds exactly one entry per (`test_id`, target `case_id`) pair — nine entries for the eight tests.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
