# 全体ワークフロー: Spec -> Plan -> Generate -> Execute -> Judge -> Tune -> Promote
この文書はプロジェクト全体の流れを単独で把握できるようにまとめる。
用語は `GLOSSARY.md` を参照する。

## 0. 仕様作成（人間）
- `Controlled Spec`（文章 + 構造化ブロック）で物理アルゴリズム（A）を決定する。
- `problem spec` は依存 `component` と採用 `profile` を参照し、統合順序を固定する。
- `tests`（自然言語中心 + 必要最小限の構造化ブロック）で実験条件と判定条件を決定する。
- 追加必須項目を `Controlled Spec` へ要求しない。検証契約は `Plan` が `controlled_spec.md` と `tests.md` と `deps.yaml` から導出する。
- 実行アルゴリズム（B）はここでは固定しない（将来探索する）。

成果物:
- `spec/<spec_kind>/<domain>/<family>/<spec_id>/controlled_spec.md`（`Controlled Spec`）
- `spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md`（テスト入力・判定条件）

## 共通規約（LLM 利用ステージ）
- `LLM` を利用する全ステージ（`Plan` 生成、コード生成、`Tune` 候補生成など）に、`SPEC.md` の「`LLM` の扱い」を適用する。
- ステージ内 `verify` の適用、コンテキスト分離方針、失敗試行保存規則、最終品質保証の定義は `SPEC.md` を正本とする。
- `LLM` 利用ステージでは各ステージの `<stage>_meta.json` を必須出力とし、`debug_mode` 規則を含む必須項目を満たすこと。コード生成ステージでは `generate_meta.json` とする。
- すべてのステージで `dummy` 出力、`dummy` データ、`dummy` 計算を禁止する。
- ステージ入力が不足する場合は当該ステージを `fail` で停止し、下流ステージ開始条件を満たす目的の人工成果物生成を禁止する。
- 明示的な指定がない場合、既存 workflow 出力（過去試行の `workspace/plans` / `workspace/pipelines` 成果物）を参照してはならない。内容閲覧を禁止し、`spec` 正本と当該実行で生成した前段成果物のみで workflow を独立実行する。
- 検証契約は `controlled_spec.md` と `tests.md` と `deps.yaml` から導出しなければならない。導出不能の場合は当該ステージを `fail` で停止し、推測補完を禁止する。

## 1. Plan 生成（決定的）
### 1-1) 物理 Plan（`case.resolved.yaml`）
- `Controlled Spec` から物理アルゴリズム（A）を読み、`tests` から入力条件と `sweep` / `refinement` を決定的に展開する。
- この層は「物理結果を保証するために決定的」である必要がある。
- `Plan` 生成で `LLM` を利用する場合も、共通規約（`SPEC.md`）を適用し、決定性要件を満たすこと。
- `Plan verify` は `controlled_spec.md` と `tests.md` と `deps.yaml` から導出した検証契約を `plan` 成果物へ保存する。推奨ファイル名は `derived_contract.json` とする。

