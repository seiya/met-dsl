# Feature Specification: Nonlinear Advection Solver DSL

**Feature Branch**: `[002-dsl-advection-solver]`  
**Created**: 2025-11-01  
**Status**: Draft  
**Input**: User description: "example として 2次元非線形移流拡散方程式のソルバーを、DSLを用いて実装する。2重周期境界条件。Arakawa-C grid. 2次中央差分。RK4時間積分。DSLでは、格子系の設定および周期境界条件の設定を指定するとともに、差分形式でのアルゴリズムを記述する。これを処理系がループを用いるなどにより実際のプログラムに展開する。"

## Clarifications

### Session 2025-11-01

- Q: How should benchmark validation be orchestrated for the example solver? → A: Trigger an external analysis script after simulation.
- Q: What maximum grid size and time-step count must the example support? → A: 256×256 grid, 500 RK4 steps.
- Q: How should specification versions be uniquely identified? → A: Auto-generated versioned IDs per revision.
- Q: What capabilities are explicitly out of scope for this example? → A: 3D grids, extra physics, non-periodic boundaries.
- Q: How should the example balance readability vs. performance optimizations? → A: Keep code readable, apply only essential optimizations.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Compose periodic solver specification (Priority: P1)

A numerical modeling scientist defines a complete 2D nonlinear advection-diffusion experiment within the DSL, including grid layout, physical parameters, boundary behavior, explicitly authored discretization stencils, and RK4 stage updates, and registers it via CLI commands so the system can generate an executable solver without manual coding in general-purpose languages.

**Why this priority**: Delivering a full solver from a single DSL specification unlocks the primary value of the platform for atmospheric and ocean modelers exploring prototype scenarios.

**Independent Test**: Provide the canonical benchmark description in the DSL and confirm the processing pipeline generates the solver artifacts end-to-end without additional scripting.

**Acceptance Scenarios**:

1. **Given** a scientist has access to the DSL authoring environment, **When** they declare a 2D Arakawa-C grid with dual periodic boundaries, nonlinear advection, and diffusion terms via the CLI specification creation command, **Then** the system produces the solver configuration and code assets ready for compilation or execution.
2. **Given** the DSL specification contains user-authored stencil expressions and RK4 stage logic, **When** the generator expands the loops, **Then** the resulting solver respects second-order spatial accuracy and RK4 temporal sequencing exactly as written in the DSL.

---

### User Story 2 - Verify solver fidelity against benchmark (Priority: P2)

A verification analyst executes the generated solver, captures outputs, and triggers a dedicated analysis script that compares results with a standard analytic solution (e.g., rotating cosine bell) to ensure numerical errors remain within acceptable tolerances across multiple time steps.

**Why this priority**: Demonstrating solver correctness on known benchmarks builds stakeholder trust and provides regression coverage for future DSL enhancements.

**Independent Test**: Run the solver on the documented benchmark, invoke the external analysis script, and confirm reported error metrics remain under predefined thresholds.

**Acceptance Scenarios**:

1. **Given** a generated solver, reference benchmark data, and the external analysis script, **When** the workflow runs the simulation and then triggers the script, **Then** maximum absolute error and mass conservation drift stay within tolerance bands defined alongside the benchmark.

---

### User Story 3 - Reuse DSL patterns across scenarios (Priority: P3)

A modeling lead clones the example specification, adjusts physical coefficients or source terms, and regenerates a solver to explore sensitivity studies without editing imperative code.

**Why this priority**: Rapid iteration on related experiments broadens adoption and reduces dependence on specialized developers.

**Independent Test**: Duplicate the base DSL file, change only named parameters, and confirm the system regenerates a solver reflecting the new settings while preserving boundary and grid semantics.

**Acceptance Scenarios**:

1. **Given** a derivative DSL specification inherits the same grid structure, **When** model coefficients are modified, **Then** the regenerated solver updates flux calculations accordingly while retaining Arakawa-C staggering and periodicity rules from the base template.

### Edge Cases

