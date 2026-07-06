# Controlled Spec: Fortran/CPU runner harness (infrastructure spec)

## 0. Meta information
- `spec_id`: `harness_fortran_cpu`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `infrastructure`
- `domain`: `infra`
- `family`: `harness`

## 1. Responsibility and scope
This `infrastructure` node (R1 harness) is responsible for the shared **runner plumbing** that every Fortran/CPU physics node's runner is built against: argv / `--cases` parsing, the case-set loop driver, the JSON emission machinery (numeric / integer / boolean / rank-1..4 real-array tokens), and the standard runner-output writers (`raw/state_snapshots/<case_id>.json`, `raw/metrics_basis.json`, `diagnostics.json`, `perf.json`). It carries **no physics**: the per-case kernel and the per-test check logic are supplied by the consuming physics node (a `case_run` / `checks_compute` callback in the physics `*_checks.f90`), never here. It targets `(language=fortran, hardware=cpu)`; a different `(language, hardware)` target is a separate harness node.

The node's own generated code is a `harness_fortran_cpu_model.f90` publishing the plumbing operations plus a self-test `harness_fortran_cpu_runner.f90` that exercises them and emits the standard runner outputs (so the harness is verified through the exact same Compile→Generate→Build→Validate path as any node; it is self-hosting — the self-test writes its evidence using its own emitters).

## 2. input/output contract
Input (to the self-test runner): the standard runner argv `--cases <spec.ir.yaml> <case_id>...` — the spec path is taken positionally and need not be read; the trailing tokens are the `case_id`s to run, one per `case.test_case_set[]`. The main program marshals the process argv into a token array and calls `harness_fortran_cpu__parse_cases` on it. Each `case_id` selects, by `select case`, the plumbing aspect that case verifies; `case_id == test_id` for every case. A missing `--cases` flag (no cases) makes `__parse_cases` return `ok=.false.` (the input guard); the guard case verifies this by calling `__parse_cases` on a length-0 token array.

