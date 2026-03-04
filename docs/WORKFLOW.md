# 全体ワークフロー: Spec -> Plan -> Generate -> Execute -> Judge -> Tune -> Promote
この文書は workflow の工程順序、段階間入出力契約、workflow 横断規約を定義する。
用語は `GLOSSARY.md` を参照する。

## 文書責務
- 本書は workflow 共通の不変規範と工程契約を正本として定義する。
- `SPEC.md` は全体方針、`spec` 管理要件、台帳要件を正本として定義する。
- 実装 `Plan` の既定値適用規則は `IMPL_PLAN_SPEC.md` を正本とする。

## workflow 共通不変規範
1. `tests` 合格または workflow 進行を目的とした `dummy` 出力を禁止する。
2. `diagnostics.json` と `perf.json` は対象 `runner` の実行結果としてのみ生成する。手書き生成、固定値埋め込み、外部後編集を禁止する。
3. `verdict.json` と `aggregate_verdict.json` は `tests.md` と同一 `execution_id` の実行成果物から導出しなければならない。
4. 工程入力が不足する場合は当該工程を `fail` で停止し、推測補完を禁止する。
5. 工程失敗時に下流工程開始条件を満たす目的で成果物ファイルを人工生成してはならない。
6. 明示的な指定がない場合、既存 workflow 出力（過去 `plan_id` / `pipeline_id` / `generation_id` / `build_id` / `execution_id`）の内容参照を禁止する。
7. `spec_kind` を問わない workflow 実行は、リポジトリ管理下の `spec` 正本と当該試行で生成した前段成果物のみを入力として使用する。
8. `spec_kind` を問わない workflow 実行は、各ステージ（`Plan` / `Generate` / `Build` / `Execute` / `Judge`）を `LLM` で実行しなければならない。専用実行スクリプト前提、手動 `copy`、手動 `json` 生成、手動 `id` 差し替えを禁止する。
9. workflow 成果物の保存先ルートは `workspace/` のみを許可する。`workspace/` が存在しない場合はリポジトリルート直下へ作成する。
10. workflow 実行中は対象 `DAG` の `workspace/plans` と `workspace/pipelines` 配下成果物を削除してはならない。
11. `quality check` は `diagnostics.json` と `verdict.json` の比較を正本とし、`stdout` 差分のみで合否を確定してはならない。
12. `lineage.json` と `trial_meta.json` の成果物参照パスは `workspace/` 起点で記録しなければならない。
13. `trial_meta.json` は `generated_by_stage`、`source_execution_id`、`source_command_ref`、`source_artifact_hash` を必須記録とする。
14. 異なる `pipeline_id` 間で `id` 系メタデータのみを変更して成果物本文を流用してはならない。検出時は `copy_based_artifact_reuse` として `invalid` とする。
15. 本規範違反は workflow 仕様違反とし、当該 `pipeline` を `invalid` とする。
16. `Promote` 以外のステージは、`workspace/` 配下以外へ書き込みを行ってはならない。`Promote` は `releases/` 配下と `spec/registry/spec_catalog.yaml` への書き込みのみを許可する。
17. `Promote` 以外のステージ開始前に、リポジトリルート配下ファイル集合の `baseline` を取得し、当該ステージ完了前に差分比較を実施しなければならない。
18. 差分比較は `workspace/` 配下以外の `add` / `modify` / `delete` を違反として検出しなければならない。`Promote` は `releases/` 配下と `spec/registry/spec_catalog.yaml` のみを例外許可する。
19. `python` 実行を workflow 経路で使用する場合、`__pycache__` が `workspace/` 配下以外へ生成されない設定を必須とする。`PYTHONDONTWRITEBYTECODE=1` または `PYTHONPYCACHEPREFIX=workspace/.pycache/<pipeline_id>/` を使用する。
20. 書き込み範囲違反を検出したステージは `fail` とし、下流ステージを開始してはならない。違反内容は `workspace/` 配下のメタデータへ記録しなければならない。
21. 書き込み範囲違反を検出した `pipeline` は `invalid` とする。違反状態を解消せずに同一試行を継続してはならない。

