---
name: workflow-plan-generate
description: Plan ステージの generate を実行し、`controlled_spec.md` と `tests.md` と依存情報から `case.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` を決定的に生成するときに使用する。Spec 起点で `plan_id` を新規発行する作業に適用する。
---

# Workflow Plan Generate

## 目的
Plan ステージの生成責務を固定し、入力 spec から決定的な resolved 成果物を作成する。

## 適用範囲
- `controlled_spec.md` と `tests.md` から `case.resolved.yaml` を生成する作業
- `target` と `toolchain` の既定値を適用して `impl.resolved.yaml` を生成する作業
- `deps.yaml` と `spec_catalog.yaml` から `dependency.resolved.yaml` を生成する作業

## 要件
- 入力は `spec/<...>/controlled_spec.md` と `spec/<...>/tests.md` と `spec/<...>/deps.yaml` と `spec/registry/spec_catalog.yaml` を正本にする。
- 追加必須項目を `Controlled Spec` へ要求してはならない。検証契約は既存入力から導出する。
- `case.resolved.yaml` の `sweep` と `refinement` は決定的な順序で展開する。
- `case.resolved.yaml` は実行時入力の決定値のみを保持し、検証出力契約を保持してはならない。
- ユーザーが言語を明示しない場合は `target.class=cpu` で `fortran`、`target.class=gpu` で `cuda_fortran` を採用する。
- `toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`toolchain.build_system` は `make` / `cmake` / `meson` / `ninja` から選択し、未指定時は `make` を採用する。
- `target.class=cpu` でループ並列化方式の指定がない場合、並列化可能ループへ `OpenMP` を既定適用として記録する。
- `dependency.resolved.yaml` は `node_key` と `direct_deps` と `transitive_deps` と `topo_level` を必須記録する。
- `derived_contract.json` を必須出力とし、`controlled_spec.md` と `tests.md` と `deps.yaml` から導出した検証契約を保持する。
- `derived_contract.json` は `io_contract.inputs` と `io_contract.outputs` を必須保持し、`outputs` は `name` と `evidence_ref` と `shape_expr` を必須保持する。
- `derived_contract.json` の `io_contract.outputs` で `evidence_ref` が `raw/state_snapshots` 以外を参照し、かつ `artifact=state_snapshots` を必須宣言する場合、当該 `output` は `raw_variables` で判定再計算に必要な `raw` 変数名を必須記録する。
- `derived_contract.json` は `raw_requirements.required_evidence` を必須保持し、`artifact` と `required` と `min_samples` と `schema`（必要時）を定義する。
- `raw_requirements.required_evidence` で `artifact=state_snapshots` を `required=true` で宣言する場合、`schema.variables[].name` と `schema.variables[].shape_expr` と `schema.time_variable` と `schema.time_shape_expr` を必須記録する。
- `derived_contract.json` は `test_evidence_requirements` を必須保持し、`tests.md` の各 `test_id` ごとに `required_raw_variables` を記録する。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` では、`derived_contract.json` に `numerical_kernel_contract` を必須保持する。
- `numerical_kernel_contract` は `state_variables[].name` と `state_variables[].shape_expr` と `required_update_paths` と `diagnostics_from_state=true` と `fallback_policy=fail_closed` を必須保持する。
- `numerical_kernel_contract` を導出できない場合は `Plan fail` とし、不完全な契約で `Generate` へ進めてはならない。
- 未登録依存、未実装依存、互換性違反依存は解決エラーにし、該当 `node` を `blocked` にする。

## 運用ルール
1. `plan_id` を `<spec_id>_<case_hash12>_<impl_hash12>` 形式で発行する。
2. 出力先は `workspace/plans/<node_key_safe>/<plan_id>/` に固定する。
3. workflow 成果物の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。
4. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
5. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Plan fail` とする。
6. `plan_meta.json` に `attempt_count` と `verification_status` と `last_fail_reason` と `debug_mode` を記録する。
7. `debug_mode=false` では失敗試行成果物を保存しない。

## 判定基準
- 同一入力で再生成したとき、`case.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` が一致する。
- `dependency.resolved.yaml` の `topo_level` が循環依存を含まない。
- 出力が `docs/WORKFLOW.md` と `docs/RUNBOOK.md` の契約に整合する。
