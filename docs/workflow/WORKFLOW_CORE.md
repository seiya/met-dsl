# 全体ワークフロー共通契約: Spec -> Compile -> Generate -> Build -> Validate

この文書は workflow の phase sequence、inter-phase input/output contract、workflow 共通規範を定義する。terms は `GLOSSARY.md` を参照する。

## 目的
- workflow を `Spec -> Compile -> Generate -> Build -> Validate` の 5 phase で定義する。
- phase 境界は **observable な primary producer の階層** で切る。各 phase は一次成果物を 1 種類だけ生産する。
- `node` 間の実行順序は `spec` の依存宣言から決定し、各 `node` 内では `Compile -> Generate -> Build -> Validate` の順で実行する。
- 各 phase の `execution input` と `verification input` と `output` を一意に定義する。
- 独立 `node` の並列実行は明示的な指示がある場合に限定し、既定では逐次実行する。

## 適用範囲
- `spec` 起点モードと `resolved` 起点モードの workflow 実行
- `Compile` / `Generate` / `Build` / `Validate`
- `node` 単位 workflow、依存 `DAG` 展開、`workspace/` 配下 artifact

## 文書責務
- 本書（`WORKFLOW_CORE.md`）は workflow 共通の不変規範、phase sequence、`phase` 別 I/O 契約一覧、artifact layout rules、完了判定基準を canonical source として定義する。各 `phase` の詳細契約は [phases/](phases/) 配下のファイルを canonical source とする。
- `ORCHESTRATION.md` は workflow のエージェント階層実行規約を canonical source として定義する。
- `SPEC.md` は全体方針、`spec` 管理要件、registry 要件を canonical source として定義する。
- 各 phase の実行手順、再試行手順、ツール呼び出し順、失敗時オペレーションは対応 `SKILL.md` を canonical source とする。
- 任意フロー（`Tune` / `Promote`）の契約は別 plan で扱う。core workflow には含めない。

## term rules
- `phase` は workflow を構成する論理単位を指し、`Spec` / `Compile` / `Generate` / `Build` / `Validate` を含む。
- `step` は 1 つの phase に対応するオーケストレーション上の実行単位として扱う。
- `substep` は `step` を分解した下位実行単位を指す。
  - `Compile` / `Generate` は `generate` と `verify` の 2 substep を持つ。
  - `Validate` は `execute` と `judge` の 2 substep を持つ。
  - `Build` は標準 substep を持たない単一 step とする。
- `stage` は `generated_by_stage`、`<stage>_meta.json`、`write_scope_baseline.json.stage` など既存フィールド名としてのみ使用する。本文では `phase` または `step` の同義語として使用してはならない。

## workflow 全体像
### phase sequence
0. `Spec`（人手）: `controlled_spec.md`、`tests.md`、`deps.yaml` を作成する。
1. `Compile`: 自然言語仕様 + 依存解決を **単一構造 IR** (`spec.ir.yaml`) に統合する。
2. `Generate`: IR を入力に `model` と `runner` のソースを生成する。
3. `Build`: 生成ソースを標準ビルドツールで決定的にバイナリ化する。
4. `Validate`: バイナリを実行し、一次証跡から判定指標を再計算して `verdict` を確定する。

### primary deliverable
| phase | primary deliverable | 性質 |
|-------|---------------------|------|
| Spec | `controlled_spec.md` / `tests.md` / `deps.yaml` | 自然言語（人手） |
| Compile | `spec.ir.yaml` | 構造化（LLM） |
| Generate | `source/<source_id>/` 配下のコード | ソース（LLM） |
| Build | `binary/<binary_id>/bin/` | バイナリ（決定的） |
| Validate | `verdict.json` / `aggregate_verdict.json` | 判定（実行 + LLM） |