## 0. 仕様作成（人間）
- `Controlled Spec` で物理アルゴリズム（A）を定義する。
- `problem spec` は依存 `component` と採用 `profile` を定義する。
- `tests` は実験条件と判定条件を定義する。
- 検証契約は `Plan` が `controlled_spec.md` と `tests.md` と `deps.yaml` から導出する。

成果物:
- `spec/<spec_kind>/<domain>/<family>/<spec_id>/controlled_spec.md`
- `spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md`
- `spec/<spec_kind>/<domain>/<family>/<spec_id>/deps.yaml`

## 共通規約（LLM 利用ステージ）
- `LLM` を利用する全ステージに `SPEC.md` の「`LLM` の扱い（全体原則）」を適用する。
- `LLM` 利用ステージは各ステージの `<stage>_meta.json`（コード生成は `generate_meta.json`）を必須出力とする。
- `debug_mode=false` では失敗試行成果物を保存しない。`debug_mode=true` で保存した場合は保存件数と保存先をメタデータへ記録する。

## 1. Plan 生成（決定的）
### 1-1) 物理 Plan（`case.resolved.yaml`）
- `Controlled Spec` から物理アルゴリズム（A）を読み、`tests` から入力条件と `sweep` / `refinement` を決定的に展開する。
- `Plan verify` は `controlled_spec.md` と `tests.md` と `deps.yaml` から導出した検証契約を `derived_contract.json` として保存する。
- `case.resolved.yaml` は実行時入力の決定値のみを保持し、検証出力契約を保持してはならない。
- `derived_contract.json` は `io_contract.inputs` と `io_contract.outputs` を必須保持し、`io_contract.outputs` は `name` と `evidence_ref` と `shape_expr` で判定対象出力の一次証跡参照を定義しなければならない。
- `io_contract.outputs` で `evidence_ref` が `raw/state_snapshots` 以外を参照し、かつ `raw_requirements.required_evidence` で `artifact=state_snapshots` を必須宣言する場合、当該 `output` は `raw_variables`（非空配列）で再計算に必要な `raw/state_snapshots` 変数名を明示しなければならない。
- `derived_contract.json` は `raw_requirements.required_evidence` を必須保持し、`artifact` と `required` と `min_samples` と `schema`（必要時）で `raw` 一次証跡の必須構成を定義しなければならない。
- `derived_contract.json` は `test_evidence_requirements` を保持し、`tests.md` の各 `test_id` ごとに `required_raw_variables` を明示しなければならない。

### 1-2) 実装 Plan（`impl.resolved.yaml`）
- 実行アルゴリズム（B）を決定し、`target.backend`、`target.architecture`、`toolchain.language`、`toolchain.build_system` を固定する。
- `toolchain.language` と `toolchain.build_system` の既定値規則、および既定値逸脱条件は `IMPL_PLAN_SPEC.md` を適用する。
- `Phase 1` は固定値を許可する。`Phase 2` 以降は `Tune` で探索可能とする。

### 1-3) 依存解決 Plan（`dependency.resolved.yaml`）
- `deps.yaml` と `spec_catalog.yaml` から依存 `DAG` を生成し、`Plan` 段階で固定する。
- `dependency.resolved.yaml` は `node_key`、`direct_deps`、`transitive_deps`、`topo_level` を必須記録とする。
- `dependency.resolved.yaml` は起点 `node` と推移依存 `node` の閉包を過不足なく 1 回ずつ保持し、`node_key` の重複と欠落を禁止する。
- `deps.yaml` と `spec_catalog.yaml` から再構成した `expected_node_set` と `dependency.resolved.yaml` の `node_key` 集合一致を `Plan pass` 条件とする。
- 未登録依存、未実装依存、互換性違反依存を `dependency` 解決エラーとする。

### 1-4) 階層実行順序
- 実行順序は `dependency.resolved.yaml` の `topo_level` 昇順に固定する。
- 親 `node` は直下依存 `node` がすべて `pass` または `xfail` になるまで開始してはならない。
- `component` / `profile` / `problem` の実行順序は `spec_kind` 固定で判定せず、`dependency DAG` の `topo_level` で判定する。
- 同一 `topo_level` 内の独立 `node` は並列実行してよい。
- 同一 `topo_level` 内で一部 `node` が `fail` しても、独立 `node` の処理を中断してはならない。`topo_level` 完了後に次レベル開始可否を判定する。

