# Workflow Orchestration Startup Contract

## 目的
- `workflow orchestration` 起動前の必須判定を最小トークンで確定する。

## 適用範囲
- `orchestration agent` 起動直後
- 子 `agent` の初回起動前

## 要件
- 起動前確認は `tools/codex_orchestration_runtime.py` を canonical source 実装として実施しなければならない。
- `init` と `preflight` は各 1 回以上実行しなければならない。
- `preflight.json` が `status=pass` かつ `can_launch_step_agents=true` かつ `can_launch_substep_agents=true` を満たさない場合、子 `agent` を起動してはならない。
- `Plan` の子 `agent` を起動する前に、対象 `node` の直下依存 `node` が `direct dependency plan readiness` を満たすことを確認しなければならない。
- `Generate` 以降の子 `agent` を起動する前に、対象 `node` の直下依存 `node` が `direct dependency execution readiness` を満たすことを確認しなければならない。
- 子 `agent` 起動直前の live 検査は `record-launch` 実行時にのみ必須とする。
- `record-agent-run` と `write-step-result` は、`preflight.json` の整合確認を満たす場合に実行してよい。
- 起動要求本文は `launches/<agent_run_id>.prompt.txt`、起動返答本文は `launches/<agent_run_id>.reply.txt` に保存しなければならない。

## 運用ルール
1. `init` を実行して `workspace/orchestrations/<orchestration_id>/` を初期化する。
2. `preflight` を実行して `preflight.json` を生成する。
3. `preflight` 判定が `pass` でない場合は `set-status --status fail` を実行して停止する。
4. `Plan` の子 `agent` 起動前に、直下依存 `node` の `plan_ref` と `plan_meta.json.verification_status` を確認する。
5. `Generate` 以降の子 `agent` 起動前に、直下依存 `node` の `plan_ref` と `pipeline_ref` と `aggregate_verdict` を確認する。
6. 子 `agent` 起動時は `record-launch` を実行する。
7. 子 `agent` 完了後は `record-agent-run` を追記する。
8. phase 完了後は `write-step-result` を記録する。

## 判定基準
- `preflight.json` が存在し、`pass` 条件を満たしている。
- `launches/` と `agent_runs.jsonl` と `step_result.json` の参照整合が取れている。
- 子 `agent` の起動失敗時に `set-status --status fail` が記録されている。
