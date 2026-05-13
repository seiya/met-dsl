# CLAUDE.md

このファイルは Claude Code 向けのプロジェクト固有規約を定義する。文体ルール、用語規則、ドキュメント参照ルール、MCP 実行ルールなどの一般規約は [AGENTS.md](AGENTS.md) を canonical source とする。

## workflow 実行
- core workflow は `Spec → Compile → Generate → Build → Validate` の 5 phase 構成。仕様への入口は [docs/WORKFLOW.md](docs/WORKFLOW.md) とする。
- `orchestration agent` と `step agent` / `substep agent` の階層実行契約は [docs/ORCHESTRATION.md](docs/ORCHESTRATION.md) を canonical source とする。
- `orchestration agent` の起動手順は [skills/workflow-orchestration/SKILL.md](skills/workflow-orchestration/SKILL.md) を参照する。
- 起動前の最小確認手順は [skills/workflow-orchestration/references/startup_contract.md](skills/workflow-orchestration/references/startup_contract.md) を参照する。
- workflow 起動は `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]` を canonical entrypoint とする。`<until_phase>` は `compile` / `generate` / `build` / `validate` のいずれかを指定する。
- workflow 実行時の `METDSL_WORKFLOW_MODE=1` と `METDSL_ORCHESTRATION_ID=<orchestration_id>` は `tools/run_workflow.py` が設定する値を canonical source とする。
- 任意フロー `Tune`（実装裁量 variant 探索）と `Promote`（正式版昇格）は core workflow と分離した entrypoint で起動する（詳細は別 plan で定義）。

## execution platform 別の子 `agent` 起動ツール

| execution platform | `preflight` の `--backend` 引数 | 子 `agent` 起動ツール | `agent_session_id` の取得方法 |
|---|---|---|---|
| Codex | `codex` | `spawn_agent` | `spawn_agent` 実応答から取得 |
| Cursor | `cursor` | `spawn_agent` | `spawn_agent` 実応答から取得 |
| Claude Code | `claude` | `Agent` tool | 起動前に発行した `agent_run_id` を `agent_session_id` として代用する |

## hook 実装方針
- backend 非依存の検証は `tools/hooks/common.py` を canonical source とする。
- backend 固有の呼び出し仕様は `tools/hooks/adapters/` 配下の adapter で吸収する。
- `Codex` の hook 呼び出し定義は `.codex/hooks.json` を canonical source とする。
- `codex` backend の `preflight` は `feature_states.codex_hooks=true` を必須とする。`codex_hooks` が未有効な環境は `status=fail` として停止する。
- `Claude Code` の hook 呼び出し定義は `.claude/settings.json` の `hooks` セクションを canonical source とする。`PreToolUse` / `PostToolUse` / `UserPromptSubmit` / `Stop` の 4 イベントを配線する。
- `Claude Code` backend は feature flag probe が不要であり、`codex_hooks` 必須チェックは Codex backend 限定。共通ポリシーは `tools/hooks/common.py` の `evaluate_common_policy()` に従う。
- `.claude/settings.json` の `matcher` は **完全一致文字列**（正規表現ではない）。`.codex/hooks.json` の `^Bash$` とは異なり、`"Bash"` と記述する。

## Claude Code 固有の実行規約

### preflight
- `preflight` 実行時は `--backend claude` を指定する。
- コマンド例: `python3 tools/run_workflow.py <spec_ref> <until_phase> --llm claude`
- Claude backend の `preflight` は `~/.claude.json` の `projects.<abs_repo>.{mcpServers,enabledMcpjsonServers} − disabledMcpjsonServers` ∪ top-level `mcpServers` に `build-runtime` が含まれることを必須とする (`run_linter` / `compile_project` / `run_program` / `run_quality_checks` / `detect_build_system` を提供)。これは Claude Code session が `mcp__build-runtime__*` tool を Agent へ露出する canonical 条件と一致する。`claude mcp list` は workspace trust dialog を skip して stdio server を spawn するため `✓ Connected` でも session が tool を露出する保証にならず (false-positive 源)、preflight gate には使わず advisory 表示のみに留める。未 enable 時は `preflight.json#checks` の `claude_mcp_build_runtime_registered` が `pass=false` となり `status=fail` で停止する。remediation はリポジトリ同梱の `.mcp.json` を `claude` workspace trust dialog で有効化するか、`claude mcp add-json --scope project build-runtime ...` を `mcp_servers/mcp_servers.example.json` の snippet で実行する。