### 1-5) `node` 単位 workflow 実行規則
- `dependency.resolved.yaml` の各 `node_key` に対して個別 workflow を完了させる。
- 直下依存が充足する `node` は `Plan -> Generate -> Build -> Execute -> Judge` を実行する。
- 直下依存が不充足の `node` は `blocked` 終端成果物を生成して完了とする。
- 各 `node_key` は個別の `plan_id` と個別の `pipeline_id` を必須発行する。
- `spec_kind` を問わない workflow 実行では、依存 `DAG` を展開した全 `node` の workflow 完了を必須とする。
- 直下依存 `node` の `aggregate_verdict` に `fail` または `blocked` がある場合、上位 `node` は `self_verdict` を評価せず `aggregate_verdict=blocked` で終了する。
- `blocked` 停止時も `aggregate_verdict.json`、`summary.json`、`trial_meta.json` を必須出力とする。`verdict.json` は `self_verdict=not_evaluated` を記録する。

## 2. 生成（Generate）
- 入力: `case.resolved.yaml`、`impl.resolved.yaml`、`dependency.resolved.yaml`、`derived_contract.json`
- 出力: 実装コード（`model` + `runner`）と `generate_meta.json`
- `Generate` は `node` 単位で実行し、対象 `node_key` 専用のソースを生成する。
- 言語に依らず `model`（物理計算）と `runner`（入出力・実行連携）を分離して生成する。
- `runner` は `model` を `call` / `use` / `import` で呼び出し、物理更新ロジックを重複実装してはならない。
- `toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`runner` が `python` / `bash` / `sh` / `node` など外部インタプリタを起動してはならない。
- `model` は数値状態更新または判定対象演算を実行しなければならない。固定値返却専用、固定 `JSON` 出力専用、`no-op` 専用実装を禁止する。
- `Generate verify` は、`model` が `case_id` 分岐と固定数値代入のみで判定指標を構成する実装を検出した場合に `fail` とする。
- 依存を持つ `node` の `model` は、`dependency.resolved.yaml` の `direct_deps` で解決された依存 `node` の公開 `operation` 呼び出しを必須とする。
- 依存 `operation` と同等機能を依存元 `node` の `model` / `runner` に再実装してはならない。検出時は `Generate fail` とする。
- 依存先が `profile` で公開 `operation` を持たない場合、依存元 `problem` は `profile` の選択結果と拘束条件を参照する実装痕跡を必須記録とする。
- `Generate verify` は `derived_contract.json` を入力として、依存 `operation` と出力指標のデータ依存を検証しなければならない。制御構造の形式（時空間ループ有無など）を固定要件にしてはならない。
- `Generate verify` は `model` 出力と無関係な定数出力、固定 `JSON` 出力、解析式直接代入による `diagnostics` 生成を検出した場合に `fail` とする。
- `model` / `runner` は、判定指標（例: `mass_drift_rel`、`momx_drift_rel`、`momy_drift_rel`、`analytic_h_l2_rel`）へ物理的根拠のない任意の定数スケーリング、定数オフセット、ケース依存補正を導入してはならない。`Controlled Spec` または `tests.md` で明示定義された評価式以外の補正を禁止する。
- `Generate verify` は、`intent(out)` 変数の最終式木が `derived_contract.json` の `semantic_dependency.required_sources` と `io_contract.outputs` で宣言された出力変数群へ到達することを検証しなければならない。
- `Generate verify` は、`spec` の目的に依存しない固定計算様式（例: 常に `flux` や常に時刻積分）を一律必須にしてはならない。判定は `derived_contract.json` の要求計算種別に基づいて実施しなければならない。
- `toolchain.language=fortran` の `module` 名と公開 `subroutine` 名は `spec_id` 由来接頭辞を含む一意名とする。
- `toolchain.language=fortran` のソースファイル名は定義 `module` 名と一致する `<module_name>.f90` を必須とする。
- `toolchain.language=fortran` で依存 `component` を持つ `node` の `model` は依存 `spec_id` ごとに `use <spec_id>_model` と `call <spec_id>__*` を必須とし、`subroutine <spec_id>__*` の再定義を禁止する。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` の場合、生成 `src/Makefile` は `use` 依存に対応したオブジェクト依存関係を明示し、依存 `.o` を各ターゲット前提条件へ必須記述する。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` の場合、`src/Makefile` は並列ビルド（例: `make -j 4`）で依存欠落による失敗を起こしてはならない。
- 同一 `pipeline` 内で異なる `node_key` に同一 `src` を複製してはならない。共通化は共通ライブラリとして明示する。
- `target.class=cpu` でループ並列化方式の明示指定がない場合、並列化可能ループへ `OpenMP` を付与する。
- 物理更新を実装できない場合は `Generate fail` とし、代替として固定文字列や固定 `JSON` を出力してはならない。

## 3. Build / Execute
- `Build` と `Execute` は `MCP` サーバー経由で実行する。
- `Build` は `compile_project` を使用し、`fortran` / `c` / `cpp` / `mixed` 系では依存関係を扱える標準ビルドツール（既定 `make`）を使用する。
- `toolchain.build_system=make` の `Build` 入力は、`src/Makefile` が言語依存のコンパイル順序依存を前提条件として明示した依存関係完全版でなければならない。
- `toolchain.build_system=make` の `Build` は、`make -j` で成否が変化しない依存記述を必須とする。
- `Execute` は `run_program` を使用し、実行コマンドへ `case.resolved.yaml` を必ず含める。
- `compile_project` と `run_program` の実コマンド記録は `JSONL` 形式で保存し、既定の保存先は `project_dir/mcp_command_log.jsonl` とする。
- `Build` と `Execute` の試行メタデータは `command_id` と `command_log_ref`（または `command_log_path`）を追跡可能に記録する。
- `Build` は依存を持つ `node` で、依存 `operation` 解決先が `dependency.resolved.yaml` と一致することを必須検証とする。不一致時は `Build fail` とする。
- `Build` / `Execute` は `node` 単位で個別実行し、他 `node` の成果物を混在させてはならない。
- `runner` の出力対象は `diagnostics.json`、`perf.json`、`raw/` 一次証跡、`stdout.log`、`stderr.log` に限定する。
- `runner` は `verdict.json`、`aggregate_verdict.json`、`summary.json`、`trial_meta.json` を書き込んではならない。
- `Execute` は `Judge` 再計算に必要な一次証跡を `execution_id/<node_key>/raw/` に保存しなければならない。
- 一次証跡の必須構成は `derived_contract.json` の `raw_requirements.required_evidence` を正本とする。固定の最小構成を全 `spec` に一律適用してはならない。
- `raw_requirements.required_evidence` は `metrics_basis.json` と `execution_trace.json` と `state_snapshots` などの `artifact` ごとに必須有無を宣言しなければならない。
- `raw_requirements.required_evidence` が `artifact=state_snapshots` かつ `required=true` を宣言する場合、`raw/state_snapshots/` は `snapshot_schema.json` で `variables[].name` と `variables[].shape_expr` と `time_variable` と `time_shape_expr` を宣言し、`min_samples` 件以上の状態ファイルへ当該項目を保持しなければならない。
- `raw_requirements.required_evidence` が `artifact=state_snapshots` を必須宣言しない場合、`raw/state_snapshots/` を必須にしてはならない。スカラー目的 `spec` を含む任意の計算課題を許容しなければならない。
- `python3 tools/validate_pipeline_semantics.py` は `derived_contract.json` で宣言された `state_snapshots` の変数名と形状式、および `time_variable` の形状式が `raw/state_snapshots/snapshot_schema.json` と各 `snapshot*.json` に一致することを検証しなければならない。
- `raw/metrics_basis.json` は一次証跡のみを保持し、`diagnostics.json` の複写を禁止する。
- `Build` または `Execute` が失敗した場合、`diagnostics.json` / `perf.json` の人工生成を禁止し、当該 `node` を `fail` とする。
- `quality_check.json` は `checks.verdict_available=true` と `checks.diagnostics_match=true` と `checks.verdict_match=true` を同時に満たさなければならない。いずれかが `false` または欠落の場合は `Execute fail` とする。
- `quality check` 実行は `run_quality_checks` の `preset` 指定のみを許可し、`python3 quality_check.py` など任意コマンド実行を禁止する。
- `perf.json` の仕様は `PERFORMANCE_DIAGNOSTICS.md` を参照する。

## 4. 判定（Judge）
- 判定正本は `tests.md` とする。
- 判定は `self_verdict`（`verdict.json`）と `aggregate_verdict`（`aggregate_verdict.json`）の 2 層で実施する。
- `Judge` 開始条件は、対象 `execution_id` 配下に `run_program` 実行記録と `diagnostics.json` と `perf.json` と `raw/` 一次証跡が存在し、同一 `execution_id` 成果物として追跡可能であることとする。
- `Judge` は `raw/` 一次証跡から独立経路で判定指標を再計算し、`diagnostics.json` と整合確認しなければならない。
- `Judge` 再計算入力は `raw/` のみに限定する。`diagnostics.json` を再計算入力へ流用してはならない。
- `Judge` は再計算不能または不整合時に `fail` としなければならない。
- `Judge` は固定スクリプト検査に加え、`LLM` による意味検査を必須実行し、`model` / `runner` / `raw` 一次証跡の整合性と捏造疑義を判定しなければならない。
- `LLM` 意味検査の結果は `semantic_review.json` として `execution_id/<node_key>/` 配下へ保存し、`review_method`、`decision`、`scope.model_ref`、`scope.runner_ref`、`scope.raw_refs`、`findings` を必須記録とする。
- `semantic_review.json` の `decision` が `fail` または欠落の場合、当該 `node` を `Judge fail` としなければならない。
- 直下依存 `node` に `fail` または `blocked` がある場合、上位 `node` は `self_verdict` を評価せず `aggregate_verdict=blocked` として終了する。
- `blocked` 終了時も `aggregate_verdict.json`、`summary.json`、`trial_meta.json` を必須出力とし、`blocked_reason` と `blocking_direct_deps` を記録する。
- `summary.json` は `self_summary` と `dependency_summary` を必須保持とする。`dependency_summary` は `total`、`pass`、`xfail`、`fail`、`blocked` を保持する。
- `verdict.json` は `per_test` を必須保持とし、`tests.md` の全 `test_id` を重複なく記録しなければならない。
- `summary.json` の `counts` は `verdict.json.per_test` の集計値と一致しなければならない。
- 判定入力不足時は `Judge fail` とし、推定値や仮定値で `verdict` を成立させてはならない。
- `python3 tools/validate_pipeline_semantics.py` は、`problem node` の `model` で `intent(out)` 変数が固定値代入のみで構成される実装と、`runner` の `diagnostics.json` が `model` 呼び出し結果を参照しない固定値埋め込み実装を検出した場合に `fail` とする。
- `Judge` 開始前と `Judge` 完了前に `python3 tools/validate_pipeline_semantics.py` を実行し、`fail` 時は当該 `pipeline` を `invalid` とする。
- `Judge` 開始前の `python3 tools/validate_pipeline_semantics.py` は、対象 `dependency.resolved.yaml` の `all_nodes` で解決された全 `node` の `pipeline_root` を検証対象に含めなければならない。起点 `problem` の単独 `pipeline_root` のみを対象にしてはならない。
- `python3 tools/validate_pipeline_semantics.py` は、`dependency.resolved.yaml` の `all_nodes` に対して `plan` または `pipeline` が未発行の `node` を検出した場合に `fail` とし、当該試行の `Judge` 開始を禁止しなければならない。
- 実装品質判定（`target.class=cpu`）は `threads_per_rank=1` と `threads_per_rank>1` の比較で実施し、比較対象は `diagnostics.json` と `verdict.json` とする。
- スレッド並列あり / なしの比較は `tests` の判定対象に含めず、`quality check` として扱う。
- 物理 `fail` 時は性能評価をスキップする。

## 5. チューニング（Tune: Phase 2+）
- 同一 `case.resolved.yaml` に対して複数 `impl.resolved.yaml` を生成し、物理合格を満たす範囲で性能目的関数を最大化する。
- 詳細は `TUNING_WORKFLOW.md` を参照する。

## 6. 正式版昇格（Promote）
- 入力条件: `verdict.json` の `overall=pass`。
- 入力条件: `aggregate_verdict.json` の `overall=pass`。
- 入力条件: 採用対象 `generation_id` / `build_id` / `execution_id` が `lineage.json` と `trial_meta.json` で追跡可能であること。
- 入力条件: 採用対象 `impl.resolved.yaml` が確定していること。
- 実施内容: `workspace` から採用成果物を `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` へ保存する。
- 実施内容: `spec/registry/spec_catalog.yaml` の対象 `spec_id` に `official_releases` を追加する。
- 登録必須項目: `release_id`、`target_architecture`、`toolchain_language`、`target_backend`、`source_pipeline_id`、`source_generation_id`、`source_build_id`、`source_execution_id`、`artifact_root`、`promoted_at`、`status`。
- 不変条件: 既存 `release_id` の上書きを禁止する。更新時は新規 `release_id` を追加し、同一 `target_architecture + toolchain_language` の旧 `release` を `deprecated` へ更新する。
- `problem` の `Promote` は推移依存を含む `aggregate_verdict.overall=pass` を必須条件とする。

## 7. 成果物配置規約（Plan / Generate / Build / Execute）
### 7-1) ルート構造
workflow 成果物の保存先は `workspace/` を正本とし、次の構造を必須とする。

```text
workspace/
  plans/
    <node_key_safe>/
      <plan_id>/
        case.resolved.yaml
        impl.resolved.yaml
        dependency.resolved.yaml
        derived_contract.json
        plan_meta.json
  pipelines/
    <node_key_safe>/
      <pipeline_id>/
        lineage.json
        generate/
          <generation_id>/
            src/
            generate_meta.json
            attempts/  # optional: debug_mode=true の場合のみ
              <attempt_id>/
        build/
          <build_id>/
            bin/
            build_meta.json
        execute/
          <execution_id>/
            <node_key>/
              diagnostics.json
              perf.json
              quality_check.json
              raw/
                state_snapshots/
                metrics_basis.json
                execution_trace.json
              verdict.json
              aggregate_verdict.json
              summary.json
              semantic_review.json
              trial_meta.json
              stdout.log
              stderr.log
  index/
    plan_index.json
    pipeline_index.json
