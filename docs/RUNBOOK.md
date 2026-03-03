# Runbook（試行を回す最小手順）

この文書は「試行を回すための最小運用手順」を定義する。運用知見に応じて更新する。

## 0. 目的
- `spec` の `Controlled Spec`（物理定義）と `tests`（検証プロファイル）から実行と判定を行い、物理妥当性と性能を評価する。
- 失敗の原因を**Spec / Plan / Generate / Execute / Judge / Tune / Promote**のどこにあるか切り分ける。

## 1. 入力と成果物（最小）
- 入力: `CONTROLLED_SPEC`（物理・アルゴリズム定義）
- 入力: `tests`（自然言語中心のケース展開・実行条件・判定閾値）
- 生成: `case.resolved.yaml`（物理アルゴリズム A の固定）
- 生成: `impl.resolved.yaml`（実行アルゴリズム B の固定または探索候補）
- 生成: `dependency.resolved.yaml`（依存 `DAG` と `topo_level` の固定）
- 生成: `derived_contract.json`（`controlled_spec.md` と `tests.md` と `deps.yaml` から導出した検証契約）
- 生成: `model`（物理計算モジュール）と `runner`（実行・判定連携）
- 出力: `diagnostics.json`,`perf.json`,`verdict.json`,`aggregate_verdict.json`,`summary.json`
- 禁止: `dummy` 出力、`dummy` データ、`dummy` 計算、workflow 進行目的の人工成果物生成

## 1-1. 成果物配置（運用必須）
- `Plan` は `workspace/plans/<node_key_safe>/<plan_id>/` に保存する。
- `Generate` / `Build` / `Execute` は `workspace/pipelines/<node_key_safe>/<pipeline_id>/` に保存する。
- 各 `pipeline` には `lineage.json` を必須配置する。
- `execution` 成果物は `workspace/pipelines/<node_key_safe>/<pipeline_id>/execute/<execution_id>/<node_key>/` に保存する。
- 判定時は `execution_id` 単位で読み込む。`execution_id` を跨ぐファイル混在を禁止する。
- 判定時は `node_key` 単位で `verdict` / `aggregate_verdict` / `summary` を分離して読み込む。
- 正式版成果物は `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` に保存する。`workspace` は試行用途に限定する。

## 1-2. 逸脱防止ゲート（運用必須）
- workflow 共通の不変規範（不正防止、過去成果物参照禁止、検証契約導出、`workspace/` ルート制約、`quality check` 判定軸）は `SPEC.md` を正本とする。
- 工程契約（`node_key` ごとの個別 workflow、依存 `DAG` 実行順、`blocked` 伝播、`copy_based_artifact_reuse` 判定）は `WORKFLOW.md` を正本とする。
- `spec_kind` を問わない workflow 実行は、各ステージを `LLM` により実行し、リポジトリ管理外パス（例: `/tmp`）の補助スクリプトを実行経路へ含めてはならない。
- `Judge` 開始前に、対象 `node_key` の同一 `execution_id` 配下へ `run_program` 実行記録と `diagnostics.json` と `perf.json` と `raw` 実行証跡が揃っていることを検証する。未達時は `Judge fail` とする。
- `Judge` 開始前と `Judge` 完了前に `python3 tools/validate_pipeline_semantics.py` を実行し、`fail` 時は当該 `pipeline` を `invalid` とする。
- `trial_meta.json` は `generated_by_stage` と `source_execution_id` と `source_command_ref` と `source_artifact_hash` を必須記録とし、欠落または不整合時は `fail` とする。
- 本節の検証に違反した試行は当該ステージで停止し、下流ステージ開始条件を満たす目的の人工成果物生成を禁止する。

