# 全体ワークフロー: Spec -> Plan -> Generate -> Build -> Execute -> Judge -> Tune -> Promote
この文書は workflow の phase sequence、inter-phase input/output contract、workflow 共通規範を定義する。
terms は `GLOSSARY.md` を参照する。

## 目的
- workflow を `Spec -> Plan -> Generate -> Build -> Execute -> Judge -> Tune -> Promote` の順で定義する。
- `node` 間の実行順序を `spec` の依存宣言から決定し、各 `node` 内では `Plan -> Generate -> Build -> Execute -> Judge` の順で実行する。
- 各 phase の `execution input` と `verification input` と `output` を一意に定義する。
- workflow 横断の不変条件、artifact layout rules、完了判定基準を定義する。
- 独立 `node` の並列実行は明示的な指示がある場合に限定し、既定では逐次実行する。

## 適用範囲
- `spec` 起点モードと `resolved` 起点モードの workflow 実行
- `Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote`
- `node` 単位 workflow、依存 `DAG` 展開、`workspace/` 配下 artifact、正式版昇格

## 文書責務
- 本書（`WORKFLOW_CORE.md`）は workflow 共通の不変規範、phase sequence、`phase` 別 I/O 契約一覧、artifact layout rules、完了判定基準を canonical source として定義する。各 `phase` の詳細契約は [phases/](phases/) 配下のファイルを canonical source とする。
- `ORCHESTRATION.md` は workflow のエージェント階層実行規約を canonical source として定義する。
- `SPEC.md` は全体方針、`spec` 管理要件、registry 要件を canonical source として定義する。
- 実装 `Plan` の既定値適用規則は `IMPL_PLAN_SPEC.md` を canonical source とする。
- 各 phase の実行手順、再試行手順、ツール呼び出し順、失敗時オペレーションは対応 `SKILL.md` を canonical source とする。

## term rules
- `phase` は workflow を構成する論理単位を指し、`Spec` / `Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote` を含む。
- `step` は `ORCHESTRATION.md` で定義する実行単位を指し、1 つの phase に対応するオーケストレーション上の実行単位として扱う。
- `substep` は `step` を分解した下位実行単位を指し、例として `generate` / `verify` を含む。
- `stage` は `generated_by_stage`、`<stage>_meta.json`、`write_scope_baseline.json.stage` など既存フィールド名または既存プレースホルダー名としてのみ使用する。本文では `phase` または `step` の同義語として使用してはならない。

## workflow 全体像
### phase sequence
0. 仕様作成（人間）: `Controlled Spec`、`tests`、`deps` を定義する。
1. `Plan`: 実行条件、生成契約、検証契約、実装方針、依存 `DAG` を確定する。
2. `Generate`: `Plan` に基づいて `model` と `runner` を生成する。
3. `Build`: 生成コードを標準ビルドツールでコンパイルする。
4. `Execute`: 実行バイナリを起動し、一次証跡と `diagnostics` を記録する。
5. `Judge`: `raw/` 一次証跡から判定指標を再計算し、`verdict` を確定する。
6. `Tune`: 物理合格を維持したまま実装候補を探索する。
7. `Promote`: 合格 artifact を正式版として `releases/` へ昇格する。

