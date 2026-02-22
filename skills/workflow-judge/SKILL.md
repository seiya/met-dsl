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
- 判定指標は `execution_id/<node_key>/raw/` の実行証跡から再計算し、`diagnostics.json` と整合確認する。再計算不能または不整合は `Judge fail` とする。
- 再計算入力は `raw` 一次証跡のみに限定し、`diagnostics.json` を再計算入力へ流用してはならない。
- 物理 `fail` の場合は性能評価をスキップする。
- `summary.json` に `self_summary` と `dependency_summary` を必須保存し、`dependency_summary` は `total` と `pass` と `xfail` と `fail` と `blocked` を持つ。
- 判定成果物の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。

## 運用ルール
1. 判定入力は同一 `execution_id` 配下成果物のみに限定する。
2. 判定入力は `diagnostics.json` と `perf.json` と `raw` 実行証跡の同時存在を必須とし、いずれか欠落時は `Judge` を開始しない。
3. `aggregate_verdict.json` は `dependency.resolved.yaml` の依存集合と一致させる。
4. `target.class=cpu` の品質比較結果は `quality check` として記録し、`tests` 判定と分離する。
5. `quality check` の比較正本は `diagnostics.json` と `verdict.json` とし、`stdout` 差分のみで合否を確定してはならない。
6. 判定失敗時は `summary.json` に失敗分類を明示し、戻り先ステージを指定する。
7. 出力先が `workspace/` でない場合は `Judge fail` とする。
8. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
9. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Judge fail` とする。

## 判定基準
- 判定根拠が `tests.md` と `diagnostics.json` に追跡できる。
- 判定根拠が `raw` 実行証跡から再計算可能である。
- `blocked` 判定条件が依存状態と一致する。
- `aggregate_verdict.json` と `summary.json` が `dependency.resolved.yaml` と整合する。
