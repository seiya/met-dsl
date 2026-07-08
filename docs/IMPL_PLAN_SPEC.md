# Implementation Plan (the `spec.ir.yaml.impl_defaults` section)

## Position
The `impl_defaults` section of `spec.ir.yaml` holds the default values for implementation discretion (B). In the core workflow, the stages from `Generate` onward use this value as a **fixed value**. Variant exploration of implementation discretion is the responsibility of the optional flow `Tune`, and `Tune` separately generates variant candidates with `spec.ir.yaml` as an invariant premise.

## Design Policy
Implementation discretion is expressed in a **2-layer structure (Abstract Knobs + Backend Overrides)**.

- **abstract**: the expression of "intent" that is less hardware/language dependent (easy to auto-explore)
- **backend**: backend-specific parameters such as OpenACC / CUDA Fortran / CUDA C++ (to land in an implementation)

This structure satisfies the following.
- Even if the optional flow `Tune` expands the exploration space, the expression is less likely to break down
- The concrete parameters needed for the implementation can be made explicit
- Even when a backend is added, the existing tuning history is less likely to be wasted

## 1. The boundary of generalization
- Generalize: the "intent" of loop transformation (tiling, fusion, parallel granularity, vectorization, the memory-layout policy, the async/overlap policy)
- Do not generalize: compiler-specific flags, GPU-architecture-specific details, the concrete way of writing a pragma/attribute
- Isolate these in `backend_overrides`

## 2. Required items
`spec.ir.yaml.impl_defaults` requires the following.

- `target.class` (cpu/gpu etc.)
- `target.backend` (e.g. `cpu_fortran_reference`, `cuda_fortran`)
- `target.architecture` (e.g. `x86_64`, `aarch64`, `nvidia_sm80`)
- `toolchain.language` (e.g. `fortran`, `cpp`, `cuda_fortran`)
- `toolchain.standard` (e.g. `2008`, `c++17`)
- `toolchain.build_system` (e.g. `make`, `cmake`, `meson`, `ninja`)
- `abstract` (language-independent knobs)
- `backend_overrides` (language/backend-dependent knobs)
- `selected.backend_key`

Rules:
- **The programming language must be fixed in `Compile`.**
- **The target architecture must be fixed in `Compile`.**
- `toolchain.language` is fixed at `Compile` time. When the user does not explicitly specify the programming language, `target.class=cpu` must adopt `fortran`, and `target.class=gpu` must adopt `cuda_fortran`.
- A deviation from the default of `toolchain.language` is permitted only when the user explicitly specifies the programming language.
- When the user does not explicitly specify the loop parallelization method for `target.class=cpu`, the generator applies `OpenMP` to parallelizable loops.
- When the user explicitly specifies the loop parallelization method, that specification takes precedence. Forcing `OpenMP` onto a non-parallelizable loop is forbidden.
- When `target.class` is other than `cpu` / `gpu`, default-value completion of `toolchain.language` is forbidden.
- When `toolchain.language` / `toolchain.standard` / `toolchain.build_system` are undefined in `impl_defaults`, it is a `fail` in `Compile.verify`.
- When `target.architecture` is undefined, it is a `fail` in `Compile.verify`.
- When `toolchain.language` is a `fortran` / `c` / `cpp` / `mixed` family, `toolchain.build_system` is one of `make` / `cmake` / `meson` / `ninja`. The default is `make`.

## 3. Optional items (environment-dependent)
- `toolchain.compiler` / `toolchain.linker` are **optional**.
- State them only when you want to fix the compiler type/version (emphasizing CI reproducibility).
- When not fixed, use the execution environment's default compiler.
- With `build_system=make` ∧ `language=fortran`, the conductor-authored `src/Makefile` pins `FC` to `toolchain.compiler` when it is set (else `gfortran`), so a future non-gfortran build (e.g. Fujitsu `frt`) only needs this field plus a `run_syntax_check` compiler adapter (`mcp_servers/README.md`). The deterministic `Generate.syntax` gate always runs its mandatory `gfortran -fsyntax-only` stage against `toolchain.standard` regardless of the build compiler (standard conformance is the contract; the build compiler is an implementation detail).
- The operation of directly calling `gcc` / `clang` / `gfortran` for a one-off build is forbidden; always build via `toolchain.build_system`.

## 4. Composition rules of the output (common across languages)
- Regardless of language, the generated code separates `model` (physics computation) and `runner` (input/output / judgment coordination).
- The `runner` calls the `model` via `call` / `use` / `import`.
- The physics-update logic must not be duplicated on the `runner` side.
