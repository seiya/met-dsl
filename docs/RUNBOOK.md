# Runbook（試行を回す最小手順）

この文書は「試行を回すための最小運用手順」を定義する。運用知見に応じて更新する。

## 0. 目的
- `spec` の `Controlled Spec`（物理定義）と `tests`（検証プロファイル）から実行と判定を行い、物理妥当性と性能を評価する。
- 失敗の原因を**Spec / Plan / Generate / Execute / Judge / Tune / Promote**のどこにあるか切り分ける。

## 1. 入力と artifact（最小）
- 入力: `CONTROLLED_SPEC`（物理・アルゴリズム定義）
- 入力: `tests`（自然言語中心のケース展開・実行条件・判定閾値）
- 生成: `case.resolved.yaml`（物理アルゴリズム A の固定）
- 生成: `algorithm.resolved.yaml`（生成契約の固定）
- 生成: `impl.resolved.yaml`（実行アルゴリズム B の固定または探索候補）
- 生成: `dependency.resolved.yaml`（依存 `DAG` と `topo_level` の固定）
- 生成: `derived_contract.json`（`controlled_spec.md` と `tests.md` と `deps.yaml` から導出した検証契約）
- 生成: `model`（物理計算モジュール）と `runner`（実行・判定連携）
- 出力: `diagnostics.json`,`perf.json`,`verdict.json`,`aggregate_verdict.json`,`summary.json`,`semantic_review.json`
- 禁止: `dummy` 出力、`dummy` データ、`dummy` 計算、workflow 進行目的の人工 artifact generation

## 1-1. artifact layout（運用必須）
- `Plan` は `workspace/plans/<node_key_safe>/<plan_id>/` に保存する。
- `Generate` / `Build` / `Execute` は `workspace/pipelines/<node_key_safe>/<pipeline_id>/` に保存する。
- 各 `pipeline` には `lineage.json` を必須配置する。
- `execution` artifact は `workspace/pipelines/<node_key_safe>/<pipeline_id>/execute/<execution_id>/<node_key>/` に保存する。
- 判定時は `execution_id` 単位で読み込む。`execution_id` を跨ぐファイル混在を禁止する。
- 判定時は `node_key` 単位で `verdict` / `aggregate_verdict` / `summary` を分離して読み込む。
- 正式版 artifact は `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` に保存する。`workspace` は試行用途に限定する。

