# Deterministic Conductor

Status: landed (`tools/workflow_conductor.py`) — deterministic loop, failure
routing + reopen, LLM diagnostician escalation. The conductor drives orchestration
for the `claude` / `codex` backends. M1 (a live `claude -p` MCP/hooks cost-measuring
integration run) remains the open verification gap: the leaf path is exercised by
mocked unit tests, not yet by a live run.

## Why

An LLM-driven orchestration loop — one LLM agent driving the Compile → Generate →
Build → Validate phase/substep loop — makes essentially no decisions for a trivial
node, yet:

- every bookkeeping CLI output (`record-launch`, `finalize-child`, `write-step-result`,
  `check-step-completed`, …) accumulates in its context and is re-read every turn, so
  `cache_read` grows **O(turns²)**;
- it must keep ~70K of static protocol docs (orchestration skill / startup contract / CLI_REFERENCE /
  phase docs) resident;
- its reasoning is spent reconciling the framework's own bookkeeping state, not physics.

Measured on a clean resume of the 1D advection-diffusion node: 242 turns, per-turn
context 22K→274K (mean 166K, over the 200K window), ~40M tokens for one resume.

## Design

A plain-Python **conductor** drives the deterministic loop and calls the existing
`orchestration_runtime.py` subcommands directly; the **LLM is invoked only as a leaf**
for each substep body (`claude -p` / `codex exec`), plus a one-shot diagnostician for an
unclassifiable failure (M4). This removes the parent LLM's turns, its resident docs, and
the per-turn accumulation. The leaf substeps (the irreducible creative work) are unchanged.

### What is deterministic (moved to the conductor)

| concern | mechanism |
|---|---|
| phase/substep loop | `Conductor.conduct` / `run_phase` / `run_substep` |
| bookkeeping | reuse `record-launch` / `finalize-child` / `write-step-result` / `check-step-completed` / `workflow-launch-check` / `reserve-phase-root` / `set-status` / `reopen-phase` via subprocess (same guards fire as on the LLM path) |
| launch-request assembly | `build_launch_request` — reproduces, field-for-field, the payload the LLM assembled from `launch_prompts.md` (validated against real `launches/*.request.json`) |
| substep pass/fail | `determine_substep_status` reads the canonical artifacts: verify→`*_meta.json#verification_status`, judge→`aggregate_verdict.json`, producers→deliverable existence |
| failure routing | `classify_failure` + the decision tables `BUILD_FAILURE_ROUTING` (`phase_03_build.md`) and `VALIDATE_JUDGE_ROUTING` (`phase_04_validate.md`); dev-mode severity gate |
| cross-phase reopen | `conduct` reopens an upstream (checkpointed-pass) phase via `reopen-phase`, capped by a per-phase budget; a re-run allocates a fresh producer id (`_ensure_fresh_producer_id`). In-place same-phase retry is intentionally NOT done (its `retry_decisions`/effective-pass bookkeeping is error-prone) — a same-phase decision terminalizes |
| id allocation | `prepare_node` reserves `ir_id` + `pipeline_id` (`reserve-phase-root`) and derives `source_id`/`binary_id`/`run_id` |
| node resolution | `resolve_node` reads `spec_catalog.yaml` |

### What stays LLM

The substep **bodies**: `compile.generate`/`verify`, `generate.generate`/`verify`,
`build` (step), `validate.execute`/`judge` — spawned as isolated leaf agents. Plus the
diagnostician (`escalate`): for a failure the decision tables can't classify, the
conductor embeds the failure-artifact content in a prompt, spawns a read-only reasoning
leaf, and parses its final JSON directive (`_parse_directive`) — validated against the
action/target vocabulary, falling back to `fail_closed` on anything malformed.

## Usage

```
python3 tools/run_workflow.py <spec_ref> <until_phase> --llm claude
```

`tools/audit_orchestration.py` reports per-run token/turn cost.

## Open risks (verified by the integration run / M1)

1. **headless `claude -p` MCP + permissions** — leaf Build/Validate.execute need
   build-runtime MCP non-interactively (the committed `.claude/settings.json` grants it;
   may need `--permission-mode`).
2. **hooks on a top-level leaf** — `output_manifest_write_guard` etc. must attribute to
   the leaf's `agent_run_id` (`record-launch` opens the active_child window;
   `finalize-child` closes it).
3. **artifact-detail exactness** — `required_outputs` lists, the MCP-command-log
   placement, and `retry_decisions` shape are grounded in real artifacts but ultimately
   confirmed by the first real run.

## Tests

`tools/tests/test_workflow_conductor.py`: payload builder vs real `request.json` (×7),
decision tables, the full happy-path bookkeeping sequence (mocked I/O), cross-phase
reopen + budget routing, fresh producer-id allocation, and node/id resolution.
