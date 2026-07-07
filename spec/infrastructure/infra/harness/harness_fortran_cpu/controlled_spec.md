# Controlled Spec: Fortran/CPU runner harness (infrastructure spec)

## 0. Meta information
- `spec_id`: `harness_fortran_cpu`
- `spec_version`: `0.2.0`
- `status`: `controlled_draft`
- `spec_kind`: `infrastructure`
- `domain`: `infra`
- `family`: `harness`

## 1. Responsibility and scope
This `infrastructure` node (R1 harness) is responsible for the shared **runner plumbing** that every Fortran/CPU physics node's runner is built against: argv / `--cases` parsing, the case-set loop driver, the JSON emission machinery (numeric / integer / boolean / rank-1..4 real-array tokens), and the standard runner-output writers (`raw/state_snapshots/<case_id>.json`, `raw/metrics_basis.json`, `diagnostics.json`, `perf.json`). It carries **no physics**: the per-case kernel and the per-test check logic are supplied by the consuming physics node (a `case_run` / `checks_compute` callback in the physics `*_checks.f90`), never here. It targets `(language=fortran, hardware=cpu)`; a different `(language, hardware)` target is a separate harness node.

The node's own generated code is a `harness_fortran_cpu_model.f90` publishing the plumbing operations plus a self-test `harness_fortran_cpu_runner.f90` that exercises them and emits the standard runner outputs (so the harness is verified through the exact same Compile→Generate→Build→Validate path as any node; it is self-hosting — the self-test writes its evidence using its own emitters).

The published surface is a **binding, signature-level contract**: §5.1 gives the canonical Fortran interface block (every public type and every operation signature) in a machine-readable fenced code block. The generated `harness_fortran_cpu_model.f90` must publish exactly those signatures; a consuming physics node's host-rendered runner glue is written against them and holds no serialization knowledge of its own (the JSON envelope assembly and the verdict fold live only inside these certified operations).

## 2. input/output contract
Input (to the self-test runner): the standard runner argv `--cases <spec.ir.yaml> <case_id>...` — the spec path is taken positionally and need not be read; the trailing tokens are the `case_id`s to run, one per `case.test_case_set[]`. The main program marshals the process argv into a token array and calls `harness_fortran_cpu__parse_cases` on it. Each `case_id` selects, by `select case`, the plumbing aspect that case verifies; `case_id == test_id` for every case. A missing `--cases` flag (no cases) makes `__parse_cases` return `ok=.false.` (the input guard); the guard case verifies this by calling `__parse_cases` on a length-0 token array.

