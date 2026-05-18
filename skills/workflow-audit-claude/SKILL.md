---
name: workflow-audit
description: 実行済み workflow の orchestration ログを調査し、hook ブロック・情報収集行動・チェック失敗によるやり直しを洗い出して報告するときに使用する。対象 orchestration_id と session_id は自動検出する。Claude Code 専用
---

# Workflow Audit

## 目的
完了または中断した workflow 実行のログを横断的に調査し、以下の 3 カテゴリで問題点を列挙する。

1. **hook ブロック** — hook が `action=block` を返した操作
2. **情報収集行動** — CLI 仕様不明・状態把握不足により `--help` 参照やファイル探索を行った箇所
3. **チェック失敗によるやり直し** — gate / validator の失敗、phase launch の複数回試行、status 設定ミス

## ログ収集元

| ログ | 収集先 |
|---|---|
| hook ブロック | `workspace/orchestrations/<orch_id>/hooks/native_hook_events.jsonl` |
| workflow フック経緯 | `workspace/orchestrations/<orch_id>/hooks/workflow_hooks.jsonl` |
| agent 実行結果 | `workspace/orchestrations/<orch_id>/agent_runs.jsonl` |
| phase 状態遷移 | `workspace/orchestrations/<orch_id>/phase_state_log.jsonl` |
| gate 結果 | `workspace/orchestrations/<orch_id>/gates/<agent_run_id>/*.json` |
| sandbox 違反 | `workspace/orchestrations/<orch_id>/violations/*.json` |
| アクセスログ | `workspace/orchestrations/<orch_id>/access_logs/<agent_run_id>.jsonl` |
| 失敗分析 | `workspace/orchestrations/<orch_id>/failure_analysis.json` |
| session 会話ログ | `~/.claude/projects/<cwd-slug>/<session_id>.jsonl` (`<cwd-slug>` は repo の絶対パスの `/` を `-` に置換した文字列) |

## 調査手順

### Step 1 — orchestration_id を特定する

```bash
ls workspace/orchestrations/
```

対象が複数ある場合は最新の `orch_YYYYMMDDTHHMMSSZ_*` ディレクトリを選ぶ。
特定の orchestration を調査する場合は指示された `orchestration_id` を使う。

### Step 2 — session_id を自動検出する

`native_hook_events.jsonl` に記録された `payload_summary.session_id` を読み取り、
`~/.claude/projects/<cwd-slug>/` 配下の対応する `.jsonl` ファイルを特定する。
`<cwd-slug>` は repo の絶対パスの `/` を `-` に置換した文字列とする（例: `/home/alice/work/met-dsl` → `-home-alice-work-met-dsl`）。

```bash
python3 - <<'EOF'
import json, pathlib

orch_id = "<orchestration_id>"   # Step 1 で確定した値を代入
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

検出した session_id ごとに対応する `.jsonl` のパスを確定する。

### Step 3 — hook ブロックを抽出する

`native_hook_events.jsonl` から `action=block` のレコードをすべて抽出する。

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

各ブロックについて以下を記録する。

- `ts` — 発生時刻
- `tool_name` — ブロックされたツール（`Read` / `Bash` / `Write` 等）
- `reason` — ブロック理由
- `audit_detail.policy` — 適用されたポリシー名
- `payload_summary` — 操作対象パスまたはコマンド（先頭 200 文字）

### Step 3.5 — policy 別ブロック数を集計する

Step 3 で取得した `blocks` をポリシー別にカウントし、5 件以上を **繰り返しエラーパターン** として強調する。

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

繰り返しエラーパターン（5 件以上）が検出された場合は、修復チートシート（`docs/RUNBOOK.md#hook-recovery`）の対応行を参照して根本原因を特定する。

### Step 4 — 情報収集行動を抽出する

session の `.jsonl` から `--help` 呼び出し、ファイル探索コマンド、ランタイム実装の grep/sed を抽出する。

```bash
python3 - <<'EOF'
import json

SESSION = "<session_jsonl_path>"   # Step 2 で確定したパス

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

抽出結果を以下の観点で分類する。

- `--help` 参照 — CLI 仕様が不明だった箇所（引数フォーマット、サブコマンド名等）
- `tools/` 直接 grep/sed/read — ランタイム実装からルールを導こうとした箇所（hook ポリシー上禁止）
- ファイル存在確認 (`ls`, `find`) — phase artifact 生成前の状態把握

### Step 4.5 — `audit_detail.fix_hint` の活用状況を集計する

`native_hook_events.jsonl` 内のブロックに `fix_hint.next_command` が含まれているか、あるいは空だったかを分類する。
「ヒントが提供されていたのに agent が無視して同じ操作を繰り返した」ケースを重点的に特定する。

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

「hint_absent」が多いポリシーがあれば `docs/RUNBOOK.md#hook-recovery` の対応行を追記する（Stream B-3 の対象）。

