# Performance diagnostics (perf.json) specification

> **Leaf agents:** the binding `perf.json` field set (§2) and the Fortran JSON
> numeric/boolean descriptor rules (§6) are mirrored, as the leaf-facing
> canonical, in [`docs/workflow/RUNNER_OUTPUT_CONTRACT.md`](workflow/RUNNER_OUTPUT_CONTRACT.md)
> (your must-read for `Generate` / `Validate.judge`). This document remains the
> full performance-measurement reference; keep the two consistent and add new
> runner-output rules to `RUNNER_OUTPUT_CONTRACT.md`.

## Purpose
- Make it possible to handle performance measurement and performance-regression detection in the same "framework" as the physical-validity tests.
- Leave CPU/GPU and optimization-transformation comparisons in a form that can be recorded and visualized.

## 1. Collection unit
- Output 1 `perf.json` per single case execution (case_id, target, impl setting).
- Output each sub-case of refinement or sweep likewise.

## 2. Minimal fields (required)
- `case_id`: string
- `target`: cpu|gpu|...
- `walltime_sec`: the wall-clock time of the whole execution (seconds)
- `steps`: the number of execution steps
- `cells_updated`: the total number of updated cells (nx*ny*nz*steps etc.)
- `throughput_cells_per_sec`: cells_updated / walltime_sec
- `parallelism`: parallelism information (required object)
  - `mpi_ranks`: the number of MPI ranks (1 when non-MPI)
  - `threads_per_rank`: the number of threads per rank (1 when single-threaded)
  - `gpu_devices`: the number of GPU devices used (0 for CPU-only)
  - `parallel_degree_total`: the total parallelism. Defined as `mpi_ranks * threads_per_rank * max(gpu_devices,1)`
- `timestamp_utc`: ISO8601 (may be optional but recommended)

## 3. Recommended fields (if possible)
- `kernel_breakdown`: the time (seconds) and ratio per main kernel
- `memory_bytes_read/write`: may be an estimate
- `device`: GPU name, SM count, etc.
- `compiler`: compiler/version, main flags
- `impl_hash`: the hash of `spec.ir.yaml.impl_defaults`
- `git_sha`: the commit of the executed code

## 4. Measurement notes
- Because there are warm-up (GPU) and cache effects, if possible run multiple times and also record statistics (mean/variance).
- In Phase 0, make the acquisition of walltime and throughput required, and do not over-expand the measurement items.

## 5. Position of performance tests
- Performance evaluation presumes "physical-test passing".
- A performance regression can be added to L3.
- Example: fail if throughput drops 10% or more below the baseline (but considering noise, it is preferable to handle it statistically).

## 6. Coordination with the runner
- The runner can read `perf.json` and append the performance check to `verdict.json`.
- On a physics fail, skip the performance evaluation (it has little meaning).
- A `perf.json` with `parallelism` missing is treated as invalid input and made unjudgeable (error).
- `perf.json` must be a single UTF-8 `JSON object` restorable by a standard parser.
- Numeric tokens follow `RFC 8259`, and `.123` and `-.123` with a missing leading zero are forbidden.
- A `toolchain.language=fortran` `runner` must not directly embed the `F0` / `F0.d` format into a `JSON` numeric token.
- A `toolchain.language=fortran` `runner`, when outputting a logical value to `JSON`, must not directly embed the `T`/`F` token that the `L`-family edit descriptor (`L1` etc.) generates into a `JSON` boolean token. A `JSON` boolean allows only the literals `true` / `false`, so branch on the logical value and write the string.
- **Enforcement is descriptor-syntactic, not output-based.** The `post_generate` static analysis (`validate_pipeline_semantics --stage post_generate`) flags the mere *presence* of an `F0` / `F0.d` numeric descriptor (and the `L`-family logical descriptor) in any runner `JSON` write format spec. It never inspects the runtime output, so a manual leading-zero fixup (e.g. computing the value, then string-patching a missing `0`) does **not** satisfy the gate — the forbidden descriptor must not appear in the format spec at all.
- **Canonical safe idiom (`fortran`):** emit reals with an explicit scientific descriptor such as `ES24.16E3` (always emits a leading digit; width 24 = sign + `d.dddddddddddddddd` + `E±ddd`, so it never overflows to `****` even for negatives — `ES23.16E3` is one column too narrow and prints `***` for a negative value), then `trim(adjustl(...))`; or, when the magnitude range is known small, a bounded explicit-width `Fw.d` (e.g. `F20.6`, never `F0`/`F0.d`) with `trim(adjustl(...))`. Emit integers with `I0`. Emit booleans by branching on the logical and writing the literal `true` / `false`. Example:

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
