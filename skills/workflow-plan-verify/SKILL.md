---
name: workflow-plan-verify
description: Plan ステージの verify を実行し、`case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` の整合性検査と `derived_contract.json` の導出・検査を行うときに使用する。Plan 生成後の `verification_status` 判定に適用する。
---

# Workflow Plan Verify

## 目的
Plan ステージ出力の契約違反を検出し、`Generate` へ進める条件を判定する。

## 適用範囲
- `workspace/plans/<node_key_safe>/<plan_id>/` の resolved artifact を検査する作業
- `plan_meta.json` の `verification_status` を更新する作業

## 要件
- 判定規則の canonical source は `docs/workflow/WORKFLOW_CORE.md` と `docs/workflow/phases/phase_01_plan.md` と `docs/RUNBOOK.md` と `controlled_spec.md` と `tests.md` と `deps.yaml` と検査対象 resolved artifact に限定する。`tools/` 配下の実装、検証 `script`、test code、validator code を読んで要求や判定規則を抽出してはならない。
- 起動要求の `dependency_ref` は必ず `spec/<component_path>/deps.yaml` 形式の値を受け取らなければならない。`workspace/plans/` 形式の値は誤りであり、検出時は即座に `fail` で停止しなければならない。`dependency_ref` は読み取りや写経の対象ではなく、規約確認のみに使用する。
- `case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` の必須項目を検査する。
- `case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` と `derived_contract.json` と `plan_meta.json` が `python3 tools/check_artifact_syntax.py --expect-top object` を通過することを検査する。
- `algorithm.resolved.yaml` の `execution_mode` と `steps[]` と `ordering` と `control_condition` と `iteration_contract` と `derived_field_rules` と `invariants` を検査する。
- `algorithm.resolved.yaml` の `step_kind` が許可語彙に一致し、`steps[]` の `operation_ref` と `dependency.resolved.yaml` の解決結果が矛盾しないことを検査する。
- `derived_contract.json` の存在と整合性を検査する。導出元は `controlled_spec.md` と `tests.md` と `deps.yaml` に限定する。
- `derived_contract.json` は `Plan verify substep` の責務として導出して保存しなければならない。`Plan generate substep` が生成してはならない。
- `derived_contract.json` の書き込み先は必ず `workspace/plans/<node_key_safe>/<plan_id>/derived_contract.json` でなければならない。`plans/` 直下（`workspace/` プレフィックスなし）への書き込みは禁止する。書き込み前に `output_manifests/<agent_run_id>.json` の `allowed_output_paths` を確認し、`workspace/plans/` で始まる path と一致することを検証すること。
- `derived_contract.json` の `io_contract.inputs` と `io_contract.outputs` の必須項目（`name` / `evidence_ref` / `shape_expr`）を検査する。
- `derived_contract.json` の `io_contract.outputs` で `evidence_ref` が `raw/state_snapshots` 以外を参照し、かつ `artifact=state_snapshots` を必須宣言する場合、`raw_variables` が非空配列であり、`schema.variables` または `schema.time_variable` を参照していることを検査する。
- `derived_contract.json` の `raw_requirements.required_evidence` を検査し、`artifact` と `required` と `min_samples` と `schema`（必要時）の整合を確認する。
- `raw_requirements.required_evidence` で `artifact=state_snapshots` を `required=true` で宣言する場合、`schema.variables[].name` と `schema.variables[].shape_expr` と `schema.time_variable` と `schema.time_shape_expr` の存在と妥当性を検査する。
- `derived_contract.json` の `test_evidence_requirements` を検査し、`tests.md` の全 `test_id` を過不足なく保持し、各 `required_raw_variables` が `schema` で宣言された変数に解決できることを確認する。
- `derived_contract.json` が生成契約を保持していないことを検査する。統合順序、更新順序、`numerical_kernel_contract`、反復条件は `algorithm.resolved.yaml` にのみ存在しなければならない。
- 既定値適用規則を検査する。対象は言語既定値、`toolchain.build_system` 既定値、`OpenMP` 既定値である。
- 直下依存 `node` の `plan_ref` と `plan_meta.json.verification_status` を確認できない場合を `dependency plan missing` として `fail` とする。
- `dependency.resolved.yaml` の `node_key` と依存集合と `topo_level` の整合性を検査する。
- 同一入力から再生成した resolved artifact との差分を検査し、determinism違反を検出する。
- 検証契約の導出に必要な情報が不足する場合は `fail` とし、推測補完で `pass` を付与してはならない。
- workflow mode は `METDSL_WORKFLOW_EXEC_MODE` を canonical source とし、未設定時は `dev` を適用する。
- `dev` mode では `issue_severity=major|critical` を検出した時点で `Plan fail` とし、軽微例外扱いを禁止する。
- 依存解決エラーの `node` が `blocked` 扱いであることを検査する。
- 検査対象 artifact の保存先ルートが `workspace/` であることを検査し、workflow ルート判定は `workspace/` のみを対象とする。

## 運用ルール
0. 作業開始直後に `output_manifests/<agent_run_id>.json` の `allowed_file_tool_paths` と `allowed_output_paths` を Read し、`derived_contract.json` の出力先が `workspace/plans/<node_key_safe>/<plan_id>/derived_contract.json` と一致することを確認する。不一致の場合は即座に fail で停止すること。
1. 検査結果を `plan_meta.json` に反映し、`verification_status` を `pass` または `fail` に更新する。
2. `fail` の場合は `last_fail_reason` を具体化し、修正対象ファイルと規約名を記録する。
3. `verification_status=pass` の `plan_id` のみ `Generate` へ進める。
4. `plan_meta.json` の必須 key（`attempt_count`、`verification_status`、`last_fail_reason`、`debug_mode`、`context_isolated`）を検査し、欠落時は `fail` とする。
5. `context_isolated=false` の場合、`constraint_reason` の非空文字列を必須検査とする。
6. `debug_mode=false` では失敗試行 artifact を保存しない。
7. workflow artifact の保存先ルートが `workspace/` でない場合は、下流 phase を開始せず `Plan fail` とする。
8. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
9. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Plan fail` とする。
10. verify 完了前に `python3 tools/validate_pipeline_semantics.py --stage plan --plan-ref workspace/plans/<node_key_safe>/<plan_id>/` を実行し、`exit code 0` を必須とする。`fail` 時は `plan_meta.json` の `verification_status=pass` を付与してはならない。
11. `dev` mode で `fail` した場合は、`failure_analysis.json` 作成に必要な根拠（違反規約、対象 artifact、失敗理由）を `last_fail_reason` に記録する。

## 判定基準
- 契約違反がない場合のみ `verification_status=pass` を付与する。
- 検査結果に再現可能な根拠ファイルを必ず付与する。
- 判定規則が `docs/workflow/WORKFLOW_CORE.md` と `docs/workflow/phases/phase_01_plan.md` と `docs/RUNBOOK.md` と一致する。