## workflow 共通不変規範
1. `tests` 合格または workflow 進行を目的とした `dummy` 出力を禁止する。
2. `diagnostics.json` と `perf.json` は対象 `runner` の execution result としてのみ生成する。手書き、固定値埋め込み、外部後編集を禁止する。
3. `verdict.json` と `aggregate_verdict.json` は `tests.md` と同一 `run_id` の実行 artifact から導出しなければならない。
4. phase input が不足する場合は当該 phase を `fail` で停止し、推測補完を禁止する。
5. phase 失敗時に下流 phase 開始条件を満たす目的で artifact ファイルを人工生成してはならない。
6. 明示的な指定がない場合、既存 workflow 出力（過去 `ir_id` / `pipeline_id` / `source_id` / `binary_id` / `run_id`）の内容参照を禁止する。`orchestration_meta.json` に `resume_enabled=true` が記録されている orchestration では、`orchestration_checkpoint.json` に記録された完了済みステップの artifact 参照を許可する。
7. `workspace/` 配下に過去 artifact が存在する場合も、中身の閲覧と入力参照を禁止する。
8. workflow 実行は、リポジトリ管理下の `spec` canonical source と当該試行で生成した前段 artifact のみを入力として使用する。
9. `docs/` と `spec/` と当該試行 artifact に定義されていない要求、判定規則、入出力契約を、`tools/` 配下の実装、検証 script、test code、validator code から抽出して補完してはならない。
10. workflow 実行は、各 phase（`Compile` / `Generate` / `Validate`）を `LLM` で実行しなければならない。`Build` は決定的処理であり、MCP サーバー経由のビルドコマンド呼び出しで実行する。
11. workflow 実行のために、複数 phase を一括代行する script を新規生成または実行してはならない。phase 実行は `orchestration agent -> step agent` または `orchestration agent -> substep agent` のみを許可する。
12. workflow artifact の保存先ルートは `workspace/` のみを許可する。`workspace/` が存在しない場合はリポジトリルート直下へ作成する。
13. workflow 実行中は対象 `DAG` の `workspace/ir` と `workspace/pipelines` 配下 artifact を削除してはならない。
14. `quality check` は `diagnostics.json` と `verdict.json` の比較を canonical source とし、`stdout` 差分のみで合否を確定してはならない。
15. `lineage.json` と `trial_meta.json` の artifact 参照パスは `workspace/` 起点で記録しなければならない。
16. `trial_meta.json` は `generated_by_stage`、`source_source_id`、`source_binary_id`、`source_command_ref`、`source_artifact_hash` を必須記録とする (`run_id` は trial_meta が配置される `runs/<run_id>/` directory path 自体が canonical encoding であり、別途 `source_run_id` フィールドを記録しない — self-referential / circular なため)。`source_command_ref` の各 entry は `tool_name` (`run_program` または `run_quality_checks`) を宣言し、対応する MCP `command_log` record の `tool_name` と一致しなければならない。`Validate` の execute 部の trial_meta は最低 1 つ `tool_name='run_program'` の entry を持たなければならない。`source_source_id` の指す `source_meta.json` は `verification_status=pass` でなければならない。`source_binary_id` の指す `<pipeline>/binary/<source_binary_id>/bin/` は実在し、`run_program` log record の executable はその bin/ 配下に解決しなければならない。
17. 異なる `pipeline_id` 間で `id` 系メタデータのみを変更して artifact 本文を流用してはならない。検出時は `copy_based_artifact_reuse` として `invalid` とする。
18. 本規範違反は workflow 仕様違反とし、当該 `pipeline` を `invalid` とする。
19. core workflow の全 phase は `workspace/` 配下以外へ書き込みを行ってはならない。任意フロー（`Promote`）の例外は別 plan で定義する。
20. 全 phase 開始前に、リポジトリルート配下ファイル集合の `baseline` を取得し、当該 phase 完了前に差分比較を実施しなければならない。
21. 差分比較は `workspace/` 配下以外の `add` / `modify` / `delete` を違反として検出しなければならない。
22. `python` 実行を workflow 経路で使用する場合、`__pycache__` が `workspace/` 配下以外へ生成されない設定を必須とする。`PYTHONDONTWRITEBYTECODE=1` または `PYTHONPYCACHEPREFIX=workspace/.pycache/<pipeline_id>/` を使用する。
23. 書き込み範囲違反を検出した phase は `fail` とし、下流 phase を開始してはならない。違反内容は `workspace/` 配下のメタデータへ記録しなければならない。
24. 書き込み範囲違反を検出した `pipeline` は `invalid` とする。違反状態を解消せずに同一試行を継続してはならない。
25. workflow の階層実行契約、`preflight`、`agent_runs.jsonl`、`agent_graph.json`、`step_result.json` の要件は `ORCHESTRATION.md` を canonical source として適用しなければならない。
26. workflow 起動は `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]` を canonical entrypoint とする。`<until_phase>` は `compile` / `generate` / `build` / `validate` のいずれかを指定する。
27. `preflight` が `fail` の場合、`orchestration agent` は子 `agent` を起動してはならない。workflow は `fail` で停止しなければならない。
28. `preflight.json` を手動編集または後編集して `pass` 化してはならない。
29. 子 `agent` 起動直前に execution platform の live 検査を再実行し、`multi_agent=true` と子 `agent` 起動可否の充足を確認しなければならない。
30. `workspace/ir/` と `workspace/pipelines/` の phase artifact は、正規 child `agent` capability 以外で生成してはならない。`orchestration agent` は reservation artifact のみを生成できる。
31. 出力形式、input/output contract、判定条件の要求定義は `controlled_spec.md` と `tests.md` と `deps.yaml` と `spec.ir.yaml` と `docs/` canonical source 文書のみを参照しなければならない。
32. `tools/` 配下の検証 python script、quality check 実装、verify 実装は妥当性確認専用入力として扱い、要求定義または出力形式定義の入力として参照してはならない。
33. 要求定義が不足する場合、検証実装からの逆算補完を禁止し、当該 phase を `fail` で停止しなければならない。
34. `quality check` 実行に必要な preset-compatible quality path は `Generate` の正式出力だけで成立しなければならない。下流 phase が `workspace/` 配下へ test source、harness、補助 script、一時 Makefile を追加生成して成立させる運用を禁止する。
35. `quality check` 実行方式は `spec.ir.yaml` の `impl_defaults.toolchain.language` と `impl_defaults.toolchain.build_system` に整合しなければならない。`toolchain.build_system=make` かつ `toolchain.language=fortran` / `c` / `cpp` / `mixed` 系では `make_test` または `make_check` を使用し、`pytest` による代替を禁止する。
36. 依存関係上は独立な `node` であっても、workflow は明示的な並列実行指示が存在しない限り逐次実行しなければならない。

