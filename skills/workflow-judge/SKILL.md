---
name: workflow-judge
description: Judge ステージを実行し、`tests.md` と `diagnostics.json` と依存情報に基づいて `verdict.json` と `aggregate_verdict.json` と `summary.json` を判定するときに使用する。依存 `DAG` の `blocked` 判定と `self_verdict` / `aggregate_verdict` 集約を行う作業に適用する。
---

# Workflow Judge

## 目的
Judge ステージの判定責務を固定し、物理合否と依存集約合否を再現可能に決定する。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/execute/<execution_id>/<node_key>/` の判定処理
- `verdict.json` と `aggregate_verdict.json` と `summary.json` の生成

## 要件
- 判定正本は対象 `node` の `tests.md` に固定する。
- `self_verdict` は当該 `node` 単体の判定結果として `verdict.json` に保存する。
- `aggregate_verdict` は推移依存 `node` を集約して `aggregate_verdict.json` に保存する。
- 直下依存 `node` が `pass` または `xfail` でない場合、当該 `node` を `blocked` にする。
- 物理 `fail` の場合は性能評価をスキップする。
- `summary.json` に `self_summary` と `dependency_summary` を必須保存し、`dependency_summary` は `total` と `pass` と `xfail` と `fail` と `blocked` を持つ。

## 運用ルール
1. 判定入力は同一 `execution_id` 配下成果物のみに限定する。
2. `aggregate_verdict.json` は `dependency.resolved.yaml` の依存集合と一致させる。
3. `target.class=cpu` の品質比較結果は `quality check` として記録し、`tests` 判定と分離する。
4. 判定失敗時は `summary.json` に失敗分類を明示し、戻り先ステージを指定する。

## 判定基準
- 判定根拠が `tests.md` と `diagnostics.json` に追跡できる。
- `blocked` 判定条件が依存状態と一致する。
- `aggregate_verdict.json` と `summary.json` が `dependency.resolved.yaml` と整合する。