## 1-2. 逸脱防止ゲート（運用必須）
- workflow 共通の不変規範（不正防止、過去 artifact 参照禁止、検証契約導出、`workspace/` ルート制約、`quality check` 判定軸）は `WORKFLOW.md` を canonical source とする。
- 全体方針と `spec` 管理要件（`spec_kind` / registry / 正式版配置 / 命名規則）は `SPEC.md` を canonical source とする。
- `spec_kind` を問わない workflow 実行は、各ステージを `LLM` により実行し、リポジトリ管理外パス（例: `/tmp`）の補助スクリプトを実行経路へ含めてはならない。
- `workflow` 実行の代替として、ステージ処理と artifact generation を一括代行する `script`（例: `python` / `bash`）を新規生成または実行してはならない。
- 各ステージ開始前に `write_scope_baseline` を取得し、各ステージ完了前に `workspace/` 配下以外の差分を検出する `write_scope` 検査を必須実行する。
- `python` 実行を workflow 経路で使用する場合、`__pycache__` を `workspace/` 配下へ限定する。`PYTHONDONTWRITEBYTECODE=1` または `PYTHONPYCACHEPREFIX=workspace/.pycache/<pipeline_id>/` を必須適用する。
- `write_scope` 検査で `workspace/` 配下以外の差分を検出した場合、当該ステージを `fail` とし、`write_scope_violation.json` を `workspace/` 配下へ記録する。
- `Generate verify` のデータ依存判定は `derived_contract.json` の `semantic_dependency.required_sources` を canonical source とし、特定計算様式の一律必須化を禁止する。
- `Generate verify` の output contract 判定は `derived_contract.json` の `io_contract.outputs` を canonical source とし、`evidence_ref` と `shape_expr` の整合を必須検査する。
- 出力形式、input/output contract、判定条件の要求定義は `controlled_spec.md` と `tests.md` と `deps.yaml` と `algorithm.resolved.yaml` と `derived_contract.json` と `docs/` canonical source から取得し、`tools/` 配下の検証 `python` スクリプトと `quality check` 実装を要求定義入力へ使用してはならない。
- 要求定義の不足を検証実装から逆算補完してはならない。不足時は当該ステージを `fail` とする。
- `Judge` は固定スクリプト検査に加えて `LLM` 意味検査を必須実行し、`semantic_review.json` の `decision=pass` を開始条件に含める。
- `Judge` 開始前に、対象 `node_key` の同一 `execution_id` 配下へ `run_program` 実行記録と `diagnostics.json` と `perf.json` と `raw` 実行証跡が揃っていることを検証する。未達時は `Judge fail` とする。
- `Judge` 開始前と `Judge` 完了前に `python3 tools/validate_pipeline_semantics.py` を実行し、`fail` 時は当該 `pipeline` を `invalid` とする。
- `python3 tools/validate_pipeline_semantics.py` は `--allow-missing-orchestration` と `--allow-missing-llm-review` を指定せずに実行しなければならない。互換移行を明示した例外運用以外で当該オプションを指定した試行は `fail` とする。
- `Judge` 開始前の `python3 tools/validate_pipeline_semantics.py` は、対象 `dependency.resolved.yaml` の `all_nodes` に対応する全 `pipeline_root` を指定して実行する。起点 `problem` の単独 `pipeline_root` 指定を禁止する。
- `python3 tools/validate_pipeline_semantics.py` が `all_nodes` の未発行 `plan` または未発行 `pipeline` を検出した場合、`Judge` を開始してはならない。
- `trial_meta.json` は `generated_by_stage` と `source_execution_id` と `source_command_ref` と `source_artifact_hash` を必須記録とし、欠落または不整合時は `fail` とする。
- 本節の検証に違反した試行は当該ステージで停止し、下流ステージ開始条件を満たす目的の人工 artifact generation を禁止する。

## 1-3. エージェント起動規約（運用必須）
- `workflow` 実行は `orchestration agent` を起点に開始し、`orchestration_id` を必須発行する。
- `workflow` 開始前に、`step agent` と `substep agent` の独立起動可否を検証する `preflight` を実行し、`pass` でない場合は開始してはならない。
- `Codex CLI` を execution platform に使う場合、`preflight.json` と `launches/` と `agent_runs.jsonl` と `step_result.json` の保存は `tools/codex_orchestration_runtime.py` を canonical source 実装として行う。
- `Codex CLI` を execution platform に使う場合、`preflight` は `python3 tools/codex_orchestration_runtime.py preflight --repo-root <repo_root> --orchestration-id <orchestration_id>` で生成しなければならない。
- 標準 `substep` を持たない各 `step` は `step agent` を独立起動して実行する。
- `substep` を持つ各 phase は `orchestration agent` が各 `substep` の `substep agent` を独立起動して実行する。
- 各 `step` / `substep` の実処理を `script` で代行してはならない。必ず独立 `agent_run_id` を持つ `LLM agent` で実行する。
- `step agent` と `substep agent` は `agent_run_id` ごとに固有 `context_id` を持ち、`context_isolated=true` を必須記録とする。
- `step` / `substep` の起動要求と起動応答は `workspace/orchestrations/<orchestration_id>/launches/` 配下へ保存し、`agent_runs.jsonl` の `launch_request_ref` と `launch_response_ref` から追跡可能にする。
- `orchestration agent` は `dependency.resolved.yaml` の `topo_level` と依存充足状態に基づいて起動順序を逐次決定する。
- `substep` を持つ phase では `orchestration agent` が対象 `step` の `SKILL` を適用して `substep` を直接起動し、`step_result.json` を返却する。
- `orchestration` の実行記録は `workspace/orchestrations/<orchestration_id>/` に保存し、`orchestration_meta.json` と `agent_graph.json` と `agent_runs.jsonl` を必須とする。
- `step_result.json` は `workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/` に保存する。
- `step_result.json` は `executor_agent_run_id` と `substep_agent_run_ids` を必須記録し、`executor_agent_run_id` は保存先 `agent_run_id` と一致させる。`substep` を持たない phase の `substep_agent_run_ids` は空配列を許可する。
- `step agent` または `substep agent` の `fail` / `timeout` / `cancel` 発生時は、当該 `step` を停止し推測補完を禁止する。