## 共通規約
### `LLM` 利用 phase
- `LLM` を利用する全 phase に `SPEC.md` の「`LLM` の扱い（全体原則）」を適用する。
- `LLM` 利用 phase は各 phase の `<stage>_meta.json`（`Compile` は `ir_meta.json`、`Generate` は `source_meta.json`、`Validate` は `validate_meta.json`）を必須出力とする。
- `<stage>_meta.json` の共通必須 key は `attempt_count`、`verification_status`、`last_fail_reason`、`debug_mode`、`context_isolated` とする。
- `context_isolated=false` の場合、`constraint_reason` を必須とする。
- `ir_meta.json` は上記共通 key のみを必須とする。
- `source_meta.json` は上記共通 key を必須とし、`verification_status=pass` の場合のみ `lint_command_ref.run_linter[]`（`command_id`、`command_log_ref`、`preset`）を必須とする。
- `validate_meta.json` は上記共通 key を必須とし、`verification_status=pass` の場合のみ `judge_command_ref` で LLM 意味検査の証跡を必須とする。
- `debug_mode=false` では失敗試行 artifact を保存しない。`debug_mode=true` で保存した場合は保存件数と保存先をメタデータへ記録する。
- workflow 起動時の execution mode は `tools/run_workflow.py` の `--mode` で指定し、既定値は `dev` とする。
- `dev` mode の `verify substep` は、`issue_severity=minor` のみを軽微問題として継続可能とし、`major` / `critical` は `fail` としなければならない。
- `dev` mode で workflow が `fail` した場合、`workspace/orchestrations/<orchestration_id>/failure_analysis.json` を生成し、失敗理由、関連 `agent_run`、関連 `step_result`、補助ログ要約を記録しなければならない。

