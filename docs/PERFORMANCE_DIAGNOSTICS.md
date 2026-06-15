# Performance diagnostics (perf.json) specification

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
- A `toolchain.language=fortran` `runner` must not directly embed the `F0.d` format into a `JSON` numeric token.
- A `toolchain.language=fortran` `runner`, when outputting a logical value to `JSON`, must not directly embed the `T`/`F` token that the `L`-family edit descriptor (`L1` etc.) generates into a `JSON` boolean token. A `JSON` boolean allows only the literals `true` / `false`, so branch on the logical value and write the string.
