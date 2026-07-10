# Tests: Fortran/CPU runner harness (L0 plumbing self-test)

## 0. Meta information
- `test_profile_id`: `harness_fortran_cpu_l0`
- `test_profile_version`: `0.3.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `infrastructure`
- `spec_ref.spec_id`: `harness_fortran_cpu`
- `spec_ref.spec_version`: `0.3.0`
- `spec_ref.controlled_spec_path`: `spec/infrastructure/infra/harness/harness_fortran_cpu/controlled_spec.md`

## 1. Test purpose
This suite verifies the published runner-plumbing operations of `harness_fortran_cpu` at `L0`: numeric / boolean / rank-1..4 array JSON emission round-trips, the case-set fan-out (one `<case_id>.json` snapshot per case), the derived `perf` throughput, the `--cases` input guard, and the `(test_id, case_id)` shape of the metrics-basis index. The self-test runner exercises each operation and records a per-case check in `diagnostics.json`; because the writers use the emitters, a faithful emitter is a precondition for a correct output.

## 2. Input-defaulting rules
- Each normal case supplies a small fixed set of sentinel values to the emitter under test (including a negative, a subnormal-scale, and a large-magnitude real for the numeric case).
- The abnormal case (`l0_missing_cases_xfail`) synthesizes an empty argv-token list (no `--cases`) and drives `harness_fortran_cpu__parse_cases` to exercise the guard.

## 3. Execution-control rules
`N/A`: the harness self-test defines no time-stepping. Each case runs its named plumbing check once (`steps = 1`).

## 4. Case-expansion rules
The suite defines six `case`s and seven `test_id`s (no sweep). Six tests are **single-target**: each names exactly one case, and that case's `case_id` equals the `test_id` — the six `case.test_case_set[].case_id` are exactly those six `test_id`s. The seventh, `l0_multi_case_evidence_pass`, is **multi-target**: it declares no case of its own and ranges over the two existing cases `l0_array_emit_pass` and `l0_numeric_roundtrip_pass`, both of which already emit `max_abs_deviation`. Each `case_id` selects the plumbing aspect that case verifies. The multi-case run itself is the `case_fanout` evidence: each case writes exactly one `raw/state_snapshots/<case_id>.json`. Each snapshot holds that case's `required_raw_variables` (the union over the tests targeting it, listed per test in §6) plus the scalar time variable `t` (value `0.0`; a single `steps=1` step).

Because `raw/metrics_basis.json` carries one entry per (`test_id`, target `case_id`) pair, the seven tests produce **eight** entries: one for each single-target test plus two for `l0_multi_case_evidence_pass`.

## 5. Diagnostics contract
- Require outputting, in `diagnostics.json`, a top-level `checks` object holding `checks.numeric_roundtrip`, `checks.boolean_literal`, `checks.array_emit`, `checks.case_fanout`, `checks.perf_derived`, and `checks.input_guard` (each `{ "status": "pass"|"fail" }`), a top-level `verdict` object with `overall` and `failed_checks`, and a `per_case` map giving each `case_id` its own `{ checks, verdict, metrics }`. The `metrics` object is assembled by `harness_fortran_cpu__write_diagnostics` from each case's supplied `h_metric` list; the self-test supplies no metrics, so every per-case `metrics` is the empty object `{}` (the metric-leaf / NA encoding is exercised by consuming physics nodes, not by this L0 self-test). The whole `diagnostics.json` is assembled inside `__write_diagnostics` from the caller-supplied `h_case_result` records (each carrying its `expected_xfail` flag); the caller supplies only honest per-case data, and the harness performs every fold.
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
  - `judgment`: `.true.` and `.false.` emit the exact JSON literals `true` / `false` (no `L`-descriptor `T`/`F`), so `bool_match == 1.0`, and `checks.boolean_literal.status == pass`.
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
- `test_id`: `l0_missing_cases_xfail`
  - `level`: `L0`
  - `operation_id`: `harness_fortran_cpu__parse_cases`
  - `expected_outcome`: `xfail`
  - `required_raw_variables`: `guard_fired` (scalar)
  - `xfail_condition`: a `--cases` flag is absent from the token list passed to `__parse_cases`
  - `pass_when`: `per_case.l0_missing_cases_xfail.verdict.overall == fail` and `per_case.l0_missing_cases_xfail.verdict.failed_checks includes 'input_guard'` (calling `__parse_cases` on a length-0 token array returns `ok=.false.`, so `guard_fired == 1.0` and the guard fires).
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
- `per_test.evidence_rule`: `raw/metrics_basis.json` holds exactly one entry per (`test_id`, target `case_id`) pair — eight entries for the seven tests.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
