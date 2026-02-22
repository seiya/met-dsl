---
name: workflow-build
description: Build ステージを実行し、`generate` 成果物を `MCP` サーバー経由の `compile_project` でビルドして `build_id` 成果物を作成するときに使用する。`fortran` / `c` / `cpp` / `mixed` 系の標準ビルドツール制約を守る作業に適用する。
---

# Workflow Build

## 目的
Build ステージの実行責務を固定し、再現可能なビルド成果物を生成する。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/build/<build_id>/` を生成する作業
- `generate_meta.json` が `verification_status=pass` の成果物をビルドする作業

## 要件
- `compile` は `MCP` サーバーの `compile_project` を使用する。
- `fortran` / `c` / `cpp` / `mixed` 系は `make` / `cmake` / `meson` / `ninja` の標準ビルドツールのみを許可する。
- `gcc` / `clang` / `gfortran` の単発ビルドを禁止する。
- `build_meta.json` に `build_system` と `compiler` と `build_log_ref` と `status` を記録する。
- 出力 `bin/` は `Execute` が参照可能な相対配置にする。

## 運用ルール
1. `build_id` を発行し、出力先を `workspace/pipelines/<pipeline_id>/build/<build_id>/` に固定する。
2. `generate_meta.json` の `verification_status=pass` を開始条件にする。
3. ビルド失敗時は `build_meta.json` に失敗原因を記録し、`Generate` へ戻す。
4. 同一 `generation_id` の再ビルドは別 `build_id` で append-only 運用にする。

## 判定基準
- ビルド手段が `MCP compile_project` のみである。
- `build_meta.json` の必須項目が欠落しない。
- `workspace` 配置規約が `docs/WORKFLOW.md` と一致する。