## 2. 最小ループ
1. **Spec 更新**: `Controlled Spec` を修正し、曖昧さ・欠落を解消する。
2. **Test 更新**: 実験条件・判定条件を `tests.md` で更新する。
3. **Plan 生成**: `case.resolved.yaml` を決定的に生成し、`controlled_spec.md` と `deps.yaml` と依存解決結果から `algorithm.resolved.yaml` を導出し、`controlled_spec.md` と `tests.md` と `deps.yaml` から `derived_contract.json` を導出する。`algorithm.resolved.yaml` は `execution_mode` と `steps[]` と `ordering` と `control_condition` と `iteration_contract` と `update_semantics` と `temporaries` と `derived_field_rules` と `invariants` と `splitting_policy` を保持し、`Generate` の canonical source 入力として使用する。`derived_contract.json` は `tests.md` の全 `test_id` を対象に `test_evidence_requirements` を定義し、各判定再計算に必要な `required_raw_variables` を明示する。`artifact=state_snapshots` を必須宣言する場合は `raw_requirements.required_evidence[].schema` に `variables[].name` と `variables[].shape_expr` と `time_variable` と `time_shape_expr` を必須記録する。`LLM` を利用する場合は `SPEC.md` の「`LLM` の扱い」を適用する。
4. **依存解決 Plan 生成**: `deps.yaml` と `spec_catalog.yaml` から `dependency.resolved.yaml` を生成し、`node_key`、`direct_deps`、`transitive_deps`、`topo_level` を固定する。`deps.yaml` と `spec_catalog.yaml` から再構成した `expected_node_set` と `dependency.resolved.yaml` の `node_key` 集合一致を確認し、重複と欠落を `fail` とする。
5. **実装 Plan 決定**: `impl.resolved.yaml` を固定（探索する場合は候補集合を用意）する。言語既定値、`OpenMP` 既定値、既定値逸脱条件は `IMPL_PLAN_SPEC.md` を適用する。
6. **階層実行順序の固定**: `dependency.resolved.yaml` の `topo_level` 昇順で実行順序を固定する。親 `node` の `Plan` は直下依存 `node` が `direct dependency plan readiness` を満たすまで開始してはならない。親 `node` の `Generate` 以降は直下依存 `node` が `direct dependency execution readiness` を満たすまで開始してはならない。同一 `topo_level` の独立 `node` も 1 件ずつ逐次実行する。
7. **`node` 単位 workflow 発行**: `orchestration agent` が各 `node_key` ごとに個別 `plan_id` と個別 `pipeline_id` を発行する。`substep` を持つ phase では `substep agent` を直接起動し、標準 `substep` を持たない phase では `step agent` を起動する。上位 `node` の `Plan` 起動前に、直下依存 `node` ごとの `plan_ref` と `plan_meta.json.verification_status` を確認し、`direct dependency plan readiness` 不成立なら当該 `node` を起動してはならない。上位 `node` の `Generate` 以降の起動前に、直下依存 `node` ごとの `plan_ref` と `pipeline_ref` と `aggregate_verdict` を確認し、`direct dependency execution readiness` 不成立なら当該 `node` を起動してはならない。
8. **生成**: 対象 `node` ごとに `LLM` またはテンプレ補完で `model` と `runner` を分離して生成する。`LLM` を利用する場合は `SPEC.md` の「`LLM` の扱い」を適用する。`Generate` は `controlled_spec.md` を直接入力にしてはならず、演算構成の要求定義は `algorithm.resolved.yaml` から解釈する。生成直後に `runner` の外部インタプリタ起動禁止と、`model` の `no-op` / 固定値返却専用実装禁止を検査し、違反時は `Generate fail` とする。`Generate verify` は `case.resolved.yaml` と `algorithm.resolved.yaml` と `derived_contract.json` と `impl.resolved.yaml` と `dependency.resolved.yaml` に基づき、`test case set` 網羅、実行時入力の伝播、演算構成、`update_semantics` と `derived_field_rules` と `invariants` の反映、依存 `operation`、出力指標のデータ依存、`impl` の target / toolchain / knob 反映、未宣言依存参照の非混入、および解析式直接代入による `diagnostics` 生成を検査する。依存を持つ `node` は、`dependency.resolved.yaml` の `direct_deps` で解決された依存 `node` の公開 `operation` を呼び出す実装を必須とし、同等機能の再実装を禁止する。依存 `node` の workflow 未完了、`plan` / `pipeline` 未発行、または `aggregate_verdict` 未充足を検出した場合、依存先 code を依存元 `src/` に内包して補完してはならず、`blocked` または `Generate fail` とする。`toolchain.language=fortran` で依存 `component` を持つ `node` は、依存 `spec_id` ごとに `model` 内の `use <spec_id>_model` と `call <spec_id>__*` を必須とし、`subroutine <spec_id>__*` の再定義を禁止する。`toolchain.language=fortran` の場合は `module` 名とソースファイル名を一致させ、`<module_name>.f90` 形式で出力する。`toolchain.build_system=make` かつ `toolchain.language=fortran` の場合は `src/Makefile` に `use` 依存に対応した `.mod` または依存 `.o` の前提条件を各オブジェクトターゲットへ明示し、依存欠落を禁止する。
9. **Build**: 対象 `node` ごとに `MCP` サーバーの `compile_project` で依存関係を扱える標準ビルドツールを実行する（`fortran` / `c` 系の既定値は `make`）。依存を持つ `node` は、依存 `operation` の解決先が `dependency.resolved.yaml` と一致することを検証し、不一致時は `Build fail` とする。`toolchain.build_system=make` の場合は `make -j` で成否が変化しない依存記述を必須とする。
10. **実行**: 対象 `node` ごとに `MCP` サーバーの `run_program` で `runner`（例: `simulate`）を実行し、`run_program` 実行コマンドに `case.resolved.yaml` を必ず含める。`runner` 経由で `model` を呼び出して `diagnostics` / `perf` を出力し、`verdict.json` と `aggregate_verdict.json` と `summary.json` と `trial_meta.json` の直接出力を禁止する。対象 `node` ごとに `execution_id/<node_key>/raw/` へ判定再計算用の一次証跡を保存する。`raw` 構成の必須条件は `derived_contract.json` の `raw_requirements.required_evidence` と `test_evidence_requirements` を canonical source とする。`artifact=state_snapshots` を必須宣言しない `spec` では状態スナップショットを必須化してはならない。`raw` へ `diagnostics` の複写を保存してはならない。
11. **実行証跡検証**: `python3 tools/validate_pipeline_semantics.py` を実行し、`raw` の一次証跡、`quality check`、`trial_meta` の追跡情報、`Generate` 由来の固定値生成パターンを検証する。`derived_contract.json` が宣言する `state_snapshots` の変数名と形状式、および `time_variable` の形状式と `test_evidence_requirements` の整合を必須検証する。検証対象は `dependency.resolved.yaml` の `all_nodes` に対応する全 `pipeline_root` とし、`all_nodes` の未発行 `plan` または未発行 `pipeline` を検出した場合を含めて `fail` の場合は `Judge` を開始しない。
12. **品質比較**: `target.class=cpu` の場合、対象 `node` ごとに `quality check` として `threads_per_rank=1` と `threads_per_rank>1` の execution result を比較する。比較対象は `diagnostics.json` と `verdict.json` とし、合否確定規則は `WORKFLOW.md` を適用する。`run_quality_checks` は `preset` 指定のみを許可し、任意 `command` と `quality_check.py` 直接実行を禁止する。
13. **判定**: `tests.md` の規則に基づく判定を対象 `node` ごとに実施し、`verdict` を生成する。依存込み判定は `aggregate_verdict.json` へ出力する。直下依存 `node` が `fail` または `blocked` の場合、上位 `node` は `blocked` として終了する。この場合も `aggregate_verdict.json` と `summary.json` と `trial_meta.json` を必須出力し、`blocked_reason` と `blocking_direct_deps` を記録する。`verdict.json` は `self_verdict=not_evaluated` を明示する。`verdict.json` は `per_test` へ `tests.md` の全 `test_id` を重複なく記録し、`summary.json.counts` は `per_test` 集計と一致させる。`Judge` は `raw` 一次証跡のみを入力として判定指標を再計算し、`diagnostics` と一致しない場合は `Judge fail` とする。固定スクリプト検査に加えて `LLM` 意味検査を実施し、`semantic_review.json` の `decision=pass` を必須条件にする。
14. **強制停止**: 入力不足または前段 artifact 不足で当該 phase を進められない場合、当該 phase を `fail` で停止する。推定補完や人工ファイル生成で進めてはならない。
15. **記録**: `spec_version` / `test_profile_version` / `case_hash` / `impl_hash` / `git_sha` を保存する。
- `plan_id` / `pipeline_id` / `generation_id` / `build_id` / `execution_id` を保存する。
- `plan_id` は `case_hash12` と `algorithm_hash12` と `impl_hash12` を識別できる形式にする。
- `node_key` / `topo_level` / `dependency_ref` を保存する。
- `LLM` 利用ステージは各ステージの `<stage>_meta.json`（コード生成は `generate_meta.json`）に `attempt_count` / `verification_status` / `last_fail_reason` / `debug_mode` を保存する。
- `step` / `substep` の `agent_runs.jsonl` は `agent_backend` / `agent_model` / `context_id` / `context_isolated=true` を記録する。
- `debug_mode=true` で失敗試行を保存した場合は保存件数と保存先を記録する。
- `dependency.resolved.yaml` の全 `node_key` について、`workspace/plans` と `workspace/pipelines` の対応が 1 対 1 で成立することを保存前に検証する。
16. **チューニング**: 物理合格を満たす候補の中から性能目的関数で最良候補を選定し、採用する `impl.resolved` を確定する。
17. **正式版昇格**: 採用する試行は `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` へ昇格保存し、`spec/registry/spec_catalog.yaml` の `official_releases` に `release_id` / `target_architecture` / `toolchain_language` / `target_backend` / `source_pipeline_id` / `source_generation_id` / `source_build_id` / `source_execution_id` / `artifact_root` / `promoted_at` / `status` を記録する。`problem` の昇格は `aggregate_verdict.overall=pass` を必須とする。
18. **次アクション**: 失敗 classification に応じて戻る場所を決める。

