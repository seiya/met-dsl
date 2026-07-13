---
name: workflow-timing-audit
description: Use this when investigating where a workflow orchestration spent wall-clock time and output tokens — breaking a run down per leaf (step.substep), separating LLM leaves from the conductor's in-process deterministic steps, and attributing each LLM leaf's time to model generation vs. tool execution vs. dominant turns. Also flags the waste/failure signals a time-and-token table hides: turns cut off by `max_tokens` (thinking that emitted nothing) and leaves that died of an API/transport error. Handles the transcript multiple-counting traps. The target orchestration_id is auto-detected. Claude Code-only.
---

# Workflow Timing & Token Audit

## Purpose
Quantify the elapsed-time and output-token breakdown of a completed or interrupted
workflow run, at four levels:

1. **Run / node level** — total leaf elapsed time, split between LLM leaves and the
   conductor's in-process deterministic steps (`Build` / `Validate.execute`, which run
   no leaf agent — see `docs/AGENT_SKILLS.md`).
2. **Per-leaf level** — for each `step.substep`: elapsed, `model-generation %` vs.
   `tool-execution %`, output tokens, generation throughput (tok/s), API-response count.
3. **Inside-leaf level** — the dominant turns, the think/text/tool split, and the
   `cache_read` range, so a single expensive generation turn can be isolated.
4. **Anomalies** — turns the API cut off at `max_tokens` (a thinking-only truncation
   emits NOTHING and the work is redone next turn), and leaves that died of an
   API/transport error rather than of their own reasoning.

This is an operator/diagnostic skill, not a workflow phase. It does not modify any
artifact; it only reads logs and transcripts.

## When to use
- "What did this run spend its time / tokens on?"
- "Which leaf / which turn dominates the wall clock?"
- Before proposing any speedup, to confirm the lever (the canonical finding is that
  ~100% of node wall time is the leaf LLM, and inside the leaf ~all of it is model
  token generation, not tooling/sandbox/IO).

## The multiple-counting traps (why naive sums are wrong)
The bundled script handles all seven structurally; do not hand-sum these:

1. **One API response = several transcript lines.** A single model response is written
   as separate jsonl lines for its `thinking`, `text`, and `tool_use` blocks. They all
   share the same `message.id` and **repeat the same `usage.output_tokens`**. Summing
   per line double/triple-counts both tokens and "turns". → collapse by `message.id`.
   Collapse on the **id itself**, not on a consecutive run of lines: a response with
   PARALLEL tool calls has its `tool_use` lines interleaved with the `tool_result` user
   lines answering them, so the same id reappears after a user line. A "current id"
   tracker splits such a response in two and counts its tokens twice (measured: +20,659
   tok on one node).
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
5. **A warm-resumed leaf REPLAYS the producer's turns.** Its transcript contains the
   prior leaf's messages verbatim — same `message.id`, same usage — so per-leaf token
   sums count that work twice (measured: 162 k tok across 3 resumed leaves, ~13%
   inflation on one closure). A warm resume leaves **no trace in the orchestration
   artifacts**, so the transcript is the only place it is visible. → dedupe `message.id`
   GLOBALLY across the run in launch order; the first leaf to emit an id owns it, and a
   later leaf reports only its NEW turns. Wall-clock is *not* affected (the leaves are
   separate processes, run sequentially) — only tokens.
