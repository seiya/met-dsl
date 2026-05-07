---
name: workflow-plan-generate
description: Plan ステージの generate を実行し、`controlled_spec.md` と `tests.md` と依存情報から `case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` を決定的に生成するときに使用する。Spec 起点で `plan_id` を新規発行する作業に適用する。
---

# Workflow Plan Generate

## 目的
Plan ステージの生成責務を固定し、入力 spec から決定的な resolved artifact を作成する。

## 適用範囲
- `controlled_spec.md` と `tests.md` から `case.resolved.yaml` を生成する作業
- `controlled_spec.md` と依存解決結果から `algorithm.resolved.yaml` を生成する作業
- `target` と `toolchain` の既定値を適用して `impl.resolved.yaml` を生成する作業
- `deps.yaml` と `spec_catalog.yaml` から `dependency.resolved.yaml` を生成する作業

## 要件
- 入力は `spec/<...>/controlled_spec.md` と `spec/<...>/tests.md` と `spec/<...>/deps.yaml` と `spec/registry/spec_catalog.yaml` を canonical source にする。
- 出力形式、input/output contract、判定条件の要求定義は `controlled_spec.md` と `tests.md` と `deps.yaml` と `algorithm.resolved.yaml` と `docs/` canonical source を参照し、`tools/` 配下の検証 `python` スクリプトと `quality check` 実装を参照してはならない。
- 追加必須項目を `Controlled Spec` へ要求してはならない。検証契約は既存入力から導出する。
- `case.resolved.yaml` の `sweep` と `refinement` は決定的な順序で展開する。
- `case.resolved.yaml` は実行時入力の決定値のみを保持し、検証 output contract を保持してはならない。
- `algorithm.resolved.yaml` を必須出力とし、`problem` の統合順序、依存 `operation` 呼び出し順序、条件分岐、反復条件、列処理、派生量定義、更新対象を保持する。
- `algorithm.resolved.yaml` は `algorithm_id` と `execution_mode` と `steps[]` と `ordering` と `control_condition` と `iteration_contract` と `update_semantics` と `temporaries` と `derived_field_rules` と `invariants` と `splitting_policy` を必須保持する。
- `execution_mode` は `sequence` / `conditional` / `iterative` / `columnwise` のみを許可する。
- `steps[]` の各要素は `step_id` と `step_kind` と `operation_ref` と `inputs` と `outputs` を必須保持する。`inputs` と `outputs` は **非空文字列の list**（例: `["U_L", "U_R", "h_B", "h_T"]`）とし、object 形式（`[{name: ..., source: ...}]`）は禁止する。参考形式: `docs/examples/algorithm.resolved.example.yaml`。
- `steps[].inputs` / `steps[].outputs` に現れる string token は、`controlled_spec.md` の直接入出力変数・`temporaries` の中間変数・`derived_field_rules` の派生量のいずれかに**追跡可能**でなければならない。スライス・エイリアス・コンポーネント分解による派生名（例: `h_L` ← `U_L[0]`）は `temporaries` または `derived_field_rules` に宣言して provenance を明示すること。直接名・派生名のいずれにも対応付けできないトークンは `algorithm.resolved.yaml` 単独でデータフローを追跡できないため `Plan fail` とする。
- `step_kind` は `boundary_apply` / `reconstruct` / `flux_compute` / `source_term` / `time_integrate` / `column_process` / `pointwise_process` / `iterative_solve` / `filter` / `reduction` / `diagnostic` のみを許可する。
- `algorithm.resolved.yaml` は `Generate` の canonical source 入力であり、`Generate` が `controlled_spec.md` を直接読まなくても実装可能な情報を保持しなければならない。
- `algorithm.summary.md` を自動生成し、閲覧専用 artifact として保存する。
- ユーザーが言語を明示しない場合は `target.class=cpu` で `fortran`、`target.class=gpu` で `cuda_fortran` を採用する。
- `toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`toolchain.build_system` は `make` / `cmake` / `meson` / `ninja` から選択し、未指定時は `make` を採用する。
- `target.class=cpu` でループ並列化方式の指定がない場合、並列化可能ループへ `OpenMP` を既定適用として記録する。
- `dependency.resolved.yaml` は `node_key` と `direct_deps` と `transitive_deps` と `topo_level` を必須記録する。
- `derived_contract.json` は `Plan verify substep` が導出して保存する検証契約とし、`Plan generate substep` は生成してはならない。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` では、`algorithm.resolved.yaml` に状態更新対象と更新順序を必須保持しなければならない。
- `algorithm.resolved.yaml` の多次元 `problem` 向け契約は `state_variables[].name` と `state_variables[].shape_expr` と `required_update_paths` と `diagnostics_from_state=true` と `fallback_policy=fail_closed` を必須保持する。
- 上位 `node` の `Plan` は、直下依存 `node` の `plan_ref` と `plan_meta.json.verification_status` を確認し、`direct dependency plan readiness` を満たさない場合は開始してはならない。
- 上記の生成契約を導出できない場合は `Plan fail` とし、不完全な契約で `Generate` へ進めてはならない。
- 未登録依存、未実装依存、互換性違反依存は解決エラーにし、該当 `node` を `blocked` にする。
- `Plan` 完了前の validator invocation は `run-gate` を原則とする。`check_artifact_syntax.py` と `validate_workspace_root.py` は read-only 検査かつ gate 非依存検査に限り直接実行を許可する。
- `Plan generate substep` 完了前に `python3 tools/check_artifact_syntax.py --expect-top object` を実行し、`case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` が標準 parser で復元可能な mapping / object であることを確認しなければならない。