Output artifacts (produced by the writers, into the run node dir relative to `cwd=RUNDIR`):
- **`diagnostics.json`** — a JSON object with a top-level `checks` object holding one entry per `io_contract.diagnostics_contract.checks[].id` (each `{ "status": "pass"|"fail", ... }`), a top-level `verdict` object `{ "overall": "pass"|"fail", "failed_checks": [<check_id>...] }`, and a `per_case` map `{ <case_id>: { "checks": {...}, "verdict": { "overall", "failed_checks" } } }` giving each case's own result. Top-level aggregation rule: `verdict.overall == fail` iff some NON-`xfail` case's per-case verdict is fail; an `xfail`-expected per-case failure (the `input_guard` firing on the guard case) is EXCLUDED from the top-level `failed_checks`, so a run where the only failure is the expected guard reports top-level `{ "overall": "pass", "failed_checks": [] }` with `checks.input_guard.status == pass` (the guard behaved as expected). The per-case `input_guard` failure is confined to `per_case.<guard_case>`.
- **`perf.json`** — one object with `case_id`, `target` (`"cpu"`), `walltime_sec`, `steps`, `cells_updated`, `throughput_cells_per_sec` (`= cells_updated / walltime_sec`), a `parallelism` object (`mpi_ranks`, `threads_per_rank`, `gpu_devices`, `parallel_degree_total = mpi_ranks*threads_per_rank*max(gpu_devices,1)`), and `timestamp_utc` (ISO-8601).
- **`raw/state_snapshots/<case_id>.json`** — exactly one per case, named at runtime as `'raw/state_snapshots/'//trim(case_id)//'.json'` (never a hardcoded/sequential literal). Each holds every variable in *that case's* `io_contract.test_evidence_requirements.required_raw_variables` plus the declared scalar `time_variable` `t` (value `0.0`; the self-test has a single `steps=1` step). The snapshot state variables (declared in `snapshot_schema.json` with their `shape_expr`) are, per case:
  - `l0_numeric_roundtrip_pass`: `x_in` (rank-1, `[3]` — the sentinel reals), `x_out` (rank-1, `[3]` — the values re-parsed from `__emit_real`'s tokens), `max_abs_deviation` (scalar).
  - `l0_boolean_literal_pass`: `bool_match` (scalar, `1.0` iff `.true.`/`.false.` emitted the exact literals `true`/`false`).
  - `l0_array_emit_pass`: `a1` (rank-1, `[2]`), `a2` (rank-2, `[2,2]`), `a3` (rank-3, `[2,2,2]`), `a4` (rank-4, `[2,2,2,2]`) — the inputs to `__emit_array_r1..r4` — and `max_abs_deviation` (scalar — max component deviation of the re-parsed arrays).
  - `l0_case_fanout_pass`: `case_index` (scalar — this case's ordinal in the run).
  - `l0_perf_derived_pass`: `throughput_residual` (scalar — `|throughput_cells_per_sec - cells_updated/walltime_sec|`).
  - `l0_missing_cases_xfail`: `guard_fired` (scalar, `1.0` when `__parse_cases` on a length-0 token array returned `ok=.false.`). A guard case still emits its snapshot, shape-valid.
  Each case's `required_raw_variables` is exactly its listed variables above; a variable is declared once in `snapshot_schema.json` and emitted only by the cases that require it.
- **`raw/metrics_basis.json`** — a `{ "per_test": [ { "test_id": <id>, <that test's required_raw_variables> }, ... ] }` index with one entry per `tests.md` `test_id`, holding only primary evidence (never a copy of `diagnostics.json`).

Numeric serialization is the runner-output contract (`docs/workflow/RUNNER_OUTPUT_CONTRACT.md §4`): reals via `ES24.16E3` then `trim(adjustl())` (or a bounded `Fw.d`, never `F0`/`F0.d`), integers via `I0`, booleans by branching to the literal token `true`/`false` (never an `L`-family descriptor).

## 3. Operation definition
The published operations (all under module `harness_fortran_cpu_model`, prefix `harness_fortran_cpu__`) are the plumbing surface a physics-node runner reuses:

- `harness_fortran_cpu__parse_cases(tokens, n_tokens, spec_path, case_ids, n_cases, ok)` — parse a supplied argv **token list** (NOT the process argv directly, so the guard is exercisable with a synthetic list): find `--cases` in `tokens`, consume the following token as the spec path (positional, unread), collect the remaining tokens as `case_ids`; return `ok = .false.` when `--cases` is absent from `tokens` or no `case_id` follows it (the input guard), else `ok = .true.`. The self-test's main program marshals the real process argv (`command_argument_count` / `get_command_argument`) into `tokens` for the normal cases, and passes a length-0 `tokens` for the guard case.
- `harness_fortran_cpu__emit_real(x) result(s)` — format a `real(dp)` scalar as a JSON numeric token (`ES24.16E3`, `trim(adjustl)`).
- `harness_fortran_cpu__emit_int(i) result(s)` — format an integer as a JSON numeric token (`I0`).
- `harness_fortran_cpu__emit_bool(b) result(s)` — format a `logical` as the JSON literal `true`/`false`.
- `harness_fortran_cpu__emit_array_r1(v) result(s)` … `__emit_array_r4(t) result(s)` — format an assumed-shape rank-1..4 `real(dp)` array as a nested JSON array (`[ ... ]`, row-major over the leading index), reusing `__emit_real` per element.
- `type(harness_fortran_cpu__h_named)` — a public **boxed-variable** record `{ name, rank, r1(:), r2(:,:), r3(:,:,:), r4(:,:,:,:), scalar }` that carries one named snapshot variable of any rank 0..4 inside a single homogeneous type, so a mixed-rank variable set can travel as one `type(harness_fortran_cpu__h_named) :: values(:)` array (Fortran forbids passing differing ranks through one plain array argument — this box is the workaround). Only the component named by `rank` is allocated/set (`rank == 0` ⇒ the `scalar` component; `rank == k` ⇒ the `rk` component).
- `harness_fortran_cpu__box(name, value) result(named)` — a generic (rank-specific `module procedure`s for a `real(dp)` scalar and rank-1..4 `real(dp)` arrays) that packs `name` and the value into a `type(harness_fortran_cpu__h_named)`, setting `rank` and the one matching component. A consuming runner boxes each of a case's snapshot variables with `__box` and hands the resulting `values(:)` array to `__write_snapshot` in one call; in a physics node the host-rendered glue emits this `__box` list mechanically from `snapshot_schema.json` (the harness stays stateless — it holds no snapshot registry).
- `harness_fortran_cpu__write_snapshot(case_id, values, time_value)` — write the per-case `raw/state_snapshots/<case_id>.json` (runtime-built filename) holding the boxed state variables in `values` (`type(harness_fortran_cpu__h_named) :: values(:)`, each dispatched by its `rank` component to the matching `__emit_array_rN` / `__emit_real` emitter) plus the scalar time variable.
- `harness_fortran_cpu__write_metrics_basis(test_ids, per_test_vars)` — write `raw/metrics_basis.json` as the `per_test` index.
- `harness_fortran_cpu__write_diagnostics(check_ids, per_case_results)` — write `diagnostics.json` (top-level `checks`/`verdict` + `per_case`) from the accumulated per-case results. Each `per_case_results` entry is a plain data record `{ case_id, checks[], verdict, expected_outcome }` supplied by the caller — including `expected_outcome ∈ {pass, xfail}`. The top-level aggregation is therefore **data-driven plumbing, not judgment**: the operation folds up the per-case verdicts and simply excludes a case whose `expected_outcome == xfail` from the top-level `failed_checks` (per §2's aggregation rule). It embeds no per-test pass/fail decision of its own — the check results and each case's expected outcome are computed by the (self-test or physics) caller and passed in.
- `harness_fortran_cpu__write_perf(case_id, steps, cells_updated, walltime_sec)` — write `perf.json` with all required fields incl. the derived `throughput_cells_per_sec` and the `parallelism` object.

The self-test `harness_fortran_cpu_runner.f90` calls `__parse_cases`, then for each `case_id` runs (via `select case`) the plumbing check that case names — verifying the emitter round-trips, the case fan-out, and the input guard — accumulates the per-case check result, and finally calls the four writers. Because the writers use the emitters, a correct emitter is necessary for a correct output; the checks are the harness's own verification that its plumbing is faithful.

## 4. Failure conditions and constraints
A missing `--cases` flag (or no `case_id` after it) is a hard input error — `__parse_cases` returns `ok=.false.`, and the self-test's `l0_missing_cases_xfail` case exercises this guard by calling `__parse_cases` on a synthesized empty token list and confirming `ok=.false.` (recorded as the `input_guard` check firing). A JSON emitter whose re-parsed token does not reproduce its input within an absolute tolerance of `1e-12` is a failure of the corresponding check.

## 5. Public API and compatibility
The published `operation_id`s are exactly: `harness_fortran_cpu__parse_cases` (token-list form, above), `harness_fortran_cpu__emit_real`, `harness_fortran_cpu__emit_int`, `harness_fortran_cpu__emit_bool`, `harness_fortran_cpu__emit_array_r1`, `harness_fortran_cpu__emit_array_r2`, `harness_fortran_cpu__emit_array_r3`, `harness_fortran_cpu__emit_array_r4`, `harness_fortran_cpu__box` (the rank-generic boxing constructor), `harness_fortran_cpu__write_snapshot` (boxed `values(:)` form, above), `harness_fortran_cpu__write_metrics_basis`, `harness_fortran_cpu__write_diagnostics`, `harness_fortran_cpu__write_perf`. The module also publishes the derived type `harness_fortran_cpu__h_named` used by `__box` / `__write_snapshot`. A change breaking `major` compatibility of any signature (or of the `h_named` component layout) is separated into a different name; consuming physics nodes are re-certified against a new harness version (harness version skew ⇒ dependent re-certification).

## 6. Prohibitions
- No physics: the harness must embed no per-case kernel or per-test judgment logic; those are the consuming physics node's `case_run` / `checks_compute` callbacks.
- No forbidden Fortran JSON descriptors anywhere in the generated source: never `F0` / `F0.d` for a numeric token, never an `L`-family logical descriptor for a boolean (branch to the literal instead).
- Never write `verdict.json`, `aggregate_verdict.json`, `summary.json`, or `trial_meta.json` — not even as a literal filename inside a comment or example string.
- No launch of an external interpreter (`python` / `bash` / `sh` / `node`).
- All output paths are written relatively (so `cd $(RUNDIR)` redirects them); no hardcoded / sequential snapshot filename literal.

## 7. Traceability
Record the harness adoption in `component_catalog.yaml` (as the `(language, hardware)` = `(fortran, cpu)` runner harness) and the resolved harness version each dependent physics node was certified against.

## 8. tests reference
The corresponding `tests.md` is `spec/infrastructure/infra/harness/harness_fortran_cpu/tests.md`, with `test_profile_version` of `0.1.0`.
