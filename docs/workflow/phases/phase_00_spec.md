# Phase 0: Spec (manual)

## Overview
The phase that manually writes the `Controlled Spec`, `tests`, and `deps` and establishes the starting point of the core workflow. It is not an LLM-using phase and is outside the scope of orchestration.

## I/O contract
- execution input: the requirements, physics requirements, and dependency-selection policy given outside the workflow
- verification input: none
- output:
  - `spec/<spec_kind>/<domain>/<family>/<spec_id>/controlled_spec.md`
  - `spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md`
  - `spec/<spec_kind>/<domain>/<family>/<spec_id>/deps.yaml`

## Required requirements
- Define the intent of the physics algorithm (A) in the `Controlled Spec`.
- A `problem spec` declares its dependent `component` and adopted `profile` in `deps.yaml`.
- `tests.md` defines the experiment conditions, judgment conditions, and the required evidence per `test_id`.
- The natural-language notation is the canonical source, and the `Compile` phase integrates it into the structured IR (`spec.ir.yaml`).
- The `spec_version` of `controlled_spec.md` is a required record. When updating a spec, update `spec_version`.

## Connection to later stages
- The `Compile` phase takes `controlled_spec.md` + `tests.md` + `deps.yaml` + `spec/registry/spec_catalog.yaml` as input and generates `spec.ir.yaml`.
- The stages from `Generate` onward use `spec.ir.yaml` as the canonical source and do not read `controlled_spec.md` directly.
- A specification change is expressed by updating one of `controlled_spec.md` / `tests.md` / `deps.yaml`, and a specification must not be changed by an implementation-side modification alone (the Spec-First principle).
