---
name: workflow-generate-generate
description: Generate ステージの generate を実行し、`case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` から `model` と `runner` を分離した実装コードを作成するときに使用する。`generation_id` 発行と `generate_meta.json` 出力に適用する。
---

# Workflow Generate Generate

## 目的
Generate ステージの生成責務を固定し、`Build` 可能な実装 artifact を作成する。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/generate/<generation_id>/src/` に実装コードを生成する作業
- `generate_meta.json` を生成する作業

## 要件
- 入力は `case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` とする。
- `controlled_spec.md` を直接入力にしてはならない。演算構成の要求定義は `algorithm.resolved.yaml` から解釈しなければならない。
- 実装コードは `model` と `runner` を分離し、`runner` は `model` を `call` / `use` / `import` で利用する。
- `runner` に物理更新ロジックを重複実装しない。
- `toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`runner` から `python` / `bash` / `sh` / `node` などの外部インタプリタを起動してはならない。
- `model` は対象 `node` の演算契約を実装し、固定値返却専用、固定 `JSON` 出力専用、`no-op` 専用実装を禁止する。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` は、`algorithm.resolved.yaml` の状態更新契約を入力として読み取り、`state_variables` を更新計算へ必須利用する。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` は、`case_id` 分岐とスカラー定数代入のみで複数の `diagnostics` 指標を生成してはならない。
- `algorithm.resolved.yaml` の状態更新契約が欠落、または `state_variables` と `required_update_paths` が欠落する場合は `Generate fail` とし、推測補完で生成を継続してはならない。
- `algorithm.resolved.yaml` の `steps[]` と `ordering` と `control_condition` と `iteration_contract` を満たす実装構成を生成しなければならない。
- 依存を持つ `node` は、`dependency.resolved.yaml` の `direct_deps` で解決された依存 `node` の公開 `operation` を呼び出す実装を必須とする。
- 依存 `operation` と同等機能を依存元 `node` の `model` / `runner` に再実装してはならない。
- `toolchain.language=fortran` で依存 `component` を持つ `node` の `model` は、依存 `spec_id` ごとに `use <spec_id>_model` と `call <spec_id>__*` を必須とし、`subroutine <spec_id>__*` の再定義を禁止する。
- 依存先が `profile` で公開 `operation` を持たない構成では、依存元 `problem` が `profile` の選択結果と拘束条件を参照する実装にしなければならない。
- `runner` は `diagnostics.json` と `perf.json` と `raw/` 一次証跡のみを出力対象とし、`verdict.json` と `aggregate_verdict.json` と `summary.json` と `trial_meta.json` を書き込んではならない。
- `target.class=cpu` かつ並列化方式未指定のとき、並列化可能ループへ `OpenMP` を既定適用する。
- 生成 artifact は対象 `node_key` と整合する構成にする。
- 生成 artifact は `node_key` ごとの差分を保持し、共通ライブラリ明示なしに `src` 全体を複製してはならない。
- `toolchain.language=fortran` の場合、`module` 名とソースファイル名を一致させ、`<module_name>.f90` 形式で出力する。
- `toolchain.language=fortran` の場合、`module` 名と公開 `subroutine` 名に `spec_id` 由来接頭辞を付与し、名前衝突を回避する。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` の場合、`src/Makefile` は `use` 依存に対応したオブジェクト依存関係を明示し、依存 `module` の `.mod` または依存 `.o` を各オブジェクトターゲットの前提条件へ必須記述する。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` の場合、`src/Makefile` は `make -j` で依存欠落による不定失敗を起こしてはならない。
- `generate_meta.json` に `attempt_count` と `verification_status` と `last_fail_reason` と `debug_mode` を記録する。
- `generate_meta.json` の `verification_status` は `fail_closed` を前提とし、検証未実施や判定不能を `pass` にしてはならない。
- workflow artifact の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。

## 運用ルール
1. `generation_id` を発行し、出力先を `workspace/pipelines/<pipeline_id>/generate/<generation_id>/` に固定する。
2. `debug_mode=false` では `attempts/` を作成しない。
3. `debug_mode=true` の場合のみ失敗試行を `attempts/<attempt_id>/` に保存する。
4. `verification_status=pass` の artifact のみ `Build` に引き渡す。
5. 出力先が `workspace/` でない場合は `Generate fail` とする。
6. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
7. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Generate fail` とする。

## 判定基準
- `model` と `runner` の責務分離が保持される。
- 出力ファイル集合が `docs/WORKFLOW.md` と `docs/RUNBOOK.md` の契約に一致する。
- `generate_meta.json` の必須項目が欠落しない。