## 2. 最小ループ
1. **Spec 更新**: `Controlled Spec` を修正し、曖昧さ・欠落を解消する。
2. **Test 更新**: 実験条件・判定条件を `tests.md` で更新する。
3. **Plan 生成**: `case.resolved.yaml` を決定的に生成し、`controlled_spec.md` と `tests.md` と `deps.yaml` から `derived_contract.json` を導出する。`LLM` を利用する場合は `SPEC.md` の「`LLM` の扱い」を適用する。
4. **依存解決 Plan 生成**: `deps.yaml` と `spec_catalog.yaml` から `dependency.resolved.yaml` を生成し、`node_key`、`direct_deps`、`transitive_deps`、`topo_level` を固定する。`deps.yaml` と `spec_catalog.yaml` から再構成した `expected_node_set` と `dependency.resolved.yaml` の `node_key` 集合一致を確認し、重複と欠落を `fail` とする。
5. **実装 Plan 決定**: `impl.resolved.yaml` を固定（探索する場合は候補集合を用意）する。言語既定値、`OpenMP` 既定値、既定値逸脱条件は `IMPL_PLAN_SPEC.md` を適用する。
6. **階層実行順序の固定**: `dependency.resolved.yaml` の `topo_level` 昇順で実行順序を固定する。親 `node` は直下依存 `node` が `pass` または `xfail` になるまで開始してはならない。同一 `topo_level` の独立 `node` は並列実行してよいが、ある `node` が `fail` しても同一 `topo_level` の独立 `node` の実行を中断しない。
7. **`node` 単位 workflow 発行**: 各 `node_key` ごとに個別 `plan_id` と個別 `pipeline_id` を発行する。同一 `topo_level` の独立 `node` は並列実行してよい。
8. **生成**: 対象 `node` ごとに `LLM` またはテンプレ補完で `model` と `runner` を分離して生成する。`LLM` を利用する場合は `SPEC.md` の「`LLM` の扱い」を適用する。生成直後に `runner` の外部インタプリタ起動禁止と、`model` の `no-op` / 固定値返却専用実装禁止を検査し、違反時は `Generate fail` とする。依存を持つ `node` は、`dependency.resolved.yaml` の `direct_deps` で解決された依存 `node` の公開 `operation` を呼び出す実装を必須とし、同等機能の再実装を禁止する。`toolchain.language=fortran` で依存 `component` を持つ `node` は、依存 `spec_id` ごとに `model` 内の `use <spec_id>_model` と `call <spec_id>__*` を必須とし、`subroutine <spec_id>__*` の再定義を禁止する。`Generate verify` は `derived_contract.json` に基づき、依存 `operation` と出力指標のデータ依存、および解析式直接代入による `diagnostics` 生成を検査する。`toolchain.language=fortran` の場合は `module` 名とソースファイル名を一致させ、`<module_name>.f90` 形式で出力する。
9. **Build**: 対象 `node` ごとに `MCP` サーバーの `compile_project` で依存関係を扱える標準ビルドツールを実行する（`fortran` / `c` 系の既定値は `make`）。依存を持つ `node` は、依存 `operation` の解決先が `dependency.resolved.yaml` と一致することを検証し、不一致時は `Build fail` とする。
10. **実行**: 対象 `node` ごとに `MCP` サーバーの `run_program` で `runner`（例: `simulate`）を実行し、`run_program` 実行コマンドに `case.resolved.yaml` を必ず含める。`runner` 経由で `model` を呼び出して `diagnostics` / `perf` を出力し、`verdict.json` と `aggregate_verdict.json` と `summary.json` と `trial_meta.json` の直接出力を禁止する。対象 `node` ごとに `execution_id/<node_key>/raw/` へ判定再計算用の一次証跡を保存する。`raw` へ `diagnostics` の複写を保存してはならない。
11. **実行証跡検証**: `python3 tools/validate_pipeline_semantics.py` を実行し、`raw` の一次証跡、`quality check`、`trial_meta` の追跡情報、`Generate` 由来の固定値生成パターンを検証する。`fail` の場合は `Judge` を開始しない。
12. **品質比較**: `target.class=cpu` の場合、対象 `node` ごとに `quality check` として `threads_per_rank=1` と `threads_per_rank>1` の実行結果を比較する。比較対象は `diagnostics.json` と `verdict.json` とし、合否確定規則は `SPEC.md` を適用する。
13. **判定**: `tests.md` の規則に基づく判定を対象 `node` ごとに実施し、`verdict` を生成する。依存込み判定は `aggregate_verdict.json` へ出力する。直下依存 `node` が `fail` または `blocked` の場合、上位 `node` は `blocked` として終了する。この場合も `aggregate_verdict.json` と `summary.json` と `trial_meta.json` を必須出力し、`blocked_reason` と `blocking_direct_deps` を記録する。`verdict.json` は `self_verdict=not_evaluated` を明示する。`Judge` は `raw` 一次証跡のみを入力として判定指標を再計算し、`diagnostics` と一致しない場合は `Judge fail` とする。
14. **強制停止**: 入力不足または前段成果物不足で当該工程を進められない場合、当該工程を `fail` で停止する。推定補完や人工ファイル生成で進めてはならない。
15. **記録**: `spec_version` / `test_profile_version` / `case_hash` / `impl_hash` / `git_sha` を保存する。
- `plan_id` / `pipeline_id` / `generation_id` / `build_id` / `execution_id` を保存する。
- `node_key` / `topo_level` / `dependency_ref` を保存する。
- `LLM` 利用ステージは各ステージの `<stage>_meta.json`（コード生成は `generate_meta.json`）に `attempt_count` / `verification_status` / `last_fail_reason` / `debug_mode` を保存する。
- `context_isolated=false` の場合は制約理由を記録する。
- `debug_mode=true` で失敗試行を保存した場合は保存件数と保存先を記録する。
- `dependency.resolved.yaml` の全 `node_key` について、`workspace/plans` と `workspace/pipelines` の対応が 1 対 1 で成立することを保存前に検証する。
16. **チューニング**: 物理合格を満たす候補の中から性能目的関数で最良候補を選定し、採用する `impl.resolved` を確定する。
17. **正式版昇格**: 採用する試行は `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` へ昇格保存し、`spec/registry/spec_catalog.yaml` の `official_releases` に `release_id` / `target_architecture` / `toolchain_language` / `target_backend` / `source_pipeline_id` / `source_generation_id` / `source_build_id` / `source_execution_id` / `artifact_root` / `promoted_at` / `status` を記録する。`problem` の昇格は `aggregate_verdict.overall=pass` を必須とする。
18. **次アクション**: 失敗分類に応じて戻る場所を決める。

