#!/usr/bin/env python3
"""Elapsed-time / token-consumption audit for a workflow orchestration.

Breaks a completed (or interrupted) orchestration run down into:
  - per-leaf elapsed time (step.substep), separating LLM leaves from the
    conductor's in-process deterministic steps (Build / Validate.execute),
  - inside each LLM leaf: model-generation latency vs. tool-execution latency,
    output tokens, generation throughput, and the dominant turns,
  - node- and run-level rollups,
  - an ANOMALIES section: turns truncated by `max_tokens` (thinking that
    produced nothing), leaf API/transport errors, dead wall clock (a dead leaf's
    resume-stamped terminal vs. a host suspend), and
    warm-resume replays.

Multiple-counting is handled structurally (see MULTIPLE-COUNTING below), so the
output tokens and latencies are not inflated.

Usage:
  python3 analyze_timing.py [orchestration_id] [--json] [--project-dir DIR]

  orchestration_id  Target under workspace/orchestrations/. Omit to auto-pick
                    the most recent orch_* directory.
  --json            Emit machine-readable JSON instead of the text report.
  --project-dir     Claude transcript dir (default: ~/.claude/projects/<slug>
                    where <slug> is the repo abs-path with '/' -> '-').

MULTIPLE-COUNTING (why naive sums are wrong, and how this script avoids it):
  1) One model API response is written to the session transcript as SEVERAL
     jsonl lines (a `thinking` line, a `text` line, one or more `tool_use`
     lines) that all share the same `message.id` and repeat the SAME
     `usage.output_tokens`. Summing output_tokens per line double/triple counts,
     and counting each line as a "turn" inflates the turn count. -> We collapse
     lines by `message.id`: one response = one latency + one output_tokens.
  2) `usage.cache_read_input_tokens` is the context RE-READ on every turn; it
     grows as the session accumulates. Summing it across turns counts the same
     prompt many times. -> We never sum cache_read; we report its range only.
  3) `agent_runs.jsonl` carries no usage and the substep records there share
     ids with the phase_state_log `step` records; joining naively mislabels
     leaves. -> Timing comes only from phase_state_log launch/terminal pairs,
     keyed by agent_run_id; the role/substep label is taken from the matching
     session_run_index / agent_runs entry, not re-derived.
  4) A WARM-RESUMED leaf's transcript REPLAYS the producer leaf's messages
     verbatim (same `message.id`, same usage). Summing per-leaf tokens then
     counts the producer's work twice — measured at ~13% inflation on a real
     closure (162k tokens across 3 resumed leaves). -> Message ids are deduped
     GLOBALLY across the run in launch order: the first leaf to emit an id owns
     it, and a later leaf that replays it reports only its NEW responses. The
     replayed span is reported separately, never summed into the totals.
  5) A leaf's `child_launched -> record_agent_run_terminal` span is WALL-CLOCK,
     and wall clock can contain time in which nothing ran. Two distinct causes
     produce the same signature (elapsed >> transcript wall), and the report must
     not assert one when it was the other:
       a) DEAD LEAF. The child died (transport error, crash) and its terminal
          event was stamped when the conductor next ran — i.e. at the operator's
          `--resume`, HOURS later. The gap is the human fix window (observed:
          6.9h elapsed for a leaf with 129s of transcript).
       b) HOST SUSPEND. The whole run was frozen mid-leaf (laptop sleep; on WSL2
          the VM is paused when Windows sleeps). The leaf resumes and COMPLETES
          NORMALLY; only the wall clock jumped (observed: 4.3h on a leaf that
          passed).
     -> The excess over the transcript wall is excluded from the elapsed either
     way (never added to the LLM/deterministic split), and its cause is then
     CLASSIFIED, not assumed: a leaf whose terminal status is not `pass` died (a);
     a leaf that passed cannot have died, and the conductor's own MONOTONIC
     elapsed (see below) confirms its process was frozen (b). Anything else is
     reported as unattributed rather than guessed.

  6) `time.monotonic()` DOES NOT ADVANCE WHILE THE HOST IS SUSPENDED (Linux
     CLOCK_MONOTONIC), so the conductor's `substep_complete.elapsed_seconds` in
     `run_logs/` is suspend-immune, whereas every timestamp in
     `phase_state_log.jsonl` / `orchestration_meta.json` is wall clock and is not.
     Their divergence over one continuously-running process is therefore a direct
     measurement of host suspend, and is what distinguishes 5(b) from 5(a).
     COROLLARY: `orchestration_meta.json`'s `finished_at - started_at` is NOT the
     run's cost. Quote the monotonic total.
"""
import glob
import json
import os
import sys
from datetime import datetime

