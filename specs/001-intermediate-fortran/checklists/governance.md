# Governance & Resilience Checklist: Intermediate IR and Fortran Emission

**Purpose**: Validate governance, audit, and failure-handling requirements for the staged emission pipeline  
**Created**: 2025-10-31  
**Feature**: [Intermediate IR and Fortran Emission](../spec.md)

## Requirement Completeness

- [X] CHK001 Are artefact retention requirements (manifest, trace, telemetry) fully enumerated across IR and Fortran stages? [Completeness, Spec §Functional Requirements (FR-005, FR-006); Spec §Observability & Diagnostics]
- [X] CHK002 Do governance workflows specify reviewer actions and required evidence after verification completes? [Completeness, Spec §User Story 3; Spec §Success Criteria (SC-004)]
- [X] CHK003 Are discovery hook outputs (supported vs experimental targets) described with required metadata fields? [Completeness, Spec §Functional Requirements (FR-011); Spec §Edge Cases]

## Requirement Clarity

- [X] CHK004 Are telemetry event schemas (fields for `emit_started`, `emit_failed`, etc.) explicitly defined and non-ambiguous? [Clarity, Spec §Observability & Diagnostics; Spec §Functional Requirements (FR-008)] 
- [X] CHK005 Is the expected content of the emission manifest (file lists, trace references, compiler notes) documented with measurable detail? [Clarity, Spec §Functional Requirements (FR-005); Quickstart §4]
- [X] CHK006 Are “minimal explanatory comments” in generated Fortran defined with criteria that auditors can evaluate? [Clarity, Spec §Edge Cases; Spec §FR-004; Spec §Clarifications (Session 2025-10-31)]

## Requirement Consistency

- [X] CHK007 Is the staged CLI flow (IR → Fortran → verify) consistent across user stories, functional requirements, and quickstart guidance? [Consistency, Spec §User Stories 1–3; Spec §Functional Requirements (FR-001–FR-010); Quickstart §3–5]
- [X] CHK008 Do compiler validation obligations match between FR-010 and success metric SC-002 without conflicting thresholds? [Consistency, Spec §FR-010; Spec §SC-002]

## Acceptance Criteria Quality

- [X] CHK009 Can success criterion “zero missing emission reports” be objectively assessed with defined audit checkpoints? [Acceptance Criteria, Spec §SC-004]
- [X] CHK010 Are telemetry/dashboard expectations measurable (e.g., defined cadence, data fields) to support Principle V reviews? [Acceptance Criteria, Spec §Observability & Diagnostics; Plan §Technical Context – Performance Goals; [Gap]]

## Scenario Coverage

- [X] CHK011 Are governance review scenarios defined for partial or failed compiler validations, including escalation paths? [Coverage, Spec §User Story 3; Spec §Edge Cases; Spec §FR-010]
- [X] CHK012 Do requirements cover re-running emissions when deterministic hashes detect drift or stale artefacts? [Coverage, Spec §FR-009; Quickstart §5]

## Edge Case Coverage

- [X] CHK013 Are behaviours for unsupported or experimental targets (discovery-only) described, including user messaging? [Edge Case, Spec §FR-011; Spec §Edge Cases; Quickstart §6]
- [X] CHK014 Is recovery behaviour documented for interrupted emissions that leave partial files in the build directory? [Edge Case, Spec §Edge Cases; [Gap]]
- [X] CHK015 Are error-reporting requirements defined when telemetry sinks are unreachable or malformed? [Edge Case, Spec §Observability & Diagnostics; [Gap]]

## Non-Functional Requirements

- [X] CHK016 Are performance goals (<5 min IR emission, sequential compiler runs) reflected in requirements with explicit measurement points? [Non-Functional, Spec §SC-001; Plan §Technical Context – Performance Goals]
- [X] CHK017 Are resource usage or scheduling constraints for running three compiler suites documented to prevent cluster contention? [Non-Functional, Plan §Technical Context – Constraints; [Gap]]

## Dependencies & Assumptions

- [X] CHK018 Are assumptions about compiler availability and versions verified and tracked for governance sign-off? [Dependencies, Spec §Assumptions & Dependencies; Plan §Target Platform]
- [X] CHK019 Is integration with governance tooling (ingesting manifests, trace JSON) backed by documented interface expectations? [Dependencies, Spec §Assumptions & Dependencies; Spec §FR-006; [Gap]]

## Ambiguities & Conflicts

- [X] CHK020 Is the scope of the discovery hook (metadata only, no artefacts) consistent across specification text and quickstart instructions? [Ambiguity, Spec §FR-011; Spec §Edge Cases; Quickstart §6]
- [X] CHK021 Are the terms “manifest,” “report,” and “trace” uniquely defined to avoid confusion during audit handovers? [Ambiguity, Spec §FR-005; Spec §SC-004; Quickstart §4–5]
