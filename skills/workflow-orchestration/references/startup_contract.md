# Workflow Orchestration Startup Contract

## 目的
- `workflow orchestration` 起動前の必須判定を最小トークンで確定する。

## 適用範囲
- `orchestration agent` 起動直後
- 子 `agent` の初回起動前

## 要件
- workflow 起動は `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]` を canonical entrypoint としなければならない。
- workflow mode は `python3 tools/run_workflow.py ... --mode <dev|prod>` で指定し、未指定時は `dev` を適用しなければならない。
- `dev` mode では verify 判定の緩和を禁止し、`issue_severity=major|critical` 検出時は fail 停止を必須とする。
- `dev` mode で fail した場合は `workspace/orchestrations/<orchestration_id>/failure_analysis.json` を保存し、失敗要因と根拠参照を必須記録とする。
- 起動前確認の canonical implementation は `tools/run_workflow.py` と `tools/codex_orchestration_runtime.py` の組み合わせとし、`preflight` の backend 指定は `tools/run_workflow.py --llm` を通じて行わなければならない。
- 子 `agent` へ渡す要求定義と判定規則の canonical source は `docs/` と `spec/` と当該試行 artifact に限定し、`tools/` 配下の実装、検証 `script`、test code、validator code を rule source として参照してはならない。
- `init` と `preflight` は各 1 回以上実行しなければならない。
- `preflight.json` が `status=pass` かつ `can_launch_step_agents=true` かつ `can_launch_substep_agents=true` を満たさない場合、子 `agent` を起動してはならない。
- phase 着手前に、対象 phase が `substep agent` 必須か `step agent` 必須かを固定表で確認しなければならない。`Plan` / `Generate` / `Tune` は `substep agent`、`Build` / `Execute` / `Judge` / `Promote` は `step agent` とする。
- 最初の `commentary` で、対象 phase、使用する `SKILL`、起動する `agent` 種別、`MCP` 使用箇所を実行宣言しなければならない。
- `Plan` の子 `agent` を起動する前に、対象 `node` の直下依存 `node` が `direct dependency plan readiness` を満たすことを確認しなければならない。
- `Generate` 以降の子 `agent` を起動する前に、対象 `node` の直下依存 `node` が `direct dependency execution readiness` を満たすことを確認しなければならない。
- 子 `agent` 起動直前の live 検査は `record-launch` 実行時にのみ必須とする。
- `record-agent-run` と `write-step-result` は、`preflight.json` の整合確認を満たす場合に実行してよい。
- 起動要求本文は `launches/<agent_run_id>.prompt.txt`、起動返答本文は `launches/<agent_run_id>.reply.txt` に保存しなければならない。
- phase artifact を直接編集または `MCP` 実行する前に、`preflight` 済み、launch prompt 準備済み、child `agent` 起動済みを満たさなければならない。
- workflow の正当性確認、検証、疎通確認を目的とした仮実装であっても、親 `agent` が子 `agent` 必須 phase の本体処理を代行してはならない。

## 運用ルール
1. `tools/run_workflow.py` を実行して `workspace/orchestrations/<orchestration_id>/` の初期化と `preflight.json` 生成を行う。
2. `tools/run_workflow.py` 以外の経路で workflow を開始してはならない。
3. `preflight` 判定が `pass` でない場合は `set-status --status fail` を実行して停止する。
4. 最初の `commentary` で、対象 phase、使用する `SKILL`、起動する `agent` 種別、`MCP` 使用箇所を宣言する。
5. 固定表で phase 種別を確認し、`Plan` / `Generate` / `Tune` では `substep agent`、`Build` / `Execute` / `Judge` / `Promote` では `step agent` を起動対象として確定する。
6. `Plan` の子 `agent` 起動前に、直下依存 `node` の `plan_ref` と `plan_meta.json.verification_status` を確認する。
7. `Generate` 以降の子 `agent` 起動前に、直下依存 `node` の `plan_ref` と `pipeline_ref` と `aggregate_verdict` を確認する。
8. `preflight` 済み、launch prompt 準備済み、child `agent` 起動済みを満たすまで phase artifact 編集と `MCP` 実行を開始しない。
9. 子 `agent` 起動時は `record-launch` を実行する。
10. 子 `agent` 完了後は `record-agent-run` を追記する。
11. phase 完了後は `write-step-result` を記録する。
12. 契約に反する近道へ逸脱しそうな場合は、子 `agent` 起動必須であることを明示して launch 手順へ戻る。

## 判定基準
- `preflight.json` が存在し、`pass` 条件を満たしている。
- 最初の `commentary` に実行宣言が存在する。
- phase 種別と起動した `agent` 種別が固定表と一致している。
- `launches/` と `agent_runs.jsonl` と `step_result.json` の参照整合が取れている。
- 子 `agent` の起動失敗時に `set-status --status fail` が記録されている。

## execution platform 別の補足

execution platform ごとの子 `agent` 起動ツールと `preflight` 引数の対応は `CLAUDE.md` の「execution platform 別の子 `agent` 起動ツール」を参照する。
