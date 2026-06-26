# Runner output contract (diagnostics.json / perf.json / raw/)

> **Audience: the LLM substeps that author, verify, or judge the `runner`'s
> program output — `Generate.generate`, `Generate.verify`, `Validate.judge`.**
> This is the single canonical source for *what the `runner` must emit* and
> *how it must serialize it*. It supersedes the runner-output rules that were
> previously duplicated in `phase_02_generate.md` §2-1, `phase_04_validate.md`
> §4-1, and `PERFORMANCE_DIAGNOSTICS.md` §2/§6. Those docs now reference this
> file. The deterministic `Build` / `Validate.execute` steps (run in-process by
> the conductor) take their contract from `phase_03_build.md` /
> `phase_04_validate.md`; this file is the LLM-facing slice.

The `runner` emits **only** `diagnostics.json`, `perf.json`, and the `raw/`
primary evidence (plus `stdout.log` / `stderr.log`). It must **never** write
`verdict.json`, `aggregate_verdict.json`, `summary.json`, or `trial_meta.json`
— those are authored by `Validate.judge`. The check is a substring match
against the **whole runner source including comments**: even an identical
string inside a comment or example is a fail.

## 1. `diagnostics.json`

Must satisfy `spec.ir.yaml.io_contract.diagnostics_contract`:

- a `checks.<id>` entry for **every** `io_contract.diagnostics_contract.checks[].id`;
- when `diagnostics_contract.verdict.required=true`, a top-level `verdict`
  object carrying the `diagnostics_contract.verdict.fields` keys (e.g.
  `verdict.overall` / `verdict.failed_checks`).

This is how the runner conveys the `tests.md §3` diagnostics contract that
`Generate` cannot read directly (Generate consumes only the IR). A custom
per-case array that omits the contracted `checks.*` / `verdict` is a fail.
Emitting a `verdict` **key inside `diagnostics.json`** is permitted and does not
conflict with the forbidden-filename rule above (that rule targets the judge
artifact *filenames*, not a diagnostics field name).

## 2. `perf.json` (required fields)

One `perf.json` per single case execution. Required fields (a custom minimal
schema such as `{case_count, wall_seconds}` is rejected by the `post_execute`
gate, which mandatorily verifies `walltime_sec` / `throughput_cells_per_sec` /
`parallelism`):

- `case_id`: string
- `target`: `cpu|gpu|...`
- `walltime_sec`: wall-clock time of the whole execution (seconds)
- `steps`: number of execution steps
- `cells_updated`: total updated cells (`nx*ny*nz*steps` etc.)
- `throughput_cells_per_sec`: `cells_updated / walltime_sec`
- `parallelism` (required object): `mpi_ranks` / `threads_per_rank` /
  `gpu_devices` / `parallel_degree_total`
  (`= mpi_ranks * threads_per_rank * max(gpu_devices,1)`)
- `timestamp_utc`: ISO8601 (recommended)

A `perf.json` missing `parallelism` is invalid/unjudgeable.

## 3. `raw/` primary evidence

The required composition is IR-driven —
`spec.ir.yaml.io_contract.raw_requirements.required_evidence` is the canonical
source; do not uniformly require a fixed minimal composition.

- **`raw/metrics_basis.json`** must hold a **`per_test` list (or `tests`
  object)** targeting all `test_id` of `io_contract.test_evidence_requirements`.
  Each entry holds the `test_id` and the primary evidence carrying that
  `test_id`'s `required_raw_variables` without omission (needed for the
  per-test recomputation of `Validate.judge`). A custom structure without a
  per-test index (e.g. a single `evidence[]`) is rejected by `post_execute`
  (`must contain per_test list or tests object`). `metrics_basis.json` holds
  only the primary evidence and must not copy `diagnostics.json`; different
  `test_id` must not overwrite each other.
- **`raw/state_snapshots/<case_id>.json`** — when `state_snapshots` is required,
  the runner writes **exactly one snapshot per case**, named from the `case_id`
  it receives on argv (`--cases <spec.ir.yaml> <case_id>...`, one positional per
  `case.test_case_set[]`). Build the path from that `case_id`
  (`trim(case_id)//'.json'`), never a hardcoded/sequential literal
  (`snapshot_0001.json`) or a single combined file: a string-literal name is
  flagged by `post_generate`; a wrong runtime-built name fails
  `Validate.execute`'s per-`<case_id>.json` deliverable gate. Each snapshot need
  only hold its own case's `test_evidence_requirements` variables (not the
  union); a no-output guard case may omit the output variables but still writes
  its `<case_id>.json`. When `required_evidence` does not declare
  `state_snapshots` as required, `raw/state_snapshots/` must not be required.

## 4. JSON serialization (UTF-8, standard-parseable)

`diagnostics.json` / `perf.json` and every `.json` under `raw/` must be a UTF-8
JSON object restorable by a standard JSON parser. Numeric tokens follow RFC 8259
(`.123` / `-.123` with a missing leading zero are forbidden).

**Fortran runner (`impl_defaults.toolchain.language=fortran`) descriptor
rules** — enforcement is **descriptor-syntactic**: `post_generate`
(`validate_pipeline_semantics --stage post_generate`) flags the mere *presence*
of a forbidden descriptor in a runner JSON write format spec; it never inspects
runtime output, so a manual leading-zero fixup does **not** pass — the
descriptor must not appear at all.

- Do **not** use the `F0` / `F0.d` numeric descriptor for a JSON numeric token.
- Do **not** use the `L`-family logical descriptor (`L1` etc., which emits
  `T`/`F`) for a JSON boolean. Branch on the logical and write the literal
  `true` / `false`.
- **Canonical safe idiom:** reals via a scientific descriptor `ES24.16E3`
  (always a leading digit; width 24 fits a sign so negatives never overflow to
  `***` — `ES23.16E3` is one column too narrow) then `trim(adjustl(...))`, or a
  bounded explicit-width `Fw.d` (e.g. `F20.6`, never `F0`/`F0.d`) with
  `trim(adjustl(...))`; integers via `I0`; booleans via the `true`/`false`
  literal.

  ```fortran
  function jnum(x) result(s)
    real(8), intent(in) :: x
    character(len=32) :: s
    write(s, '(ES24.16E3)') x      ! leading digit guaranteed; width fits a sign; never F0/F0.d
    s = adjustl(s)                 ! trim(adjustl(s)) at the JSON write site
  end function jnum

  function jbool(b) result(s)
    logical, intent(in) :: b
    character(len=5) :: s
    s = merge('true ', 'false', b) ! literal true/false; never an L descriptor
  end function jbool
  ```

## 5. Other runner constraints

- When `impl_defaults.toolchain.language` is a `fortran` / `c` / `cpp` /
  `mixed` family, the `runner` must not launch an external interpreter
  (`python` / `bash` / `sh` / `node`).
- The `runner` writes its output paths **relatively** so a `cd $(RUNDIR)` in the
  `make test`/`check` target redirects them under the run dir (see the Makefile
  contract in `phase_02_generate.md` / the `generate-generate` SKILL).
