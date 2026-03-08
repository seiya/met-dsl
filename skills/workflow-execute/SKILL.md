---
name: workflow-execute
description: Execute ステージを実行し、`build` 成果物を `MCP` サーバー経由の `run_program` で実行して `diagnostics.json` と `perf.json` と実行ログを生成するときに使用する。`quality check` を `run_quality_checks` で実行する作業に適用する。
---

# Workflow Execute

## 目的
Execute ステージの実行責務を固定し、判定可能なランタイム成果物を生成する。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/execute/<execution_id>/<node_key>/` の成果物生成
- `runner` 実行と `quality check` 実行

## 要件
- `run` は `MCP` サーバーの `run_program` を使用する。
- `run_program` の実行コマンドは `case.resolved.yaml` を入力引数として必ず含まなければならない。
- `quality check` は `MCP` サーバーの `run_quality_checks` を使用する。
- `runner` が `model` を呼び出し、`diagnostics.json` と `perf.json` を出力する。
- `runner` が `verdict.json` と `aggregate_verdict.json` と `summary.json` と `trial_meta.json` を直接出力してはならない。
- `execution_id/<node_key>/raw/` に `Judge` 再計算用の実行証跡を必須保存する。必須構成は `derived_contract.json` の `raw_requirements.required_evidence` を正本とする。
- `raw_requirements.required_evidence` が `artifact=state_snapshots` を必須宣言する場合のみ、状態スナップショットを必須保存する。
- `raw` は一次証跡のみを保存し、`diagnostics.json` の複写を `metrics_basis` として保存してはならない。
- `stdout.log` と `stderr.log` と `trial_meta.json` を必須保存する。
- `trial_meta.json` に `runner_command` と `process_trace_ref` と `raw_artifact_refs` を必須記録する。
- `target.class=cpu` の品質比較は `threads_per_rank=1` と `threads_per_rank>1` の実行結果を比較対象として保存する。
- `quality check` の比較正本は `diagnostics.json` と `verdict.json` とし、`stdout` 差分のみで合否を確定してはならない。
- workflow 成果物の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。
- `Execute` 完了前に `python3 tools/validate_pipeline_semantics.py` を `--allow-missing-orchestration` と `--allow-missing-llm-review` を指定せずに実行し、`fail` 時は `Execute fail` とする。

## 運用ルール
1. `execution_id` を発行し、成果物を `execution_id` 単位で分離保存する。
2. 判定入力の混在を避けるため、`execution_id` を跨いだ成果物参照を禁止する。
3. `node_key` ごとに成果物ディレクトリを分離する。
4. 実行失敗時は `trial_meta.json` に環境情報と失敗原因を記録する。
5. `raw` 実行証跡が欠落する場合は `Execute fail` とし、`Judge` を開始してはならない。
6. 出力先が `workspace/` でない場合は `Execute fail` とし、当該 `execution` を無効化する。
7. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
8. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Execute fail` とする。
9. `python3 tools/validate_pipeline_semantics.py` を `--allow-missing-orchestration` と `--allow-missing-llm-review` を指定せずに実行し、`fail` 時は `Judge` を開始してはならない。

## 判定基準
- `diagnostics.json` と `perf.json` とログ群が揃っている。
- `raw` 実行証跡と `trial_meta.json` の参照情報が整合している。
- `node_key` 単位で成果物が分離されている。
- 実行方式が `MCP run_program` と `MCP run_quality_checks` に限定される。
- `python3 tools/validate_pipeline_semantics.py` が `PASS` を返す。
