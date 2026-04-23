# CLAUDE.md

このファイルは Claude Code 向けのプロジェクト固有規約を定義する。文体ルール、用語規則、ドキュメント参照ルール、MCP 実行ルールなどの一般規約は [AGENTS.md](AGENTS.md) を canonical source とする。

## workflow 実行
- workflow 仕様への入口は [docs/WORKFLOW.md](docs/WORKFLOW.md) とする。
- `orchestration agent` と `step agent` / `substep agent` の階層実行契約は [docs/ORCHESTRATION.md](docs/ORCHESTRATION.md) を canonical source とする。
- `orchestration agent` の起動手順は [skills/workflow-orchestration/SKILL.md](skills/workflow-orchestration/SKILL.md) を参照する。
- 起動前の最小確認手順は [skills/workflow-orchestration/references/startup_contract.md](skills/workflow-orchestration/references/startup_contract.md) を参照する。
- workflow 起動は `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]` を canonical entrypoint とする。
- workflow 実行時の `METDSL_WORKFLOW_MODE=1` と `METDSL_ORCHESTRATION_ID=<orchestration_id>` は `tools/run_workflow.py` が設定する値を canonical source とする。

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

### 子 `agent` 起動
- Claude Code では `spawn_agent` の代わりに `Agent` tool を使用して子 `agent` を起動する。
- `Agent` tool の `prompt` 引数には [skills/workflow-orchestration/references/launch_prompts.md](skills/workflow-orchestration/references/launch_prompts.md) の対応テンプレートを適用する。
- `Agent` tool の `subagent_type` は `general-purpose` を既定とし、起動する phase に応じて適切な値を選択する。
- `Agent` tool を呼び出す前に `agent_run_id`（UUID 形式）を発行し、その値を `agent_session_id` として `record-launch` に記録する。
- `Agent` tool の戻り値（agent の最終応答テキスト）を `launch_reply_ref` に保存する `launches/<agent_run_id>.reply.txt` の内容とする。
- `context_isolated=true` は Claude Code の `Agent` tool が独立コンテキストで実行されることを指し、常に `true` として記録する。

### `record-launch` の `response.json`
- `Agent` tool の起動応答には Codex の `spawn_agent` のような構造化 JSON が存在しない。
- `launches/<agent_run_id>.response.json` には、`agent_run_id`、`agent_session_id`（= 発行した `agent_run_id`）、`started_at`、`backend: "claude"` を最低限含む JSON を記録する。
- `child.response.json` は上記と同一内容を保存する。