Output artifacts (produced by the writers, into the run node dir relative to `cwd=RUNDIR`):
- **`diagnostics.json`** — a JSON object with a top-level `checks` object holding one entry per `io_contract.diagnostics_contract.checks[].id` (each `{ "status": "pass"|"fail" }`), a top-level `verdict` object `{ "overall": "pass"|"fail", "failed_checks": [<check_id>...] }`, and a `per_case` map `{ <case_id>: { "checks": {...}, "verdict": { "overall", "failed_checks" }, "metrics": {...} } }` giving each case's own result. The assembly is done entirely inside `__write_diagnostics` from the caller-supplied per-case result records — the harness performs the fold, the caller supplies only the honest per-case check/metric data and each case's `expected_xfail` flag (§3). Top-level aggregation rule: `verdict.overall == fail` iff some case with `expected_xfail == .false.` has a failing per-case verdict; a per-case failure of a case with `expected_xfail == .true.` (the `input_guard` firing on the guard case) is EXCLUDED from the top-level `failed_checks`, so a run where the only failure is the expected guard reports top-level `{ "overall": "pass", "failed_checks": [] }` with `checks.input_guard.status == pass` (the guard behaved as expected). The per-case `input_guard` failure is confined to `per_case.<guard_case>`. The per-case `metrics` object holds one leaf per `h_metric` the caller supplied for that case (dotted-address key ⇒ numeric value; a `is_na` metric is written as `"<address>": null` plus a sibling `"<address>_reason_na": "<reason>"`); the self-test supplies no metrics, so each of its per-case `metrics` is `{}`.
- **`perf.json`** — one object with `case_id`, `target` (`"cpu"`), `walltime_sec`, `steps`, `cells_updated`, `throughput_cells_per_sec` (`= cells_updated / walltime_sec`), a `parallelism` object (`mpi_ranks`, `threads_per_rank`, `gpu_devices`, `parallel_degree_total = mpi_ranks*threads_per_rank*max(gpu_devices,1)`), and `timestamp_utc` (ISO-8601).
- **`raw/state_snapshots/<case_id>.json`** — exactly one per case, named at runtime as `'raw/state_snapshots/'//trim(case_id)//'.json'` (never a hardcoded/sequential literal). Each holds every variable in *that case's* `io_contract.test_evidence_requirements.required_raw_variables` plus the declared scalar `time_variable` `t` (value `0.0`; the self-test has a single `steps=1` step). The snapshot state variables (declared in `snapshot_schema.json` with their `shape_expr`) are, per case:
  - `l0_numeric_roundtrip_pass`: `x_in` (rank-1, `[3]` — the sentinel reals), `x_out` (rank-1, `[3]` — the values re-parsed from `__emit_real`'s tokens), `max_abs_deviation` (scalar).
  - `l0_boolean_literal_pass`: `bool_match` (scalar, `1.0` iff `.true.`/`.false.` emitted the exact literals `true`/`false`).
  - `l0_array_emit_pass`: `a1` (rank-1, `[2]`), `a2` (rank-2, `[2,2]`), `a3` (rank-3, `[2,2,2]`), `a4` (rank-4, `[2,2,2,2]`) — the inputs to `__emit_array_r1..r4` — and `max_abs_deviation` (scalar — max component deviation of the re-parsed arrays).
  - `l0_case_fanout_pass`: `case_index` (scalar — this case's ordinal in the run).
  - `l0_perf_derived_pass`: `throughput_residual` (scalar — `|throughput_cells_per_sec - cells_updated/walltime_sec|`).
  - `l0_missing_cases_xfail`: `guard_fired` (scalar, `1.0` when `__parse_cases` on a length-0 token array returned `ok=.false.`). A guard case still emits its snapshot, shape-valid.
  Each case's `required_raw_variables` is exactly its listed variables above; a variable is declared once in `snapshot_schema.json` and emitted only by the cases that require it.
- **`raw/metrics_basis.json`** — a `{ "per_test": [ { "test_id": <id>, <that test's required_raw_variables> }, ... ] }` index with one entry per `tests.md` `test_id`, holding only primary evidence (never a copy of `diagnostics.json`). Assembled inside `__write_metrics_basis` from the caller-supplied per-test entry records (§3).

Numeric serialization is the runner-output contract (`docs/workflow/RUNNER_OUTPUT_CONTRACT.md §4`): reals via `ES24.16E3` then `trim(adjustl())` (or a bounded `Fw.d`, never `F0`/`F0.d`), integers via `I0`, booleans by branching to the literal token `true`/`false` (never an `L`-family descriptor).

## 3. Operation definition
The published operations (all under module `harness_fortran_cpu_model`, prefix `harness_fortran_cpu__`) are the plumbing surface a physics-node runner reuses. The canonical machine-readable interface (every signature and public type, verbatim) is §5.1; the prose below states each operation's contract. The module declares two module-level integer parameters the signatures reference: `dp = real64` (double precision kind) and `case_id_len = 64` (the fixed storage width of a parsed case id, because an assumed-length `intent(out)` character dummy is disallowed). These are internal parameters (not part of the public export list); a consuming runner passes matching `real(real64)` actuals and declares its own `character(len=64)` case-id buffer. Their VALUES are pinned (the gate rejects a drifted `case_id_len`), because a consumer's hardcoded length must match.

### 3.1 Published derived types
The module publishes five derived types (each named by its fully-qualified `harness_fortran_cpu__<name>`):
- `harness_fortran_cpu__h_named` `{ name: character(:), json: character(:) }` — a **boxed named value**: a JSON key `name` and its already-serialized JSON value `json`. It lets heterogeneous-rank / ragged snapshot and metrics-basis variables travel through one homogeneous `type(harness_fortran_cpu__h_named) :: values(:)` array (Fortran forbids passing differing ranks through one plain array argument — this box, holding the pre-serialized token, is the workaround). The caller `__box`es a value into it before serialization is complete.
- `harness_fortran_cpu__h_check` `{ id: character(:), status: character(len=4) }` — one per-case check result: its `id` and honest `status` (`'pass'` / `'fail'` / `'na  '`, right-padded to width 4). The caller computes `status`; the harness does not judge.
- `harness_fortran_cpu__h_metric` `{ name: character(:), value: real(dp), is_na: logical, reason_na: character(:) }` — one per-case metric leaf: its dotted-address `name`, numeric `value`, an `is_na` flag, and a `reason_na` string used only when `is_na` is true.
- `harness_fortran_cpu__h_case_result` `{ case_id: character(:), expected_xfail: logical, checks(:): type(h_check) allocatable, metrics(:): type(h_metric) allocatable }` — one case's complete result: its `case_id`, whether its failure is expected (`expected_xfail`, driving the top-level xfail exclusion), and its `checks` and `metrics` arrays. This is the data-driven record `__write_diagnostics` folds; it embeds no harness-side judgment.
- `harness_fortran_cpu__h_mb_entry` `{ test_id: character(:), values(:): type(h_named) allocatable }` — one metrics-basis per-test entry: its `test_id` and the caller-boxed (ragged, any-rank) `values` that form its primary-evidence body.

### 3.2 Published operations
- `harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, ncases, ok)` — parse a supplied argv **token list** (NOT the process argv directly, so the guard is exercisable with a synthetic list): find `--cases` in `tokens`, skip the following token (the positional, unread spec path), collect the remaining tokens as `case_ids` (each stored in a `character(len=case_id_len)` slot); return `ok = .false.` when `--cases` is absent from `tokens` or no `case_id` follows it (the input guard), else `ok = .true.`. The self-test's main program marshals the real process argv (`command_argument_count` / `get_command_argument`) into `tokens` for the normal cases, and passes a length-0 `tokens` for the guard case.
- `harness_fortran_cpu__emit_real(x) result(s)` — format a `real(dp)` scalar as a JSON numeric token (`ES24.16E3`, `trim(adjustl)`).
- `harness_fortran_cpu__emit_int(i) result(s)` — format an integer as a JSON numeric token (`I0`).
- `harness_fortran_cpu__emit_bool(b) result(s)` — format a `logical` as the JSON literal `true`/`false`.
- `harness_fortran_cpu__emit_array_r1(a) result(s)` … `__emit_array_r4(a) result(s)` — format an assumed-shape rank-1..4 `real(dp)` array as a nested JSON array (`[ ... ]`, row-major over the leading index), reusing `__emit_real` per element.
- `harness_fortran_cpu__box(name, json) result(nv)` — pack a JSON key `name` and an already-serialized JSON value `json` into a `type(harness_fortran_cpu__h_named)`. A consuming runner emits each of a case's snapshot variables (via the matching `__emit_*`), boxes each with `__box`, and hands the resulting `values(:)` array to `__write_snapshot` in one call; in a physics node the host-rendered glue emits this `__box` list mechanically from `snapshot_schema.json` (the harness stays stateless — it holds no snapshot registry, and does no serialization of the boxed value beyond copying the caller's token).
- `harness_fortran_cpu__write_snapshot(case_id, values, time)` — write the per-case `raw/state_snapshots/<case_id>.json` (runtime-built filename) holding the boxed state variables in `values` (`type(harness_fortran_cpu__h_named) :: values(:)`, each written under its `name` with its already-serialized `json`) plus the scalar time variable.
- `harness_fortran_cpu__write_metrics_basis(entries, n)` — write `raw/metrics_basis.json` as the `per_test` index from `entries(1:n)` (`type(harness_fortran_cpu__h_mb_entry) :: entries(:)`); the harness assembles the `{ "per_test": [ ... ] }` envelope and, per entry, the `{ "test_id": ..., <boxed values> }` body from the entry's `values`. **Data-driven plumbing**: the caller supplies the boxed evidence; the harness owns the JSON envelope.
- `harness_fortran_cpu__write_diagnostics(results, n)` — write `diagnostics.json` from `results(1:n)` (`type(harness_fortran_cpu__h_case_result) :: results(:)`). The harness computes every derived value: each case's per-case verdict (`overall == fail` iff any of that case's `checks` has `status == 'fail'`; `failed_checks` = those check ids), the top-level `checks` object (one entry per distinct check id; `status == fail` iff that id fails in some case with `expected_xfail == .false.`), and the top-level `verdict` (xfail-excluded fold per §2). It emits the full JSON: top-level `checks` / `verdict` and the `per_case` map (each case's `checks`, `verdict`, and `metrics` leaf object, with an `is_na` metric encoded as `null` + a `_reason_na` sibling). **Data-driven plumbing, not judgment**: the harness folds and serializes; the per-case check statuses, metric values, and each case's `expected_xfail` are computed by the (self-test or physics) caller and passed in. It embeds no per-test pass/fail decision of its own.
- `harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, walltime_sec, mpi_ranks, threads_per_rank, gpu_devices)` — write `perf.json` with all required fields incl. the derived `throughput_cells_per_sec` and the `parallelism` object (`parallel_degree_total = mpi_ranks*threads_per_rank*max(gpu_devices,1)`).

The self-test `harness_fortran_cpu_runner.f90` calls `__parse_cases`, then for each `case_id` runs (via `select case`) the plumbing check that case names — verifying the emitter round-trips, the case fan-out, and the input guard — builds that case's `h_case_result` (its `checks`, empty `metrics`, and `expected_xfail` from the case's expected outcome) and its `h_mb_entry`, and finally calls the four writers. Because the writers use the emitters, a correct emitter is necessary for a correct output; the checks are the harness's own verification that its plumbing is faithful.

