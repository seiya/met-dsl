#!/usr/bin/env python3
"""Elapsed-time / token-consumption audit for a workflow orchestration.

Breaks a completed (or interrupted) orchestration run down into:
  - per-leaf elapsed time (step.substep), separating LLM leaves from the
    conductor's in-process deterministic steps (Build / Validate.execute),
  - inside each LLM leaf: model-generation latency vs. tool-execution latency,
    output tokens, generation throughput, and the dominant turns,
  - node- and run-level rollups.

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
"""
import json
import os
import sys
from datetime import datetime


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


def analyze_transcript(path):
    """Collapse a session transcript by message.id (handles multiple-counting).

    Returns None if the transcript has no model turns (deterministic step).
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

    responses = []          # one entry per distinct message.id
    tool_time = 0.0
    tool_by = {}            # name -> [count, secs]
    pending = {}            # tool_use_id -> (name, ts)
    cache_reads = []
    last_event_t = ts(events[0]["timestamp"])
    cur_id = None
    cur = None

    def close_cur():
        nonlocal cur, cur_id
        if cur is not None:
            responses.append(cur)
        cur = None
        cur_id = None

    for d in events:
        T = ts(d["timestamp"])
        typ = d.get("type")
        msg = d.get("message", {}) if isinstance(d.get("message"), dict) else {}
        content = msg.get("content") or []
        if typ == "assistant":
            mid = msg.get("id")
            usage = msg.get("usage") or {}
            blocks = [b.get("type") for b in content if isinstance(b, dict)]
            if mid != cur_id:
                close_cur()
                cur = {
                    "lat": max(0.0, (T - last_event_t).total_seconds()),
                    "out": usage.get("output_tokens", 0),
                    "blocks": set(blocks),
                    "text_chars": 0,
                    "tooluse_chars": 0,
                }
                cur_id = mid
            else:
                # same API response continued on another line: keep one usage,
                # merge block kinds, do NOT add output_tokens again.
                cur["out"] = usage.get("output_tokens", cur["out"])
                cur["blocks"].update(blocks)
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
                    pending[b.get("id")] = (b.get("name"), T)
            last_event_t = T
        elif typ == "user":
            close_cur()
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid in pending:
                        name, tu = pending.pop(tid)
                        dt = (T - tu).total_seconds()
                        tool_time += dt
                        e = tool_by.setdefault(name, [0, 0.0])
                        e[0] += 1
                        e[1] += dt
            last_event_t = T
    close_cur()

    gen_time = sum(r["lat"] for r in responses)
    out_tok = sum(r["out"] for r in responses)
    # Token attribution: usage.output_tokens INCLUDES extended-thinking tokens,
    # which are billed/counted as output but never appear in the written files.
    # Estimate the visible share (text + serialized tool inputs, ~4 chars/token)
    # so "output tokens" is not mistaken for "source emitted".
    visible_tok = sum((r["text_chars"] + r["tooluse_chars"]) for r in responses) // 4
    thinking_tok = max(0, out_tok - visible_tok)
    wall = (ts(events[-1]["timestamp"]) - ts(events[0]["timestamp"])).total_seconds()

    def kind(r):
        b = r["blocks"]
        if "text" in b:
            return "text"
        if "thinking" in b and "tool_use" not in b:
            return "think_only"
        return "tool_only"

    buckets = {}
    for r in responses:
        k = kind(r)
        e = buckets.setdefault(k, [0, 0.0])
        e[0] += 1
        e[1] += r["lat"]

    top = sorted(responses, key=lambda r: -r["lat"])[:5]
    return {
        "wall_s": wall,
        "n_responses": len(responses),
        "model_gen_s": gen_time,
        "tool_exec_s": tool_time,
        "out_tokens": out_tok,
        "visible_tokens": visible_tok,
        "thinking_tokens": thinking_tok,
        "gen_rate_tok_s": (out_tok / gen_time) if gen_time > 0 else 0,
        "cache_read_min": min(cache_reads) if cache_reads else 0,
        "cache_read_max": max(cache_reads) if cache_reads else 0,
        "buckets": buckets,
        "tool_by": tool_by,
        "top_responses": [
            {"lat_s": r["lat"], "out_tokens": r["out"],
             "visible_tokens": (r["text_chars"] + r["tooluse_chars"]) // 4,
             "thinking_tokens": max(0, r["out"] - (r["text_chars"] + r["tooluse_chars"]) // 4),
             "rate": (r["out"] / r["lat"]) if r["lat"] > 0 else 0,
             "blocks": sorted(r["blocks"])}
            for r in top
        ],
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

    leaves = []
    for row in durations:
        rid = row["run_id"]
        lab = labels.get(rid, {"role": "", "substep": "", "status": ""})
        sub = lab["substep"]
        name = f"{row['step']}.{sub}" if sub else row["step"]
        tpath = os.path.join(project_dir, rid + ".jsonl")
        tr = analyze_transcript(tpath) if os.path.exists(tpath) else None
        leaves.append({
            "run_id": rid, "step": row["step"], "name": name,
            "role": lab["role"], "status": lab["status"],
            "elapsed_s": row["dur_s"], "is_llm": tr is not None,
            "transcript": tr,
        })

    meta_path = os.path.join(orch_path, "orchestration_meta.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}

    result = {
        "orchestration_id": orch_id,
        "spec_ref": meta.get("spec_ref"),
        "status": meta.get("status"),
        "project_dir": project_dir,
        "leaves": leaves,
    }
    if as_json:
        print(json.dumps(result, indent=2))
        return

    render(result)


def render(r):
    llm = [l for l in r["leaves"] if l["is_llm"]]
    det = [l for l in r["leaves"] if not l["is_llm"]]
    total = sum(l["elapsed_s"] for l in r["leaves"])
    llm_total = sum(l["elapsed_s"] for l in llm)
    out_total = sum(l["transcript"]["out_tokens"] for l in llm)
    think_total = sum(l["transcript"]["thinking_tokens"] for l in llm)
    vis_total = sum(l["transcript"]["visible_tokens"] for l in llm)

    print(f"orchestration: {r['orchestration_id']}  ({r.get('status')})")
    print(f"spec: {r.get('spec_ref')}")
    print(f"transcripts: {r['project_dir']}")
    print()
    print(f"leaf elapsed total: {total:.0f}s = {total/60:.1f} min")
    print(f"  LLM leaves:           {llm_total:6.0f}s ({pct(llm_total,total)})")
    print(f"  deterministic (cond): {total-llm_total:6.0f}s ({pct(total-llm_total,total)})  "
          f"(Build / Validate.execute, no LLM)")
    print(f"  output tokens={out_total}  =  thinking {think_total} ({pct(think_total,out_total)}) "
          f"+ visible {vis_total} ({pct(vis_total,out_total)})")
    print("    NOTE output_tokens includes extended thinking (billed as output, not in "
          "the files); visible = text + serialized tool inputs (the emitted source).")
    print()
    print("per-leaf:")
    hdr = (f"  {'step.substep':24s} {'elapsed':>8s} {'kind':>6s} {'gen%':>5s} {'tool%':>6s} "
           f"{'out_tok':>8s} {'think%':>7s} {'vis_tok':>8s} {'tok/s':>6s} {'resp':>5s}")
    print(hdr)
    for l in r["leaves"]:
        if l["is_llm"]:
            t = l["transcript"]
            print(f"  {l['name']:24s} {l['elapsed_s']:7.0f}s {'LLM':>6s} "
                  f"{pct(t['model_gen_s'],t['wall_s']):>5s} {pct(t['tool_exec_s'],t['wall_s']):>6s} "
                  f"{t['out_tokens']:>8d} {pct(t['thinking_tokens'],t['out_tokens']):>7s} "
                  f"{t['visible_tokens']:>8d} {t['gen_rate_tok_s']:>6.0f} {t['n_responses']:>5d}")
        else:
            print(f"  {l['name']:24s} {l['elapsed_s']:7.0f}s {'det':>6s} {'—':>5s} {'—':>6s} "
                  f"{'—':>8s} {'—':>7s} {'—':>8s} {'—':>6s} {'—':>5s}")
    print()
    print("dominant turns inside LLM leaves (multiple-counting collapsed by message.id):")
    for l in sorted(llm, key=lambda x: -x["transcript"]["model_gen_s"]):
        t = l["transcript"]
        bk = ", ".join(f"{k}:{v[0]}t/{v[1]:.0f}s" for k, v in
                       sorted(t["buckets"].items(), key=lambda x: -x[1][1]))
        print(f"  {l['name']}  model_gen={t['model_gen_s']:.0f}s tool={t['tool_exec_s']:.1f}s "
              f"cache_read={t['cache_read_min']}..{t['cache_read_max']}")
        print(f"     by-kind: {bk}")
        for tr in t["top_responses"][:3]:
            print(f"     top: {tr['lat_s']:6.1f}s  {tr['out_tokens']:6d} tok "
                  f"(think {tr['thinking_tokens']} / vis {tr['visible_tokens']})  "
                  f"{tr['rate']:5.0f} tok/s  {tr['blocks']}")


def pct(a, b):
    return f"{(100*a/b):.0f}%" if b else "—"


if __name__ == "__main__":
    main()
