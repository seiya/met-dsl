---
name: workflow-promote
description: Promote ステージを実行し、`verdict` と `aggregate_verdict` が合格した artifact を `releases/` へ昇格して `spec_catalog.yaml` の `official_releases` を更新するときに使用する。`release_id` 不変条件と追跡情報登録を満たす作業に適用する。
---

# Workflow Promote

## 目的
Promote ステージの昇格責務を固定し、正式版 artifact を再現可能な形で登録する。

## 適用範囲
- `workspace` artifact を `releases/<...>/<release_id>/` へ昇格する作業
- `spec/registry/spec_catalog.yaml` の `official_releases` を更新する作業

## 要件
- 入力条件として `verdict.json` の `overall=pass` を要求する。
- 入力条件として `aggregate_verdict.json` の `overall=pass` を要求する。
- 採用対象の `source_id` と `binary_id` と `run_id` が `lineage.json` と `trial_meta.json` で追跡可能であることを要求する。
- 登録時は `release_id` と `target_architecture` と `toolchain_language` と `target_backend` と `source_pipeline_id` と `source_source_id` と `source_binary_id` と `source_run_id` と `artifact_root` と `promoted_at` と `status` を必須記録する。
- 既存 `release_id` の上書きを禁止する。

## 運用ルール
1. 昇格先を `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` に固定する。
2. 同一 `target_architecture + toolchain_language` の旧 `release` は `deprecated` に更新する。
3. `problem` の昇格時は推移依存 `node` の集約状態が `pass` または `xfail` のみで構成されることを確認する。
4. 昇格後に `spec_catalog.yaml` を同期更新し、探索 canonical source と登録 canonical source の不一致を残さない。

## 判定基準
- 昇格 artifact と `official_releases` 登録内容が一致する。
- `release_id` 不変条件が維持される。
- 追跡情報のみで昇格元試行を再現できる。