### エージェント階層実行
- workflow の階層実行契約、親子関係、起動順、停止条件、実行記録形式は `ORCHESTRATION.md` を適用する。
- 本書は `orchestration agent` が子 `agent` へ渡す phase contract の canonical source として、各 phase の `execution input` と `verification input` と `出力` を定義する。

### artifact layout rules
#### ルート構造
workflow artifact の保存先は `workspace/` を canonical source とし、次の構造を必須とする。

```text
workspace/
  orchestrations/
    <orchestration_id>/
      orchestration_meta.json
      preflight.json
      phase_state.json
      phase_state_log.jsonl
      orchestration_checkpoint.json
      agent_graph.json
      agent_runs.jsonl
      launches/
        <agent_run_id>.request.json
        <agent_run_id>.response.json
        <agent_run_id>.prompt.txt
        <agent_run_id>.reply.txt
      agents/
        <agent_run_id>/
          dialogs/
            child.request.json
            child.response.json
            child.prompt.txt
            child.reply.txt
            agent.result.json
            agent.summary.txt
      access_policies/
      access_logs/
      capabilities/
      gates/
      violations/
      steps/
        <node_key_safe>/
          <step>/
            <agent_run_id>/
              step_result.json
  ir/
    <node_key_safe>/
      <ir_id>/
        spec.ir.yaml
        ir_meta.json
  pipelines/
    <node_key_safe>/
      <pipeline_id>/
        lineage.json
        source/
          <source_id>/
            src/
            source_meta.json
            attempts/  # optional: debug_mode=true の場合のみ
        binary/
          <binary_id>/
            bin/
            binary_meta.json
        runs/
          <run_id>/
            <node_key_safe>/
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
              validate_meta.json
              stdout.log
              stderr.log
  index/
    ir_index.json
    pipeline_index.json
```

#### `ID` と不変条件

##### `node_key` フォーマット
- `node_key` は `<spec_kind>/<spec_id>@<spec_version>` 形式とする。
  - `spec_kind`: `deps.yaml` の `spec_kind` フィールド値（例: `component`、`problem`、`profile`）
  - `spec_id`: `deps.yaml` の `spec_id` フィールド値
  - `spec_version`: `controlled_spec.md` の `spec_version` フィールド値
- `node_key_safe` は `node_key` の保存用表記とし、`<spec_kind>__<spec_id>__<spec_version>` 形式とする。
  - 正規表現: `^[a-z][a-z0-9_]*__[a-z0-9][a-z0-9_]*__[0-9][0-9A-Za-z._-]*$`

##### `ID` 命名規則
- `orchestration_id` は 1 回の workflow 全体を識別する `ID` とする。
- `ir_id` は `node` 単位で `spec.ir.yaml` を識別する `ID` とする。
  - 形式: `<slug>_<YYYYMMDD>_<seq3>`
  - `slug` は `spec_id` 由来の短い可読 token（ハイフン区切り、英数字）。
  - 正規表現: `^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$`
