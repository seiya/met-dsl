# Checks-module contract (`<spec_id>_checks.f90`, fixed ABI)

> **Audience: the `Generate.generate` / `Generate.verify` leaves of an *M3c
> physics node* — a `build_system=make`, `language=fortran` node with exactly
> one `infrastructure` (runner-harness) dependency.** On such a node the leaf
> authors **two** Fortran sources: `<spec_id>_model.f90` (the physics kernel +
> the published `__apply` operation) and `<spec_id>_checks.f90` (this contract).
> It does **not** author `<spec_id>_runner.f90` or `src/Makefile` — those are
> host-rendered by the conductor (`tools/runner_renderer.py` /
> `_write_makefile`) and are outside the leaf's `allowed_output_paths`. This is
> not read by `Validate.judge`.

The rendered runner is glue: it drives this module's callbacks and emits the
standard runner outputs **through the certified `harness_fortran_cpu` plumbing**,
which owns all JSON serialization and the verdict fold. So the checks module
holds **no serialization and no I/O**: it computes honest per-case state, checks,
and metrics; the harness folds and writes them. Getting this split right is what
lets the runner be host-rendered (deterministic, no leaf regenerate loop).

## 1. The fixed ABI

`module <spec_id>_checks` publishes **exactly** these ten non-prefixed names
(module scope makes them collision-free — the harness's symbols are all
`harness_fortran_cpu__*` and the model's are `<spec_id>__*`, so a bare
`case_run` cannot clash; non-prefixed names keep every identifier under the
f2008 63-character limit, which a `<spec_id>__checks_compute` would exceed for a
long `spec_id`). Author them verbatim:

```fortran
public :: case_setup, case_run, get_time
public :: get_scalar, get_r1, get_r2, get_r3, get_r4
public :: checks_compute, metric_compute

! Initialize this case's state from the spec's fixed inputs/constants. ok=.false.
! rejects a guard / xfail input (e.g. an invalid grid size) — the case still
! proceeds so its snapshot + input_guard check are produced.
subroutine case_setup(case_id, ok)
  character(len=*), intent(in) :: case_id
  logical, intent(out) :: ok
end subroutine case_setup

! Run the model kernel's time loop for this case and return the perf counters.
! A non-time-stepping component uses steps=1 and cells_updated = cells touched.
subroutine case_run(case_id, steps, cells_updated, ok)
  character(len=*), intent(in) :: case_id
  integer, intent(out) :: steps, cells_updated
  logical, intent(out) :: ok
end subroutine case_run

! The scalar time value for this case's single snapshot (real(dp), 0.0 for an
! untimed component).
subroutine get_time(t)
  real(dp), intent(out) :: t
end subroutine get_time

! Snapshot getters, one per rank. `name` is a state-snapshot variable name;
! `found` is .true. iff this case owns that variable. The rank-N getters return
! an ALLOCATABLE array the getter allocates to the variable's shape.
subroutine get_scalar(name, val, found)
  character(len=*), intent(in) :: name
  real(dp), intent(out) :: val
  logical, intent(out) :: found
end subroutine get_scalar

subroutine get_r1(name, arr, found)
  character(len=*), intent(in) :: name
  real(dp), allocatable, intent(out) :: arr(:)
  logical, intent(out) :: found
end subroutine get_r1
! get_r2(name, arr(:,:), found), get_r3(name, arr(:,:,:), found),
! get_r4(name, arr(:,:,:,:), found) — identical apart from the array rank.

! The honest per-case check results. Fill `check_ids(1:ncheck)` /
! `status(1:ncheck)` for the checks THIS case evaluates. `status` is one of
! 'pass' / 'fail' / 'na  ' (right-padded to width 4). Report the honest result —
! an xfail case whose guard fired reports its check as 'fail'; the xfail
! adjustment is the harness fold's job, NOT this module's.
subroutine checks_compute(case_id, ncheck, check_ids, status)
  character(len=*), intent(in) :: case_id
  integer, intent(out) :: ncheck
  character(len=32), intent(out) :: check_ids(:)
  character(len=4), intent(out) :: status(:)
end subroutine checks_compute

! One diagnostics_contract.metrics leaf (dotted address, e.g. 'error.l2') for
! this case. `found` is .false. when this case does not produce that metric (it
! is then omitted). `is_na`/`reason_na` carry an honestly-unavailable value.
subroutine metric_compute(case_id, name, val, is_na, reason_na, found)
  character(len=*), intent(in) :: case_id
  character(len=*), intent(in) :: name
  real(dp), intent(out) :: val
  logical, intent(out) :: is_na
  character(len=:), allocatable, intent(out) :: reason_na
  logical, intent(out) :: found
end subroutine metric_compute
```