# A leaf's elapsed may legitimately exceed its transcript wall by process
# startup/teardown (seconds). Beyond this, the excess is dead time
# (see MULTIPLE-COUNTING 5), not leaf activity.
STALE_GAP_S = 600.0


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def find_repo_root(start):
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, "workspace", "orchestrations")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.abspath(start)
        cur = parent


def pick_orch(orch_dir, requested):
    if requested:
        p = os.path.join(orch_dir, requested)
        if not os.path.isdir(p):
            sys.exit(f"orchestration not found: {p}")
        return requested
    # Sort by mtime, not name: distinct runs can share the orch_ timestamp
    # prefix (it is the parent run's start), so a name sort is unreliable.
    cands = [d for d in os.listdir(orch_dir) if d.startswith("orch_")]
    if not cands:
        sys.exit(f"no orch_* directories under {orch_dir}")
    cands.sort(key=lambda d: os.path.getmtime(os.path.join(orch_dir, d)))
    return cands[-1]


def load_leaf_durations(orch_path):
    """Pair child_launched -> record_agent_run_terminal from phase_state_log.

    Returns ordered list of dicts: {run_id, step, dur_s, t0, t1}.
    """
    log = os.path.join(orch_path, "phase_state_log.jsonl")
    launched = {}
    rows = []
    for line in open(log):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        ev = d.get("event")
        rid = d.get("agent_run_id")
        if ev == "child_launched":
            launched[rid] = (ts(d["ts"]), d.get("step", ""))
        elif ev == "record_agent_run_terminal" and rid in launched:
            t0, step = launched.pop(rid)
            t1 = ts(d["ts"])
            rows.append(
                {"run_id": rid, "step": step, "dur_s": (t1 - t0).total_seconds(),
                 "t0": t0, "t1": t1}
            )
    return rows


def load_monotonic_elapsed(orch_path):
    """run_id -> the conductor's own monotonic elapsed for that substep.

    `substep_complete.elapsed_seconds` is measured with `time.monotonic()`, which
    does not tick while the host is suspended (MULTIPLE-COUNTING 6). It is thus
    the only suspend-immune timing in the run, and the ground truth against which
    a wall-clock gap is classified. Every conductor process that touched this
    orchestration writes its own run_log; a later pass (a `--resume`) re-runs the
    substep and legitimately overwrites the entry, so last-write wins.
    """
    mono = {}
    for path in sorted(glob.glob(os.path.join(orch_path, "run_logs", "*.jsonl"))):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("event") != "substep_complete":
                continue
            rid, el = d.get("agent_run_id"), d.get("elapsed_seconds")
            if rid and el is not None:
                mono[rid] = float(el)
    return mono


def classify_gap(status, wall_s, mono_s):
    """Why did this leaf's wall span exceed its transcript wall? (see MULTIPLE-COUNTING 5)

    Classified from evidence, never assumed: a leaf that PASSED cannot have died,
    so a resume-stamped terminal is ruled out; and if the conductor's monotonic
    clock advanced far less than the wall clock, its process was frozen.
    """
    if status and status != "pass":
        return "dead_leaf"
    if mono_s is not None and wall_s - mono_s > STALE_GAP_S:
        return "host_suspend"
    return "unattributed"