## workflow 共通不変規範
1. `tests` 合格または workflow 進行を目的とした `dummy` 出力を禁止する。
2. `diagnostics.json` と `perf.json` は対象 `runner` の execution result としてのみ生成する。手書き生成、固定値埋め込み、外部後編集を禁止する。
3. `verdict.json` と `aggregate_verdict.json` は `tests.md` と同一 `execution_id` の実行 artifact から導出しなければならない。
4. phase inputが不足する場合は当該 phase を `fail` で停止し、推測補完を禁止する。
5. phase 失敗時に下流 phase 開始条件を満たす目的で artifact ファイルを人工生成してはならない。
6. 明示的な指定がない場合、既存 workflow 出力（過去 `plan_id` / `pipeline_id` / `generation_id` / `build_id` / `execution_id`）の内容参照を禁止する。
7. `workspace/` 配下に過去 artifact が存在する場合も、中身の閲覧と入力参照を禁止する。
8. `spec_kind` を問わない workflow 実行は、リポジトリ管理下の `spec` canonical source と当該試行で生成した前段 artifact のみを入力として使用する。
9. `docs/` と `spec/` と当該試行 artifact に定義されていない要求、判定規則、入出力契約を、`tools/` 配下の実装、検証 `script`、test code、validator code から抽出して補完してはならない。これらの実装は execution mechanism または gate implementation であり、要求定義の canonical source ではない。
10. `spec_kind` を問わない workflow 実行は、各 phase（`Plan` / `Generate` / `Build` / `Execute` / `Judge`）を `LLM` で実行しなければならない。専用実行スクリプト前提、手動 `copy`、手動 `json` 生成、手動 `id` 差し替えを禁止する。
11. `workflow` 実行のために、複数 phase を一括代行する `script`（例: `python` / `bash`）を新規生成または実行してはならない。phase 実行は `orchestration agent -> step agent` または `orchestration agent -> substep agent` のみを許可する。
12. workflow artifact の保存先ルートは `workspace/` のみを許可する。`workspace/` が存在しない場合はリポジトリルート直下へ作成する。
13. workflow 実行中は対象 `DAG` の `workspace/plans` と `workspace/pipelines` 配下 artifact を削除してはならない。
14. `quality check` は `diagnostics.json` と `verdict.json` の比較を canonical source とし、`stdout` 差分のみで合否を確定してはならない。
15. `lineage.json` と `trial_meta.json` の artifact 参照パスは `workspace/` 起点で記録しなければならない。
16. `trial_meta.json` は `generated_by_stage`、`source_execution_id`、`source_command_ref`、`source_artifact_hash` を必須記録とする。
17. 異なる `pipeline_id` 間で `id` 系メタデータのみを変更して artifact 本文を流用してはならない。検出時は `copy_based_artifact_reuse` として `invalid` とする。
18. 本規範違反は workflow 仕様違反とし、当該 `pipeline` を `invalid` とする。
19. `Promote` 以外の phase は、`workspace/` 配下以外へ書き込みを行ってはならない。`Promote` は `releases/` 配下と `spec/registry/spec_catalog.yaml` への書き込みのみを許可する。
20. `Promote` 以外の phase 開始前に、リポジトリルート配下ファイル集合の `baseline` を取得し、当該 phase 完了前に差分比較を実施しなければならない。
21. 差分比較は `workspace/` 配下以外の `add` / `modify` / `delete` を違反として検出しなければならない。`Promote` は `releases/` 配下と `spec/registry/spec_catalog.yaml` のみを例外許可する。
22. `python` 実行を workflow 経路で使用する場合、`__pycache__` が `workspace/` 配下以外へ生成されない設定を必須とする。`PYTHONDONTWRITEBYTECODE=1` または `PYTHONPYCACHEPREFIX=workspace/.pycache/<pipeline_id>/` を使用する。
23. 書き込み範囲違反を検出した phase は `fail` とし、下流 phase を開始してはならない。違反内容は `workspace/` 配下のメタデータへ記録しなければならない。
24. 書き込み範囲違反を検出した `pipeline` は `invalid` とする。違反状態を解消せずに同一試行を継続してはならない。
25. `workflow` の階層実行契約、`preflight`、`agent_runs.jsonl`、`agent_graph.json`、`step_result.json` の要件は `ORCHESTRATION.md` を canonical source として適用しなければならない。
26. `preflight` が `fail` の場合、`orchestration agent` は子 `agent` を起動してはならない。`workflow` は `fail` で停止しなければならない。
27. `preflight.json` を手動編集または後編集して `pass` 化してはならない。`preflight` canonical source は `tools/codex_orchestration_runtime.py preflight --backend <backend>` の execution result とする。`backend` 未指定時は既定値 `codex` とする。
28. 子 `agent` 起動直前に execution platform の live 検査を再実行し、`multi_agent=true` と子 `agent` 起動可否の充足を確認しなければならない。未充足時は即時 `fail` とする。
29. 出力形式、input/output contract、判定条件の要求定義は `controlled_spec.md` と `tests.md` と `deps.yaml` と `derived_contract.json` と `docs/` canonical source 文書のみを参照しなければならない。
30. `tools/` 配下の検証 `python` スクリプト、`quality check` 実装、`verify` 実装は妥当性確認専用入力として扱い、要求定義または出力形式定義の入力として参照してはならない。
31. 要求定義が不足する場合、検証実装からの逆算補完を禁止し、当該 phase を `fail` で停止しなければならない。
32. `quality check` 実行に必要な preset-compatible quality path は `Generate` の正式出力だけで成立しなければならない。下流 phase が `workspace/` 配下へ `test` source、harness、補助 `script`、一時 `Makefile` を追加生成して成立させる運用を禁止する。
33. `quality check` 実行方式は `impl.resolved.yaml` の `toolchain.language` と `toolchain.build_system` に整合しなければならない。`toolchain.build_system=make` かつ `toolchain.language=fortran` / `c` / `cpp` / `mixed` 系では `make_test` または `make_check` を使用し、`pytest` による代替を禁止する。
34. 依存関係上は独立な `node` であっても、workflow は明示的な並列実行指示が存在しない限り逐次実行しなければならない。依存充足のみを根拠に自動並列実行してはならない。

