---
name: workflow-compile-generate
description: Compile ステージの generate を実行し、`controlled_spec.md` と `tests.md` と依存情報から単一の `spec.ir.yaml` を決定的に生成するときに使用する。Spec 起点で `ir_id` を新規発行する作業に適用する。
---

# Workflow Compile Generate

## 目的
Compile ステージの生成責務を固定し、入力 spec から決定的な構造化 IR (`spec.ir.yaml`) を作成する。

## 適用範囲
- `controlled_spec.md` と `tests.md` から `spec.ir.yaml` の `case` セクションを生成する作業
- `controlled_spec.md` と依存解決結果から `spec.ir.yaml` の `algorithm` セクションを生成する作業
- `target` と `toolchain` の既定値を適用して `spec.ir.yaml` の `impl_defaults` セクションを生成する作業
- `deps.yaml` と `spec_catalog.yaml` から `spec.ir.yaml` の `dependency` セクションを生成する作業
- `spec.ir.yaml` の `io_contract` セクション（IO 契約と検証契約）を導出する作業

## 要件
- 入力は `spec/<...>/controlled_spec.md` と `spec/<...>/tests.md` と `spec/<...>/deps.yaml` と `spec/registry/spec_catalog.yaml` を canonical source にする。
- 出力形式、input/output contract、判定条件の要求定義は `controlled_spec.md` と `tests.md` と `deps.yaml` と `docs/` canonical source を参照し、`tools/` 配下の検証 `python` スクリプトと `quality check` 実装を参照してはならない。
- 追加必須項目を `Controlled Spec` へ要求してはならない。検証契約は既存入力から導出する。
- `spec.ir.yaml` は **単一ファイル**に `case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency` の 5 セクションを統合保持する。schema の canonical source は `docs/workflow/phases/phase_01_compile.md`。
- `case` セクションの `sweep` と `refinement` は決定的な順序で展開する。`case` は実行時入力の決定値のみを保持し、検証 output contract を保持してはならない。
- `algorithm` セクションは `execution_mode` と `steps[]` と `ordering` と `control_condition` と `iteration_contract` と `update_semantics` と `temporaries` と `derived_field_rules` と `invariants` と `splitting_policy` を必須保持し、`problem` の統合順序、依存 `operation` 呼び出し順序、条件分岐、反復条件、列処理、派生量定義、更新対象を表現する。
- `temporaries[].shape_expr` および `state_variables[].shape_expr` の表現規則は `spec/schema/plan/shape_expr.schema.json` を canonical source とする。許容形式は `scalar` (case-insensitive) / `[d1, d2, ...]` / `(d1, d2, ...)` の 3 形式に限り、`vector(N)` / `matrix(M,N)` / `tensor` 等の関数呼び出し記法は `Compile fail` とする。
- `algorithm.execution_mode` は `sequence` / `conditional` / `iterative` / `columnwise` のみを許可する。
- `algorithm.steps[]` の各要素は `step_id` と `step_kind` と `operation_ref` と `inputs` と `outputs` を必須保持する。`inputs` と `outputs` は **非空文字列の list**（例: `["U_L", "U_R", "h_B", "h_T"]`）とし、object 形式（`[{name: ..., source: ...}]`）は禁止する。参考形式: `docs/examples/spec_ir_algorithm_section.example.yaml`。
- `algorithm.steps[].inputs` / `algorithm.steps[].outputs` に現れる string token は、`controlled_spec.md` の直接入出力変数・`temporaries` の中間変数・`derived_field_rules` の派生量のいずれかに**追跡可能**でなければならない。スライス・エイリアス・コンポーネント分解による派生名（例: `h_L` ← `U_L[0]`）は `temporaries` または `derived_field_rules` に宣言して provenance を明示すること。直接名・派生名のいずれにも対応付けできないトークンは `spec.ir.yaml` 単独でデータフローを追跡できないため `Compile fail` とする。
- `algorithm.steps[].step_kind` は `boundary_apply` / `reconstruct` / `flux_compute` / `source_term` / `time_integrate` / `column_process` / `pointwise_process` / `iterative_solve` / `filter` / `reduction` / `diagnostic` のみを許可する。
- `spec.ir.yaml` は `Generate` の canonical source 入力であり、`Generate` が `controlled_spec.md` を直接読まなくても実装可能な情報を保持しなければならない。
- `algorithm.summary.md` を自動生成し、閲覧専用 artifact として `workspace/ir/<node_key_safe>/<ir_id>/` 配下に保存する。
- ユーザーが言語を明示しない場合は `impl_defaults.target.class=cpu` で `fortran`、`target.class=gpu` で `cuda_fortran` を採用する。
- `impl_defaults.toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`toolchain.build_system` は `make` / `cmake` / `meson` / `ninja` から選択し、未指定時は `make` を採用する。
- `impl_defaults.target.class=cpu` でループ並列化方式の指定がない場合、`impl_defaults.abstract` 内で並列化可能ループへ `OpenMP` を既定適用として記録する。
- `impl_defaults` の fixed / knob レイヤ境界 (`target.*` / `toolchain.*` / `selected.*` は fixed、`abstract.*` / `backend_overrides.*` は knob) は `docs/workflow/phases/phase_01_compile.md` の「impl_defaults の fixed / knob 境界」節を canonical source とする。`Compile.generate` は全 fixed sub-key を欠落なく決定し、knob レイヤの leaf 値も既定値として確定させる（`null` / `<TBD>` 等の plug-hole 禁止）。
- `dependency` セクションは `node_key` と `direct_deps` と `transitive_deps` と `topo_level` を必須記録する。
- `io_contract` セクションは `Compile.verify substep` が導出して `spec.ir.yaml` の対応セクションへ書き込む。`Compile.generate substep` は `io_contract` セクションを生成してはならない（`Compile.verify` の責務）。
- 本 substep が起動できる validator gate は `skills/workflow-orchestration/references/launch_prompts.md` の「substep ↔ allowed validator gate 対応表」を canonical source とする (responsibility 外 gate の launch prompt 混入は `noncanonical_phase_write_attempt` を発火する)。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` では、`algorithm` セクションに状態更新対象と更新順序を必須保持しなければならない。
- 多次元 `problem` 向け契約は `algorithm.state_variables[].name` と `algorithm.state_variables[].shape_expr` と `algorithm.required_update_paths` と `algorithm.diagnostics_from_state=true` と `algorithm.fallback_policy=fail_closed` を必須保持する。`state_variables[].shape_expr` も `spec/schema/plan/shape_expr.schema.json` の 3 形式に限る。
- 上位 `node` の `Compile` は、直下依存 `node` の `ir_ref` と `ir_meta.json.verification_status` を確認し、`direct dependency compile readiness` を満たさない場合は開始してはならない。
- 上記の生成契約を導出できない場合は `Compile fail` とし、不完全な IR で `Generate` へ進めてはならない。
- 未登録依存、未実装依存、互換性違反依存は解決エラーにし、該当 `node` を `blocked` にする。
- `Compile` 完了前の validator invocation は `run-gate` を原則とする。`check_artifact_syntax.py` と `validate_workspace_root.py` は read-only 検査かつ gate 非依存検査に限り直接実行を許可する。
- `Compile.generate substep` 完了前に `python3 tools/check_artifact_syntax.py --expect-top object` を実行し、`spec.ir.yaml` が標準 parser で復元可能な mapping / object であることを確認しなければならない。