## 3. 失敗時の戻り先（指針）
- **`LLM` ステージ実行不能**: workflow の各ステージで `LLM` による実行ができない -> input contract または `MCP` 接続定義へ戻る。手動 `copy` 運用を禁止する。
- **Spec 不備**: 曖昧・欠落・単位不整合 -> `Spec` へ戻る。
- **Test 不備**: ケース展開・閾値・実行条件の矛盾 -> `tests` へ戻る。
- **Dependency 解決 fail**: 未登録依存、未実装依存、互換性違反 -> `deps.yaml` / `spec_catalog.yaml` へ戻る。
- **Dependency block**: 下層 `node` が `fail` のため上層が `blocked` -> 下層 `node` へ戻る。
- **上位 workflow fail（依存起因）**: 直下依存 `node` の `fail` / `blocked` により上位 `node` を終了 -> 下層 `node` へ戻る。
- **LLM ステージ検証 fail**: `LLM` 利用ステージの出力が input contract と不一致 -> 当該ステージへ戻る（必要に応じて `Spec` / `Test` へ戻る）。
- **物理 fail**: A の選択ミス、境界実装の矛盾 -> `Controlled Spec` または `case` へ戻る。
- **実装 fail**: 生成ミス、未対応ノブ -> `Generate` / `impl` へ戻る。
- **依存統合 fail**: 依存 `operation` 呼び出し欠落、依存 `operation` の再実装、依存解決先不一致 -> `Generate` または `Build` へ戻る。
- **依存 Plan 未完了**: 直下依存 `node` の `plan` 未発行、または `plan_meta.json.verification_status!=pass` のまま上位 `node` の `Plan` を開始 -> `Orchestration` または下層 `node` へ戻る。
- **依存 workflow 未実行**: 直下依存 `node` の `plan` / `pipeline` 未発行、または `aggregate_verdict` 未充足のまま上位 `node` の `Generate` 以降を開始 -> `Orchestration` または下層 `node` へ戻る。
- **不正生成 fail**: `dummy` 出力、人工データ作成、根拠なき判定結果固定 -> 当該 phase を破棄し `Spec` / phase input定義へ戻る。
- **性能未達**: B の探索不足 -> `impl` 探索へ戻る。
- **再現性崩れ**: determinismの破壊 -> `Plan` / 実行環境へ戻る。

