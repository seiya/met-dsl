---
name: workflow-build
description: Build ステージを実行し、`generate` artifact を `MCP` サーバー経由の `compile_project` でビルドして `build_id` artifact を作成するときに使用する。`fortran` / `c` / `cpp` / `mixed` 系の標準ビルドツール制約を守る作業に適用する。
---

# Workflow Build

## 目的
Build ステージの実行責務を固定し、再現可能なビルド artifact を生成する。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/build/<build_id>/` を生成する作業
- `generate_meta.json` が `verification_status=pass` の artifact をビルドする作業

## 要件
- `compile` は `MCP` サーバーの `compile_project` を使用する。
- `fortran` / `c` / `cpp` / `mixed` 系は `make` / `cmake` / `meson` / `ninja` の標準ビルドツールのみを許可する。
- `gcc` / `clang` / `gfortran` の単発ビルドを禁止する。
- `toolchain.build_system=make` の場合、入力 `src/Makefile` は言語依存のコンパイル順序依存をターゲット前提条件として明示し、`make -j` で成否が変化しないことを必須とする。
- 依存を持つ `node` は、依存 `operation` の解決先が `dependency.resolved.yaml` と一致することを `Build` 時に検証しなければならない。不一致時は `Build fail` とする。
- `build_meta.json` に `build_system` と `compiler` と `build_log_ref` と `status` を記録する。
- 出力 `bin/` は `Execute` が参照可能な相対配置にする。
- workflow artifact の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。

## 運用ルール
1. `build_id` を発行し、出力先を `workspace/pipelines/<pipeline_id>/build/<build_id>/` に固定する。
2. `generate_meta.json` の `verification_status=pass` を開始条件にする。
3. ビルド失敗時は `build_meta.json` に失敗原因を記録し、`Generate` へ戻す。
4. 同一 `generation_id` の再ビルドは別 `build_id` で append-only 運用にする。
5. 出力先が `workspace/` でない場合は `Build fail` とする。
6. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
7. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Build fail` とする。
8. 完了前に `python3 tools/validate_pipeline_semantics.py --stage post_build --pipeline-root workspace/pipelines/<node_key_safe>/<pipeline_id>/` を実行し、必要に応じて `--generation-id <generation_id>` を付与する。`exit code 0` を必須とし、`fail` 時は `Build fail` とする。

## 判定基準
- ビルド手段が `MCP compile_project` のみである。
- `build_meta.json` の必須項目が欠落しない。
- `workspace` 配置規約が `docs/WORKFLOW.md` と一致する。