### 1-2) 実装 Plan（`impl.resolved.yaml`）
- `target` や環境に応じて、実行アルゴリズム（B）を決める。
- この時点で `target.backend` と `toolchain.language`（例: `Fortran` / `C++`）を固定する。
- ユーザーからプログラミング言語の明示指定がない場合、`target.class=cpu` では `fortran`、`target.class=gpu` では `cuda_fortran` を必ず採用する。
- `toolchain.language` の既定値からの逸脱は、ユーザーがプログラミング言語を明示指定した場合にのみ許可する。
- `target.class=cpu` でユーザーがループ並列化方式を指定しない場合、並列化可能ループは `OpenMP` を既定で適用する。
- この時点で `target.architecture`（例: `x86_64`,`nvidia_sm80`）を固定する。
- この時点で `toolchain.build_system`（例: `make`,`cmake`）を固定する。
- `compiler` 種別は任意（再現性が必要な運用でのみ固定）とする。
- `toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`toolchain.build_system` は `make` / `cmake` / `meson` / `ninja` のいずれかを使用する。既定値は `make` とする。
- `Phase 1` では固定値でもよい。
- `Phase 2` 以降は `tuner` が複数候補を生成し探索する。

### 1-3) 依存解決 Plan（`dependency.resolved.yaml`）
- `deps.yaml` と `spec_catalog.yaml` から依存 `DAG` を生成し、`Plan` 段階で固定する。
- `dependency.resolved.yaml` は次を必須記録とする。
  - `node_key`（`<spec_kind>/<spec_id>@<spec_version>`）
  - `direct_deps`
  - `transitive_deps`
  - `topo_level`
- `dependency.resolved.yaml` は、起点 `node_key` と推移依存 `node_key` の閉包を過不足なく 1 回ずつ保持する。`node_key` の重複と欠落を禁止する。
- `deps.yaml` と `spec_catalog.yaml` から再計算した `expected_node_set` と `dependency.resolved.yaml` の `node_key` 集合一致を `Plan pass` 条件とする。
- 依存解決に失敗した `node` は `blocked` とし、`Generate` 以降へ進めない。
- 未登録依存、未実装依存、互換性違反依存は `dependency` 解決エラーとする。

### 1-4) 階層実行順序
- 実行順序は `dependency.resolved.yaml` の `topo_level` 昇順に固定する。
- 親 `node` は、直下依存 `node` がすべて `pass` または `xfail` になるまで `Plan` を開始してはならない。
- `component` / `profile` / `problem` の順序は、`spec_kind` 固定ではなく `DAG` の順序で評価する。
- 同一 `topo_level` 内の独立 `node` は並列実行してよい。
- 同一 `topo_level` 内で `fail` が発生しても、独立 `node` の実行は中断してはならない。`topo_level` 完了後に次レベルの開始可否を判定する。

### 1-5) `node` 単位 workflow 実行規則
- `dependency.resolved.yaml` の各 `node_key` について、個別 workflow を完了させる。直下依存が充足する `node` は `Plan -> Generate -> Build -> Execute -> Judge` を完了し、直下依存が不充足の `node` は `blocked` 終端成果物を生成して完了とする。
- 各 `node_key` は、個別の `plan_id` と個別の `pipeline_id` を発行する。
- `spec_kind` を問わない workflow 実行では、依存 `DAG` を展開した全 `node` の workflow 完了を必須とする。
- 上位 `node` の workflow は、直下依存 `node` の workflow 成果物（`aggregate_verdict`）を入力条件として参照する。
- 直下依存 `node` に `fail` または `blocked` が存在する場合、上位 `node` の workflow は `dependency_failed` として終了し、上位 `node` を `blocked` とする。
- 上位 `node` の停止時は、上位 `node` 向け `diagnostics` / `verdict` の人工生成を禁止する。
- `blocked` で停止する `node` でも、個別の `plan_id` と `pipeline_id` を保持し、`lineage.json` と `trial_meta.json` に停止理由を記録する。
- `spec_kind` を問わない workflow 実行の完了条件は、`dependency.resolved.yaml` の全 `node_key` が `workspace/plans` と `workspace/pipelines` に存在し、`lineage.json` の `dependency_ref` が一致することとする。

## 2. 生成（コード生成）
- 入力: `case.resolved`（A 固定）と `impl.resolved`（B の指定、または候補集合）と `dependency.resolved`（対象 `node`）
- 出力: 実装コード（`model` + `runner`）と付随ドキュメント
- `Generate` は `node` 単位に実行し、対象 `node_key` 専用のソースコードを生成する。
- 言語に依らず、**モデル本体（物理計算）** と **テスト `runner`（入出力・判定連携）** を分離して生成する。
- `runner` は `model` を `call` / `use` / `import` で呼び出し、物理更新ロジックを重複実装しない。
- `toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`runner` から `python` / `bash` / `sh` / `node` などの外部インタプリタを起動してはならない。検出時は `Generate fail` とする。
- `model` は対象 `node` の物理・演算契約を実装し、数値状態更新または判定対象演算を実行しなければならない。固定値返却専用、固定 `JSON` 出力専用、`no-op` 専用実装を禁止する。
- `Generate verify` は、検証契約で要求された依存 `operation` と出力指標のデータ依存を検証しなければならない。時空間ループなど特定制御構造を一律必須にしてはならない。
- `Generate verify` は、`model` 出力と無関係な定数出力、固定 `JSON` 出力、解析式直接代入による `diagnostics` 生成を検出した場合に `fail` とする。
- 依存を持つ `node` の `model` は、`dependency.resolved.yaml` の `direct_deps` で解決された依存 `node` の公開 `operation` を呼び出さなければならない。
- 依存 `operation` と同等機能を依存元 `node` の `model` / `runner` に再実装してはならない。検出時は `Generate fail` とする。
- 依存先が `profile` の場合、`profile` が公開 `operation` を持たない構成では、依存元 `problem` は `profile` の選択結果と拘束条件を参照した実装にしなければならない。参照痕跡が欠落する場合は `Generate fail` とする。
- `toolchain.language=fortran` では、`module` 名とソースファイル名を一致させ、`<module_name>.f90` 形式で出力しなければならない。汎用ファイル名 `model.f90` への集約を禁止する。
- `toolchain.language=fortran` では、`module` 名と公開 `subroutine` 名に `spec_id` 由来の接頭辞を付与し、`node` 間の名前衝突を防止しなければならない。
- 同一 `pipeline` 内で異なる `node_key` に同一 `src` を複製してはならない。共通化するコードは共通ライブラリとして明示し、`node` 専用コードとの差分を保持する。
- `target.class=cpu` では、ループ並列化方式の明示指定がない限り、並列化可能ループに `OpenMP` を付与する。
- コード生成で `LLM` を利用する場合は、共通規約（`SPEC.md`）を適用する。
- 物理更新を実装できない場合は `Generate fail` とし、固定文字列出力や固定 `JSON` 出力による代替を禁止する。