- `pipeline_id` は `node` 単位で 1 回の `Generate -> Build -> Validate` 系列を識別する `ID` とする。`ir_id` と同一形式・正規表現とする。
- `source_id` / `binary_id` / `run_id` は各段階の試行単位 `ID` とし、推奨形式は `<prefix>_<date>_<seq3>` とする。`prefix` は `src` / `bin` / `run` を使用する。
- workflow は毎回独立実行し、`ir_id` / `pipeline_id` / `source_id` / `binary_id` / `run_id` を毎回新規発行しなければならない。
- `agent_run_id` は `step agent` / `substep agent` / `orchestration agent` の実行単位 `ID` とし、`step` / `substep` では `parent_agent_run_id` を必須記録とする。
- `agent_runs.jsonl` の `step` / `substep` ロールは `agent_backend` と `agent_model` と `context_id` と `context_isolated` を必須記録とする。
- `agent_runs.jsonl` の終端状態行（`pass` / `fail` / `blocked` / `timeout` / `cancel`）は `finished_at` を必須記録とする。
- `step` / `substep` ロールの `context_id` は `orchestration_id` 内で一意でなければならない。
- `Validate` の判定単位は `node_key` とする。`run_id` 配下で複数 `node_key` を扱う場合は `node_key` ごとの artifact 分離を必須とする。
- `ir_id` 配下の `spec.ir.yaml` は `immutable` とし、更新時は新規 `ir_id` を発行する。
- `pipeline_id` 配下は `append-only` とし、既存 `run_id` の上書きを禁止する。

#### 起点モード
- `spec` 起点モード: `spec` から依存 `DAG` を解決し、`node` ごとに新しい `ir_id` を発行して `pipeline` を開始する。
- `ir` 起点モード: 既存 `ir_id` を指定し、`Generate` 以降のみを実行する。
- `lineage.json` は `spec_ref`、`ir_ref`、各段階 `id`、`dependency_ref`、`node_key`、`direct_dependency_status` を必須記録とする。

#### 再実行規則
- 同一 `ir_id` で `Generate` を複数回実行してよい。各試行は別 `source_id` とする。
- 同一 `source_id` で `Build` を複数回実行してよい。各試行は別 `binary_id` とする。
- 同一 `binary_id` で `Validate` を複数回実行してよい。各試行は別 `run_id` とする。これは full Validate retry (`execute` 再実行 + `judge` 再評価) と judge 単独再評価 (`execute` 出力を流用して `judge` のみ再実行) の両方に適用する: いずれの場合も新規 `run_id` を発行し、既存 `runs/<run_id>/` ディレクトリに上書きしてはならない。同一 `run_id` 配下で `judge` を 2 回以上実行すると `verdict.json` / `aggregate_verdict.json` / `summary.json` / `semantic_review.json` (judge の canonical 出力) が上書きされて以前の判定根拠が失われるため、同一 `run_id` の再利用を禁止する。judge 単独再評価で `execute` の `raw/` / `diagnostics.json` / `perf.json` / `quality_check.json` / `trial_meta.json` をそのまま流用する場合、orchestration agent はそれらを新 `run_id` 配下にコピーし、`trial_meta.json` の `source_source_id` / `source_binary_id` は元 `run_id` と一致しなければならない (binary を生成した source と build の provenance を維持するため)。さらに、判定根拠の存続条件として、`trial_meta.json.source_source_id` の指す `<pipeline_ref>/source/<source_source_id>/` ディレクトリと `trial_meta.json.source_binary_id` の指す `<pipeline_ref>/binary/<source_binary_id>/` ディレクトリは、当該 `trial_meta.json` を保持する全ての `run_id` (元 + 再評価で複製された全ての run) が存在する限り削除してはならない (judge が `source_meta.json` 検査と `source/<source_id>/src/` の意味検査に依存するため、provenance チェーンが宙ぶらりんになると再評価不能になる)。
- `Build` 開始条件は対象 `source_id` の `source_meta.json` で `verification_status=pass` であることとする。
- `debug_mode=false` の `Generate` は `attempts/` を生成してはならない。
- `Validate` 入力は常に同一 `run_id` 配下 artifact とし、他 `run_id` との混在を禁止する。
- 各 phase `fail` 時は下流 phase 開始条件を満たす目的のファイル後付け生成を禁止する。
- `substep` を持つ phase の再投入戦略（`repair_strategy=reuse` / `restart`）と記録要件は `ORCHESTRATION.md` を canonical source として適用する。

