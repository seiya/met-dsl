---
name: workflow-plan-verify
description: Plan ステージの verify を実行し、`case.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` の整合性、決定性、既定値適用を検査するときに使用する。Plan 生成後の `verification_status` 判定に適用する。
---

# Workflow Plan Verify

## 目的
Plan ステージ出力の契約違反を検出し、`Generate` へ進める条件を判定する。

## 適用範囲
- `workspace/plans/<node_key_safe>/<plan_id>/` の resolved 成果物を検査する作業
- `plan_meta.json` の `verification_status` を更新する作業

## 要件
- `case.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` の必須項目を検査する。
- `derived_contract.json` の存在と整合性を検査する。導出元は `controlled_spec.md` と `tests.md` と `deps.yaml` に限定する。
- `derived_contract.json` の `io_contract.inputs` と `io_contract.outputs` の必須項目（`name` / `evidence_ref` / `shape_expr`）を検査する。
- `derived_contract.json` の `raw_requirements.required_evidence` を検査し、`artifact` と `required` と `min_samples` と `schema`（必要時）の整合を確認する。
- 既定値適用規則を検査する。対象は言語既定値、`toolchain.build_system` 既定値、`OpenMP` 既定値である。
- `dependency.resolved.yaml` の `node_key` と依存集合と `topo_level` の整合性を検査する。
- 同一入力から再生成した resolved 成果物との差分を検査し、決定性違反を検出する。
- 検証契約の導出に必要な情報が不足する場合は `fail` とし、推測補完で `pass` を付与してはならない。
- 依存解決エラーの `node` が `blocked` 扱いであることを検査する。
- 検査対象成果物の保存先ルートが `workspace/` であることを検査し、workflow ルート判定は `workspace/` のみを対象とする。

## 運用ルール
1. 検査結果を `plan_meta.json` に反映し、`verification_status` を `pass` または `fail` に更新する。
2. `fail` の場合は `last_fail_reason` を具体化し、修正対象ファイルと規約名を記録する。
3. `verification_status=pass` の `plan_id` のみ `Generate` へ進める。
4. `debug_mode=false` では失敗試行成果物を保存しない。
5. workflow 成果物の保存先ルートが `workspace/` でない場合は、下流工程を開始せず `Plan fail` とする。
6. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
7. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Plan fail` とする。

## 判定基準
- 契約違反がない場合のみ `verification_status=pass` を付与する。
- 検査結果に再現可能な根拠ファイルを必ず付与する。
- 判定規則が `docs/WORKFLOW.md` と `docs/RUNBOOK.md` と一致する。
