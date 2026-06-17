# CLI Reference (rare subcommand overview)

## Position of this document

An overview of the **infrequently used** rare subcommands of `tools/orchestration_runtime.py`. For the detailed argument specification, `python3 tools/orchestration_runtime.py <sub> --help` is the canonical source.

For the detailed specification of the frequent subcommands (Tier-A), refer to [docs/CLI_REFERENCE.md](CLI_REFERENCE.md). The information-acquisition policy per tool / subcommand uses the "CLI reference conventions" section of `CLAUDE.md` as the canonical source.

Related canonical sources:
- frequent subcommand details: [docs/CLI_REFERENCE.md](CLI_REFERENCE.md)
- the startup contract of the whole workflow: `skills/workflow-orchestration/SKILL.md` and `skills/workflow-orchestration/references/startup_contract.md`
- exception recovery procedures: [docs/RUNBOOK.md](RUNBOOK.md)

## Common conventions

- `--repo-root` / `--orchestration-id` are **required** in (almost) all subcommands.
- ISO 8601 timestamps are canonically UTC (`Z` suffix).
- For the detailed arguments (required / optional / default values), confirm with `<sub> --help`.

## Rare subcommand list

| subcommand | purpose | main caller / situation |
|---|---|---|
| `init` | start an orchestration / generate `orchestration_meta.json` | usually launched via `tools/run_workflow.py`. A direct call is for exceptional operation only |
| `preflight` | execution-platform launchability probe / generate `preflight.json` | called internally by `tools/run_workflow.py`. A manual call is forbidden (see `AGENTS.md`) |
| `preflight-status` | read back an existing `preflight.json` | post-launch state confirmation |
| `record-timeout` | the canonical recovery path for an `Agent` tool API stream idle timeout etc. | the exception recovery flow when a child agent wedges. `--force-reason` is the last resort for a marker-check bypass |
| `read-checkpoint` | obtain `workspace/orchestrations/<orch>/orchestration_checkpoint.json` | at the resume decision in an orchestration with `resume_enabled=true` |
| `verify-checkpoint-integrity` | reconcile the artifact hash recorded in the checkpoint with the current state | the consistency confirmation at resume start. On `stale` detection, that step must not be skipped |
| `check-step-completed` | with `resume_enabled=true`, confirm the completion state of the target step | the canonical skip-decision path. A skip must not be decided by a direct reference to `step_result.json` |
| `orchestration-read` | the gate-mediated read of a path outside the manifest | usually called via `run-gate --gate orchestration_read --args-json '{"read_path": "..."}'` |
| `repair-agent-runs` | in-place backfill the `parent_agent_run_id` / `agent_model` missing from the step/substep rows of a pre-`caa10ab` `agent_runs.jsonl`, and make it `pre_judge`-compliant | auto-run at `--resume`. Only when auto-derivation is `needs_manual`, run it manually with `--agent-model <id>` (for details, `RUNBOOK.md` §3-1) |
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
- the whole resume flow including `check-step-completed`: `skills/workflow-orchestration/SKILL.md` Operations Rule 19