## 共通規約
### `LLM` 利用 phase
- `LLM` を利用する全 phase に `SPEC.md` の「`LLM` の扱い（全体原則）」を適用する。
- `LLM` 利用 phase は各 phase の `<stage>_meta.json`（コード生成は `generate_meta.json`）を必須出力とする。
- `debug_mode=false` では失敗試行 artifact を保存しない。`debug_mode=true` で保存した場合は保存件数と保存先をメタデータへ記録する。

### エージェント階層実行
- `workflow` の階層実行契約、親子関係、起動順、停止条件、実行記録形式は `ORCHESTRATION.md` を適用する。
- 本書は `orchestration agent` が子 `agent` へ渡す phase contractの canonical source として、各 phase の `execution input` と `verification input` と `出力` を定義する。

### artifact layout rules
#### ルート構造
workflow artifact の保存先は `workspace/` を canonical source とし、次の構造を必須とする。

```text
workspace/
  orchestrations/
    <orchestration_id>/
      orchestration_meta.json
      agent_graph.json
      agent_runs.jsonl
      steps/
        <node_key_safe>/
          <step>/
            <agent_run_id>/
              step_result.json
  plans/
    <node_key_safe>/
      <plan_id>/
        case.resolved.yaml
        algorithm.resolved.yaml
        impl.resolved.yaml
        dependency.resolved.yaml
        derived_contract.json
        algorithm.summary.md
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

#### `ID` と不変条件
- `orchestration_id` は 1 回の workflow 全体を識別する `ID` とする。
- `node_key_safe` は `node_key` の保存用表記とし、推奨形式は `<spec_kind>__<spec_id>__<spec_version>` とする。
- `plan_id` は `node` 単位で `case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` の組を識別する `ID` とする。推奨形式は `<node_key_safe>_<case_hash12>_<algorithm_hash12>_<impl_hash12>` とする。
- `pipeline_id` は `node` 単位で 1 回の `Generate -> Build -> Execute` 系列を識別する `ID` とする。推奨形式は `<plan_id>_<utc_ts>_<seq3>` とする。
- `generation_id` / `build_id` / `execution_id` は各段階の試行単位 `ID` とする。
- workflow は毎回独立実行し、`plan_id` / `pipeline_id` / `generation_id` / `build_id` / `execution_id` を毎回新規発行しなければならない。
- `agent_run_id` は `step agent` / `substep agent` / `orchestration agent` の実行単位 `ID` とし、`step` / `substep` では `parent_agent_run_id` を必須記録とする。
- `agent_runs.jsonl` の `step` / `substep` ロールは `agent_backend` と `agent_model` と `context_id` と `context_isolated` を必須記録とする。
- `agent_runs.jsonl` の終端状態行（`pass` / `fail` / `blocked` / `timeout` / `cancel`）は `finished_at` を必須記録とする。
- `step` / `substep` ロールの `context_id` は `orchestration_id` 内で一意でなければならない。
- `execution` の判定単位は `node_key` とする。`execution_id` 配下で複数 `node_key` を扱う場合は `node_key` ごとの artifact 分離を必須とする。
- `plan_id` 配下の `resolved` ファイルは `immutable` とし、更新時は新規 `plan_id` を発行する。
- `pipeline_id` 配下は `append-only` とし、既存 `execution_id` の上書きを禁止する。

#### 起点モード
- `spec` 起点モード: `spec` から依存 `DAG` を解決し、`node` ごとに新しい `plan_id` を発行して `pipeline` を開始する。
- `resolved` 起点モード: 既存 `plan_id` を指定し、`Generate` 以降のみを実行する。
- `lineage.json` は `spec_ref`、`plan_ref`、各段階 `id`、`dependency_ref`、`node_key`、`direct_dependency_status` を必須記録とする。

#### 再実行規則
- 同一 `plan_id` で `Generate` を複数回実行してよい。各試行は別 `generation_id` とする。
- 同一 `generation_id` で `Build` を複数回実行してよい。各試行は別 `build_id` とする。
- 同一 `build_id` で `Execute` を複数回実行してよい。各試行は別 `execution_id` とする。
- `Build` 開始条件は対象 `generation_id` の `generate_meta.json` で `verification_status=pass` であることとする。
- `debug_mode=false` の `Generate` は `attempts/` を生成してはならない。
- `Judge` 入力は常に同一 `execution_id` 配下 artifact とし、他 `execution_id` との混在を禁止する。
- 各 phase `fail` 時は下流 phase 開始条件を満たす目的のファイル後付け生成を禁止する。
- `substep` を持つ phase の再投入戦略（`repair_strategy=reuse` / `restart`）と記録要件は `ORCHESTRATION.md` を canonical source として適用する。

#### 参照規則
- `orchestration` から `step` / `substep` 実行を参照するときは `orchestration_id + agent_run_id` を使用し、ログ本文の全文検索だけで追跡してはならない。
- `step` 完了判定は `workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/step_result.json` を canonical source とし、`stdout` 文字列のみで代替してはならない。`substep` を持つ phase では `agent_run_id=orchestration agent_run_id`、標準 `substep` を持たない phase では `agent_run_id=step agent_run_id` を canonical source とする。
- `pipeline` から `plan` を参照するときは `node_key_safe + plan_id` を使用し、相対ファイルパス直参照を禁止する。
- `execution` の再現は `lineage.json` と `trial_meta.json` のみで可能でなければならない。
- `trial_meta.json` は `runner_command`、`process_trace_ref`、`raw_artifact_refs` を必須記録とする。
- `index/plan_index.json` と `index/pipeline_index.json` は探索専用とし、判定ロジックの canonical source に使ってはならない。
- `aggregate_verdict.json` は常に `dependency.resolved.yaml` と整合し、依存集合の省略を禁止する。

#### 依存 workflow 網羅チェック
- `dependency.resolved.yaml` の `node_key` 集合と `workspace/plans/*/<plan_id>/` の `node_key_safe` 集合は 1 対 1 で一致しなければならない。
- `dependency.resolved.yaml` の `node_key` 集合と `workspace/pipelines/*/<pipeline_id>/lineage.json` の `node_key` 集合は 1 対 1 で一致しなければならない。
- `dependency.resolved.yaml` が `all_nodes` を保持する場合、`python3 tools/validate_pipeline_semantics.py` の `--stage post_execute`、`--stage pre_judge`、および省略時（`--stage full` 相当）の invocation は、`all_nodes` の全 `node_key` について `lineage` と `plan_ref` の両方を検証し、未発行 `node` を `fail` としなければならない。
- 異なる `node_key` で生成された `generate/<generation_id>/src/` のコードハッシュが一致した場合、共通ライブラリとして明示されたファイルを除き `copy_based_artifact_reuse` として `invalid` にしなければならない。
- `spec_kind` を問わない workflow 実行の完了宣言前に、対象依存 `DAG` の `workspace/plans` / `workspace/pipelines` artifact を削除してはならない。

#### 書き込み範囲ガード
- 各 phase 開始時に `write_scope_baseline.json` を `workspace/` 配下へ保存し、比較対象の `baseline` を固定しなければならない。
- `write_scope_baseline.json` は、少なくとも `stage`、`node_key`、`pipeline_id`、`captured_at`、`tracked_diff`、`untracked_files` を保持しなければならない。
- 各 phase 完了前に `write_scope_baseline.json` との差分を計算し、`workspace/` 配下以外の変化を `write_scope_violation` として判定しなければならない。
- 違反未検出時は `write_scope_check.status=pass` を phase メタデータへ記録しなければならない。
- 違反検出時は `write_scope_violation.json` を `workspace/` 配下へ出力し、`violation_paths` と `stage` と `node_key` と `pipeline_id` と `detected_at` を必須記録しなければならない。
- `write_scope_violation` 検出時は当該 phase を `fail` とし、当該 `pipeline` の `aggregate_verdict` 確定を禁止する。

## phase 別 input/output contract 一覧
本節では、各 phase の入力を `execution input` と `verification input` に分けて記述する。両者の role が重なる場合、同一 artifact を両方へ記載してよい。

### 0. 仕様作成（人間）
- execution input: workflow 外部で与える要求事項、物理要件、依存選択方針
- verification input: なし
- 出力: `spec/<spec_kind>/<domain>/<family>/<spec_id>/controlled_spec.md`、`spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md`、`spec/<spec_kind>/<domain>/<family>/<spec_id>/deps.yaml`

### 1. Plan
- execution input: `controlled_spec.md`、`tests.md`、`deps.yaml`、`spec/registry/spec_catalog.yaml`
- verification input: `controlled_spec.md`、`tests.md`、`deps.yaml`、`spec/registry/spec_catalog.yaml`
- 出力: `case.resolved.yaml`、`algorithm.resolved.yaml`、`impl.resolved.yaml`、`dependency.resolved.yaml`、`derived_contract.json`、`algorithm.summary.md`、`plan_meta.json`

### 2. Generate
- execution input: `case.resolved.yaml`、`algorithm.resolved.yaml`、`impl.resolved.yaml`、`dependency.resolved.yaml`
- verification input: `case.resolved.yaml`、`algorithm.resolved.yaml`、`derived_contract.json`、`dependency.resolved.yaml`、`impl.resolved.yaml`
- 出力: `generate/<generation_id>/src/`、`generate_meta.json`
- `Generate` 完了前に、`impl.resolved.yaml` の `toolchain.language` に整合する MCP `run_linter` を成功させ、`generate_meta.json` の `lint_command_ref.run_linter` へ MCP 証跡（各要素に `command_id` と `command_log_ref` と `preset`）を記録しなければならない。`static lint` は `Build` の `compile_project` や `toolchain.build_system` が経由するビルドではなく、専用リンター（例: `fortitude` / `cppcheck` / `ruff`）を `run_linter` の `preset` のみで起動する。`Execute` の `quality check`（`run_quality_checks`）とは別物であり、`diagnostics.json` や `verdict.json` の比較を canonical source としない。
- `Generate` は `Execute` が `run_quality_checks` の `preset` だけで実行可能な preset-compatible quality path を正式出力へ含めなければならない。不足時は `Generate fail` とする。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` / `c` / `cpp` / `mixed` 系では、`generate/<generation_id>/src/Makefile` に `test` または `check` target を必須定義しなければならない。欠落時は `Generate fail` とする。
- `Generate verify` は、`quality check` 成立のために下流 phase で追加 source 生成が不要であることを検査しなければならない。

