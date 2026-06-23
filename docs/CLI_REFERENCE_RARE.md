# CLI Reference (rare subcommand overview)

## Position of this document

An overview of the **infrequently used** rare subcommands of `tools/orchestration_runtime.py`. For the detailed argument specification, `python3 tools/orchestration_runtime.py <sub> --help` is the canonical source.

For the detailed specification of the frequent subcommands (Tier-A), refer to [docs/CLI_REFERENCE.md](CLI_REFERENCE.md). The information-acquisition policy per tool / subcommand uses the "Information-acquisition policy" section of [docs/CLI_REFERENCE.md](CLI_REFERENCE.md) as the canonical source.

Related canonical sources:
- frequent subcommand details: [docs/CLI_REFERENCE.md](CLI_REFERENCE.md)
- workflow operation / startup: [docs/RUNBOOK.md](RUNBOOK.md) (operator procedure) and [docs/ORCHESTRATION.md](ORCHESTRATION.md) (conductor/orchestration contract)
- exception recovery procedures: [docs/RUNBOOK.md](RUNBOOK.md)

## Common conventions

- `--repo-root` / `--orchestration-id` are **required** in (almost) all subcommands.
- ISO 8601 timestamps are canonically UTC (`Z` suffix).
- For the detailed arguments (required / optional / default values), confirm with `<sub> --help`.

## Rare subcommand list

| subcommand | purpose | main caller / situation |
|---|---|---|
| `init` | start an orchestration / generate `orchestration_meta.json` | usually launched via `tools/run_workflow.py`. A direct call is for exceptional operation only. `--agent-model <id>` records the orchestration agent's own model on its `agent_runs.jsonl` row (`run_workflow.py` passes the operator's unpinned claude alias — e.g. `opus`, read from `~/.claude/settings.json` — by default for the claude backend; never a pinned version, which would go stale) |
| `preflight` | execution-platform launchability probe / generate `preflight.json` | called internally by `tools/run_workflow.py`. A manual call is forbidden |
| `preflight-status` | read back an existing `preflight.json` | post-launch state confirmation |
| `record-timeout` | the canonical recovery path for an `Agent` tool API stream idle timeout etc. | the exception recovery flow when a child agent wedges. `--force-reason` is the last resort for a marker-check bypass |
| `read-checkpoint` | obtain `workspace/orchestrations/<orch>/orchestration_checkpoint.json` | at the resume decision in an orchestration with `resume_enabled=true` |
| `verify-checkpoint-integrity` | reconcile the artifact hash recorded in the checkpoint with the current state | the consistency confirmation at resume start. On `stale` detection, that step must not be skipped |
| `check-step-completed` | with `resume_enabled=true`, confirm the completion state of the target step | the canonical skip-decision path. A skip must not be decided by a direct reference to `step_result.json` |
| `orchestration-read` | the gate-mediated read of a path outside the manifest | usually called via `run-gate --gate orchestration_read --args-json '{"read_path": "..."}'` |
| `repair-agent-runs` | in-place backfill the `parent_agent_run_id` / `agent_model` missing from the step/substep rows of a pre-`caa10ab` `agent_runs.jsonl`, and make it `pre_judge`-compliant. The orchestration row is also covered for `agent_model` only (it is the graph root, so no `parent_agent_run_id` is added) | auto-run at `--resume`. Only when auto-derivation is `needs_manual`, run it manually with `--agent-model <id>` (for details, `RUNBOOK.md` §3-1) |
| `repair-step-result-executor` | relocate a substep-aware `step_result.json` (`--node-key` / `--step`) whose `executor_agent_run_id` is a verify-substep arid to the orchestration-arid directory, rewrite the field, and preserve the substep linkage | recovers an orchestration locked at `step_result_written` by `validate_pre_judge_step_result_executor_integrity` without a fresh orchestration. Auto-run for every corrupt node/step at `--resume` (best-effort, idempotent); refuses when the wrong arid is not a recorded substep for that node/step or a legitimate step_result already exists at the orchestration dir |
| `reopen-phase` | reopen a checkpointed-pass phase (`--from-phase`) and every downstream phase for `--node-key`, so a cross-phase retry (`Validate.judge` `structural_violation`/`ir` → Compile, or `Generate.verify` `ir_inconsistency` → Compile) runs in place. Snapshots the prior attempt's step/substep runs as superseded (exempt from the pass-completion vouch), archives their `step_result.json` aside to `step_result.superseded.<seq>.json`, drops the affected `completed_steps` checkpoint entries, and resets the affected `phase_state` to `not_started` | used by the orchestration agent when the decision table routes a `Validate` / `Generate` failure back to an already-passed `Compile` (the `pass` upstream phase cannot otherwise be re-pointed: `check-step-completed` reads the stale IR as `integrity=ok`, the phase sits at `step_result_written`, and `retry_decisions` only models within-step retries). Idempotent. `--trigger-agent-run-id` must be a terminal non-pass step/substep strictly downstream of `--from-phase` (the anti-abuse gate — refuses to erase a passing pipeline). The trigger is resolved from `agent_runs.jsonl`; when absent there it falls back to an `agent_runs_invalid.jsonl` entry **only if** a matching `violations/<arid>.unauthorized_write_violation.json` exists (the recovery path for a phase whose failure mode *is* an unauthorized write — that run is diverted to the invalid log and would otherwise be an unusable trigger; the result/log records `trigger_source`). On `--resume` of an `attribution=ir` failure, or an unauthorized-write failure attributed to a single upstream phase, `orchestration_meta.resume_directive` records the parameters to feed here (for details, `RUNBOOK.md` §3-1) |
| `dismiss-violation` | mark a known benign `unauthorized_write_violation` as operator-approved, and pass the terminal validation of `record-agent-run` on retry | used when an intentionally benign path such as a gitignore-derived `.pyc` / `.pycache` is recorded in a violation. `--paths` can specify only a path included in the `unauthorized_paths` of `violations/<arid>.unauthorized_write_violation.json` (matched as a subset). The `record-agent-run` on retry passes only when `dismissed_paths` contains the detected unauthorized paths |

## Argument-acquisition path

Confirm the required / optional arguments and return-value schema of each subcommand with the following command.

```bash
python3 tools/orchestration_runtime.py <subcommand> --help
```

The argparse output includes the description / the help string of all arguments, and provides details in a way that complements this doc. The `--help` call itself is outside the scope of `forbid_tools_direct_read`, and its usage frequency is recorded by the `cli_help_invocation_observed` audit policy of `tools/hooks/common.py` (it is not blocked).

## Links to exception recovery flows

- the use condition of `record-timeout`'s `--force-reason`: `docs/RUNBOOK.md#substep-timeout-recovery`
- the recovery for an incomplete launch (dangling active_child window / `reason_code=launch_incomplete_active_child`), and reading the `launch_incident.runtime.*.json` diagnostics snapshot via `python3 tools/audit_orchestration.py --orchestration-id <id>` ("Dangling launch" section): `docs/RUNBOOK.md#launch-incomplete-recovery`
- the response when `verify-checkpoint-integrity` detects `stale`: the relevant section of `docs/RUNBOOK.md`
- the whole resume flow including `check-step-completed`: [docs/RUNBOOK.md](RUNBOOK.md) §3-1 (the conductor drives resume; `tools/workflow_conductor.py` is the implementation)