GAP_CAUSE_NOTE = {
    "dead_leaf": "dead leaf's terminal record stamped when the conductor next ran "
                 "(the operator's --resume); the gap is the human fix window",
    "host_suspend": "host suspended mid-leaf (the leaf then passed); wall clock jumped "
                    "while the conductor's monotonic clock stood still",
    "unattributed": "cause not established from the logs",
}


def load_labels(orch_path):
    """run_id -> (role, substep, status) from session_run_index + agent_runs."""
    labels = {}
    sri = os.path.join(orch_path, "session_run_index.json")
    if os.path.exists(sri):
        for e in json.load(open(sri)).get("entries", []):
            labels[e["agent_run_id"]] = {
                "role": e.get("agent_role", ""), "substep": "", "status": e.get("status", "")
            }
    ar = os.path.join(orch_path, "agent_runs.jsonl")
    if os.path.exists(ar):
        for line in open(ar):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            rid = d.get("agent_run_id")
            lab = labels.setdefault(rid, {"role": "", "substep": "", "status": ""})
            if d.get("substep"):
                lab["substep"] = d["substep"]
            if d.get("status"):
                lab["status"] = d["status"]
    return labels


def read_transcript(path):
    """Per-response records from a session transcript, collapsed by message.id.

    Returns None if the transcript has no model turns (deterministic step).
    Aggregation is deliberately NOT done here: a warm-resumed leaf replays the
    producer's responses, so which of them count is a RUN-level question that
    only `summarize` (with the globally-seen id set) can answer.
    """
    events = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if "timestamp" in d:
            events.append(d)
    if not events:
        return None
    events.sort(key=lambda d: d["timestamp"])

    # Keyed by message.id, NOT by a consecutive run of lines: a response that
    # makes PARALLEL tool calls has its `tool_use` lines interleaved with the
    # `tool_result` user lines that answer them, so the same message.id reappears
    # after a user line. Tracking "the current id" and closing it on a user line
    # splits such a response in two and counts its output_tokens TWICE (measured:
    # +20,659 tok on one node). The id is the identity; nothing else is.
    responses = {}          # message.id -> record (insertion-ordered)
    api_errors = []
    pending = {}            # tool_use_id -> (name, ts, owning response)
    cache_reads = []
    last_event_t = ts(events[0]["timestamp"])

    for d in events:
        T = ts(d["timestamp"])
        typ = d.get("type")
        msg = d.get("message", {}) if isinstance(d.get("message"), dict) else {}
        content = msg.get("content") or []
        if d.get("isApiErrorMessage"):
            # The claude CLI reports a transport/API fault as a normal assistant
            # line flagged with isApiErrorMessage. This is the ONLY in-transcript
            # evidence that a leaf died of infrastructure rather than of its own
            # reasoning, so surface it instead of letting it look like prose.
            txt = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text")
            api_errors.append({"ts": d["timestamp"], "text": " ".join(txt.split())[:200]})
        if typ == "assistant":
            mid = msg.get("id")
            usage = msg.get("usage") or {}
            blocks = [b.get("type") for b in content if isinstance(b, dict)]
            cur = responses.get(mid)
            if cur is None:
                cur = responses[mid] = {
                    "id": mid,
                    "lat": max(0.0, (T - last_event_t).total_seconds()),
                    "out": usage.get("output_tokens", 0),
                    "stop_reason": msg.get("stop_reason"),
                    "blocks": set(blocks),
                    "text_chars": 0,
                    "tooluse_chars": 0,
                    "tool_s": 0.0,
                }
            else:
                # same API response continued on another line: keep one usage,
                # merge block kinds, do NOT add output_tokens again.
                cur["out"] = usage.get("output_tokens", cur["out"])
                cur["blocks"].update(blocks)
                cur["stop_reason"] = msg.get("stop_reason") or cur["stop_reason"]
            cr = usage.get("cache_read_input_tokens")
            if cr:
                cache_reads.append(cr)
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    cur["text_chars"] += len(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    # the serialized tool input is the *visible* generated output
                    # for this block (e.g. the file content written by Write/Edit)
                    cur["tooluse_chars"] += len(json.dumps(b.get("input", {})))
                    pending[b.get("id")] = (b.get("name"), T, cur)
            last_event_t = T
        elif typ == "user":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid in pending:
                        name, tu, owner = pending.pop(tid)
                        dt = (T - tu).total_seconds()
                        # Attribute tool time to the RESPONSE that made the call,
                        # so a replayed response's tool time drops out with it.
                        owner["tool_s"] += dt
                        owner.setdefault("tools", []).append((name, dt))
            last_event_t = T

    wall = (ts(events[-1]["timestamp"]) - ts(events[0]["timestamp"])).total_seconds()
    return {
        "wall_s": wall,
        "responses": list(responses.values()),
        "api_errors": api_errors,
        "cache_read_min": min(cache_reads) if cache_reads else 0,
        "cache_read_max": max(cache_reads) if cache_reads else 0,
    }


def summarize(raw, seen_ids):
    """Aggregate one leaf's transcript over the responses it actually PRODUCED.

    `seen_ids` is the set of message ids already owned by an earlier leaf; any
    response whose id is in it is a warm-resume REPLAY of that leaf's work and
    is excluded from every total (see MULTIPLE-COUNTING 4). It mutates seen_ids
    with this leaf's new ids, so callers must walk leaves in LAUNCH order.
    """
    fresh, replayed = [], []
    for r in raw["responses"]:
        (replayed if r["id"] in seen_ids else fresh).append(r)
    seen_ids.update(r["id"] for r in fresh)

    def vis(r):
        return (r["text_chars"] + r["tooluse_chars"]) // 4

    gen_time = sum(r["lat"] for r in fresh)
    tool_time = sum(r["tool_s"] for r in fresh)
    out_tok = sum(r["out"] for r in fresh)
    # Token attribution: usage.output_tokens INCLUDES extended-thinking tokens,
    # which are billed/counted as output but never appear in the written files.
    # Estimate the visible share (text + serialized tool inputs, ~4 chars/token)
    # so "output tokens" is not mistaken for "source emitted".
    visible_tok = sum(vis(r) for r in fresh)
    thinking_tok = max(0, out_tok - visible_tok)

    def kind(r):
        b = r["blocks"]
        if "text" in b:
            return "text"
        if "thinking" in b and "tool_use" not in b:
            return "think_only"
        return "tool_only"

    buckets, tool_by = {}, {}
    for r in fresh:
        e = buckets.setdefault(kind(r), [0, 0.0])
        e[0] += 1
        e[1] += r["lat"]
        for name, dt in r.get("tools", []):
            te = tool_by.setdefault(name, [0, 0.0])
            te[0] += 1
            te[1] += dt

    def resp_json(r):
        return {"lat_s": r["lat"], "out_tokens": r["out"], "visible_tokens": vis(r),
                "thinking_tokens": max(0, r["out"] - vis(r)),
                "rate": (r["out"] / r["lat"]) if r["lat"] > 0 else 0,
                "stop_reason": r["stop_reason"], "blocks": sorted(r["blocks"])}

    # A turn the API cut off at the output ceiling. When its blocks are thinking
    # ONLY, the leaf spent the whole ceiling reasoning and emitted NOTHING — no
    # text, no tool call — and the next turn redoes the work. Observed twice in a
    # real closure: 64000 tok / ~747s each, ~9% of the run's leaf time, for zero
    # output. The fix is more room (CLAUDE_CODE_MAX_OUTPUT_TOKENS), not less
    # thinking: thinking tokens count toward max_tokens.
    truncated = [resp_json(r) for r in fresh if r["stop_reason"] == "max_tokens"]

    return {
        "wall_s": raw["wall_s"],
        "n_responses": len(fresh),
        # TOKENS survive a warm resume exactly (usage is replayed verbatim), but LATENCIES
        # do not: the replayed lines are re-stamped at resume time, which compresses the
        # producer's timeline and makes the first fresh turn's latency (measured against a
        # replayed line) meaningless. Everything derived from a latency — model_gen_s,
        # gen%, tok/s — is therefore reported but NOT trusted for a resumed leaf; the
        # renderer blanks those columns rather than printing a plausible-looking lie
        # (observed: 835 tok/s, ~10x the physical floor).
        "latency_reliable": not replayed,
        "model_gen_s": gen_time,
        "tool_exec_s": tool_time,
        "out_tokens": out_tok,
        "visible_tokens": visible_tok,
        "thinking_tokens": thinking_tok,
        "gen_rate_tok_s": (out_tok / gen_time) if gen_time > 0 else 0,
        "cache_read_min": raw["cache_read_min"],
        "cache_read_max": raw["cache_read_max"],
        "buckets": buckets,
        "tool_by": tool_by,
        "top_responses": [resp_json(r) for r in sorted(fresh, key=lambda r: -r["lat"])[:5]],
        "truncated_turns": truncated,
        "api_errors": raw["api_errors"],
        "replayed_responses": len(replayed),
        "replayed_tokens": sum(r["out"] for r in replayed),
    }


def main():
    args = sys.argv[1:]
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    project_dir = None
    if "--project-dir" in args:
        i = args.index("--project-dir")
        project_dir = args[i + 1]
        del args[i:i + 2]
    requested = args[0] if args else None

    repo_root = find_repo_root(os.getcwd())
    orch_dir = os.path.join(repo_root, "workspace", "orchestrations")
    orch_id = pick_orch(orch_dir, requested)
    orch_path = os.path.join(orch_dir, orch_id)

    if project_dir is None:
        slug = repo_root.replace("/", "-")
        project_dir = os.path.expanduser(os.path.join("~/.claude/projects", slug))

    durations = load_leaf_durations(orch_path)
    labels = load_labels(orch_path)
    mono = load_monotonic_elapsed(orch_path)

    # Read every transcript first, then summarize in LAUNCH order: the global
    # message-id dedupe (MULTIPLE-COUNTING 4) must attribute a replayed response
    # to the leaf that first produced it, which is the earlier-launched one.
    raws = {}
    for row in durations:
        tpath = os.path.join(project_dir, row["run_id"] + ".jsonl")
        raws[row["run_id"]] = read_transcript(tpath) if os.path.exists(tpath) else None

    seen_ids = set()
    summaries = {}
    for row in sorted(durations, key=lambda r: r["t0"]):
        raw = raws[row["run_id"]]
        summaries[row["run_id"]] = summarize(raw, seen_ids) if raw else None

    leaves = []
    for row in durations:
        rid = row["run_id"]
        lab = labels.get(rid, {"role": "", "substep": "", "status": ""})
        sub = lab["substep"]
        name = f"{row['step']}.{sub}" if sub else row["step"]
        tr = summaries[rid]
        elapsed = row["dur_s"]
        mono_s = mono.get(rid)
        # Dead wall time (MULTIPLE-COUNTING 5): the leaf's wall span exceeds the
        # work its transcript shows. Exclude the excess regardless of cause, then
        # classify the cause from evidence (dead leaf vs host suspend).
        stale_gap = 0.0
        gap_cause = None
        if tr is not None and elapsed - tr["wall_s"] > STALE_GAP_S:
            stale_gap = elapsed - tr["wall_s"]
            elapsed = tr["wall_s"]
            gap_cause = classify_gap(lab["status"], row["dur_s"], mono_s)
        leaves.append({
            "run_id": rid, "step": row["step"], "name": name,
            "role": lab["role"], "status": lab["status"],
            "elapsed_s": elapsed, "raw_elapsed_s": row["dur_s"], "stale_gap_s": stale_gap,
            "gap_cause": gap_cause, "monotonic_s": mono_s,
            "is_llm": tr is not None,
            "transcript": tr,
        })

    meta_path = os.path.join(orch_path, "orchestration_meta.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}

    # Run wall clock is NOT the run's cost: it contains any host suspend
    # (MULTIPLE-COUNTING 6). Reported only to be contrasted with the leaf totals.
    run_wall_s = None
    if meta.get("started_at") and meta.get("finished_at"):
        run_wall_s = (ts(meta["finished_at"]) - ts(meta["started_at"])).total_seconds()

    result = {
        "orchestration_id": orch_id,
        "spec_ref": meta.get("spec_ref"),
        "status": meta.get("status"),
        "project_dir": project_dir,
        "run_wall_s": run_wall_s,
        "leaves": leaves,
    }
    if as_json:
        print(json.dumps(result, indent=2))
        return

    render(result)


def render(r):
    llm = [l for l in r["leaves"] if l["is_llm"]]
    total = sum(l["elapsed_s"] for l in r["leaves"])
    llm_total = sum(l["elapsed_s"] for l in llm)
    out_total = sum(l["transcript"]["out_tokens"] for l in llm)
    think_total = sum(l["transcript"]["thinking_tokens"] for l in llm)
    vis_total = sum(l["transcript"]["visible_tokens"] for l in llm)
    replay_tok = sum(l["transcript"]["replayed_tokens"] for l in llm)
    replay_leaves = [l for l in llm if l["transcript"]["replayed_responses"]]

    print(f"orchestration: {r['orchestration_id']}  ({r.get('status')})")
    print(f"spec: {r.get('spec_ref')}")
    print(f"transcripts: {r['project_dir']}")
    print()
    wall = r.get("run_wall_s")
    if wall:
        print(f"run wall clock: {wall/60:.1f} min  (meta finished_at - started_at -- NOT the cost: "
              f"wall clock also ticks while the host is suspended)")
    print(f"leaf elapsed total: {total:.0f}s = {total/60:.1f} min")
    print(f"  LLM leaves:           {llm_total:6.0f}s ({pct(llm_total,total)})")
    print(f"  deterministic (cond): {total-llm_total:6.0f}s ({pct(total-llm_total,total)})  "
          f"(Build / Validate.execute, no LLM)")
    print(f"  output tokens={out_total}  =  thinking {think_total} ({pct(think_total,out_total)}) "
          f"+ visible {vis_total} ({pct(vis_total,out_total)})")
    print("    NOTE output_tokens includes extended thinking (billed as output, not in "
          "the files); visible = text + serialized tool inputs (the emitted source).")
    for cause in ("dead_leaf", "host_suspend", "unattributed"):
        gap = sum(l["stale_gap_s"] for l in r["leaves"] if l["gap_cause"] == cause)
        if gap:
            print(f"  EXCLUDED dead wall:   {gap:6.0f}s = {gap/3600:.1f}h  "
                  f"({GAP_CAUSE_NOTE[cause]}; not leaf activity)")
    if replay_tok:
        print(f"  EXCLUDED replay:      {replay_tok} tok across {len(replay_leaves)} warm-resumed "
              f"leaf(s) (producer's turns, already counted once)")
    print()
    print("per-leaf:")
    hdr = (f"  {'step.substep':24s} {'elapsed':>8s} {'kind':>6s} {'gen%':>5s} {'tool%':>6s} "
           f"{'out_tok':>8s} {'think%':>7s} {'vis_tok':>8s} {'tok/s':>6s} {'resp':>5s}")
    print(hdr)
    for l in r["leaves"]:
        if l["is_llm"]:
            t = l["transcript"]
            flags = ""
            if l["stale_gap_s"]:
                flags += (f"  [+{l['stale_gap_s']/3600:.1f}h dead wall dropped: "
                          f"{l['gap_cause']}]")
            if t["replayed_responses"]:
                flags += (f"  [warm resume: {t['replayed_responses']} replayed turns dropped; "
                          f"latencies unreliable]")
            reliable = t["latency_reliable"]
            gen = pct(t["model_gen_s"], t["wall_s"]) if reliable else "—"
            tool = pct(t["tool_exec_s"], t["wall_s"]) if reliable else "—"
            rate = f"{t['gen_rate_tok_s']:.0f}" if reliable else "—"
            print(f"  {l['name']:24s} {l['elapsed_s']:7.0f}s {'LLM':>6s} "
                  f"{gen:>5s} {tool:>6s} "
                  f"{t['out_tokens']:>8d} {pct(t['thinking_tokens'],t['out_tokens']):>7s} "
                  f"{t['visible_tokens']:>8d} {rate:>6s} {t['n_responses']:>5d}"
                  f"{flags}")
        else:
            print(f"  {l['name']:24s} {l['elapsed_s']:7.0f}s {'det':>6s} {'—':>5s} {'—':>6s} "
                  f"{'—':>8s} {'—':>7s} {'—':>8s} {'—':>6s} {'—':>5s}")
    print()
    render_anomalies(r, llm)
    print("dominant turns inside LLM leaves (multiple-counting collapsed by message.id):")
    for l in sorted(llm, key=lambda x: -x["transcript"]["model_gen_s"]):
        t = l["transcript"]
        bk = ", ".join(f"{k}:{v[0]}t/{v[1]:.0f}s" for k, v in
                       sorted(t["buckets"].items(), key=lambda x: -x[1][1]))
        warn = "" if t["latency_reliable"] else "  (WARM RESUME: latencies unreliable)"
        print(f"  {l['name']}  model_gen={t['model_gen_s']:.0f}s tool={t['tool_exec_s']:.1f}s "
              f"cache_read={t['cache_read_min']}..{t['cache_read_max']}{warn}")
        print(f"     by-kind: {bk}")
        for tr in t["top_responses"][:3]:
            print(f"     top: {tr['lat_s']:6.1f}s  {tr['out_tokens']:6d} tok "
                  f"(think {tr['thinking_tokens']} / vis {tr['visible_tokens']})  "
                  f"{tr['rate']:5.0f} tok/s  {tr['blocks']}")


def render_anomalies(r, llm):
    """Failure/waste signals that a pure time-and-token table hides."""
    trunc = [(l, t) for l in llm for t in l["transcript"]["truncated_turns"]]
    errs = [(l, e) for l in llm for e in l["transcript"]["api_errors"]]
    if not trunc and not errs:
        return
    print("anomalies:")
    if trunc:
        waste_tok = sum(t["out_tokens"] for _, t in trunc)
        waste_s = sum(t["lat_s"] for _, t in trunc)
        blind = [(l, t) for l, t in trunc if t["blocks"] == ["thinking"]]
        print(f"  turns cut off by max_tokens: {len(trunc)}  "
              f"({waste_tok} out_tok, {waste_s:.0f}s = {waste_s/60:.1f} min)")
        for l, t in sorted(trunc, key=lambda x: -x[1]["lat_s"]):
            zero = "  <- THINKING ONLY: zero visible output, the work is REDONE next turn" \
                if t["blocks"] == ["thinking"] else ""
            print(f"    {l['name']:22s} {t['lat_s']:6.0f}s  {t['out_tokens']:6d} tok "
                  f"(think {t['thinking_tokens']} / vis {t['visible_tokens']})  "
                  f"blocks={t['blocks']}{zero}")
        if blind:
            print("    => thinking tokens count toward max_tokens. The lever is MORE ROOM "
                  "(CLAUDE_CODE_MAX_OUTPUT_TOKENS, 128000 on Opus 4.8), not less thinking.")
    if errs:
        print(f"  leaf API/transport errors: {len(errs)}")
        for l, e in errs:
            print(f"    {l['name']:22s} {e['ts']}  {e['text']}")
        print("    => the leaf died of infrastructure, not of its own reasoning. Cross-check "
              "the conductor's reason_code (leaf_transport_error) and the persisted "
              "agents/<arid>/dialogs/leaf.stdout.log.")
    print()


def pct(a, b):
    return f"{(100*a/b):.0f}%" if b else "—"


if __name__ == "__main__":
    main()