### 3. Build
- execution input: `generate/<generation_id>/src/`、`impl.resolved.yaml`
- verification input: `dependency.resolved.yaml`、`generate_meta.json`、`impl.resolved.yaml`
- 出力: `build/<build_id>/bin/`、`build_meta.json`、`compile_project` の `command_id` と `command_log_ref`

### 4. Execute
- execution input: `build/<build_id>/bin/`、`case.resolved.yaml`
- verification input: `derived_contract.json`、`dependency.resolved.yaml`、`build/<build_id>/bin/`
- 出力: `diagnostics.json`、`perf.json`、`quality_check.json`、`raw/`、`stdout.log`、`stderr.log`、`run_program` の `command_id` と `command_log_ref`

### 5. Judge
- execution input: `tests.md`、`derived_contract.json`、同一 `execution_id` 配下の `raw/`
- verification input: `dependency.resolved.yaml`、同一 `execution_id` 配下の `diagnostics.json` / `perf.json` / `quality_check.json` / `raw/`、対象 `generation_id` の `model` / `runner`
- 出力: `semantic_review.json`、`verdict.json`、`aggregate_verdict.json`、`summary.json`、`trial_meta.json`

### 6. Tune
- execution input: 固定した `case.resolved.yaml`、探索対象 `impl` 候補
- verification input: 候補ごとの `diagnostics.json` / `perf.json` / `verdict.json`
- 出力: 採用 `impl.resolved.yaml`、チューニング試行ごとの評価結果