## 運用ルール
0. `.json` artifact（`plan_meta.json` 等）の書き込みは `guarded-apply-patch` を唯一の経路とする。Bash 変数代入・heredoc リダイレクト・`python3 -c` によるインライン書き込みは `output_manifest_write_guard` / `forbid_python_inline_write` でブロックされる。`.yaml` / `.md` artifact は `output_manifests/<agent_run_id>.json` の `allowed_file_tool_paths` に列挙された path に限り `Edit` / `Write` tool で直接書き込む。書き込み前に `allowed_output_paths` を確認し、`workspace/` で始まるプロジェクトルート相対パスのみを使用すること。`plan_meta.json` を含むすべての出力 path は必ず `workspace/plans/<node_key_safe>/<plan_id>/...` 形式で指定すること。`plans/` 等の `workspace/` 接頭辞を欠くパスは `output_manifest_write_guard` でブロックされ `unauthorized_write_violation` として記録される。
1. `plan_id` を `<slug>_<date>_<seq3>` 形式で発行する。`slug` は `spec_id` 由来の短い可読 token、`date` は `YYYYMMDD`、`seq3` は同日内 3 桁連番とする。
2. 出力先は `workspace/plans/<node_key_safe>/<plan_id>/` に固定する。
3. workflow artifact の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。
4. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
5. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行してよい。`run-gate --gate validate_workspace_root` が利用可能な実行環境では `run-gate` を優先し、`fail` 時は `Plan fail` とする。
6. `plan_meta.json` に `attempt_count` と `verification_status` と `last_fail_reason` と `debug_mode` と `context_isolated` を記録する。
7. `context_isolated=false` の場合、`plan_meta.json.constraint_reason` を必須記録する。
8. `debug_mode=false` では失敗試行 artifact を保存しない。
9. 完了前に `python3 tools/check_artifact_syntax.py --expect-top object` を対象 resolved artifact へ実行してよい。`run-gate` 経由の同等検査が利用可能な実行環境では `run-gate` を優先し、`fail` 時は `Plan fail` とする。

## 判定基準
- 同一入力で再生成したとき、`case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` が一致する。
- `dependency.resolved.yaml` の `topo_level` が循環依存を含まない。
- 出力が `docs/workflow/WORKFLOW_CORE.md` と `docs/workflow/phases/phase_01_plan.md` と `docs/RUNBOOK.md` の契約に整合する。
