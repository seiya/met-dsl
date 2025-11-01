# Research Findings: Nonlinear Advection Solver DSL

## DSL Expression for Arakawa-C Grid Physics
- Decision: Model Arakawa-C staggered fields and second-order stencil expressions using existing IR primitives with explicit DSL macros for cell/edge alignment.
- Rationale: Reusing the IR keeps compatibility with other DSL features while allowing authors to write human-readable stencil code that the generator already understands.
- Alternatives considered: Introduce a new bespoke grid IR (rejected: duplicative and increases maintenance), infer staggering automatically (rejected: harms transparency and conflicts with requirement that users author stencils directly).

## Runge–Kutta Time Integration Authoring
- Decision: Require DSL writers to declare RK4 stages via structured blocks that expand to generator loops, leveraging existing stage combinators.
- Rationale: Satisfies requirement that RK4 logic lives in DSL while letting the generator orchestrate code emission consistently with other time integrators.
- Alternatives considered: Auto-generate RK4 (rejected: violates spec), allow free-form scripting in Python (rejected: breaks DSL purity and portability).

## Numerical Kernels and Performance
- Decision: Use NumPy/SciPy vectorized operations in generated artifacts and permit optional loop unrolling only when the benchmark targets fail performance goals.
- Rationale: Vectorized backends align with current toolchain, keep code concise, and meet 256×256×500 performance expectations without premature optimization.
- Alternatives considered: Handmade C/Fortran kernels (rejected: complicates build chain), pure interpreted loops (rejected: unlikely to meet stability window).

## NetCDF Output and Validation Pipeline
- Decision: Persist solver states as CF-compliant NetCDF files via Xarray and emit a JSON manifest for the external validation script to consume.
- Rationale: Aligns with active technology stack, ensures benchmark runner locates outputs deterministically, and supports reproducible validation runs.
- Alternatives considered: Custom binary dumps (rejected: tooling burden), in-memory handoff to validation script (rejected: encourages tight coupling and increases resource pressure).

## Specification Version Traceability
- Decision: Derive monotonic version identifiers from the existing metadata registry, tagging each DSL save with `spec_id` + ISO timestamp + sequence number.
- Rationale: Provides stable lineage independent of filenames, integrates with audit tooling, and remains human-readable for documentation.
- Alternatives considered: Git commit hashes (rejected: detached from DSL storage), user-provided strings (rejected: inconsistent and error-prone).

## CLI Exposure and Documentation
- Decision: Extend Typer CLI with subcommands for generating the solver, running the benchmark, and invoking the validation script with structured JSON output.
- Rationale: Honors CLI-first principle, ensures automation parity, and centralizes user entry points for the example workflow.
- Alternatives considered: Ad-hoc scripts (rejected: fragment tooling), GUI integration (rejected: out of scope and higher maintenance).
