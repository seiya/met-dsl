---
name: workflow-generate-verify
description: Generate ステージの verify を実行し、生成コードの責務分離、入出力契約、`generate_meta.json` の整合性を検査するときに使用する。`Build` 開始条件である `verification_status=pass` 判定に適用する。
---

# Workflow Generate Verify

## 目的
Generate ステージ出力の検証責務を固定し、`Build` 失敗を事前に低減する。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/generate/<generation_id>/src/` の検査
- `workspace/pipelines/<pipeline_id>/generate/<generation_id>/generate_meta.json` の更新

## 要件
- `runner` が `model` 呼び出しに集約され、物理更新ロジックを重複実装していないことを検査する。
- 生成コードが対象 `node_key` の入出力契約に一致することを検査する。
- `impl.resolved.yaml` の言語と `build_system` に整合する構成であることを検査する。
- `generate_meta.json` の必須項目を検査する。
- `debug_mode=false` の場合に `attempts/` が存在しないことを検査する。

## 運用ルール
1. 検査結果に基づき `generate_meta.json` の `verification_status` を更新する。
2. `fail` の場合は `last_fail_reason` に規約違反内容と修正対象を記録する。
3. `verification_status=fail` の場合は regenerate を要求し、同一 `plan_id` で新しい `generation_id` を発行する。
4. `verification_status=pass` の場合のみ `Build` を開始する。

## 判定基準
- 検査項目がすべて `pass` の場合のみ `verification_status=pass` とする。
- 検査結果が再現可能なファイル参照を持つ。
- 判定規則が `docs/WORKFLOW.md` と `docs/RUNBOOK.md` に整合する。