## 3. 失敗時の戻り先（指針）
- **`LLM` ステージ実行不能**: workflow の各ステージで `LLM` による実行ができない -> 入力契約または `MCP` 接続定義へ戻る。手動 `copy` 運用を禁止する。
- **Spec 不備**: 曖昧・欠落・単位不整合 -> `Spec` へ戻る。
- **Test 不備**: ケース展開・閾値・実行条件の矛盾 -> `tests` へ戻る。
- **Dependency 解決 fail**: 未登録依存、未実装依存、互換性違反 -> `deps.yaml` / `spec_catalog.yaml` へ戻る。
- **Dependency block**: 下層 `node` が `fail` のため上層が `blocked` -> 下層 `node` へ戻る。
- **上位 workflow fail（依存起因）**: 直下依存 `node` の `fail` / `blocked` により上位 `node` を終了 -> 下層 `node` へ戻る。
- **LLM ステージ検証 fail**: `LLM` 利用ステージの出力が入力契約と不一致 -> 当該ステージへ戻る（必要に応じて `Spec` / `Test` へ戻る）。
- **物理 fail**: A の選択ミス、境界実装の矛盾 -> `Controlled Spec` または `case` へ戻る。
- **実装 fail**: 生成ミス、未対応ノブ -> `Generate` / `impl` へ戻る。
- **依存統合 fail**: 依存 `operation` 呼び出し欠落、依存 `operation` の再実装、依存解決先不一致 -> `Generate` または `Build` へ戻る。
- **不正生成 fail**: `dummy` 出力、人工データ作成、根拠なき判定結果固定 -> 当該工程を破棄し `Spec` / 工程入力定義へ戻る。
- **性能未達**: B の探索不足 -> `impl` 探索へ戻る。
- **再現性崩れ**: 決定性の破壊 -> `Plan` / 実行環境へ戻る。