```

### 7-2) ID と不変条件
- `node_key_safe` は `node_key` の保存用表記とし、推奨形式は `<spec_kind>__<spec_id>__<spec_version>` とする。
- `plan_id` は `node` 単位で `case.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` の組を識別する `ID` とする。推奨形式は `<node_key_safe>_<case_hash12>_<impl_hash12>` とする。
- `pipeline_id` は `node` 単位で 1 回の `Generate -> Build -> Execute` 系列を識別する `ID` とする。推奨形式は `<plan_id>_<utc_ts>_<seq3>` とする。
- `generation_id` / `build_id` / `execution_id` は各段階の試行単位 `ID` とする。
- `execution` の判定単位は `node_key` とする。`execution_id` 配下で複数 `node_key` を扱う場合は `node_key` ごとの成果物分離を必須とする。
- `plan_id` 配下の `resolved` ファイルは `immutable` とし、更新時は新規 `plan_id` を発行する。
- `pipeline_id` 配下は `append-only` とし、既存 `execution_id` の上書きを禁止する。

### 7-3) 起点モード
- `spec` 起点モード: `spec` から依存 `DAG` を解決し、`node` ごとに新しい `plan_id` を発行して `pipeline` を開始する。
- `resolved` 起点モード: 既存 `plan_id` を指定し、`Generate` 以降のみを実行する。
- `lineage.json` は `spec_ref`、`plan_ref`、各段階 `id`、`dependency_ref`、`node_key`、`direct_dependency_status` を必須記録とする。

### 7-4) 再実行規則
- 同一 `plan_id` で `Generate` を複数回実行してよい。各試行は別 `generation_id` とする。
- 同一 `generation_id` で `Build` を複数回実行してよい。各試行は別 `build_id` とする。
- 同一 `build_id` で `Execute` を複数回実行してよい。各試行は別 `execution_id` とする。
- `Build` 開始条件は対象 `generation_id` の `generate_meta.json` で `verification_status=pass` であることとする。
- `debug_mode=false` の `Generate` は `attempts/` を生成してはならない。
- `Judge` 入力は常に同一 `execution_id` 配下成果物とし、他 `execution_id` との混在を禁止する。
- 各ステージ `fail` 時は下流ステージ開始条件を満たす目的のファイル後付け生成を禁止する。

### 7-5) 参照規則
- `pipeline` から `plan` を参照するときは `node_key_safe + plan_id` を使用し、相対ファイルパス直参照を禁止する。
- `execution` の再現は `lineage.json` と `trial_meta.json` のみで可能でなければならない。
- `trial_meta.json` は `runner_command`、`process_trace_ref`、`raw_artifact_refs` を必須記録とする。
- `index/plan_index.json` と `index/pipeline_index.json` は探索専用とし、判定ロジックの正本に使ってはならない。
- `aggregate_verdict.json` は常に `dependency.resolved.yaml` と整合し、依存集合の省略を禁止する。

### 7-6) 依存 workflow 網羅チェック
- `dependency.resolved.yaml` の `node_key` 集合と `workspace/plans/*/<plan_id>/` の `node_key_safe` 集合は 1 対 1 で一致しなければならない。
- `dependency.resolved.yaml` の `node_key` 集合と `workspace/pipelines/*/<pipeline_id>/lineage.json` の `node_key` 集合は 1 対 1 で一致しなければならない。
- `dependency.resolved.yaml` が `all_nodes` を保持する場合、`python3 tools/validate_pipeline_semantics.py` は `all_nodes` の全 `node_key` について `lineage` と `plan_ref` の両方を検証し、未発行 `node` を `fail` としなければならない。
- 異なる `node_key` で生成された `generate/<generation_id>/src/` のコードハッシュが一致した場合、共通ライブラリとして明示されたファイルを除き `copy_based_artifact_reuse` として `invalid` にしなければならない。
- `spec_kind` を問わない workflow 実行の完了宣言前に、対象依存 `DAG` の `workspace/plans` / `workspace/pipelines` 成果物を削除してはならない。

### 7-7) 書き込み範囲ガード
- 各ステージ開始時に `write_scope_baseline.json` を `workspace/` 配下へ保存し、比較対象の `baseline` を固定しなければならない。
- `write_scope_baseline.json` は、少なくとも `stage`、`node_key`、`pipeline_id`、`captured_at`、`tracked_diff`、`untracked_files` を保持しなければならない。
- 各ステージ完了前に `write_scope_baseline.json` との差分を計算し、`workspace/` 配下以外の変化を `write_scope_violation` として判定しなければならない。
- 違反未検出時は `write_scope_check.status=pass` をステージメタデータへ記録しなければならない。
- 違反検出時は `write_scope_violation.json` を `workspace/` 配下へ出力し、`violation_paths` と `stage` と `node_key` と `pipeline_id` と `detected_at` を必須記録しなければならない。
- `write_scope_violation` 検出時は当該ステージを `fail` とし、当該 `pipeline` の `aggregate_verdict` 確定を禁止する。

## 8. 完了判定基準
- workflow 完了条件は、`dependency.resolved.yaml` の全 `node_key` に対して `workspace/plans/<node_key_safe>/<plan_id>/` と `workspace/pipelines/<node_key_safe>/<pipeline_id>/` が存在し、`lineage.json` の `node_key` と `dependency_ref` が一致することとする。
- workflow 完了宣言は、`dependency workflow` 網羅チェックと `trial_meta` 完整性チェックと `copy_based_artifact_reuse` 非検出を同時に満たす場合のみ許可する。
- workflow 完了宣言は、全ステージで `write_scope_violation` 非検出を同時に満たす場合のみ許可する。
- `CI` は `python3 tools/validate_workspace_root.py` と `python3 tools/validate_pipeline_semantics.py` の実行結果を `pass` 条件として扱う。

補足:
- `impl` の仕様は `IMPL_PLAN_SPEC.md` を参照する。
- 自動チューニング運用は `TUNING_WORKFLOW.md` を参照する。
