# Phase contract: Plan


### 1. Plan
- execution input: `controlled_spec.md`、`tests.md`、`deps.yaml`、`spec/registry/spec_catalog.yaml`
- verification input: `controlled_spec.md`、`tests.md`、`deps.yaml`、`spec/registry/spec_catalog.yaml`
- 出力: `case.resolved.yaml`、`algorithm.resolved.yaml`、`impl.resolved.yaml`、`dependency.resolved.yaml`、`derived_contract.json`、`algorithm.summary.md`、`plan_meta.json`

#### 1-1. generate substep
- `generate substep` は `case.resolved.yaml`、`algorithm.resolved.yaml`、`impl.resolved.yaml`、`dependency.resolved.yaml`、`algorithm.summary.md` を生成する。
- `Controlled Spec` から物理アルゴリズム（A）を読み、`tests` から入力条件と `sweep` / `refinement` を決定的に展開する。
- `case.resolved.yaml` は実行時入力の決定値のみを保持し、検証 output contract を保持してはならない。
- `case.resolved.yaml` は演算構成、依存 `operation` 呼び出し順序、条件分岐、反復条件を保持してはならない。
- `Plan` は `controlled_spec.md` と `deps.yaml` と `profile` 解決結果から `algorithm contract` を導出し、`algorithm.resolved.yaml` として保存する。
- `algorithm.resolved.yaml` は `Generate` の canonical source 入力であり、`Generate` は元の `controlled_spec.md` を直接読んではならない。
- `algorithm.resolved.yaml` は `YAML` mapping artifact とし、`JSON` 文字列として検証してはならない。
- `algorithm.resolved.yaml` は `algorithm_id` と `execution_mode` と `steps[]` と `ordering` と `control_condition` と `iteration_contract` と `update_semantics` と `temporaries` と `derived_field_rules` と `invariants` と `splitting_policy` を必須保持しなければならない。
- `Plan` 完了前に `python3 tools/check_artifact_syntax.py --expect-top object` を用いて `case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` と `derived_contract.json` と `plan_meta.json` の構文妥当性を検査し、`fail` 時は `Plan fail` としなければならない。
- `Plan verify` 完了前に `python3 tools/validate_pipeline_semantics.py --stage plan --plan-ref workspace/plans/<node_key_safe>/<plan_id>/` を実行し、`exit code 0` を必須としなければならない。`fail` 時は `plan_meta.json` の `verification_status=pass` を付与してはならない。
- `plan_meta.json` の必須 key は `attempt_count`、`verification_status`、`last_fail_reason`、`debug_mode`、`context_isolated` とする。
- `context_isolated=false` の場合、`plan_meta.json.constraint_reason` を必須とする。
- `plan_id` は `<slug>_<date>_<seq3>` 形式を必須とし、`slug` は `spec_id` 由来の短い可読 token、`date` は `YYYYMMDD`、`seq3` は同日内 3 桁連番とする。
- `ordering` は `step_id` の列、または `before` / `after` を持つ dependency object の列として表現しなければならない。
- `control_condition` は文字列、文字列配列、または object のいずれかで表現しなければならない。
- `iteration_contract` は object とし、`execution_mode=iterative` の場合は空 object を禁止する。
- `temporaries` は文字列配列、または `name` と任意の `shape_expr` を持つ object 配列として表現しなければならない。
- `invariants` は非空文字列配列としなければならない。
- `splitting_policy` は `kind` を持つ object としなければならない。
- `execution_mode` は `sequence` / `conditional` / `iterative` / `columnwise` のみを許可する。
- `steps[]` の各要素は `step_id` と `step_kind` と `operation_ref` と `inputs` と `outputs` を必須保持しなければならない。
- `step_kind` は `boundary_apply` / `reconstruct` / `flux_compute` / `source_term` / `time_integrate` / `column_process` / `pointwise_process` / `iterative_solve` / `filter` / `reduction` / `diagnostic` のみを許可する。
- `algorithm.resolved.yaml` は `problem` の統合順序と `profile` が選択した `component` 群の拘束を分離表現しなければならない。
- `algorithm.resolved.yaml` は力学、`microphysics`、`radiation`、`land_surface`、`turbulence` を含む気象計算一般で使用できる表現力を持たなければならない。
- `algorithm.summary.md` は `algorithm.resolved.yaml` から自動生成する閲覧専用 artifact とし、`plan_id` 算出や下流 phase input に含めてはならない。
- 実行アルゴリズム（B）を決定し、`impl.resolved.yaml` に `target.backend`、`target.architecture`、`toolchain.language`、`toolchain.build_system` を固定する。
- `toolchain.language` と `toolchain.build_system` の既定値規則、および既定値逸脱条件は `IMPL_PLAN_SPEC.md` を適用する。
- `Phase 1` は固定値を許可する。`Phase 2` 以降は `Tune` で探索可能とする。
- `deps.yaml` と `spec_catalog.yaml` から依存 `DAG` を生成し、`dependency.resolved.yaml` として `Plan` 段階で固定する。
- `dependency.resolved.yaml` は `node_key`、`direct_deps`、`transitive_deps`、`topo_level` を必須記録とする。
- `dependency.resolved.yaml` は起点 `node` と推移依存 `node` の閉包を過不足なく 1 回ずつ保持し、`node_key` の重複と欠落を禁止する。
- `workflow` の `node` 実行順序は `spec` の canonical source である `deps.yaml` と `spec_catalog.yaml` から再構成した依存関係に基づいて決定しなければならない。
- `dependency.resolved.yaml` は依存解決結果の記録と整合性検証に使用する artifact とし、`workflow` の実行順序決定の canonical source にしてはならない。
- 依存関係上は独立な `node` の並列実行は、workflow 入力または orchestration 指示で明示的に許可された場合にのみ開始してよい。許可がない場合は `topo_level` が同一でも逐次実行しなければならない。
- 依存を持つ `node` は、`deps.yaml` と `spec_catalog.yaml` から再構成した直下依存 `node` の `direct dependency plan readiness` を満たす場合にのみ `Plan` を開始してよい。
- 依存を持つ `node` は、`deps.yaml` と `spec_catalog.yaml` から再構成した直下依存 `node` の `direct dependency execution readiness` を満たす場合にのみ `Generate -> Build -> Execute -> Judge` を開始してよい。
- `component` / `profile` / `problem` の実行順序は `spec_kind` 固定で判定してはならず、`spec` 依存関係に基づいて判定しなければならない。
- `dependency` 不充足の `node` は `blocked` とし、推測補完で起動してはならない。
- 未登録依存、未実装依存、互換性違反依存を `dependency` 解決エラーとする。

