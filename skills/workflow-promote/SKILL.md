---
name: workflow-promote
description: Use this when running the Promote stage and promoting an artifact whose `verdict` and `aggregate_verdict` passed to `releases/` and updating the `official_releases` of `spec_catalog.yaml`. It applies to the work of satisfying the `release_id` invariant and the tracking-information registration.
---

# Workflow Promote

## Purpose
Fix the promotion responsibility of the Promote stage, and register the official-version artifact in a reproducible form.

## Scope
- the work of promoting a `workspace` artifact to `releases/<...>/<release_id>/`
- the work of updating the `official_releases` of `spec/registry/spec_catalog.yaml`

## Requirements
- Require `verdict.json`'s `overall=pass` as an input condition.
- Require `aggregate_verdict.json`'s `overall=pass` as an input condition.
- Require that the adopted `source_id`, `binary_id`, and `run_id` are traceable in `lineage.json` and `trial_meta.json`.
- At registration, record `release_id`, `target_architecture`, `toolchain_language`, `target_backend`, `source_pipeline_id`, `source_source_id`, `source_binary_id`, `source_run_id`, `artifact_root`, `promoted_at`, and `status` as required.
- Forbid overwriting an existing `release_id`.

## Operations Rules
1. Fix the promotion destination to `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/`.
2. Update the old `release` of the same `target_architecture + toolchain_language` to `deprecated`.
3. On a `problem` promotion, confirm that the aggregated state of the transitive dependency `node` consists only of `pass` or `xfail`.
4. After promotion, sync-update `spec_catalog.yaml`, leaving no discrepancy between the search canonical source and the registration canonical source.

## Decision Criteria
- The promoted artifact and the `official_releases` registration content match.
- The `release_id` invariant is maintained.
- The promotion-source trial can be reproduced from the tracking information alone.
