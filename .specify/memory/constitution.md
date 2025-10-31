<!-- Sync Impact Report
Version: 0.0.0 → 1.0.0
Modified Principles:
- Template Principle 1 → I. Specification-Driven Language Changes
- Template Principle 2 → II. Executable Semantics as Tests
- Template Principle 3 → III. CLI-First Tooling Exposure
- Template Principle 4 → IV. Incremental Feature Delivery
- Template Principle 5 → V. Traceable Observability and Documentation
Added Sections:
- Core Principles
- Language Architecture Constraints
- Delivery Workflow Expectations
- Governance
Removed Sections: None
Templates requiring updates:
- ✅ .specify/templates/plan-template.md
- ✅ .specify/templates/spec-template.md
- ✅ .specify/templates/tasks-template.md
Follow-up TODOs: None
-->

# Met DSL Constitution

## Core Principles

### I. Specification-Driven Language Changes
- Every new or altered DSL construct MUST originate from a feature specification in `specs/<id>/spec.md` that defines syntax, semantics, user stories, and acceptance criteria.
- Implementation plans MUST confirm that the Constitution Check is satisfied before Phase 0 research begins and block execution if specification gaps remain.
- Pull requests MUST reference the governing spec and its plan, and reviewers MUST verify that code matches the documented semantics.
Rationale: Formal specifications ensure language decisions remain deliberate, reviewable, and repeatable.

### II. Executable Semantics as Tests
- Each language change MUST include executable examples (golden files, contract tests, or evaluation fixtures) that run via the CLI to prove deterministic behaviour.
- Negative cases and failure diagnostics MUST be captured alongside positive scenarios to prevent silent regressions.
- CI pipelines MUST run the full DSL test suite on every merge candidate; failures block integration.
Rationale: Executable specifications keep the DSL trustworthy and prevent semantic drift.

### III. CLI-First Tooling Exposure
- All DSL evaluation, compilation, and scaffolding capabilities MUST be exposed through a documented CLI entry point.
- CLI commands MUST support both human-readable output and structured formats (JSON or NDJSON) for automation.
- Long-running commands MUST provide progress or status output so that automated workflows can detect stalled executions.
Rationale: A CLI-first interface keeps the language operable in local, CI, and scripted environments.

### IV. Incremental Feature Delivery
- Specs, plans, and tasks MUST slice features into independently valuable user stories whose code paths can be shipped without unfinished dependencies.
- Implementation tasks MUST declare explicit ordering and blocking relationships so partial merges never disable existing functionality.
- Feature flags MAY gate new behaviour, but they MUST default to safe values and include removal tasks within the same feature scope.
Rationale: Incremental delivery preserves release cadence and lowers risk when evolving the DSL.

### V. Traceable Observability and Documentation
- Each feature MUST document observable behaviour changes (CLI commands, syntax, outputs) in its plan and update runtime docs before release.
- DSL runtime components MUST emit structured diagnostics for parsing errors, evaluation failures, and performance hotspots.
- Specifications MUST identify the telemetry or logging required to validate the change after deployment.
Rationale: Traceability makes debugging and operational validation of the language feasible.

## Language Architecture Constraints

- Maintain a clear separation between the DSL core (parsing, evaluation), the CLI presentation layer, and any adapters or SDKs to avoid cross-layer coupling.
- Language packages MUST remain dependency-light; external runtime integrations require documented justification in the plan.
- Backward compatibility MUST be preserved for released syntax. Breaking removals require a major version bump with migration guidance.
- Reference implementations and samples MUST live alongside the feature that introduces them to keep examples synchronized with behaviour.

## Delivery Workflow Expectations

- Feature work MUST progress through the standard artefact flow: specification → implementation plan → tasks → code.
- Specifications MUST enumerate measurable success criteria and story-level independent tests before planning begins.
- Plans MUST articulate repository structure impacts, Constitution Check outcomes, and instrumentation decisions aligned with the principles above.
- Task lists MUST group work by user story, distinguish parallelizable work, and call out required diagnostics, documentation, and cleanup tasks.
- Reviews MUST confirm that each artefact remains aligned; divergence triggers an immediate update cycle before implementation proceeds.

## Governance

- Amendments require: (1) a proposal capturing rationale and impact, (2) review by the core language maintainers, and (3) explicit version bump recorded here.
- Constitution versions follow semantic versioning: MAJOR for incompatible governance or principle changes, MINOR for new principles or sections, PATCH for clarifications.
- The Constitution Check in every plan acts as the compliance gate; reviewers MUST block work that bypasses principles or mandatory artefacts.
- A quarterly governance review evaluates adherence metrics (spec completeness, test coverage, CLI availability) and schedules corrective actions when gaps emerge.
- Runtime guidance documents (README, quickstarts, CLI help) MUST reflect the current constitution within one iteration of any amendment.

**Version**: 1.0.0 | **Ratified**: 2025-10-31 | **Last Amended**: 2025-10-31