Pinned widths (the rendered runner declares matching actuals, so they must
match): `check_ids` is `character(len=32)`, `status` is `character(len=4)`.
`reason_na` is a deferred-length allocatable (`character(len=:), allocatable`).

## 2. Semantics the harness relies on

- **Snapshot getters return shape-valid values even for a rejected case.** A
  guard/xfail case whose `case_setup` returned `ok=.false.` must STILL return a
  shape-valid array/scalar for every snapshot variable that case requires (the
  runner always emits the case's snapshot). Return a defined placeholder (e.g.
  zeros of the right shape), never leave the array unallocated.
- **`checks_compute` is honest, never judgmental.** Report `'fail'` when a check
  fails, even for an xfail case. The harness `__write_diagnostics` computes the
  per-case verdict (`overall == 'fail'` iff any of that case's checks is
  `'fail'`), the top-level fold, and the **xfail exclusion** (a failing case
  whose `expected_xfail` is true — supplied by the runner from the IR predicates
  — is excluded from the top-level `failed_checks`). Do not pre-adjust for xfail
  here; doing so double-counts and breaks the guard-passes-at-top-level rule.
- **NA metrics.** When a metric is honestly unavailable set `found=.true.`,
  `is_na=.true.`, and `reason_na` to a short reason; the harness encodes it as
  `"<address>": null` plus a sibling `"<address>_reason_na": "<reason>"`. A
  metric that simply does not apply to a case sets `found=.false.` (omitted).
- **`case_run` perf counters** feed the single `perf.json` (`steps` summed,
  `cells_updated` summed across the run); make them the real work done.
- **Metrics-basis scope (M3c-β).** The host-rendered runner records each test's
  `raw/metrics_basis.json` per-test evidence from that test's **first target case**.
  Every M3c test today is 1:1 case↔test, so this is exact. A multi-target-case test
  (a convergence/resolution sweep needing evidence from *every* targeted case) is an
  R3 test-kind not yet served by the renderer — do not author such a test on an M3c
  node until R3 extends the metrics-basis shape.

## 3. Module-level state is expected

The runner calls `case_setup` then `case_run` then the getters for one case at a
time. Keeping the current case's fields (and any cross-case accumulators a metric
needs) in **module-level variables** is the intended pattern — the getters read
that state. Key any cross-case accumulation by `case_id`.

## 4. Prohibitions

- **No `use harness_*`** in EITHER `<spec_id>_checks.f90` or
  `<spec_id>_model.f90`. The physics node never depends on the harness module at
  the source level — the rendered runner is the sole `use harness_fortran_cpu_model`
  site. (The harness's `<spec_id>_model.o` is linked via the closure, but the
  physics sources must not name it.)
- **No file I/O in the checks module** — no `open` / `write(unit=...)` to a file,
  no `verdict.json` / `aggregate_verdict.json` / `summary.json` / `trial_meta.json`
  even as a comment or example string. Emission is the harness's exclusive job;
  the checks module only computes.

## 5. Fortran legality guards

- **`intent(out)` character dummies** must be fixed-length (`character(len=32)`)
  or a deferred-length allocatable (`character(len=:), allocatable`) — an
  assumed-length `intent(out)` (`character(len=*)`) is illegal, matching the
  harness's own rule.
- **`spec_id` ≤ 55 characters** so the derived `<spec_id>_checks` / `_runner` /
  `_model` identifiers stay within the f2008 63-character limit (the renderer
  fails closed above this).
- Author lint-clean f2008 (`use ..., only:`, the inline `! allow(C003)` directive
  before `implicit none`, ≤100-column lines) — the deterministic `Generate.lint`
  substep lints the whole `src/` tree, this module included.