6. **A leaf's elapsed is WALL clock, and wall clock contains time in which nothing ran.**
   Two different causes produce the same signature (`child_launched → terminal` far
   exceeding the transcript), so the script excludes the excess either way and then
   **classifies** it — it must not assert one cause when it was the other:
   - **`dead_leaf`** — the child died (transport error, crash) and its
     `record_agent_run_terminal` was stamped when the conductor next ran, i.e. at the
     operator's `--resume`, hours later. The span is mostly *waiting for a human*
     (observed: a leaf reporting **6.9 h** whose transcript spans **129 s**, status `fail`).
   - **`host_suspend`** — the host slept mid-leaf (on WSL2 the VM is paused when Windows
     sleeps). The leaf resumes and **completes normally**; only the wall clock jumped
     (observed: **4.3 h** on a leaf that *passed*, E2E #5).

   → when elapsed exceeds the transcript wall by more than `STALE_GAP_S` (10 min), the
   script uses the transcript wall as the effective elapsed and reports the excess as
   `EXCLUDED dead wall` with its cause. A leaf whose status is not `pass` died; a leaf
   that **passed cannot have died**, and trap 7 then confirms its process was frozen.
   Anything else is reported `unattributed` rather than guessed.
7. **`time.monotonic()` does not tick while the host is suspended** (Linux
   `CLOCK_MONOTONIC`), so the conductor's `substep_complete.elapsed_seconds` in
   `run_logs/` is **suspend-immune**, while every timestamp in `phase_state_log.jsonl`
   and `orchestration_meta.json` is wall clock and is **not**. Their divergence across one
   continuously-running process is a *direct measurement* of host suspend, and is what
   separates trap 6's two causes (measured on E2E #5: monotonic **216 s** vs wall
   **15,719 s** on the same leaf). → **Corollary: a run's headline wall clock
   (`orchestration_meta.started_at → finished_at`) is NOT its cost** — it also ticks
   through suspend and through any `--resume` wait. Quote the leaf totals, never the
   headline. The report prints the headline only to label it as such.

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
1. Read the **anomalies** section FIRST. It names waste and failure the time table hides:
   a `max_tokens` truncation (especially a thinking-only one — zero output, work redone)
   and a leaf API/transport error (the leaf died of infrastructure, not of its reasoning).
   Either changes what the run's numbers *mean* before you interpret them.
2. Confirm the **LLM vs. deterministic** split. Deterministic steps have no transcript;
   if one shows as LLM, the run predates the conductor in-process migration. `Build` and
   most `Validate.execute` steps are a few %, but on an M3c problem node `validate.execute`
   is the **real multi-resolution simulation** and can legitimately be 10–20% of leaf time
   — that is compute, not overhead.
3. Find the leaf with the largest **elapsed** and check its **gen% / tool%**. tool% in
   the low single digits is expected; a high tool% is the anomaly worth reporting. Note
   any leaf flagged `[dead wall dropped]` or `[warm resume]` — its raw numbers are not
   comparable to the others'.
4. In the inside-leaf section, identify the **dominant turn(s)**. A `generate.generate`
   leaf is typically dominated by one large "write the whole source" turn; `verify` /
   `judge` leaves spread time across many `thinking`-heavy turns.

### Step 3 — report
State, per node: total min, LLM share, the slowest leaf, and its dominant turn
(latency + output tokens + tok/s), plus any anomaly. Anchor any speedup claim to whether
it reduces **output tokens generated** (the throughput floor is ~85–115 tok/s for Opus);
tooling / sandbox / IO reductions do not move the wall clock here. A truncated turn is the
exception — it is pure waste and is removed by giving the leaf MORE room, not less
thinking.

## Interpretation reference (canonical findings)
- ~85–100% of node leaf time is the leaf `claude -p` calls. The conductor's deterministic
  steps are negligible EXCEPT `validate.execute` on an M3c problem node, where it runs the
  actual multi-resolution simulation (measured: 739 s = 19% of that node's leaf time for
  3 resolutions up to n128). That is real compute and is not a target for optimization.
- Inside an LLM leaf, ~98–100% of time is model generation latency (thinking + output
  streaming); all tool execution combined is ≤ ~3 s.
- **A turn can hit `max_tokens` while still thinking, and then emit nothing at all.**
  Thinking tokens count toward `max_tokens` on Opus 4.8, so with no headroom the model
  reasons up to the ceiling, gets cut off with `stop_reason: "max_tokens"` and a
  thinking-only content block, and **redoes the whole turn**. Observed twice in one
  closure — 64,000 tok / ~747 s each (exactly the ceiling, at a normal ~86 tok/s), ~9% of
  the run's leaf time and ~11% of its tokens, for zero output. This is NOT a rate limit,
  a stall, or a sleep: the API returned a normal billed response (a `requestId`, a
  `service_tier`, no `isApiErrorMessage`), and the latency equals tokens ÷ throughput.
  The lever is `CLAUDE_CODE_MAX_OUTPUT_TOKENS` (128,000 on Opus 4.8), not less thinking.
- **A leaf can die of infrastructure.** `API Error: Connection closed mid-response.` (and
  friends) is written to the leaf's piped stdout, persisted at
  `workspace/orchestrations/<id>/agents/<arid>/dialogs/leaf.stdout.log`, and flagged in
  the transcript with `isApiErrorMessage`. The conductor reports it as
  `reason_code=leaf_transport_error`. Do not read such a leaf's timing as model behavior,
  and do not read its `elapsed` at all (trap 6).
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