## 3. 実行（runner / simulate）
- 入力: `case.resolved.yaml` と（任意で）`impl.resolved.yaml` と `dependency.resolved.yaml`
- `Build` と `Execute` は `MCP` サーバー経由で実行する。`compile` は `compile_project`、`run` は `run_program` を使用する。
- `Build` と `Execute` は `node` 単位に個別実行し、他 `node` の成果物を混在させない。
- `Build` は、依存を持つ `node` に対して、依存 `operation` の解決先が `dependency.resolved.yaml` と一致することを検証しなければならない。不一致時は `Build fail` とする。
- `runner` が `model` を呼び出し、`diagnostics.json` と `perf.json` を出力する。
- `Execute` は `Judge` が再計算可能な実行証跡を同一 `execution_id` 配下に保存しなければならない。`raw/` 配下の状態スナップショット、ケース別メトリクス元データ、実行トレースを必須とする。
- `raw` 成果物は一次証跡のみを保持し、`diagnostics.json` の複写を `metrics_basis` として保存してはならない。
- 実行証跡が不足する場合、`diagnostics.json` と `perf.json` が存在しても当該 `node` を `Execute fail` とする。
- `perf.json` の仕様は `PERFORMANCE_DIAGNOSTICS.md` を参照する。
- `Build` / `Execute` 失敗時は当該 `node` を `fail` とし、`diagnostics.json` / `perf.json` の人工生成を禁止する。

## 4. 判定（runner）
- 判定正本は `tests.md` とする。`problem` / `component` / `profile` の種別に応じて定義された判定規則を評価する。
- 判定は 2 層で実施する。
  - `self_verdict`: 当該 `node` の `tests.md` に基づく単体判定（正本: `verdict.json`）
  - `aggregate_verdict`: 当該 `node` と推移依存 `node` を合算した集約判定（正本: `aggregate_verdict.json`）