## 4. 運用の最小チェックリスト
- `Spec` に未定義項目がない。
- `case.resolved` が決定的に生成できる。
- `algorithm.resolved.yaml` が `Generate` に必要な演算構成を単独で保持している。
- `derived_contract.json` が `controlled_spec.md` と `tests.md` と `deps.yaml` から導出されている。
- `derived_contract.json` が生成契約を保持していない。
- `derived_contract.json` が `io_contract.inputs` と `io_contract.outputs` を保持し、`outputs` の `evidence_ref` が `raw` 実体に解決できる。
- `derived_contract.json` の `test_evidence_requirements` が `tests.md` の全 `test_id` を過不足なく保持している。
- `derived_contract.json` の `raw_requirements.required_evidence` で `artifact=state_snapshots` を必須宣言する場合、`schema.variables[].name` と `schema.variables[].shape_expr` と `schema.time_variable` と `schema.time_shape_expr` が定義されている。
- 各ステージで `write_scope_baseline` を取得し、完了前に差分比較を実施している。
- `write_scope` 検査で `workspace/` 配下以外の差分が検出されていない。
- `python` 実行時の `__pycache__` 出力先が `workspace/` 配下に限定されている。
- `derived_contract.json` の `semantic_dependency.required_sources` に基づく `Generate verify` 判定が実施されている。
- `algorithm.resolved.yaml` の `steps` と `ordering` と `control_condition` と `iteration_contract` に基づく `Generate verify` 判定が実施されている。
- `raw` の必須構成が `derived_contract.json` の `raw_requirements.required_evidence` と一致している。
- `raw/state_snapshots` の各 `snapshot*.json` が `derived_contract.json` の `schema` で宣言された変数名とサイズを満たしている。
- `LLM` 利用ステージのメタデータで `verification_status` が `pass` である。
- `debug_mode=false` の試行で失敗試行 artifact が保存されていない。
- `diagnostics` / `perf` / `verdict` が揃って出る。
- `aggregate_verdict` と `summary.dependency_summary` が `dependency.resolved` と整合する。
- `dependency.resolved` の `node_key` 集合と `workspace/plans` / `workspace/pipelines` の `node` 集合が一致する。
- `workspace/orchestrations/<orchestration_id>/` に `orchestration_meta.json` / `agent_graph.json` / `agent_runs.jsonl` が存在する。
- `step_result.json` が `workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/` に存在し、`required_outputs` と `executor_agent_run_id` と `substep_agent_run_ids` を保持している。
- 各 `step` と各 `substep` が独立 `agent_run_id` を持ち、`parent_agent_run_id` で親子関係を追跡できる。
- 各 `step` と各 `substep` の `context_id` が重複せず、全件で `context_isolated=true` が記録されている。
- `workspace/orchestrations/<orchestration_id>/preflight.json` が存在し、`can_launch_step_agents=true` と `can_launch_substep_agents=true` を満たしている。
- `agent_runs.jsonl` の `step` / `substep` ロールが `agent_session_id` と `launch_request_ref` と `launch_response_ref` を保持し、参照先ファイルが存在している。
- `spec_kind` を問わない workflow 実行で各 `node_key` の個別 `plan_id` と個別 `pipeline_id` が発行されている。
- 実行証跡から、`script` 一括実行ではなく `orchestration -> step` または `orchestration -> substep` の独立 `agent` 実行であることを確認できる。
- 明示的な指定がない試行で、既存 workflow 出力の参照または閲覧が実施されていない。
- `lineage.json` が `node` 単位で分離され、単一 `lineage` に複数 `node_key` が混在していない。
- `Judge` 入力は同一 `execution_id` の `run_program` 実行記録と `diagnostics` / `perf` に限定されている。
- 依存を持つ `node` が `dependency.resolved.yaml` で解決された依存 `operation` を呼び出している。
- 依存 `operation` と同等機能を依存元 `node` へ再実装していない。
- 上位 `node` の `generate/src/` に依存 `node` 実装本体が複製・再配置・再定義されていない。
- `toolchain.language=fortran` の依存 `component` を持つ `node` で `use <spec_id>_model` と `call <spec_id>__*` が実装され、`subroutine <spec_id>__*` の再定義がない。
- `toolchain.language=fortran` の artifact で `module` 名とソースファイル名が一致している。
- `toolchain.language=fortran` の公開 `module` / `subroutine` 名に `spec_id` 由来接頭辞が付与されている。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` の artifact で、`src/Makefile` の各オブジェクトターゲットが `use` 依存に対応した `.mod` または依存 `.o` の前提条件を明示している。
- `trial_meta.json` の `generated_by_stage` / `source_execution_id` / `source_command_ref` / `source_artifact_hash` が欠落していない。
- `trial_meta.json` の `runner_command` / `process_trace_ref` / `raw_artifact_refs` が欠落していない。
- `trial_meta.json` の `source_command_ref` が参照する `run_program` 実行コマンドに `case.resolved.yaml` が含まれている。
- `lineage.json` と `trial_meta.json` の artifact 参照パスが `workspace/` 起点で記録されている。
- `blocked` で終了した `node` に `aggregate_verdict.json` / `summary.json` / `trial_meta.json` が存在し、`blocked_reason` が記録されている。
- `spec_kind` を問わない workflow 実行の完了前に、対象 `DAG` の `plans` / `pipelines` artifact が削除されていない。
- 物理判定の根拠が追跡できる。
- 正式版昇格を実施した試行は `spec_catalog.yaml` の `official_releases` と `release` artifact layoutが一致する。
- `dummy` 出力や人工生成ファイルが存在しない。
- `runner` が `python` / `bash` / `sh` / `node` など外部インタプリタを起動していない。
- `runner` が `verdict.json` / `aggregate_verdict.json` / `summary.json` / `trial_meta.json` を書き込んでいない。
- `execution_id/<node_key>/raw/` が存在し、`Judge` 再計算に必要なファイルが揃っている。
- `raw/metrics_basis.json` が `diagnostics.json` の複写ではなく、一次証跡から構成されている。
- workflow ルート判定が `workspace/` のみに対して実施されている。
- `python3 tools/validate_workspace_root.py` が `PASS` を返している。
- `python3 tools/validate_pipeline_semantics.py` が `PASS` を返している。
- `python3 tools/validate_pipeline_semantics.py` の実行引数に `--allow-missing-orchestration` と `--allow-missing-llm-review` が含まれていない。
- `semantic_review.json` が存在し、`decision=pass` である。
- 異なる `node_key` の `generate/src` が不正に完全一致していない。
- `copy_based_artifact_reuse` が未検出である。
- `write_scope_violation.json` が未生成である。
