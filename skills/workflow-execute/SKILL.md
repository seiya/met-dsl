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
- `quality check` は `MCP` サーバーの `run_quality_checks` を使用する。
- `runner` が `model` を呼び出し、`diagnostics.json` と `perf.json` を出力する。
- `stdout.log` と `stderr.log` と `trial_meta.json` を必須保存する。
- `target.class=cpu` の品質比較は `threads_per_rank=1` と `threads_per_rank>1` の実行結果を比較対象として保存する。

## 運用ルール
1. `execution_id` を発行し、成果物を `execution_id` 単位で分離保存する。
2. 判定入力の混在を避けるため、`execution_id` を跨いだ成果物参照を禁止する。
3. `node_key` ごとに成果物ディレクトリを分離する。
4. 実行失敗時は `trial_meta.json` に環境情報と失敗原因を記録する。

## 判定基準
- `diagnostics.json` と `perf.json` とログ群が揃っている。
- `node_key` 単位で成果物が分離されている。
- 実行方式が `MCP run_program` と `MCP run_quality_checks` に限定される。