#### 1-2. verify substep
- `verify substep` は `controlled_spec.md` と `tests.md` と `deps.yaml` から導出した検証契約を `derived_contract.json` として保存する。
- `verify substep` は `algorithm.resolved.yaml` の演算構成と `derived_contract.json` の検証契約を混在させてはならない。
- `derived_contract.json` は `io_contract.inputs` と `io_contract.outputs` を必須保持し、`io_contract.outputs` は `name` と `evidence_ref` と `shape_expr` で判定対象出力の一次証跡参照を定義しなければならない。
- `io_contract.outputs[].evidence_ref` が `raw/state_snapshots` を参照する場合、`raw_variables` を非空配列で必須記録し、各要素は `raw_requirements.required_evidence[].schema.variables[].name` または `time_variable` を参照しなければならない。
- `io_contract.outputs[].evidence_ref` が `raw/state_snapshots` を参照し、`raw_variables` が単一の `state variable` または `time_variable` を指す場合、`io_contract.outputs[].shape_expr` は参照先 schema の `shape_expr` と一致しなければならない。
- `io_contract.outputs` で `evidence_ref` が `raw/state_snapshots` 以外を参照し、かつ `raw_requirements.required_evidence` で `artifact=state_snapshots` を必須宣言する場合、当該 `output` は `raw_variables`（非空配列）で再計算に必要な `raw/state_snapshots` 変数名を明示しなければならない。
- `derived_contract.json` は `raw_requirements.required_evidence` を必須保持し、`artifact` と `required` と `min_samples` と `schema`（必要時）で `raw` 一次証跡の必須構成を定義しなければならない。
- `derived_contract.json` は `test_evidence_requirements` を保持し、`tests.md` の各 `test_id` ごとに `required_raw_variables` を明示しなければならない。
- `test_evidence_requirements[].required_raw_variables` は、`artifact=state_snapshots` を参照する場合に `schema.variables[].name` または `time_variable` のみを許可する。未定義語彙は `fail` とする。
- `semantic_dependency.required_sources` は非空文字列の配列を canonical form とする。object 配列は互換入力としてのみ許可し、生成側は出力してはならない。
- `derived_contract.json` は生成契約を保持してはならない。`numerical_kernel_contract`、統合順序、更新段数、反復条件は `algorithm.resolved.yaml` 側へ保持しなければならない。
- `verify substep` は `dependency.resolved.yaml` の整合性検証を必須責務として実行しなければならない。
- `deps.yaml` と `spec_catalog.yaml` から再構成した `expected_node_set` と `dependency.resolved.yaml` の `node_key` 集合一致を `Plan pass` 条件とする。
- `verify substep` は `dependency.resolved.yaml` の各依存辺について、対象 `node` の `controlled_spec.md` と依存先 `node` の `controlled_spec.md` と `deps.yaml` と照合し、依存方向、依存種別、公開 `operation` 参照、`profile` 選択拘束が矛盾しないことを検証しなければならない。
- 依存先 `node` に既存 `plan` が存在する場合、`verify substep` は依存先 `node` の `case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` と `plan_meta.json` を照合し、`dependency.resolved.yaml` が依存先 `plan` 文書と矛盾しないことを検証しなければならない。
- `verify substep` は `dependency.resolved.yaml` が `deps.yaml` の転記に留まらず、`spec` 文書および依存先 `node` の `plan` 文書と整合する解決結果であることを検証しなければならない。
- 前項の照合で矛盾、欠落、未解決参照、依存先 `node` の公開契約不一致を検出した場合、`Plan fail` としなければならない。