## 4. Failure conditions and constraints
A missing `--cases` flag (or no `case_id` after it) is a hard input error — `__parse_cases` returns `ok=.false.`, and the self-test's `l0_missing_cases_xfail` case exercises this guard by calling `__parse_cases` on a synthesized empty token list and confirming `ok=.false.` (recorded as the `input_guard` check firing, with the case's `h_case_result` carrying `expected_xfail=.true.`). A JSON emitter whose re-parsed token does not reproduce its input within an absolute tolerance of `1e-12` is a failure of the corresponding check.

## 5. Public API and compatibility
The published `operation_id`s are exactly: `harness_fortran_cpu__parse_cases`, `harness_fortran_cpu__emit_real`, `harness_fortran_cpu__emit_int`, `harness_fortran_cpu__emit_bool`, `harness_fortran_cpu__emit_array_r1`, `harness_fortran_cpu__emit_array_r2`, `harness_fortran_cpu__emit_array_r3`, `harness_fortran_cpu__emit_array_r4`, `harness_fortran_cpu__box`, `harness_fortran_cpu__write_snapshot`, `harness_fortran_cpu__write_metrics_basis`, `harness_fortran_cpu__write_diagnostics`, `harness_fortran_cpu__write_perf`. The module also publishes the derived types `harness_fortran_cpu__h_named`, `harness_fortran_cpu__h_check`, `harness_fortran_cpu__h_metric`, `harness_fortran_cpu__h_case_result`, and `harness_fortran_cpu__h_mb_entry`. A change breaking `major` compatibility of any signature (or of a published derived type's component layout) is separated into a different name; consuming physics nodes are re-certified against a new harness version (harness version skew ⇒ dependent re-certification).

### 5.1 Canonical interface block
The exact published surface, as a machine-readable Fortran interface block. The generated `harness_fortran_cpu_model.f90` must publish every type and operation below with these signatures verbatim (formatting/continuations/comments may differ; identifiers, argument names, argument order, types, ranks, `intent`s, and `result` names may not). The deterministic gates parse this block: the `--stage compile` gate cross-checks its symbol set against §5, and the `Generate.static` gate pins the generated model source against these signatures (normalized: comments stripped, continuations joined, case-folded, whitespace-insensitive).

```fortran
! Module integer parameters referenced by the signatures below (internal, value-pinned; not exported).
integer, parameter :: dp = real64
integer, parameter :: case_id_len = 64

! ---- Published derived types ----
type :: harness_fortran_cpu__h_named
  character(len=:), allocatable :: name
  character(len=:), allocatable :: json
end type harness_fortran_cpu__h_named

type :: harness_fortran_cpu__h_check
  character(len=:), allocatable :: id
  character(len=4) :: status
end type harness_fortran_cpu__h_check

type :: harness_fortran_cpu__h_metric
  character(len=:), allocatable :: name
  real(dp) :: value
  logical :: is_na
  character(len=:), allocatable :: reason_na
end type harness_fortran_cpu__h_metric

type :: harness_fortran_cpu__h_case_result
  character(len=:), allocatable :: case_id
  logical :: expected_xfail
  type(harness_fortran_cpu__h_check), allocatable :: checks(:)
  type(harness_fortran_cpu__h_metric), allocatable :: metrics(:)
end type harness_fortran_cpu__h_case_result

type :: harness_fortran_cpu__h_mb_entry
  character(len=:), allocatable :: test_id
  type(harness_fortran_cpu__h_named), allocatable :: values(:)
end type harness_fortran_cpu__h_mb_entry

! ---- Published operations ----
subroutine harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, ncases, ok)
  character(len=*), intent(in) :: tokens(:)
  integer, intent(in) :: ntokens
  character(len=case_id_len), intent(out) :: case_ids(:)
  integer, intent(out) :: ncases
  logical, intent(out) :: ok
end subroutine harness_fortran_cpu__parse_cases

function harness_fortran_cpu__emit_real(x) result(s)
  real(dp), intent(in) :: x
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_real

function harness_fortran_cpu__emit_int(i) result(s)
  integer, intent(in) :: i
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_int

function harness_fortran_cpu__emit_bool(b) result(s)
  logical, intent(in) :: b
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_bool

function harness_fortran_cpu__emit_array_r1(a) result(s)
  real(dp), intent(in) :: a(:)
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_array_r1

function harness_fortran_cpu__emit_array_r2(a) result(s)
  real(dp), intent(in) :: a(:,:)
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_array_r2

function harness_fortran_cpu__emit_array_r3(a) result(s)
  real(dp), intent(in) :: a(:,:,:)
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_array_r3

function harness_fortran_cpu__emit_array_r4(a) result(s)
  real(dp), intent(in) :: a(:,:,:,:)
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_array_r4

function harness_fortran_cpu__box(name, json) result(nv)
  character(len=*), intent(in) :: name
  character(len=*), intent(in) :: json
  type(harness_fortran_cpu__h_named) :: nv
end function harness_fortran_cpu__box

subroutine harness_fortran_cpu__write_snapshot(case_id, values, time)
  character(len=*), intent(in) :: case_id
  type(harness_fortran_cpu__h_named), intent(in) :: values(:)
  real(dp), intent(in) :: time
end subroutine harness_fortran_cpu__write_snapshot

subroutine harness_fortran_cpu__write_metrics_basis(entries, n)
  type(harness_fortran_cpu__h_mb_entry), intent(in) :: entries(:)
  integer, intent(in) :: n
end subroutine harness_fortran_cpu__write_metrics_basis

subroutine harness_fortran_cpu__write_diagnostics(results, n)
  type(harness_fortran_cpu__h_case_result), intent(in) :: results(:)
  integer, intent(in) :: n
end subroutine harness_fortran_cpu__write_diagnostics

subroutine harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, walltime_sec, mpi_ranks, threads_per_rank, gpu_devices)
  character(len=*), intent(in) :: case_id
  character(len=*), intent(in) :: target
  integer, intent(in) :: steps
  integer, intent(in) :: cells_updated
  real(dp), intent(in) :: walltime_sec
  integer, intent(in) :: mpi_ranks
  integer, intent(in) :: threads_per_rank
  integer, intent(in) :: gpu_devices
end subroutine harness_fortran_cpu__write_perf
```

## 6. Prohibitions
- No physics: the harness must embed no per-case kernel or per-test judgment logic; those are the consuming physics node's `case_run` / `checks_compute` callbacks. `__write_diagnostics` folds caller-supplied statuses and each case's `expected_xfail`; it never decides pass/fail itself.
- No forbidden Fortran JSON descriptors anywhere in the generated source: never `F0` / `F0.d` for a numeric token, never an `L`-family logical descriptor for a boolean (branch to the literal instead).
- Never write `verdict.json`, `aggregate_verdict.json`, `summary.json`, or `trial_meta.json` — not even as a literal filename inside a comment or example string.
- No launch of an external interpreter (`python` / `bash` / `sh` / `node`).
- All output paths are written relatively (so `cd $(RUNDIR)` redirects them); no hardcoded / sequential snapshot filename literal.

## 7. Traceability
Record the harness adoption in `component_catalog.yaml` (as the `(language, hardware)` = `(fortran, cpu)` runner harness) and the resolved harness version each dependent physics node was certified against.

## 8. tests reference
The corresponding `tests.md` is `spec/infrastructure/infra/harness/harness_fortran_cpu/tests.md`, with `test_profile_version` of `0.2.0`.