#### 参照規則
- `orchestration` から `step` / `substep` 実行を参照するときは `orchestration_id + agent_run_id` を使用し、ログ本文の全文検索だけで追跡してはならない。
- `step` 完了判定は `workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/step_result.json` を canonical source とする。
- `pipeline` から `ir` を参照するときは `node_key_safe + ir_id` を使用し、相対ファイルパス直参照を禁止する。
- `Validate` の再現は `lineage.json` と `trial_meta.json` のみで可能でなければならない。
- `trial_meta.json` は `runner_command`、`process_trace_ref`、`raw_artifact_refs` を必須記録とする。
- `index/ir_index.json` と `index/pipeline_index.json` は探索専用とし、判定ロジックの canonical source に使ってはならない。
- `aggregate_verdict.json` は常に `spec.ir.yaml` の `dependency` セクションと整合し、依存集合の省略を禁止する。

#### 依存 workflow 網羅チェック
- `spec.ir.yaml` の `dependency.all_nodes` 集合と `workspace/ir/*/<ir_id>/` の `node_key_safe` 集合は 1 対 1 で一致しなければならない。
- `spec.ir.yaml` の `dependency.all_nodes` 集合と `workspace/pipelines/*/<pipeline_id>/lineage.json` の `node_key` 集合は 1 対 1 で一致しなければならない。
- 異なる `node_key` で生成された `source/<source_id>/src/` のコードハッシュが一致した場合、共通ライブラリとして明示されたファイルを除き `copy_based_artifact_reuse` として `invalid` にしなければならない。
- workflow 実行の完了宣言前に、対象依存 `DAG` の `workspace/ir` / `workspace/pipelines` artifact を削除してはならない。

#### 書き込み範囲ガード
- 各 phase 開始時に `write_scope_baseline.json` を `workspace/` 配下へ保存し、比較対象の `baseline` を固定しなければならない。
- `write_scope_baseline.json` は、少なくとも `stage`、`node_key`、`pipeline_id`、`captured_at`、`tracked_diff`、`untracked_files` を保持しなければならない。
- 各 phase 完了前に `write_scope_baseline.json` との差分を計算し、`workspace/` 配下以外の変化を `write_scope_violation` として判定しなければならない。
- 違反未検出時は `write_scope_check.status=pass` を phase メタデータへ記録しなければならない。
- 違反検出時は `write_scope_violation.json` を `workspace/` 配下へ出力し、`violation_paths` と `stage` と `node_key` と `pipeline_id` と `detected_at` を必須記録しなければならない。
- `write_scope_violation` 検出時は当該 phase を `fail` とし、当該 `pipeline` の `aggregate_verdict` 確定を禁止する。

## phase 別 input/output contract 一覧
本節では、各 phase の入力を `execution input` と `verification input` に分けて記述する。両者の role が重なる場合、同一 artifact を両方へ記載してよい。

### 0. Spec（人手）
- execution input: workflow 外部で与える要求事項、物理要件、依存選択方針
- verification input: なし
- 出力: `spec/<spec_kind>/<domain>/<family>/<spec_id>/controlled_spec.md`、`spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md`、`spec/<spec_kind>/<domain>/<family>/<spec_id>/deps.yaml`

### 1. Compile
- execution input: `controlled_spec.md`、`tests.md`、`deps.yaml`、`spec/registry/spec_catalog.yaml`
- verification input: `controlled_spec.md`、`tests.md`、`deps.yaml`、`spec/registry/spec_catalog.yaml`、生成された `spec.ir.yaml`
- 出力: `spec.ir.yaml`、`ir_meta.json`

