---
name: workflow-generate-generate
description: Generate ステージの generate を実行し、`case.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` から `model` と `runner` を分離した実装コードを作成するときに使用する。`generation_id` 発行と `generate_meta.json` 出力に適用する。
---

# Workflow Generate Generate

## 目的
Generate ステージの生成責務を固定し、`Build` 可能な実装成果物を作成する。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/generate/<generation_id>/src/` に実装コードを生成する作業
- `generate_meta.json` を生成する作業

## 要件
- 入力は `case.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` とする。
- 実装コードは `model` と `runner` を分離し、`runner` は `model` を `call` / `use` / `import` で利用する。
- `runner` に物理更新ロジックを重複実装しない。
- `target.class=cpu` かつ並列化方式未指定のとき、並列化可能ループへ `OpenMP` を既定適用する。
- 生成成果物は対象 `node_key` と整合する構成にする。
- `generate_meta.json` に `attempt_count` と `verification_status` と `last_fail_reason` と `debug_mode` を記録する。

## 運用ルール
1. `generation_id` を発行し、出力先を `workspace/pipelines/<pipeline_id>/generate/<generation_id>/` に固定する。
2. `debug_mode=false` では `attempts/` を作成しない。
3. `debug_mode=true` の場合のみ失敗試行を `attempts/<attempt_id>/` に保存する。
4. `verification_status=pass` の成果物のみ `Build` に引き渡す。

## 判定基準
- `model` と `runner` の責務分離が保持される。
- 出力ファイル集合が `docs/WORKFLOW.md` と `docs/RUNBOOK.md` の契約に一致する。
- `generate_meta.json` の必須項目が欠落しない。