### 7. Promote
- execution input: 採用 `impl.resolved.yaml`、`lineage.json`、採用対象の生成物
- verification input: `verdict.json`、`aggregate_verdict.json`、`trial_meta.json`、`lineage.json`
- 出力: `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` 配下の正式版 artifact、`spec/registry/spec_catalog.yaml` の `official_releases` 更新

## phase 詳細（参照）

本節では、標準 `substep` を持つ `Plan` / `Generate` / `Tune` を `generate substep` と `verify substep` に分けて記述する。標準 `substep` を持たない `Build` / `Execute` / `Judge` / `Promote` は単一 `step` として記述する。

phase ごとの契約詳細は [phases/](phases/) 配下のファイルを canonical source とする。

| phase | ファイル |
|-------|----------|
| 0 仕様作成（人間） | [phases/phase_00_spec.md](phases/phase_00_spec.md) |
| 1 Plan | [phases/phase_01_plan.md](phases/phase_01_plan.md) |
| 2 Generate | [phases/phase_02_generate.md](phases/phase_02_generate.md) |
| 3 Build | [phases/phase_03_build.md](phases/phase_03_build.md) |
| 4 Execute | [phases/phase_04_execute.md](phases/phase_04_execute.md) |
| 5 Judge | [phases/phase_05_judge.md](phases/phase_05_judge.md) |
| 6 Tune | [phases/phase_06_tune.md](phases/phase_06_tune.md) |
| 7 Promote | [phases/phase_07_promote.md](phases/phase_07_promote.md) |

