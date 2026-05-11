---
name: workflow-compile-verify
description: Compile ステージの verify を実行し、`spec.ir.yaml` の構造 invariant 検査と `io_contract` セクションの導出・検査を行うときに使用する。Compile 生成後の `verification_status` 判定に適用する。
---

# Workflow Compile Verify

## 目的
Compile ステージ出力の構造 invariant 違反を検出し、`Generate` へ進める条件を判定する。意味的正しさは `Validate` 実行結果に委ねる「ハイブリッド検証」原則（`docs/workflow/phases/phase_01_compile.md`）に従い、self-check の範囲を構造 invariant に限定する。

## 適用範囲
- `workspace/ir/<node_key_safe>/<ir_id>/` の `spec.ir.yaml` を検査する作業
- `spec.ir.yaml` の `io_contract` セクションを `controlled_spec.md` と `tests.md` と `deps.yaml` から導出する作業
- `ir_meta.json` の `verification_status` を更新する作業

## 要件
- 判定規則の canonical source は `docs/workflow/WORKFLOW_CORE.md` と `docs/workflow/phases/phase_01_compile.md` と `docs/RUNBOOK.md` と `controlled_spec.md` と `tests.md` と `deps.yaml` と検査対象 `spec.ir.yaml` に限定する。`tools/` 配下の実装、検証 `script`、test code、validator code を読んで要求や判定規則を抽出してはならない。
- 起動要求の `dependency_ref` は必ず `spec/<component_path>/deps.yaml` 形式の値を受け取らなければならない。`workspace/ir/` 形式の値は誤りであり、検出時は即座に `fail` で停止しなければならない。`dependency_ref` は読み取りや写経の対象ではなく、規約確認のみに使用する。
- `spec.ir.yaml` の `case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency` 5 セクションの必須項目を検査する。
- `spec.ir.yaml` と `ir_meta.json` が `python3 tools/check_artifact_syntax.py --expect-top object` を通過することを検査する。
- `spec.ir.yaml.algorithm` の `execution_mode` と `steps[]` と `ordering` と `control_condition` と `iteration_contract` と `derived_field_rules` と `invariants` を検査する。`steps[].inputs` と `steps[].outputs` が **非空文字列の list**（例: `["U_L", "U_R"]`）であることを必須検査とし、object list 形式（`[{name: ..., source: ...}]`）は `must be string list` 違反として `fail` とする。参考形式: `docs/examples/spec_ir_algorithm_section.example.yaml`。
- `steps[].inputs` / `steps[].outputs` に現れる各 string token が、`controlled_spec.md` の直接入出力変数・`temporaries` の中間変数・`derived_field_rules` の派生量のいずれかに**追跡可能**であることを検査する。スライス・エイリアス・コンポーネント分解による派生名は `temporaries` または `derived_field_rules` に宣言されていれば有効とする。直接名・派生名のいずれにも対応付けできないトークンは未定義バインディングとして `Compile fail` とする。
- `spec.ir.yaml.algorithm.step_kind` が許可語彙に一致し、`steps[]` の `operation_ref` と `spec.ir.yaml.dependency` の解決結果が矛盾しないことを検査する。
- `spec.ir.yaml.io_contract` セクションの存在と整合性を検査する。導出元は `controlled_spec.md` と `tests.md` と `deps.yaml` に限定する。
- `spec.ir.yaml.io_contract` セクションは `Compile.verify substep` の責務として導出して `spec.ir.yaml` 本体へ書き込まなければならない。`Compile.generate substep` が生成してはならない。
- `spec.ir.yaml` の書き込み先は必ず `workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml` でなければならない。`ir/` 直下（`workspace/` プレフィックスなし）への書き込みは禁止する。書き込み前に `output_manifests/<agent_run_id>.json` の `allowed_output_paths` を確認し、`workspace/ir/` で始まる path と一致することを検証すること。
- `io_contract.inputs` と `io_contract.outputs` の必須項目（`name` / `evidence_ref` / `shape_expr`）を検査する。
- `io_contract.outputs` で `evidence_ref` が `raw/state_snapshots` 以外を参照し、かつ `artifact=state_snapshots` を必須宣言する場合、`raw_variables` が非空配列であり、`schema.variables` または `schema.time_variable` を参照していることを検査する。
- `io_contract.raw_requirements.required_evidence` を検査し、`artifact` と `required` と `min_samples` と `schema`（必要時）の整合を確認する。
- `raw_requirements.required_evidence` で `artifact=state_snapshots` を `required=true` で宣言する場合、`schema.variables[].name` と `schema.variables[].shape_expr` と `schema.time_variable` と `schema.time_shape_expr` の存在と妥当性を検査する。
- `io_contract.test_evidence_requirements` を検査し、`tests.md` の全 `test_id` を過不足なく保持し、各 `required_raw_variables` が `schema` で宣言された変数に解決できることを確認する。
- `io_contract` セクションが生成契約を保持していないことを検査する。統合順序、更新順序、`numerical_kernel_contract`、反復条件は `spec.ir.yaml.algorithm` にのみ存在しなければならない。
- `impl_defaults` の fixed / knob レイヤ境界を検査する。fixed sub-key (`target.class` / `target.backend` / `target.architecture` / `toolchain.language` / `toolchain.standard` / `toolchain.build_system` / `selected.backend_key`) が全て値を持つこと (V6 invariant)、knob sub-key (`abstract.*` / `backend_overrides.*`) の leaf 値が plug-hole (`null` / `<TBD>`) でないこと (V7 invariant) を検査する。詳細は `docs/workflow/phases/phase_01_compile.md`。
- 既定値適用規則を検査する。対象は言語既定値、`toolchain.build_system` 既定値、`OpenMP` 既定値である。
- 直下依存 `node` の `ir_ref` と `ir_meta.json.verification_status` を確認できない場合を `dependency compile missing` として `fail` とする。
- `spec.ir.yaml.dependency` の `node_key` と依存集合と `topo_level` の整合性を検査する。
- 同一入力から再生成した `spec.ir.yaml` との差分を検査し、determinism違反を検出する。
- 構造 invariant の導出に必要な情報が不足する場合は `fail` とし、推測補完で `pass` を付与してはならない。
- workflow mode は `METDSL_WORKFLOW_EXEC_MODE` を canonical source とし、未設定時は `dev` を適用する。
- `dev` mode では `issue_severity=major|critical` を検出した時点で `Compile fail` とし、軽微例外扱いを禁止する。
- 依存解決エラーの `node` が `blocked` 扱いであることを検査する。
- 検査対象 artifact の保存先ルートが `workspace/` であることを検査し、workflow ルート判定は `workspace/` のみを対象とする。