## 4. 運用の最小チェックリスト
- `Spec` に未定義項目がない。
- `case.resolved` が決定的に生成できる。
- `derived_contract.json` が `controlled_spec.md` と `tests.md` と `deps.yaml` から導出されている。
- `LLM` 利用ステージのメタデータで `verification_status` が `pass` である。
- `debug_mode=false` の試行で失敗試行成果物が保存されていない。
- `diagnostics` / `perf` / `verdict` が揃って出る。
- `aggregate_verdict` と `summary.dependency_summary` が `dependency.resolved` と整合する。
- `dependency.resolved` の `node_key` 集合と `workspace/plans` / `workspace/pipelines` の `node` 集合が一致する。
- `spec_kind` を問わない workflow 実行で各 `node_key` の個別 `plan_id` と個別 `pipeline_id` が発行されている。
- 明示的な指定がない試行で、既存 workflow 出力の参照または閲覧が実施されていない。
- `lineage.json` が `node` 単位で分離され、単一 `lineage` に複数 `node_key` が混在していない。
- `Judge` 入力は同一 `execution_id` の `run_program` 実行記録と `diagnostics` / `perf` に限定されている。
- 依存を持つ `node` が `dependency.resolved.yaml` で解決された依存 `operation` を呼び出している。
- 依存 `operation` と同等機能を依存元 `node` へ再実装していない。
- `toolchain.language=fortran` の依存 `component` を持つ `node` で `use <spec_id>_model` と `call <spec_id>__*` が実装され、`subroutine <spec_id>__*` の再定義がない。
- `toolchain.language=fortran` の成果物で `module` 名とソースファイル名が一致している。
- `toolchain.language=fortran` の公開 `module` / `subroutine` 名に `spec_id` 由来接頭辞が付与されている。
- `trial_meta.json` の `generated_by_stage` / `source_execution_id` / `source_command_ref` / `source_artifact_hash` が欠落していない。
- `trial_meta.json` の `runner_command` / `process_trace_ref` / `raw_artifact_refs` が欠落していない。
- `trial_meta.json` の `source_command_ref` が参照する `run_program` 実行コマンドに `case.resolved.yaml` が含まれている。
- `lineage.json` と `trial_meta.json` の成果物参照パスが `workspace/` 起点で記録されている。
- `blocked` で終了した `node` に `aggregate_verdict.json` / `summary.json` / `trial_meta.json` が存在し、`blocked_reason` が記録されている。
- `spec_kind` を問わない workflow 実行の完了前に、対象 `DAG` の `plans` / `pipelines` 成果物が削除されていない。
- 物理判定の根拠が追跡できる。
- 正式版昇格を実施した試行は `spec_catalog.yaml` の `official_releases` と `release` 成果物配置が一致する。
- `dummy` 出力や人工生成ファイルが存在しない。
- `runner` が `python` / `bash` / `sh` / `node` など外部インタプリタを起動していない。
- `runner` が `verdict.json` / `aggregate_verdict.json` / `summary.json` / `trial_meta.json` を書き込んでいない。
- `execution_id/<node_key>/raw/` が存在し、`Judge` 再計算に必要なファイルが揃っている。
- `raw/metrics_basis.json` が `diagnostics.json` の複写ではなく、一次証跡から構成されている。
- workflow ルート判定が `workspace/` のみに対して実施されている。
- `python3 tools/validate_workspace_root.py` が `PASS` を返している。
- `python3 tools/validate_pipeline_semantics.py` が `PASS` を返している。
- 異なる `node_key` の `generate/src` が不正に完全一致していない。
- `copy_based_artifact_reuse` が未検出である。
