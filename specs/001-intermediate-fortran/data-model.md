# Data Model: Intermediate IR and Fortran Emission

## IRPackage
- **Identity**: `dsl_model_id` (string) + `config_hash` (sha256 hex) → composite primary identifier.
- **Attributes**:
  - `dsl_model_name` (string) – human-readable label from DSL metadata.
  - `dsl_version` (string) – semantic version provided by author.
  - `created_at` (datetime UTC) – timestamp when IR generated.
  - `normalized_ir` (JSON object) – ordered representation of operations, parameters, and dependencies.
  - `validation_status` (enum: `valid`, `invalid`, `warning`) – result of IR validation pass.
  - `issues` (array of objects) – each item `{code, severity, message, location}`.
  - `artifacts` (array of file paths) – IR files persisted to disk.
- **Relationships**:
  - **1:1** with `EmissionManifest` (each manifest references the originating IR package).
  - **1:many** with `CompilerValidation` (each compiler run ties back to the same IR).
- **Validation Rules**:
  - `normalized_ir` MUST be canonicalised (sorted keys, stable ordering).
  - `issues` MUST be empty when `validation_status = valid`.
  - Config hash MUST match SHA256 of serialized configuration payload.
- **Lifecycle**:
  - `draft` (emission initiated) → `validated` (IR built & passes validation) → `superseded` (newer IR with same composite key generated) or `invalid` (irrecoverable validation failure recorded).

## TargetConfiguration
- **Identity**: `config_path` (absolute path) + `config_hash`.
- **Attributes**:
  - `target` (enum: `fortran2003`, `experimental`) – only `fortran2003` allowed for code generation.
  - `optimization_preset` (enum: `baseline`, `balanced`, `aggressive`) – influences compiler flags.
  - `compiler_overrides` (object) – per-compiler flag adjustments.
  - `discovery_only` (bool) – true when invoked via discovery hook.
  - `metadata` (object) – free-form annotations (e.g., author, notes).
- **Relationships**:
  - Associated with `IRPackage` through `config_hash`.
  - Referenced by `CompilerValidation` to tailor runtime compilation.
- **Validation Rules**:
  - When `target != fortran2003`, CLI MUST operate in discovery mode (no artefacts).
  - `optimization_preset` MUST default to `balanced` if unspecified.
  - `compiler_overrides` keys restricted to `gfortran`, `oneapi`, `nvfortran`.
- **Lifecycle**:
  - `loaded` (parsed from file) → `validated` (schema + semantic checks) → `finalized` (used in emission) → `archived` (stored alongside artefacts).

## EmissionManifest
- **Identity**: `manifest_id` (UUID) generated per emission run.
- **Attributes**:
  - `ir_package_ref` (composite key reference).
  - `fortran_files` (array of file paths) – generated source files.
  - `support_files` (array) – build scripts, README, metadata.
  - `trace_report` (file path) – JSON mapping IR nodes to Fortran lines.
  - `telemetry_log` (file path) – NDJSON event stream.
  - `status` (enum: `pending-validation`, `validated`, `rejected`).
  - `notes` (string) – governance reviewer commentary.
- **Relationships**:
  - **1:many** with `CompilerValidation`.
  - Links to `IRPackage` as parent.
- **Validation Rules**:
  - `fortran_files` MUST be non-empty when `status != rejected`.
  - `trace_report` MUST exist for governance submission.
  - `telemetry_log` MUST cover all lifecycle events.
- **Lifecycle**:
  - `pending-validation` (after emission) → `validated` (all compilers succeed & governance OK) → `rejected` (if any blocking issue).

## CompilerValidation
- **Identity**: `validation_id` (UUID) per compiler run.
- **Attributes**:
  - `compiler` (enum: `gfortran`, `oneapi`, `nvfortran`).
  - `command` (string) – full invocation recorded for reproducibility.
  - `exit_code` (int).
  - `stdout_log` / `stderr_log` (file paths).
  - `duration_seconds` (float).
  - `result` (enum: `passed`, `failed`).
- **Relationships**:
  - Belongs to one `EmissionManifest`.
- **Validation Rules**:
  - `result` MUST be `passed` when `exit_code = 0` and smoke tests succeed.
  - Failure logs MUST be retained even after retries.
- **Lifecycle**:
  - `queued` → `running` → `passed`/`failed`.
