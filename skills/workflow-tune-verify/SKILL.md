---
name: workflow-tune-verify
description: Use this when running the verify of the Tune stage and evaluating the physics passing, quality conditions, and performance objective function of a candidate `spec.ir.yaml.impl_defaults` variant to judge whether to adopt it. It applies to fixing the `best impl` and judging the move to regression.
---

# Workflow Tune Verify

## Purpose
Fix the verification responsibility of the Tune stage candidates, and select the adopted candidate by objective metrics. This flow is part of an **optional flow** separated from the core workflow.

## Scope
- the trial-result evaluation of a candidate `spec.ir.yaml.impl_defaults` variant
- the finalization of the `best impl` and the re-tuning judgment

## Requirements
- Exclude a physics `fail` candidate from the performance-evaluation target.
- Do not adopt a candidate whose quality-comparison result is a failure.
- The performance evaluation uses the statistics of `perf.json`, and ranks by the objective function.
- Handle the re-measurement result of the same point, and make a noise-robust judgment.
- Make it a required condition that the adopted candidate is traceable in `trial_meta.json` and `lineage.json`.
- The workflow mode uses `METDSL_WORKFLOW_EXEC_MODE` as the canonical source, and applies `dev` when unset.
- In `dev` mode, it is a `Tune fail` the moment `issue_severity=major|critical` is detected, and treating it as a minor exception is forbidden.

## Operations Rules
1. Keep only candidates whose `verdict` and `aggregate_verdict` are `pass` as adoption candidates.
2. Among the adoption candidates, fix the variant `spec.ir.yaml.impl_defaults` with the maximum objective function as the `best impl`.
3. Under a new-architecture or new-compiler condition, run the re-tuning judgment.
4. Save the judgment basis in the `tuning`-family metadata, and hand it to regression monitoring.
5. When it `fail` in `dev` mode, record the basis needed to create `failure_analysis.json` (the failure reason, the target trial, the judgment evidence).

## Decision Criteria
- The adopted candidate satisfies the required physics and quality conditions.
- The performance ranking can be reproduced from the saved values of `perf.json`.
- The finalization basis of the `best impl` is traceable.
