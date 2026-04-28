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
| session 会話ログ | `~/.claude/projects/-home-seiya-work-met-dsl/<session_id>.jsonl` |

## 調査手順

### Step 1 — orchestration_id を特定する

```bash
ls workspace/orchestrations/
```

対象が複数ある場合は最新の `orch_YYYYMMDDTHHMMSSZ_*` ディレクトリを選ぶ。
特定の orchestration を調査する場合は指示された `orchestration_id` を使う。

### Step 2 — session_id を自動検出する

`native_hook_events.jsonl` に記録された `payload_summary.session_id` を読み取り、
`~/.claude/projects/-home-seiya-work-met-dsl/` 配下の対応する `.jsonl` ファイルを特定する。

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

projects_dir = pathlib.Path.home() / ".claude/projects/-home-seiya-work-met-dsl"
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

### Step 5 — チェック失敗とやり直しを抽出する

#### 5-a. phase launch の複数回試行

`workflow_hooks.jsonl` で同じ `node_key + step` の `pre_phase_launch` が 2 回以上現れる場合、起動失敗によるやり直しが発生している。

```bash
python3 - <<'EOF'
import json
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

for key, cnt in counter.items():
    if cnt > 1:
        print(f"RETRY x{cnt}: {key}")
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

---

## 注意事項

- `tools/` 配下の実装は hook ポリシーで直接読み込みが禁止されている。ルールの導出は `docs/` と `spec/` のみを参照すること。
- session `.jsonl` は数万行になる場合がある。先頭から全行読まず、Python で必要フィールドのみを抽出すること。
- 同一 session_id で orchestration agent と子 agent が混在する場合（Claude backend では同一 session に記録される）、`payload_summary.session_id` ではなく `native_hook_events.jsonl` の `agent_run_id` に対応する `capabilities/<agent_run_id>.json` で agent_role を判別すること。