## 運用ルール
0. `.json` artifact（`ir_meta.json` 等）の書き込みは `guarded-apply-patch` を唯一の経路とする。Bash 変数代入・heredoc リダイレクト・`python3 -c` によるインライン書き込みは `output_manifest_write_guard` / `forbid_python_inline_write` でブロックされる。`.yaml` / `.md` artifact は `output_manifests/<agent_run_id>.json` の `allowed_file_tool_paths` に列挙された path に限り `Edit` / `Write` tool で直接書き込む。書き込み前に `allowed_output_paths` を確認し、`workspace/` で始まるプロジェクトルート相対パスのみを使用すること。`ir_meta.json` を含むすべての出力 path は必ず `workspace/ir/<node_key_safe>/<ir_id>/...` 形式で指定すること。`ir/` 等の `workspace/` 接頭辞を欠くパスは `output_manifest_write_guard` でブロックされ `unauthorized_write_violation` として記録される。
1. `ir_id` を `<slug>_<date>_<seq3>` 形式で発行する。`slug` は `spec_id` 由来の短い可読 token、`date` は `YYYYMMDD`、`seq3` は同日内 3 桁連番とする。
2. 出力先は `workspace/ir/<node_key_safe>/<ir_id>/` に固定する。
3. workflow artifact の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。
4. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
5. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行してよい。`run-gate --gate validate_workspace_root` が利用可能な実行環境では `run-gate` を優先し、`fail` 時は `Compile fail` とする。
6. `ir_meta.json` に `attempt_count` と `verification_status` と `last_fail_reason` と `debug_mode` と `context_isolated` を記録する。
7. `context_isolated=false` の場合、`ir_meta.json.constraint_reason` を必須記録する。
8. `debug_mode=false` では失敗試行 artifact を保存しない。
9. 完了前に `python3 tools/check_artifact_syntax.py --expect-top object` を `spec.ir.yaml` へ実行してよい。`run-gate` 経由の同等検査が利用可能な実行環境では `run-gate` を優先し、`fail` 時は `Compile fail` とする。
10. `Validate` からの retry を受けて再投入された場合 (`launches/<agent_run_id>.request.json#repair_reason` に `validate_feedback` を含む場合)、`docs/workflow/phases/phase_01_compile.md` の「Validate からの retry 受け入れ」節に従い、修正対象 `spec.ir.yaml` section を `ir_meta.json.repair_target_sections[]` に記録する。

## 判定基準
- 同一入力で再生成したとき、`spec.ir.yaml` の全セクションが一致する。
- `dependency.topo_level` が循環依存を含まない。
- 出力が `docs/workflow/WORKFLOW_CORE.md` と `docs/workflow/phases/phase_01_compile.md` と `docs/RUNBOOK.md` の契約に整合する。