### Step 5 — チェック失敗とやり直しを抽出する

#### 5-a. phase launch の複数回試行

`workflow_hooks.jsonl` で同じ `node_key + step` の `pre_phase_launch` が現れる回数を確認する。

**注意:** `pre_phase_launch` は `workflow-launch-check` コマンドと `record-launch` コマンドの両方から書き込まれる。
plan step のように substep が複数ある場合は「1 回の workflow-launch-check + substep 数分の record-launch」が正常パターンであり、起動失敗ではない。
agents ディレクトリ (`workspace/orchestrations/<orch_id>/agents/`) に存在する agent_run_id 数と比較して、`pre_phase_launch` 回数が「1 + 実起動 agent 数」を超える場合のみ retry と判定する。

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

# 実起動 agent 数 (record-launch が成功して capabilities が存在するもの)
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

#### 5-b. gate 失敗と再実行回数

`workflow_hooks.jsonl` で `hook=pre_command_execute` かつ同じ `gate` が複数回現れる場合、gate 失敗後の修正ループが発生している。

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

実際の gate 失敗内容は `gates/<agent_run_id>/<gate_name>.json` を読んで `violations` フィールドを確認する。

```bash
ls workspace/orchestrations/<orch_id>/gates/
# agent_run_id ごとに全 gate 結果を確認
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

#### 5-c. sandbox 違反の確認

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

#### 5-d. phase_state_log で fail/fail_closed を確認

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

### Step 5.5 — fail_closed 直前の hook イベント 5 件を時系列で表示する

`fail_closed` が発生した場合、直前の hook イベントを遡って何がトリガになったかを確認する。

```bash
python3 - <<'EOF'
import json

orch_id = "<orchestration_id>"
hook_log = f"workspace/orchestrations/{orch_id}/hooks/native_hook_events.jsonl"
phase_log = f"workspace/orchestrations/{orch_id}/phase_state_log.jsonl"

# fail_closed timestamp を phase_state_log から取得
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

### Step 6 — 結果を報告する

以下の形式で 3 カテゴリにまとめて報告する。

---

#### 1. hook でブロックされたもの

各ブロックを表形式で列挙する。

| 時刻 (UTC) | agent | ツール | ポリシー | 操作対象 |
|---|---|---|---|---|
| … | … | … | … | … |

ポリシーが同種のものはグループ化して原因を一行で説明する。

#### 2. 情報収集を行ったもの

`--help` 参照・`tools/` grep・状態把握 `ls` / `find` をそれぞれ列挙し、**何が分からなかったか**を一文で添える。

#### 3. チェック失敗によるやり直し

phase launch 複数回試行・gate 失敗ループ・sandbox 違反・status 設定ミスを時系列で列挙し、各やり直しの **原因** と **最終結果** を記す。

#### 4. 修復ヒントの要約（新設）

Step 3.5 / 4.5 / 5.5 から、**agent が次に取るべき正規アクション**をポリシー別にまとめる。

| ポリシー | ブロック数 | fix_hint あり/なし | 推奨アクション |
|---|---|---|---|
| read_manifest_read_guard | … | … | `guarded-apply-patch` / `run-gate` 経由で取得 |
| output_manifest_write_guard | … | … | `allowed_tmp_root` の literal path (`workspace/tmp/<agent_run_id>/...`) を直接指定する。`export TMPDIR=...` / `jq -er ...` の bootstrap Bash は禁止 (Claude Code session sandbox approval で workflow が停止する) |
| forbid_python_inline_write | … | … | `guarded-apply-patch` または Edit/Write tool を使う |
| forbid_tools_direct_read | … | … | `docs/` / `spec/` のみを参照する |
| enforce_guarded_apply_patch | … | … | `guarded-apply-patch` を使い `allowed_file_tool_paths` に追加する |

繰り返しエラーパターン（5 件以上）は太字で強調し、対応する `docs/RUNBOOK.md#hook-recovery` の行番号を添える。

---

## 注意事項

- `tools/` 配下の実装は hook ポリシーで直接読み込みが禁止されている。ルールの導出は `docs/` と `spec/` のみを参照すること。
- session `.jsonl` は数万行になる場合がある。先頭から全行読まず、Python で必要フィールドのみを抽出すること。
- 同一 session_id で orchestration agent と子 agent が混在する場合（Claude backend では同一 session に記録される）、`payload_summary.session_id` ではなく `native_hook_events.jsonl` の `agent_run_id` に対応する `capabilities/<agent_run_id>.json` で agent_role を判別すること。
