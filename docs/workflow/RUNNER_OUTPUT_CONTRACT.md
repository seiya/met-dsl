# Runner output contract (diagnostics.json / perf.json / raw/)

> **Audience: `Validate.judge` and a runner-authoring `Generate` leaf.** The single
> canonical source for *what the `runner` must emit* and *how it serializes it* — §1
> (`diagnostics.json`) and §3 (`raw/` evidence) are what the judge recomputes against
> the runner's output. It supersedes the runner-output rules once duplicated in
> `phase_02_generate.md` §2-1, `phase_04_validate.md` §4-1, and
> `PERFORMANCE_DIAGNOSTICS.md` §2/§6. The deterministic `Build` / `Validate.execute`
> steps take their contract from `phase_03_build.md` / `phase_04_validate.md`.

> **Scope note (R1/M3c-β → M3d).** On an *M3c node* (make+fortran physics node with
> exactly one `infrastructure`/harness dependency) the `runner` is **host-rendered** —
> the leaf authors `<spec_id>_model.f90` + `<spec_id>_checks.f90` (see
> `CHECKS_MODULE_CONTRACT.md`) and the harness owns the JSON assembly + verdict fold.
> Since M3d this doc is **not a must-read for a physics `Generate` leaf** (it authors
> no runner). It IS still a must-read for a runner-authoring `Generate` leaf — the
> `infrastructure` harness self-test (whose §3 cites §4 here) or a legacy no-harness
> node — and for `Validate.judge`. The *authoring* rules below also survive as
> **deterministic backstops** (name / forbidden-output / JSON-descriptor /
> snapshot-filename gates in `validate_pipeline_semantics.py`).

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
  `Validate.execute`'s per-`<case_id>.json` deliverable gate. Each snapshot must
  hold **every** state variable in *that case's*
  `test_evidence_requirements.required_raw_variables` (not the union across
  cases) **plus the declared `time_variable`**, each shape-matching its
  `snapshot_schema.json` declaration (state variables their `shape_expr`, the
  `time_variable` its `time_shape_expr`). A case that rejects its input or
  produces no meaningful values (e.g. a `*_xfail` length-guard case) **still
  emits those required variables, shape-valid, and must not drop the key** — a
  1-D var as the empty array `[]` (infers shape `[0]`, binding its extent to 0).
  Note a bare `[]` infers as 1-D `[0]` and satisfies **only** a 1-D `shape_expr`;
  a rank-≥2 required var must instead emit a value whose JSON-inferred shape
  matches its `shape_expr` (a nested empty such as `[[]]` infers `[1,0]`). It may
  additionally record a guard flag. "May omit" applies
  **only** to variables *outside* that case's required set; never omit a declared
  required variable.
  - Correct (rejected length-0 guard case): `{"case_id":"l0_invalid_length_xfail","step":0,"n":0,"x":[],"invalid_rejected":true}` — required `x` present as `[]` (1-D, extent 0).
  - Wrong: `{"case_id":"l0_invalid_length_xfail","step":0,"invalid_rejected":true}` — drops the required `x` (and any other required var) → `post_execute` fails with "declared state_variables missing".

  When `required_evidence` does not declare `state_snapshots` as required,
  `raw/state_snapshots/` must not be required.

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
- The `runner` is **always** invoked with `--cases <spec.ir.yaml> <case_id>...`:
  by `run_program` directly and by the `make test`/`check` target (which forwards
  the same argv, so the re-run's `diagnostics.json` equals `run_program`'s). It
  may treat a missing `--cases` as a hard error (no no-argv mode needed); it takes
  the spec path positionally and need not read the spec file.
