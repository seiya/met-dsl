# Phase contract: Promote

### 7. Promote
- execution input: 採用 `impl.resolved.yaml`、`lineage.json`、採用対象の生成物
- verification input: `verdict.json`、`aggregate_verdict.json`、`trial_meta.json`、`lineage.json`
- 出力: `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` 配下の正式版 artifact、`spec/registry/spec_catalog.yaml` の `official_releases` 更新
- `Promote` は標準 `substep` を持たない単一 `step` とする。
- 入力条件: `verdict.json` の `overall=pass`
- 入力条件: `aggregate_verdict.json` の `overall=pass`
- 入力条件: 採用対象 `generation_id` / `build_id` / `execution_id` が `lineage.json` と `trial_meta.json` で追跡可能であること
- 入力条件: 採用対象 `impl.resolved.yaml` が確定していること
- 実施内容: `workspace` から採用 artifact を `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` へ保存する。
- 実施内容: `spec/registry/spec_catalog.yaml` の対象 `spec_id` に `official_releases` を追加する。
- 登録必須項目: `release_id`、`target_architecture`、`toolchain_language`、`target_backend`、`source_pipeline_id`、`source_generation_id`、`source_build_id`、`source_execution_id`、`artifact_root`、`promoted_at`、`status`
- 不変条件: 既存 `release_id` の上書きを禁止する。更新時は新規 `release_id` を追加し、同一 `target_architecture + toolchain_language` の旧 `release` を `deprecated` へ更新する。
- `problem` の `Promote` は推移依存を含む `aggregate_verdict.overall=pass` を必須条件とする。