- 上位 `node` は、直下依存 `node` に `fail` または `blocked` がある場合、`self_verdict` を評価せず `aggregate_verdict=blocked` として終了する。
- `blocked` 終了時でも `aggregate_verdict.json` と `summary.json` と `trial_meta.json` を必須出力とし、`blocked_reason` と `blocking_direct_deps` を記録する。
- `blocked` 終了時の `verdict.json` は `self_verdict=not_evaluated` を必須記録とする。
- 物理判定: `diagnostics` を用いて `checks` / `thresholds` を評価する（詳細は `PHYSICAL_VALIDATION.md`）。
- `Judge` は `raw/` の実行証跡から判定指標を再計算し、再計算値と `diagnostics` の整合を確認しなければならない。再計算不能または不整合時は `Judge fail` とする。
- `Judge` の再計算入力は `raw/` 一次証跡に限定する。`diagnostics.json` を再計算入力へ流用してはならない。
- 実装品質判定: `target.class=cpu` の場合、同一 `case.resolved.yaml` を `threads_per_rank=1` と `threads_per_rank>1` で実行し、結果を比較する。比較対象は `diagnostics.json` と `verdict.json` とする。
- `quality check` は `diagnostics.json` と `verdict.json` の比較結果を正本とし、`stdout` 差分のみで合否を確定してはならない。
- スレッド並列あり / なしの比較は `tests` の判定対象に含めず、`quality check` として扱う。
- 性能判定（任意）: `perf` を用いて `performance regression` を評価する。
- 物理 `fail` 時は性能評価をスキップする。
- `summary.json` は `self_summary` と `dependency_summary` を必須保持とする。
- `dependency_summary` は少なくとも `total`、`pass`、`xfail`、`fail`、`blocked` を保持する。
- 判定入力不足時は `Judge fail` とし、推定値や仮定値で `verdict` を成立させることを禁止する。

## 5. チューニング（tuner: Phase 2+）
- 同一 `case.resolved` に対し複数 `impl.resolved` を生成し、物理合格を満たす範囲で性能目的関数を最大化する。
- 詳細は `TUNING_WORKFLOW.md` を参照する。

## 6. 正式版昇格（Promote）
- 入力条件: `execution_id` の `verdict.json` で `overall=pass` であること。
- 入力条件: `execution_id` の `aggregate_verdict.json` で `overall=pass` であること。
- 入力条件: 採用対象の `generation_id` / `build_id` / `execution_id` が `lineage.json` と `trial_meta.json` で追跡可能であること。
- 入力条件: `tuning` の結果から採用対象の `impl.resolved` が確定していること。
- 実施内容: `workspace` から採用成果物を `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` に保存し、正式版の参照正本を当該 `release` に固定する。
- 実施内容: `spec/registry/spec_catalog.yaml` の対象 `spec_id` に `official_releases` エントリを追加する。
- 登録必須項目: `release_id`、`target_architecture`、`toolchain_language`、`target_backend`、`source_pipeline_id`、`source_generation_id`、`source_build_id`、`source_execution_id`、`artifact_root`、`promoted_at`、`status`。
- 不変条件: 既存 `release_id` の上書きを禁止する。更新時は新規 `release_id` を追加し、同一 `target_architecture + toolchain_language` の旧 `release` を `deprecated` に変更する。
- 不変条件: `problem` の `Promote` では、推移依存 `node` の `aggregate` 集約結果が `pass` または `xfail` のみで構成されることを必須とする。

## 7. 成果物配置規約（Plan / Generate / Build / Execute）
### 7-1) ルート構造
ワークフロー成果物の保存先は `workspace/` を正本とし、次の構造を必須とする。
- 成果物保存先のルートは、リポジトリルート直下の `workspace/` のみを許可する。
- workflow ルート判定は `workspace/` のみを対象とし、`workspace/` 以外のディレクトリは判定対象に含めない。
- `Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote` は、保存先が `workspace/` でない場合に `fail` で停止しなければならない。
- workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成しなければならない。
- workflow 実行の開始前と完了前に `python3 tools/validate_workspace_root.py` を必須実行し、終了コードが 0 でない場合は当該 workflow を `fail` で停止しなければならない。

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
              raw/
                state_snapshots/
                metrics_basis.json
                execution_trace.json
              verdict.json
              aggregate_verdict.json
              summary.json
              trial_meta.json
              stdout.log
              stderr.log
  index/
    plan_index.json
    pipeline_index.json