## エージェント参照範囲

- 子 `step agent` / `substep agent` の `skill_must_read_refs` は `tools/codex_orchestration_runtime.py` の `build_skill_must_read_refs` で組み立てられる。
- 既定では `docs/workflow/WORKFLOW_CORE.md` と `docs/ORCHESTRATION.md` と対象 phase の `docs/workflow/phases/phase_*.md` と `skill_ref` と verify 必須 artifact を含む。`docs/WORKFLOW.md` は仕様への入口である。

## 完了判定基準
- workflow 完了条件は、対象 workflow の `orchestration_id` 配下に `orchestration_meta.json` と `agent_graph.json` と `agent_runs.jsonl` が存在することとする。
- workflow 完了条件は、`dependency.resolved.yaml` の全 `node_key` に対して `workspace/plans/<node_key_safe>/<plan_id>/` と `workspace/pipelines/<node_key_safe>/<pipeline_id>/` が存在し、`lineage.json` の `node_key` と `dependency_ref` が一致することとする。
- workflow 完了宣言は、`dependency workflow` 網羅チェックと `trial_meta` 完整性チェックと `copy_based_artifact_reuse` 非検出を同時に満たす場合のみ許可する。
- workflow 完了宣言は、上位 `node` の `src/` に依存 `node` 実装の内包が存在しないことを同時に満たす場合のみ許可する。
- workflow 完了宣言は、全 phase で `write_scope_violation` 非検出を同時に満たす場合のみ許可する。
- `CI` は `python3 tools/validate_workspace_root.py` と `python3 tools/validate_pipeline_semantics.py`（`--stage full` または省略時）の execution result を `pass` 条件として扱う。

## 参照文書
- `IMPL_PLAN_SPEC.md`
- `ORCHESTRATION.md`
- `PERFORMANCE_DIAGNOSTICS.md`
- `SPEC.md`
- `TUNING_WORKFLOW.md`