### 子 `agent` 起動
- Claude Code では `spawn_agent` の代わりに `Agent` tool を使用して子 `agent` を起動する。
- `Agent` tool の `prompt` 引数には [skills/workflow-orchestration/references/launch_prompts.md](skills/workflow-orchestration/references/launch_prompts.md) の対応テンプレートを適用する。
- `Agent` tool の `subagent_type` は `general-purpose` を既定とし、起動する phase に応じて適切な値を選択する。
- `context_isolated=true` は Claude Code の `Agent` tool が独立コンテキストで実行されることを指し、常に `true` として記録する。

### Claude Code における `record-launch` の実行順序

Claude Code では `record-launch` を **Agent tool より前** に呼び出す。Codex の `spawn_agent` と異なり、同期的に `Agent` tool を呼び出すためには子 agent が実行中に参照する `capability_token` と `output_manifest` を事前に生成しておく必要がある。

```
手順:
1. agent_run_id（UUID）を発行する
   - canonical 経路: `python3 tools/new_agent_run_id.py` を bare 実行し、Bash の標準出力に印字された UUID を後続コマンドへ literal 文字列として埋め込む。
   - `CHILD_ARID=$(python3 tools/new_agent_run_id.py)` の **2-step shell var 割り当て形式は使わない** — 先頭 `CHILD_ARID=` が `Bash(python3 tools/new_agent_run_id.py)` allowlist 一致を壊し、session sandbox の approval を都度要求する原因になる。
   - `cat /proc/sys/kernel/random/uuid` / `uuidgen` は session sandbox の approval 要求で都度停止するため使用しない。`python3 -c 'import uuid; …'` は `forbid_python_inline_write` でブロックされる。
2. reserve-phase-root で ir_id / pipeline_id を予約する（未予約なら）
3. record-launch を実行する（Agent tool 起動 前）
   → capability_token / sandbox_profile / output manifest / read manifest が生成される
   → launches/<agent_run_id>.reply.txt には暫定内容が書き込まれる
4. Agent tool を起動する（子 agent は capabilities/<agent_run_id>.json から
   capability_token を読み取って guarded-apply-patch 等を実行する）
5. Agent tool の戻り値（最終応答テキスト）を受け取る
6. record-child-return を実行して Agent tool 戻り観測の証跡 (child_returns/<agent_run_id>.txt) を残す
   → 必須引数: `--return-token "$(cat workspace/orchestrations/<orchestration_id>/launches/<agent_run_id>.parent_return_token)"` (Adv-30: 任意 caller による forge 防止の parent-bound token; record-launch が自動生成)
   → ack 不在 or token 不一致だと手順 7 の deactivate-child が ValueError で拒否される（Adv-20/Adv-30 ガード）
7. deactivate-child を実行して active context を orchestration agent へ切り戻す
8. record-reply で launches/<agent_run_id>.reply.txt に応答テキストを上書き保存する
9. record-agent-run を実行して agent_runs.jsonl へ追記する
```

**手順 4–9 連続実行の必須性 (active_child window):** 手順 4 で `Agent` tool を起動すると active context が child へ切り替わり、手順 7 の `deactivate-child` まで child のままとなる。**この間（手順 5 の Agent tool 戻りから手順 7 完了まで）に親 orchestration agent が `Write` / `Edit` / `Bash` による file write を発行してはならない**。発行すると親自身の `workspace/tmp/<self_arid>/` への書き込みであっても child の `output_manifest` で評価され、`output_manifest_write_guard` (`agent_run_id=<child>`) で reject される（同 hook ブロックは過去 audit で実観測）。手順 4 から 9 までは中断なく連続実行し、親自身の tmp script や任意 path 書き込みは手順 9 完了後または手順 3 (`record-launch`) 直前にまとめること。