```

### 7-2) ID と不変条件
- `node_key_safe` は `node_key` の保存用表記とし、`<spec_kind>__<spec_id>__<spec_version>` 形式を推奨する。
- `plan_id` は `node` 単位で `case.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` の組を一意に識別する `ID` とする。推奨形式は `<node_key_safe>_<case_hash12>_<impl_hash12>` とする。
- `pipeline_id`: `node` 単位で 1 回の `Generate -> Build -> Execute` 系列を一意に識別する `ID` とする。推奨形式は `<plan_id>_<utc_ts>_<seq3>` とする。
- `generation_id` / `build_id` / `execution_id`: 各段階の試行単位 `ID` とする。
- `execution` の判定単位は `node_key` とする。`execution_id` 配下で複数 `node_key` を扱う場合、`node_key` ごとの成果物分離を必須とする。
- `plan_id` 配下の `resolved` ファイルは `immutable` とする。更新ではなく新しい `plan_id` を発行する。
- `pipeline_id` 配下は `append-only` とし、既存 `execution_id` の上書きを禁止する。

### 7-3) 起点モード
- `spec` 起点モード: `spec` から依存 `DAG` を解決し、`node` ごとに新しい `plan_id` を発行して `pipeline` を開始する。
- `resolved` 起点モード: 既存 `plan_id` を指定し、`Generate` 以降のみを実行する。
- `lineage.json` は、`spec_ref` と `plan_ref` と各段階 `id` を必須記録する。
- `lineage.json` は `dependency_ref`（`dependency.resolved.yaml` の識別子）を必須記録する。
- `lineage.json` は `node_key` と `direct_dependency_status` を必須記録する。

### 7-4) 再実行規則
- 同一 `plan_id` で `Generate` を複数回実行してよい。各試行は別 `generation_id` とする。
- 同一 `generation_id` で `Build` を複数回実行してよい。各試行は別 `build_id` とする。
- 同一 `build_id` で `Execute` を複数回実行してよい。各試行は別 `execution_id` とする。
- `Build` 開始条件は「対象 `generation_id` の `generate_meta.json` で `verification_status=pass`」である。
- `debug_mode=false` の `Generate` は `attempts/` を生成してはならない。
- `Judge` の入力は常に `execution_id` 配下成果物とし、他 `execution_id` との混在を禁止する。
- `Judge` 開始条件は「対象 `node_key` の直下依存 `node` が `pass` または `xfail`」である。未達時は `blocked` を返し、当該 `node` の workflow を `fail` で終了する。
- `spec_kind` を問わない workflow 実行では、同一 `topo_level` の独立 `node` を並列再実行してよい。
- 各ステージ `fail` 時は、下流ステージ開始条件を満たすためのファイル後付け生成を禁止する。

### 7-5) 参照規則
- `pipeline` から `plan` を参照するときは `node_key_safe + plan_id` を使う。相対ファイルパス直参照を禁止する。
- `execution` の再現は `lineage.json` と `trial_meta.json` のみで可能でなければならない。
- `lineage.json` と `trial_meta.json` の成果物参照パスは、リポジトリ相対の `workspace/` 起点で記録しなければならない。
- `trial_meta.json` は `runner_command`、`process_trace_ref`、`raw_artifact_refs` を必須記録しなければならない。未記録時は `pipeline fail` とする。
- `index/plan_index.json` と `index/pipeline_index.json` は探索専用とし、判定ロジックの正本に使ってはならない。
- `aggregate_verdict.json` は常に `dependency.resolved.yaml` と整合しなければならない。依存集合の省略を禁止する。

### 7-6) 依存 workflow 網羅チェック
- `dependency.resolved.yaml` の `node_key` 集合と `workspace/plans/*/<plan_id>/` の `node_key_safe` 集合は 1 対 1 で一致しなければならない。
- `dependency.resolved.yaml` の `node_key` 集合と `workspace/pipelines/*/<pipeline_id>/lineage.json` の `node_key` 集合は 1 対 1 で一致しなければならない。
- workflow ルートの網羅チェックは `workspace/` のみを対象とし、`workspace/` 以外のディレクトリを検査対象に含めない。
- `lineage.json` と `trial_meta.json` の成果物参照パスは、`python3 tools/validate_workspace_root.py` の検査を `pass` しなければならない。
- 異なる `node_key` の `generate/<generation_id>/src/` が完全一致する場合は `copy_based_artifact_reuse` として検出し、共通ライブラリ明示がない限り `invalid` とする。
- `spec_kind` を問わない workflow 実行の完了宣言前に、対象依存 `DAG` の `workspace/plans` / `workspace/pipelines` 成果物を削除してはならない。

補足:
- `impl` の仕様: `IMPL_PLAN_SPEC.md`
- 自動チューニング運用: `TUNING_WORKFLOW.md`