## 運用ルール
0. 作業開始直後に `output_manifests/<agent_run_id>.json` の `allowed_file_tool_paths` と `allowed_output_paths` を Read し、`spec.ir.yaml` の出力先が `workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml` と一致することを確認する。不一致の場合は即座に fail で停止すること。
0.5. `spec.ir.yaml` への `io_contract` セクション追記は `guarded-apply-patch` を唯一の経路とする。手順: (a) `allowed_output_paths` から `workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml` を確認（`workspace/` で始まることを必須検証）、(b) 存在確認→apply 失敗時に逆の patch 形式で 1 回リトライして race window を吸収すること、(c) `python3 tools/orchestration_runtime.py guarded-apply-patch ...` を実行する。`--patch-file $TMPDIR/guarded_patch_input.txt` で patch を渡すことで argv の ARG_MAX 制限を回避すること。NG: `tee`・`cat <<EOF >file` 等のリダイレクト・`python3 -c` によるファイル書き込み、`workspace/` 接頭辞欠落のパス指定。
1. 検査結果を `ir_meta.json` に反映し、`verification_status` を `pass` または `fail` に更新する。
2. `fail` の場合は `last_fail_reason` を具体化し、違反 invariant ID (V1〜V7) と修正対象セクションと規約名を記録する。
3. `verification_status=pass` の `ir_id` のみ `Generate` へ進める。
4. `ir_meta.json` の必須 key（`attempt_count`、`verification_status`、`last_fail_reason`、`debug_mode`、`context_isolated`）を検査し、欠落時は `fail` とする。
5. `context_isolated=false` の場合、`constraint_reason` の非空文字列を必須検査とする。
6. `debug_mode=false` では失敗試行 artifact を保存しない。
7. workflow artifact の保存先ルートが `workspace/` でない場合は、下流 phase を開始せず `Compile fail` とする。
8. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
9. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Compile fail` とする。
10. verify 完了前に `python3 tools/validate_pipeline_semantics.py --stage compile --ir-ref workspace/ir/<node_key_safe>/<ir_id>/` を実行し、`exit code 0` を必須とする。`fail` 時は `ir_meta.json` の `verification_status=pass` を付与してはならない。
11. `dev` mode で `fail` した場合は、`failure_analysis.json` 作成に必要な根拠（違反規約、対象 artifact、失敗理由）を `last_fail_reason` に記録する。

## 判定基準
- 構造 invariant V1〜V7 の違反がない場合のみ `verification_status=pass` を付与する。
- 検査結果に再現可能な根拠ファイルを必ず付与する。
- 判定規則が `docs/workflow/WORKFLOW_CORE.md` と `docs/workflow/phases/phase_01_compile.md` と `docs/RUNBOOK.md` と一致する。
