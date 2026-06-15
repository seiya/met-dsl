# Auto-tuning operational workflow (optional flow)

## Position
`Tune` is treated as an **optional flow** separated from the core workflow (`Spec → Compile → Generate → Build → Validate`). The core workflow uses the `impl_defaults` of `spec.ir.yaml` as a fixed value, but `Tune` explores variants of `impl_defaults` with `spec.ir.yaml` as an invariant premise.

## Basic policy
The tuner is separated from the generator, and the following loop is the standard operation.

1. Fix the structural IR (`spec.ir.yaml`, already passed in the core workflow)
2. Explore implementation-discretion variants (define overrides of **only the knob layer** of `impl_defaults` in `tuning.spec`)
3. Automate generation/build/execution/evaluation (run the same Generate / Build / Validate as the core workflow per variant)
4. Optimize a performance objective function with physical passing as a constraint
5. Fix the best candidate and move to regression monitoring

## The override-allowed boundary of `impl_defaults` (must read)
The range `Tune` can override via `tuning.spec` is **limited to the knob layer of `impl_defaults`**. For the canonical fixed / knob boundary, refer to the "fixed / knob boundary of impl_defaults" section of `docs/workflow/phases/phase_01_compile.md`. Key points:

| crossing-forbidden (fixed) | override-allowed (knob) |
|---|---|
| `target.class` / `target.backend` / `target.architecture` | `abstract.*` (the intent of parallelization granularity / layout / fusion / tiling, etc.) |
| `toolchain.language` / `toolchain.standard` / `toolchain.build_system` | `backend_overrides.<key>.*` (backend-specific values such as thread count / block size / vector width) |
| `selected.backend_key` | |

When `tuning.spec` includes an entry that overrides a fixed sub-key, `Tune` shall **stop with fail_closed at launch**, and must not generate a variant inside `Tune`. This guarantees that Tune does not break the structure of `spec.ir.yaml`.

To change the fixed layer for new hardware/compiler, redo `Compile` from the core workflow and issue a new `ir_id`. This is not the responsibility of Tune.

Design points:
- The physics guarantee (A fixed) and the performance exploration (B exploration) can be clearly separated (separation at the IR level)
- Even if the generation model is replaced, the tuner's logic does not change
- The cause is easy to localize on failure (physics fail vs performance noise vs unimplemented)

## 1. Composition of the loop (the practical minimal form)
### Inputs
- `spec.ir.yaml` (invariant, finalized in the core workflow)
- `tuning.spec` (a Tune-dedicated input that defines the search_space of the exploration range)
- code templates (a group of implementation patterns)

### Per-trial Outputs
- a `spec.ir.yaml` for the variant (a copy with `impl_defaults` overridden by `tuning.spec`)
- `<stage>_meta.json` (the result of the in-`LLM`-stage verification)
- `diagnostics.json` (physics)
- `perf.json` (performance)
- `verdict.json` (physics pass/fail + performance judgment if possible)
- `trial_meta.json` (build log, environment, random seed, git sha)

### Loop
- candidate generation (LLM-assisted / BO / rules)
- (if needed) generate a code diff with the generator
- LLM-using candidate generation / code generation applies the "Handling of the LLM" of `SPEC.md`
- the standard operation is `debug_mode=false`, and does not save failed-attempt artifacts. Only during investigation is `debug_mode=true` permitted
- recommended: **fix the code structure with templates and branch with impl knobs**. Use the LLM only when adding a new implementation pattern.
- build (per target)
- quick physics gate (a subset of L0-L2)
- perf measurement (multiple times, statistics)
- model update (propose the next candidate)

## 2. Candidate-generation method
Select a staged strategy.

### Stage A: rule-based (required first)
- only safe knobs (tile, fuse, vectorize, layout)
- narrow the number of exploration points (e.g. 20-50)
- purpose: find the region that "clearly gets faster"

### Stage B: Bayesian optimization (recommended)
- assume a BO that can handle discrete knobs (TPE etc.)
- considering noise, re-measure the same point

### Stage C: LLM-assisted (only when needed)
- add a new implementation pattern (e.g. newly create a fused kernel, add an async halo)
- propose "knob additions" and expand the search_space
- note: the LLM is not the main player of the exploration but is used to assist the design of the exploration space

## 3. Physics-passing gate
- do not evaluate the performance of a physics-fail candidate (reduce evaluation cost)
- the tolerance is physical-validity agreement (bitwise not needed)
- select the 2-stage quick→full
- quick: small nx / short t_end (the balance of noise and time)
- full: a case close to production

## 4. Handling of performance measurement
- `perf.json` requires at minimum `walltime_sec`, `throughput_cells_per_sec`, and `parallelism`
- for GPU, add a warm-up, measure multiple times, and save the mean/variance
- a performance regression compared with the baseline can be added to L3

## 5. Cache and reuse
- cache the result by `case_hash` and `impl_hash`, and do not re-run the same trial
- reuse the build artifact by hash too (if possible)

## 6. When to "fix"
- fix the best impl obtained by tuning as an override variant of `spec.ir.yaml.impl_defaults`.
- after fixing, move to regression (physics + performance).
- re-tune only for a new architecture / new compiler
- promote the adopted variant to `releases/` with the optional flow `Promote`.
