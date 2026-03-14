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
- `toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`runner` から `python` / `bash` / `sh` / `node` などの外部インタプリタ起動がないことを検査する。
- `model` が対象 `node` の演算契約を実装していることを検査する。固定値返却専用、固定 `JSON` 出力専用、`no-op` 専用実装を `fail` とする。
- `model` が `algorithm.resolved.yaml` で要求された `steps[]` と `ordering` と `control_condition` と `iteration_contract` を満たすことを検査する。
- `model` が `derived_contract.json` で要求された依存 `operation` と出力指標のデータ依存（`semantic_dependency.required_sources` と `io_contract.outputs`）を満たすことを検査する。時空間ループなど特定制御構造を一律必須にしてはならない。
- 出力指標が `model` 実行結果に依存しない定数出力、固定 `JSON` 出力、解析式直接代入を検出した場合は `fail` とする。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` は、`algorithm.resolved.yaml` の状態更新契約を必須検査し、欠落または `fallback_policy!=fail_closed` を検出した場合は `fail` とする。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` は、複数 `intent(out)` 指標を出力する `subroutine` が `state_variables` 配列参照を持たない場合を `metric-only scalar kernel` として `fail` とする。
- 生成コードが対象 `node_key` の入出力契約に一致することを検査する。
- 依存を持つ `node` は、`dependency.resolved.yaml` の `direct_deps` で解決された依存 `node` の公開 `operation` 呼び出しを実装していることを検査する。欠落時は `fail` とする。
- 依存 `operation` と同等機能の再実装を検出した場合は `fail` とする。
- `toolchain.language=fortran` で依存 `component` を持つ `node` は、依存 `spec_id` ごとに `model` 内の `use <spec_id>_model` と `call <spec_id>__*` を必須検査し、`subroutine <spec_id>__*` の再定義を検出した場合は `fail` とする。
- 依存先が `profile` で公開 `operation` を持たない構成では、依存元 `problem` の実装が `profile` の選択結果と拘束条件を参照していることを検査する。欠落時は `fail` とする。
- `runner` が `verdict.json` と `aggregate_verdict.json` と `summary.json` と `trial_meta.json` を書き込む実装を検出した場合は `fail` とする。
- `impl.resolved.yaml` の言語と `build_system` に整合する構成であることを検査する。
- 異なる `node_key` の `generate/src` との完全一致を検査し、共通ライブラリ明示がない複製を `copy_based_artifact_reuse` として `fail` にする。
- `toolchain.language=fortran` の場合、`module` 名とソースファイル名が `<module_name>.f90` で一致することを検査する。
- `toolchain.language=fortran` の場合、`module` 名と公開 `subroutine` 名に `spec_id` 由来接頭辞が付与されていることを検査する。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` の場合、`src/Makefile` の各オブジェクトターゲットが `use` 依存に対応した `.mod` または依存 `.o` の前提条件を明示していることを検査する。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` の場合、`src/Makefile` が `make -j` 互換の依存関係記述を欠落していないことを検査する。
- `generate_meta.json` の必須項目を検査する。
- `debug_mode=false` の場合に `attempts/` が存在しないことを検査する。
- 検査対象成果物の保存先ルートが `workspace/` であることを検査し、workflow ルート判定は `workspace/` のみを対象とする。

## 運用ルール
1. 検査結果に基づき `generate_meta.json` の `verification_status` を更新する。
2. `fail` の場合は `last_fail_reason` に規約違反内容と修正対象を記録する。
3. `verification_status=fail` の場合は regenerate を要求し、同一 `plan_id` で新しい `generation_id` を発行する。
4. `verification_status=pass` の場合のみ `Build` を開始する。
5. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
6. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Generate fail` とする。

## 判定基準
- 検査項目がすべて `pass` の場合のみ `verification_status=pass` とする。
- 検査結果が再現可能なファイル参照を持つ。
- 判定規則が `docs/WORKFLOW.md` と `docs/RUNBOOK.md` に整合する。
