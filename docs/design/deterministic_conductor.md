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

The LLM substep **bodies**: `compile.generate`/`verify`, `generate.generate`/`verify`,
and `validate.judge` — spawned as isolated leaf agents. The deterministic substeps
`compile.static`, `generate.lint`, `generate.static`, `build` (step), and `validate.execute`
run IN-PROCESS in the conductor (no leaf), not as LLM. Plus the
diagnostician (`escalate`): for a failure the decision tables can't classify, the
conductor embeds the failure-artifact content in a prompt, spawns a read-only reasoning
leaf, and parses its final JSON directive (`_parse_directive`) — validated against the
action/target vocabulary, falling back to `fail_closed` on anything malformed.

### Leaf session handling

Each substep leaf is launched with `claude --session-id <agent_run_id>` so its Claude
Code session id equals its `agent_run_id` — the per-arid transcript is addressable, and a
later repair can `--resume` it. Guard evaluation keys on the active_child marker
(`active_child_agent_run_id.txt` = the new arid), not the session id, so a resumed leaf is
still evaluated against its own manifest.

**Minor-fix reuse resume (always-on, claude only).** On a `repair_strategy=reuse` retry, the
repair leaf resumes the producer leaf's session (`--resume <producer_arid> --fork-session`) to
inherit its context and design intent instead of cold-starting (re-reading the spec/source from
scratch). `restart` stays cold (no resume) to avoid anchoring on the defective reasoning — so the
warm/cold choice is driven by `repair_strategy` (deterministic-gate findings route `reuse`→warm;
LLM-verify-attributed `restart`→cold). This is always-on (the former opt-in env flags
`METDSL_CONDUCTOR_REUSE_RESUME` / `METDSL_CONDUCTOR_REUSE_SLIM_PROMPT` were removed); it falls back
to a cold launch when the producer transcript was GC'd. Verified live in a billed E2E
(`orch_20260630T061511Z_536a8586`): a `compile.static` finding forked the `compile.generate`
session and the rotated repair passed.

**Leaf sandboxing (bwrap, unconditionally mandatory; Linux+userns only).**
`record-launch` builds a per-arid bwrap profile (`sandbox_profiles/<arid>.json`: repo
read-only; writes confined to the child's `write_roots` + `workspace/tmp`; the backend's
install dir bound read-only and its config/credential home — `~/.claude{,.json}` — bound
writable for auth + session transcript) and records `sandbox_enforced: true`. `spawn_leaf`
**always** wraps every leaf (claude and codex) in that profile via `render_bwrap_command`;
there is no opt-out. The conductor **fails closed** — a leaf with no usable profile (a
missing/invalid file, or a caller with no `child_arid` such as the read-only diagnostician)
raises rather than launching unconfined; the diagnostician's failure is caught and routed
to `fail_closed`. The backend type (not the launch command string, which may be a custom
`--llm-command` wrapper) keys the config-home bind, and a creatable-but-absent home (e.g.
`~/.codex` in a fresh env) is created before binding. `sandbox_enforced: true` is a
runtime-**required invariant** — preflight and `record-launch` reject anything else
(writing a `sandbox_not_enforced` violation). This is sound precisely because enforcement
is unconditional: the FS-diff write-authorization model authorizes a leaf write purely by
`write_roots` containment, which holds only while bwrap actually confines the leaf. The
sandbox is *strictly* more confined than an unconfined leaf — which would run with the
user's full filesystem access — so it is a net restriction, not a new exposure. A host
without unprivileged user namespaces / `bwrap` cannot run leaves and is unsupported (the
conductor fails closed there). `docs/BWRAP_ENABLEMENT.md` is the live re-verification
runbook (auth / MCP build-runtime / hooks / `--session-id` transcript + the
Build-output-in-`write_roots` check under the sandbox).

## Usage

```
python3 tools/run_workflow.py <spec_ref> <until_phase> --llm claude
```

`tools/audit_orchestration.py` reports per-run token/turn cost.

## Open risks (verified by the integration run / M1)

> The live re-verification that exercises the bwrap sandbox (items below + the
> Build-output-in-`write_roots` check) is in `docs/BWRAP_ENABLEMENT.md`.

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
