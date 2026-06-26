---
name: workflow-timing-audit
description: Use this when investigating where a workflow orchestration spent wall-clock time and output tokens — breaking a run down per leaf (step.substep), separating LLM leaves from the conductor's in-process deterministic steps, and attributing each LLM leaf's time to model generation vs. tool execution vs. dominant turns. Handles the transcript multiple-counting traps. The target orchestration_id is auto-detected. Claude Code-only.
---

# Workflow Timing & Token Audit

## Purpose
Quantify the elapsed-time and output-token breakdown of a completed or interrupted
workflow run, at three levels:

1. **Run / node level** — total leaf elapsed time, split between LLM leaves and the
   conductor's in-process deterministic steps (`Build` / `Validate.execute`, which run
   no leaf agent — see `docs/AGENT_SKILLS.md`).
2. **Per-leaf level** — for each `step.substep`: elapsed, `model-generation %` vs.
   `tool-execution %`, output tokens, generation throughput (tok/s), API-response count.
3. **Inside-leaf level** — the dominant turns, the think/text/tool split, and the
   `cache_read` range, so a single expensive generation turn can be isolated.

This is an operator/diagnostic skill, not a workflow phase. It does not modify any
artifact; it only reads logs and transcripts.

## When to use
- "What did this run spend its time / tokens on?"
- "Which leaf / which turn dominates the wall clock?"
- Before proposing any speedup, to confirm the lever (the canonical finding is that
  ~100% of node wall time is the leaf LLM, and inside the leaf ~all of it is model
  token generation, not tooling/sandbox/IO).

## The multiple-counting traps (why naive sums are wrong)
The bundled script handles all three structurally; do not hand-sum these:

1. **One API response = several transcript lines.** A single model response is written
   as separate jsonl lines for its `thinking`, `text`, and `tool_use` blocks. They all
   share the same `message.id` and **repeat the same `usage.output_tokens`**. Summing
   per line double/triple-counts both tokens and "turns". → collapse by `message.id`.
2. **`cache_read_input_tokens` is re-read every turn.** It grows as the session
   accumulates; summing it counts the same prompt many times. → report its range, never
   its sum.
3. **`agent_runs.jsonl` has no usage and shares ids with `phase_state_log` step
   records.** Joining them naively mislabels leaves. → take timing from the
   `child_launched` → `record_agent_run_terminal` pairs in `phase_state_log.jsonl`,
   keyed by `agent_run_id`; take the role/substep label from `session_run_index.json` /
   `agent_runs.jsonl`.
4. **`usage.output_tokens` ≠ source emitted.** It INCLUDES extended-thinking tokens,
   which are billed/generated as output but never appear in the written files. A leaf
   that produces a ~10 KB source file (~3 k visible tokens) can report ~35 k output
   tokens — the rest is invisible thinking. Reading "output tokens" as "how much source
   it wrote" is a misattribution. → the report splits every leaf and turn into
   `thinking` vs `visible` (= text + serialized tool inputs, ~4 chars/token); the
   thinking column is usually ~80–90%.

## Log / transcript sources

| data | source |
|---|---|
| per-leaf elapsed | `workspace/orchestrations/<orch_id>/phase_state_log.jsonl` (`child_launched`→`record_agent_run_terminal`) |
| role / substep / status label | `workspace/orchestrations/<orch_id>/session_run_index.json`, `agent_runs.jsonl` |
| run status / spec | `workspace/orchestrations/<orch_id>/orchestration_meta.json` |
| per-leaf full transcript (turns, usage, tools) | `~/.claude/projects/<cwd-slug>/<agent_run_id>.jsonl` (`<cwd-slug>` = repo abs-path with `/`→`-`; the leaf `agent_session_id` == `agent_run_id` == filename) |

Note: `agent.result.json` under `agents/<id>/dialogs/` reports `usage: unavailable`
("no locatable child transcript") — the conductor does not capture usage, so the
per-leaf session transcript above is the only token source.

## Procedure

### Step 1 — run the analyzer
```bash
# auto-pick the most recent orch_* directory
python3 skills/workflow-timing-audit/scripts/analyze_timing.py

# or target a specific orchestration
python3 skills/workflow-timing-audit/scripts/analyze_timing.py orch_YYYYMMDDTHHMMSSZ_xxxxxxxx

# machine-readable
python3 skills/workflow-timing-audit/scripts/analyze_timing.py <orch_id> --json
```
If transcripts live elsewhere, pass `--project-dir <dir>`.

### Step 2 — read the report top-down
1. Confirm the **LLM vs. deterministic** split (deterministic `Build` / `Validate.execute`
   should be a few % and have no transcript; if one shows as LLM, the run predates the
   conductor in-process migration).
2. Find the leaf with the largest **elapsed** and check its **gen% / tool%**. tool% in
   the low single digits is expected; a high tool% is the anomaly worth reporting.
3. In the inside-leaf section, identify the **dominant turn(s)**. A `generate.generate`
   leaf is typically dominated by one large "write the whole source" turn; `verify` /
   `judge` leaves spread time across many `thinking`-heavy turns.

### Step 3 — report
State, per node: total min, LLM share, the slowest leaf, and its dominant turn
(latency + output tokens + tok/s). Anchor any speedup claim to whether it reduces
**output tokens generated** (the throughput floor is ~85–115 tok/s for Opus); tooling /
sandbox / IO reductions do not move the wall clock here.

## Interpretation reference (canonical findings)
- ~97–100% of node wall time is the leaf `claude -p` calls; the conductor's deterministic
  steps are negligible.
- Inside an LLM leaf, ~98–100% of time is model generation latency (thinking + output
  streaming); all tool execution combined is ≤ ~3 s.
- Wall time ≈ total output tokens (thinking + visible) ÷ ~100 tok/s. **The output is
  dominated by extended thinking (~80–90%), NOT by the emitted source** — the final
  files are small (~3 k visible tokens) regardless of how long the leaf runs. The
  largest single event observed is a ~270 s `generate.generate` turn that was ~24 k
  thinking tokens ending in a single tool call. So the lever is reducing *thinking*, not
  source output; shrinking the file does not help. See the thinking-reduction triage in
  `project_leaf_thinking_purpose_triage` / `project_leaf_thinking_injection_implemented`
  (deterministic injection of gate runbook / task card / dependency facts removes the
  non-essential ~44% of thinking without touching the correctness-bearing core).
- See the project memory `project_workflow_walltime_structure` / `project_workflow_token_cost_structure`.
