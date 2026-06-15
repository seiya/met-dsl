---
name: workflow-audit
description: Use this when investigating the orchestration logs of an executed workflow and surfacing/reporting hook blocks, information-gathering behavior, and redos due to check failures. The target orchestration_id and session_id are auto-detected. Claude Code-only
---

# Workflow Audit

## Purpose
Investigate the logs of a completed or interrupted workflow execution across the board, and enumerate problems in the following 3 categories.

1. **hook blocks** — operations for which a hook returned `action=block`
2. **information-gathering behavior** — places where, due to unclear CLI specifications or insufficient state awareness, a `--help` reference or file exploration was performed
3. **redos due to check failures** — gate / validator failures, multiple phase-launch attempts, status-setting mistakes

## Log collection sources

| log | collection source |
|---|---|
| hook blocks | `workspace/orchestrations/<orch_id>/hooks/native_hook_events.jsonl` |
| workflow hook history | `workspace/orchestrations/<orch_id>/hooks/workflow_hooks.jsonl` |
| agent execution results | `workspace/orchestrations/<orch_id>/agent_runs.jsonl` |
| phase state transitions | `workspace/orchestrations/<orch_id>/phase_state_log.jsonl` |
| gate results | `workspace/orchestrations/<orch_id>/gates/<agent_run_id>/*.json` |
| sandbox violations | `workspace/orchestrations/<orch_id>/violations/*.json` |
| access logs | `workspace/orchestrations/<orch_id>/access_logs/<agent_run_id>.jsonl` |
| failure analysis | `workspace/orchestrations/<orch_id>/failure_analysis.json` |
| session conversation log | `~/.claude/projects/<cwd-slug>/<session_id>.jsonl` (`<cwd-slug>` is the repo's absolute path with `/` replaced by `-`) |

## Investigation procedure

### Step 1 — Identify the orchestration_id

```bash
ls workspace/orchestrations/
```

When there are multiple targets, choose the most recent `orch_YYYYMMDDTHHMMSSZ_*` directory.
To investigate a specific orchestration, use the instructed `orchestration_id`.

### Step 2 — Auto-detect the session_id

Read the `payload_summary.session_id` recorded in `native_hook_events.jsonl`, and
identify the corresponding `.jsonl` file under `~/.claude/projects/<cwd-slug>/`.
`<cwd-slug>` is the repo's absolute path with `/` replaced by `-` (e.g. `/home/alice/work/met-dsl` → `-home-alice-work-met-dsl`).

```bash
python3 - <<'EOF'
import json, pathlib

orch_id = "<orchestration_id>"   # substitute the value fixed in Step 1
hook_log = pathlib.Path(f"workspace/orchestrations/{orch_id}/hooks/native_hook_events.jsonl")
session_ids = set()
for line in hook_log.read_text().splitlines():
    if not line.strip():
        continue
    obj = json.loads(line)
    sid = obj.get("payload_summary", {}).get("session_id")
    if sid:
        session_ids.add(sid)

cwd_slug = str(pathlib.Path.cwd().resolve()).replace("/", "-")
projects_dir = pathlib.Path.home() / ".claude/projects" / cwd_slug
for sid in sorted(session_ids):
    p = projects_dir / f"{sid}.jsonl"
    exists = "found" if p.exists() else "NOT FOUND"
    print(f"{sid}: {exists}  ({p})")
EOF
```

For each detected session_id, fix the path of the corresponding `.jsonl`.

### Step 3 — Extract hook blocks

Extract all records with `action=block` from `native_hook_events.jsonl`.

```bash
python3 - <<'EOF'
import json

orch_id = "<orchestration_id>"
path = f"workspace/orchestrations/{orch_id}/hooks/native_hook_events.jsonl"
blocks = []
with open(path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("action") == "block":
            blocks.append(obj)

for b in blocks:
    print(json.dumps(b, ensure_ascii=False, indent=2))
EOF
```

Record the following for each block.

- `ts` — the time of occurrence
- `tool_name` — the blocked tool (`Read` / `Bash` / `Write` etc.)
- `reason` — the block reason
- `audit_detail.policy` — the applied policy name
- `payload_summary` — the operation-target path or command (first 200 characters)

### Step 3.5 — Aggregate block counts per policy

Count the `blocks` obtained in Step 3 per policy, and highlight 5 or more as a **repeated error pattern**.

```bash
python3 - <<'EOF'
import json
from collections import Counter

orch_id = "<orchestration_id>"
path = f"workspace/orchestrations/{orch_id}/hooks/native_hook_events.jsonl"
policy_counter: Counter = Counter()
with open(path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("action") == "block":
            policy = (obj.get("audit_detail") or {}).get("policy", "unknown")
            policy_counter[policy] += 1

print("=== Policy block counts ===")
for policy, cnt in policy_counter.most_common():
    flag = " *** REPEATED ERROR PATTERN ***" if cnt >= 5 else ""
    print(f"  {policy}: {cnt}{flag}")
EOF
```

When a repeated error pattern (5 or more) is detected, refer to the corresponding row of the repair cheat sheet (`docs/RUNBOOK.md#hook-recovery`) to identify the root cause.

### Step 4 — Extract information-gathering behavior

Extract `--help` calls, file-exploration commands, and grep/sed of the runtime implementation from the session's `.jsonl`.

```bash
python3 - <<'EOF'
import json

SESSION = "<session_jsonl_path>"   # the path fixed in Step 2

patterns = ["--help", "grep -n", "grep -rn", "sed -n", r"find \.", "ls /home", "ls workspace", "cat tools/"]

results = []
with open(SESSION) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            inp = block.get("input", {})
            if name == "Bash":
                cmd = inp.get("command", "")
                for pat in patterns:
                    if pat in cmd:
                        results.append({"tool": "Bash", "match": pat, "command": cmd[:300]})
                        break
            elif name == "Read":
                fp = inp.get("file_path", "")
                if "tools/" in fp:
                    results.append({"tool": "Read", "match": "tools/ direct read", "file_path": fp})

for r in results:
    print(json.dumps(r, ensure_ascii=False))
EOF
```

Classify the extracted results by the following perspectives.

- `--help` references — places where the CLI specification was unclear (argument format, subcommand name, etc.)
- `tools/` direct grep/sed/read — places where a rule was attempted to be derived from the runtime implementation (forbidden by hook policy)
- file-existence confirmation (`ls`, `find`) — state awareness before phase-artifact generation

### Step 4.5 — Aggregate the utilization status of `audit_detail.fix_hint`

Classify whether the blocks in `native_hook_events.jsonl` include `fix_hint.next_command` or were empty.
Focus on identifying cases where "the hint was provided but the agent ignored it and repeated the same operation".

```bash
python3 - <<'EOF'
import json
from collections import Counter, defaultdict

orch_id = "<orchestration_id>"
path = f"workspace/orchestrations/{orch_id}/hooks/native_hook_events.jsonl"
hint_present: Counter = Counter()   # policy → count of blocks WITH fix_hint
hint_absent: Counter = Counter()    # policy → count of blocks WITHOUT fix_hint
hint_ignored: defaultdict = defaultdict(list)  # policy → list of repeated commands

prev_commands: list[str] = []
with open(path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("action") != "block":
            continue
        policy = (obj.get("audit_detail") or {}).get("policy", "unknown")
        fix_hint = (obj.get("audit_detail") or {}).get("fix_hint")
        cmd = (obj.get("payload_summary") or {}).get("command", "") or obj.get("payload_summary", "")
        if fix_hint and fix_hint.get("next_command"):
            hint_present[policy] += 1
        else:
            hint_absent[policy] += 1
        if cmd and cmd in prev_commands:
            hint_ignored[policy].append(cmd[:200])
        prev_commands.append(cmd)

print("=== fix_hint present (structured recovery hint available) ===")
for p, c in hint_present.most_common():
    print(f"  {p}: {c}")
print("=== fix_hint absent (no structured hint — potential docs gap) ===")
for p, c in hint_absent.most_common():
    print(f"  {p}: {c}")
if hint_ignored:
    print("=== Hint possibly ignored (same command blocked multiple times) ===")
    for p, cmds in hint_ignored.items():
        print(f"  {p}: {len(cmds)} repeat(s)")
        for c in cmds[:3]:
            print(f"    {c}")
EOF
```

If there is a policy with many "hint_absent", add the corresponding row to `docs/RUNBOOK.md#hook-recovery` (the target of Stream B-3).

### Step 5 — Extract check failures and redos

#### 5-a. Multiple phase-launch attempts

Confirm the number of times the `pre_phase_launch` of the same `node_key + step` appears in `workflow_hooks.jsonl`.

**Note:** `pre_phase_launch` is written from both the `workflow-launch-check` command and the `record-launch` command.
When there are multiple substeps like a plan step, "1 workflow-launch-check + record-launch for the number of substeps" is the normal pattern, not a launch failure.
Compare with the number of agent_run_id present in the agents directory (`workspace/orchestrations/<orch_id>/agents/`), and judge it a retry only when the `pre_phase_launch` count exceeds "1 + the number of actually-launched agents".

```bash
python3 - <<'EOF'
import json, pathlib
from collections import Counter

orch_id = "<orchestration_id>"
path = f"workspace/orchestrations/{orch_id}/hooks/workflow_hooks.jsonl"
counter = Counter()
entries = []
with open(path) as f:
    for line in f:
        obj = json.loads(line.strip())
        entries.append(obj)

for e in entries:
    if e.get("hook") == "pre_phase_launch":
        key = f"{e.get('node_key')}::{e.get('step')}"
        counter[key] += 1

# the number of actually-launched agents (those whose record-launch succeeded and a capability exists)
caps = list(pathlib.Path(f"workspace/orchestrations/{orch_id}/capabilities").glob("*.json"))
launched_per_step: Counter = Counter()
for p in caps:
    obj = json.loads(p.read_text())
    key = f"{obj.get('node_key')}::{obj.get('step')}"
    launched_per_step[key] += 1

for key, cnt in counter.items():
    expected = 1 + launched_per_step.get(key, 0)  # 1 for workflow-launch-check
    if cnt > expected:
        print(f"RETRY x{cnt - expected} (pre_phase_launch={cnt}, expected={expected}): {key}")
    else:
        print(f"OK (pre_phase_launch={cnt}, expected={expected}): {key}")
EOF
```

#### 5-b. Gate failures and re-execution counts

When `hook=pre_command_execute` and the same `gate` appears multiple times in `workflow_hooks.jsonl`, a fix loop after a gate failure has occurred.

```bash
python3 - <<'EOF'
import json
from collections import Counter

orch_id = "<orchestration_id>"
path = f"workspace/orchestrations/{orch_id}/hooks/workflow_hooks.jsonl"
counter = Counter()
with open(path) as f:
    for line in f:
        obj = json.loads(line.strip())
        if obj.get("hook") == "pre_command_execute" and obj.get("gate"):
            key = f"{obj['gate']}::{obj.get('step')}"
            counter[key] += 1

for key, cnt in counter.items():
    if cnt > 1:
        print(f"GATE RETRY x{cnt}: {key}")
EOF
```

For the actual gate-failure content, read `gates/<agent_run_id>/<gate_name>.json` and confirm the `violations` field.

```bash
ls workspace/orchestrations/<orch_id>/gates/
# confirm all gate results per agent_run_id
python3 -c "
import json, pathlib, sys
orch_id = '<orchestration_id>'
for p in sorted(pathlib.Path(f'workspace/orchestrations/{orch_id}/gates').rglob('*.json')):
    obj = json.loads(p.read_text())
    if obj.get('status') != 'pass':
        print(p)
        print(json.dumps(obj, ensure_ascii=False, indent=2))
        print()
"
```

#### 5-c. Confirm sandbox violations

```bash
ls workspace/orchestrations/<orch_id>/violations/
python3 -c "
import json, pathlib
orch_id = '<orchestration_id>'
for p in sorted(pathlib.Path(f'workspace/orchestrations/{orch_id}/violations').glob('*.json')):
    print(p.name)
    print(json.dumps(json.loads(p.read_text()), ensure_ascii=False, indent=2))
    print()
"
```

#### 5-d. Confirm fail/fail_closed in phase_state_log

```bash
python3 -c "
import json
orch_id = '<orchestration_id>'
with open(f'workspace/orchestrations/{orch_id}/phase_state_log.jsonl') as f:
    for line in f:
        obj = json.loads(line.strip())
        if obj.get('event') in ('set_status',) and obj.get('to') in ('fail', 'fail_closed'):
            print(json.dumps(obj, ensure_ascii=False))
"
```

### Step 5.5 — Display the 5 hook events just before fail_closed chronologically

When `fail_closed` occurred, go back through the immediately preceding hook events to confirm what triggered it.

```bash
python3 - <<'EOF'
import json

orch_id = "<orchestration_id>"
hook_log = f"workspace/orchestrations/{orch_id}/hooks/native_hook_events.jsonl"
phase_log = f"workspace/orchestrations/{orch_id}/phase_state_log.jsonl"

# obtain the fail_closed timestamp from phase_state_log
fail_ts = None
with open(phase_log) as f:
    for line in f:
        obj = json.loads(line.strip())
        if obj.get("to") == "fail_closed" or obj.get("event") == "set_status" and obj.get("new_state") == "fail_closed":
            fail_ts = obj.get("ts") or obj.get("timestamp")

if fail_ts is None:
    print("No fail_closed event found in phase_state_log.")
else:
    print(f"fail_closed at: {fail_ts}")
    # Collect all hook events before fail_ts, take last 5
    events_before = []
    with open(hook_log) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            ts = obj.get("ts") or obj.get("timestamp", "")
            if ts <= fail_ts:
                events_before.append(obj)
    last_5 = events_before[-5:]
    print(f"\n=== Last {len(last_5)} hook events before fail_closed ===")
    for e in last_5:
        ts = e.get("ts") or e.get("timestamp", "?")
        action = e.get("action", "?")
        tool = e.get("tool_name") or (e.get("payload_summary") or {}).get("tool_name", "?")
        policy = (e.get("audit_detail") or {}).get("policy", "")
        summary = str(e.get("payload_summary", ""))[:120]
        print(f"  [{ts}] {action} | {tool} | {policy} | {summary}")
EOF
```

### Step 6 — Report the results

Report grouped into the 3 categories in the following format.

---

#### 1. Blocked by hooks

Enumerate each block in a table.

| time (UTC) | agent | tool | policy | operation target |
|---|---|---|---|---|
| … | … | … | … | … |

Group those with the same policy and explain the cause in one line.

#### 2. Performed information gathering

Enumerate `--help` references, `tools/` grep, and state-awareness `ls` / `find` respectively, and add one sentence on **what was unclear**.

#### 3. Redos due to check failures

Enumerate multiple phase-launch attempts, gate-failure loops, sandbox violations, and status-setting mistakes chronologically, and note the **cause** and **final result** of each redo.

#### 4. Summary of repair hints (new)

From Step 3.5 / 4.5 / 5.5, summarize the **legitimate action the agent should take next** per policy.

| policy | block count | fix_hint present/absent | recommended action |
|---|---|---|---|
| read_manifest_read_guard | … | … | obtain via `guarded-apply-patch` / `run-gate` |
| output_manifest_write_guard | … | … | directly specify the literal path of `allowed_tmp_root` (`workspace/tmp/<agent_run_id>/...`). Bootstrap Bash such as `export TMPDIR=...` / `jq -er ...` is forbidden (the workflow stops on a Claude Code session sandbox approval) |
| forbid_python_inline_write | … | … | use `guarded-apply-patch` or the Edit/Write tool |
| forbid_tools_direct_read | … | … | reference only `docs/` / `spec/` |
| enforce_guarded_apply_patch | … | … | use `guarded-apply-patch` and add to `allowed_file_tool_paths` |

Highlight a repeated error pattern (5 or more) in bold, and add the corresponding line number of `docs/RUNBOOK.md#hook-recovery`.

---

## Notes

- The implementation under `tools/` is forbidden to read directly by hook policy. Derive rules by referencing only `docs/` and `spec/`.
- The session `.jsonl` can be tens of thousands of lines. Do not read all lines from the top; extract only the necessary fields with Python.
- When the orchestration agent and a child agent are mixed under the same session_id (on the Claude backend they are recorded in the same session), determine the agent_role not by `payload_summary.session_id` but by `capabilities/<agent_run_id>.json` corresponding to the `agent_run_id` of `native_hook_events.jsonl`.