- Incomplete grid, physics, boundary, or timestep definitions must produce actionable CLI errors before solver generation proceeds.
- DSL specification omits any RK4 stage update: system must flag the error with actionable guidance before code generation.
- Requested grid resolution yields non-integer staggering offsets: system must block generation and explain valid dimension constraints for Arakawa-C layouts.
- Stability criteria violated by user-selected time step: system must surface warnings with recommended limits rather than silently producing unstable solvers.
- Benchmark validation detects mass or energy drift beyond tolerance: workflow must report failure and retain diagnostics for review.
- RK4 stage definitions or stencil expressions reference undefined fields: system must halt generation and point authors to the missing declarations.
- External analysis script unavailable or returns errors: workflow must mark validation incomplete and display guidance to rerun once the script succeeds.
- Attempt to declare out-of-scope physics (e.g., buoyancy, Coriolis) or boundary types triggers a descriptive validation error pointing authors to supported configurations.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The DSL MUST allow authors to declare 2D staggered grid dimensions, spacing, and Arakawa-C staggering rules in a single specification block.
- **FR-002**: The DSL MUST capture dual periodic boundary conditions for both spatial axes and ensure generated solvers wrap flux computations accordingly.
- **FR-003**: Authors MUST be able to express nonlinear advection and diffusion operators using second-order centered difference stencil expressions directly in the DSL.
- **FR-004**: The DSL MUST allow authors to script fourth-order Runge-Kutta stage updates, including intermediate state accumulation and coefficient weighting, within the DSL algorithm section.
- **FR-005**: The processing pipeline MUST transform a valid specification into executable solver artifacts (code, configuration, and runtime metadata) without manual loop authoring, preserving readability while applying only essential optimizations to meet performance targets.
- **FR-006**: The workflow MUST validate specifications for completeness (grid, physics, boundary, and timestep data) and return human-readable CLI errors and structured telemetry before generation if inputs are insufficient.
- **FR-007**: Generated solvers MUST emit outputs and metadata in a format consumable by an external validation script and provide a trigger point for launching that script post-simulation.
- **FR-008**: The system MUST allow users to clone or extend existing DSL specifications while maintaining traceability between versions and shared components.
- **FR-009**: The generated workflow MUST reliably execute scenarios up to a 256×256 grid over 500 RK4 time steps without exceeding resource or stability constraints.
- **FR-010**: Each saved DSL specification revision MUST receive an auto-generated versioned identifier that persists across renames and enables lineage tracking.
- **FR-011**: The solver workflow MUST detect timestep stability violations and emit CLI warnings with recommended limits before or during execution.
- **FR-012**: The example workflow MUST capture onboarding duration metrics and collect pilot feedback artifacts to evaluate success criteria.

### Key Entities *(include if feature involves data)*

- **DSL Specification**: Represents the complete declarative description of grid layout, physics terms, boundary conditions, numerical schemes, and validation rules for a solver.
- **Generated Solver Artifact**: Bundled output including executable code, configuration files, and metadata produced by expanding the DSL specification.
- **Benchmark Scenario**: Reference dataset or analytic solution definition used to validate solver accuracy and stability once generation is complete.
- **Specification Version Record**: Audit trail capturing lineage between base DSL templates and derivative scenarios for reuse and compliance.
- **Specification Revision Identifier**: Auto-generated version label applied to every saved DSL change, ensuring consistent traceability regardless of user-provided names.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Domain scientists can author the full nonlinear advection-diffusion solver specification and obtain generated artifacts end-to-end within 30 minutes of guided onboarding.
- **SC-002**: Generated solvers reproduce reference benchmark results with maximum absolute error under 5% and conservation drift under 1% across 100 time steps.
- **SC-003**: At least 80% of pilot users report that the DSL example eliminates the need for hand-written loop code during solver setup in initial feedback sessions.
- **SC-004**: 100% of specification validation failures provide actionable CLI messaging and structured telemetry that enable authors to correct inputs without engineering intervention.

## Assumptions

- Target users are numerical modeling scientists familiar with advection-diffusion benchmarks and require ready-to-run examples as onboarding aids.
- Reference benchmarks and tolerance thresholds are provided by the modeling team and considered authoritative for acceptance testing.
- Generated solver artifacts can leverage existing runtime infrastructure already supported by the platform without additional provisioning work in this feature.
- DSL layer provides domain-specific primitives (e.g., staggered field references, derivative operators) but requires authors to script algorithmic steps such as RK4 stages and flux calculations themselves.
- External benchmark analysis scripts are maintained alongside the DSL example and invoked automatically after solver runs complete.
- Performance expectations are based on workstation-class hardware capable of handling 256×256 grids and 500 RK4 steps within acceptable runtime.
- Version identifiers are automatically generated by the platform, removing reliance on user-defined slugs for lineage.
- Example remains limited to 2D nonlinear advection-diffusion with dual periodic boundaries; 3D grids, additional physics, and alternate boundary conditions require separate features.
- Essential optimizations are permitted when required to hit stated performance goals, but the DSL example should remain legible for instructional use.
- Telemetry pipelines capture onboarding session timing and associate pilot feedback artifacts to support success criteria measurement.

## Out of Scope

- Extending the DSL example to three-dimensional grids or hybrid dimensionality.
- Introducing additional physical processes (e.g., buoyancy, Coriolis, chemical reactions) beyond nonlinear advection-diffusion.
- Supporting non-periodic, mixed, or user-defined boundary conditions within this example workflow.