### 2. Generate
- execution input: `spec.ir.yaml`
- verification input: `spec.ir.yaml`、生成された `source/<source_id>/src/`
- 出力: `source/<source_id>/src/`、`source_meta.json`
- `Generate` 完了前に、`spec.ir.yaml` の `impl_defaults.toolchain.language` に整合する MCP `run_linter` を成功させ、`source_meta.json` の `lint_command_ref.run_linter` へ MCP 証跡（各要素に `command_id` と `command_log_ref` と `preset`）を記録しなければならない。
- `Generate` は `Validate.execute` が `run_quality_checks` の `preset` だけで実行可能な preset-compatible quality path を正式出力へ含めなければならない。不足時は `Generate fail` とする。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` / `c` / `cpp` / `mixed` 系では、`source/<source_id>/src/Makefile` に `test` または `check` target を必須定義しなければならない。欠落時は `Generate fail` とする。

### 3. Build
- execution input: `source/<source_id>/src/`、`spec.ir.yaml` の `impl_defaults`
- verification input: `spec.ir.yaml`、`source_meta.json`
- 出力: `binary/<binary_id>/bin/`、`binary_meta.json`、`compile_project` の `command_id` と `command_log_ref`

### 4. Validate
- execution input: `binary/<binary_id>/bin/`、`spec.ir.yaml`、`tests.md`
- verification input: `spec.ir.yaml`、`source/<source_id>/`、同一 `run_id` 配下の `raw/` / `diagnostics.json` / `perf.json` / `quality_check.json`
- 出力: `diagnostics.json`、`perf.json`、`quality_check.json`、`raw/`、`stdout.log`、`stderr.log`、`semantic_review.json`、`verdict.json`、`aggregate_verdict.json`、`summary.json`、`trial_meta.json`、`validate_meta.json`、`run_program` の `command_id` と `command_log_ref`
- `Validate` は `execute` substep と `judge` substep を持つ。`execute` substep は MCP 経由で `run_program` を呼んで実行証跡を生成し、`judge` substep は LLM 意味検査と判定指標再計算で `verdict` を確定する。

## phase 詳細（参照）

本節では、`Compile` / `Generate` / `Validate` を substep を持つ phase として記述し、`Build` を単一 step として記述する。

phase ごとの契約詳細は [phases/](phases/) 配下のファイルを canonical source とする。

| phase | ファイル | substep |
|-------|----------|---------|
| 0 Spec（人手） | [phases/phase_00_spec.md](phases/phase_00_spec.md) | - |
| 1 Compile | [phases/phase_01_compile.md](phases/phase_01_compile.md) | generate / verify |
| 2 Generate | [phases/phase_02_generate.md](phases/phase_02_generate.md) | generate / verify |
| 3 Build | [phases/phase_03_build.md](phases/phase_03_build.md) | - |
| 4 Validate | [phases/phase_04_validate.md](phases/phase_04_validate.md) | execute / judge |

## エージェント参照範囲

- 子 `step agent` / `substep agent` の `skill_must_read_refs` は `tools/orchestration_runtime.py` の `build_skill_must_read_refs` で組み立てられる。
- 既定では `docs/workflow/WORKFLOW_CORE.md` と `docs/ORCHESTRATION.md` と対象 phase の `docs/workflow/phases/phase_*.md` と `skill_ref` と verify 必須 artifact を含む。`docs/WORKFLOW.md` は仕様への入口である。

## 完了判定基準
- workflow 完了条件は、対象 workflow の `orchestration_id` 配下に `orchestration_meta.json` と `agent_graph.json` と `agent_runs.jsonl` が存在することとする。
- workflow 完了条件は、`spec.ir.yaml` の `dependency.all_nodes` 集合に対して `workspace/ir/<node_key_safe>/<ir_id>/` と `workspace/pipelines/<node_key_safe>/<pipeline_id>/` が存在し、`lineage.json` の `node_key` と `dependency_ref` が一致することとする。
- workflow 完了宣言は、`dependency workflow` 網羅チェックと `trial_meta` 完整性チェックと `copy_based_artifact_reuse` 非検出を同時に満たす場合のみ許可する。
- workflow 完了宣言は、上位 `node` の `src/` に依存 `node` 実装の内包が存在しないことを同時に満たす場合のみ許可する。
- workflow 完了宣言は、全 phase で `write_scope_violation` 非検出を同時に満たす場合のみ許可する。
- `CI` は `python3 tools/validate_workspace_root.py` と `python3 tools/validate_pipeline_semantics.py`（`--stage full` または省略時）の execution result を `pass` 条件として扱う。

## 参照文書
- `ORCHESTRATION.md`
- `PERFORMANCE_DIAGNOSTICS.md`
- `SPEC.md`