**手順 6 における `parent_return_token` の参照規約:** `--return-token` には `"$(cat workspace/orchestrations/<orchestration_id>/launches/<agent_run_id>.parent_return_token)"` の **Bash command substitution 形式** を literal 引数として渡す（`<orchestration_id>` / `<agent_run_id>` は literal 置換）。**`Read` tool で当該 file を先に読んではならない**。active_child window 中の Read は child arid の `read_manifest` で評価され、parent 自身の broader manifest にあっても `read_manifest_read_guard` で block される（過去 audit で `launches/<arid>.parent_return_token` への Read tool ブロックを観測）。

### CLI 仕様の確認規約 (場所別 total cost 最適化)

CLI 引数情報の取得経路は、対象 subcommand の頻度・payload schema 複雑度・doc 同期コストで使い分ける。canonical source の選択は次表に従う。

| 対象 | canonical source | 理由 |
|---|---|---|
| `tools/orchestration_runtime.py` の頻出 subcommand (`record-launch` / `record-agent-run` / `record-child-return` / `deactivate-child` / `record-reply` / `set-status` / `write-step-result` / `workflow-launch-check` / `reserve-phase-root` / `mark-dependency-readiness` / `guarded-apply-patch` / `run-gate`) | [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) (Tier-A) + `references/startup_contract.md` のテンプレート | payload schema 複雑、phase 別必須切替あり、`--help` 単独では不足 |
| `tools/orchestration_runtime.py` の稀少 subcommand (`init` / `preflight` / `preflight-status` / `record-timeout` / `read-checkpoint` / `verify-checkpoint-integrity` / `check-step-completed` / `orchestration-read`) | `python3 tools/orchestration_runtime.py <sub> --help`。overview は [docs/CLI_REFERENCE_RARE.md](docs/CLI_REFERENCE_RARE.md) | doc maintenance cost が使用頻度に見合わない |
| `tools/run_workflow.py` / `tools/validate_pipeline_semantics.py` / `tools/audit_orchestration.py` | `<tool> --help` | 専用 doc を作らない |
| `tools/new_agent_run_id.py` | literal (`python3 tools/new_agent_run_id.py`) | 引数なし |
| step / substep agent から `guarded-apply-patch` / `run-gate` 等の呼び出し | parent の launch prompt に埋め込まれた literal (`references/launch_prompts.md`) | child context 節約、parent が template で pin |

`tools/` 配下の実装直読 (`Read` tool / `grep` / `sed` / `cat` 等で `.py` 実装を読む経路) は `forbid_tools_direct_read` および `read_manifest_read_guard` の対象として引き続き禁止する。`<tool> --help` は argparse 出力に限定された情報取得経路であり、`forbid_tools_direct_read` の対象外として許容する (hook は audit-log のみ記録)。

### `record-launch` の `response.json`
- `Agent` tool の起動応答には Codex の `spawn_agent` のような構造化 JSON が存在しない。
- `record-launch --response-json` には以下の最小構成 JSON を渡す。`sandbox_runtime`・`sandbox_enforced`・`sandbox_profile_ref` は `record-launch` が自動付与する。

```json
{
  "agent_run_id": "<agent_run_id>",
  "agent_session_id": "<agent_run_id>",
  "started_at": "<ISO8601>",
  "backend": "claude"
}
```

- `agent_session_id` は発行した `agent_run_id` と同一値を使用する（Claude Code には Codex のような固有 session ID が存在しないため）。
- `launches/<agent_run_id>.response.json` と `agents/<agent_run_id>/dialogs/child.response.json` には上記と `record-launch` が付与したフィールドを含む内容が保存される。
- `launches/<agent_run_id>.reply.txt` は手順 3 で record-launch が暫定書き込みし、手順 8 の record-reply で Agent tool の実際の応答テキストに上書きする。
