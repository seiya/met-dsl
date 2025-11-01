# Data Model: Nonlinear Advection Solver DSL

## Entities

### DSL Specification
- **Fields**
  - `spec_id` (UUID) – Stable identifier for the specification record.
  - `version_id` (string) – Auto-generated monotonic label (`spec_id` + timestamp + sequence).
  - `grid_config` (object) – Dimensions, staggering metadata, spacing for Arakawa-C layout (must validate even cell counts along both axes).
  - `boundary_conditions` (object) – Dual periodic configuration with axis-level toggles; only `periodic` accepted for this example.
  - `physics_terms` (list) – Nonlinear advection and diffusion stencil definitions, each containing symbolic DSL expressions and coefficient metadata.
  - `rk4_stages` (array[stage]) – Ordered collection of stage expressions, each with intermediate field assignments and weights.
  - `validation_hooks` (object) – Definitions for outputs required by the external analysis script, including manifest path and variables of interest.
  - `derived_from` (nullable string) – Reference to parent specification version for reuse flows.
  - `created_at` (datetime) / `updated_at` (datetime) – Timestamps for auditing.
- **Relationships**
  - 1-to-many with **Specification Version Record** (each save creates a version entry).
  - 1-to-many with **Generated Solver Artifact** (each generation run produces an artifact tied to a specific version).
- **Validation Rules**
  - Must include both advection and diffusion operators with second-order centered differences.
  - Must define all four RK4 stages; omission blocks generation.
  - Must reference only declared staggered fields in stencil expressions.

### Specification Version Record
- **Fields**
  - `spec_id` (UUID)
  - `version_id` (string)
  - `sequence` (integer) – Incrementing per specification.
  - `timestamp` (datetime)
  - `author` (string)
  - `change_summary` (string)
  - `derived_from_version` (nullable string)
- **Relationships**
  - Belongs to **DSL Specification**.
- **Validation Rules**
  - `sequence` must increment by exactly 1 relative to previous version for the same `spec_id`.

### Generated Solver Artifact
- **Fields**
  - `artifact_id` (UUID)
  - `spec_version_id` (string)
  - `code_bundle_path` (path)
  - `runtime_config_path` (path)
  - `netcdf_output_dir` (path)
  - `benchmark_manifest_path` (path) – JSON manifest consumed by validation script.
  - `status` (enum: pending, succeeded, failed)
  - `created_at` (datetime)
- **Relationships**
  - Belongs to **DSL Specification** via `spec_version_id`.
  - Provides inputs to **Benchmark Scenario** evaluations.
- **Validation Rules**
  - `status` transitions limited to `pending → succeeded|failed`; retries create new artifacts.
  - Paths must exist within workspace-write sandbox and be versioned by run id.

### Benchmark Scenario
- **Fields**
  - `scenario_id` (string)
  - `analytic_solution` (reference) – Pointer to analytic function or dataset used by analysis script.
  - `initial_condition_path` (path)
  - `tolerance_metrics` (object) – Includes max absolute error (≤5%) and conservation drift (≤1%) thresholds.
  - `duration_steps` (integer) – Expected RK4 steps for validation (e.g., 100).
  - `analysis_script_path` (path)
- **Relationships**
  - Consumes outputs from **Generated Solver Artifact** runs.
- **Validation Rules**
  - `duration_steps` must not exceed specification's total steps (500) for sanity check.
  - `analysis_script_path` must resolve to maintained external toolkit.

## State Transitions

1. **Draft Spec** → **Validated Spec** when completeness checks (grid, boundary, physics, RK4, validation hooks) pass.
2. **Validated Spec** → **Generated Artifact** when solver generation runs successfully.
3. **Generated Artifact** → **Benchmarked Artifact** when external analysis script completes without errors.
4. **Benchmarked Artifact** → **Reusable Template** when author clones or updates derived specifications referencing the recorded version id.

## Data Volume & Scale Assumptions

- Example workload targets up to a 256×256 grid with 500 RK4 steps, producing NetCDF files on the order of hundreds of MB per run.
- Version history expected to remain small (tens of revisions) for the instructional example but must scale to hundreds for real scenarios without redesign.
- Benchmark scenarios limited to a handful of curated cases bundled with the example; external datasets may be pointed to via path references but not stored in repo.
