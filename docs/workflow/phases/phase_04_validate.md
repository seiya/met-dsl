# Phase 4: Validate

## 概要
`Build` が生産したバイナリを実行し、一次証跡を取得して合否判定を確定する phase。`execute` (実行と一次証跡収集) と `judge` (合否判定) の 2 substep を持つ単一 phase として定義する。

## I/O 契約
- execution input: `binary/<binary_id>/bin/`、`spec.ir.yaml`、`tests.md`
- verification input: `spec.ir.yaml`、`source/<source_id>/`、同一 `run_id` 配下の `raw/` / `diagnostics.json` / `perf.json` / `quality_check.json` / `trial_meta.json`。`<source_id>` の解決経路は substep ごとに異なる:
  - `Validate.execute`: launch request に必須記録される `source_id` (runtime enforce 済み; `binary_meta.json.source_source_id` との一致も verify される)。
  - `Validate.judge`: launch request は `run_id` のみ必須なので、同一 `run_id` 配下の `trial_meta.json.source_source_id` を読んで `<source_id>` を解決する (trial_meta は execute が書き込み、runtime が binary_meta との一致を verify 済み)。retry で複数の `source_id` が `pipeline_id` 配下に共存する場合でも、この経路で execute が実際に使用した正確な source が一意に pin される。
- 出力: `workspace/pipelines/<node_key_safe>/<pipeline_id>/runs/<run_id>/<node_key>/` 配下の以下:
  - `diagnostics.json`、`perf.json`、`quality_check.json`、`raw/`、`stdout.log`、`stderr.log`（execute substep）
  - `semantic_review.json`、`verdict.json`、`aggregate_verdict.json`、`summary.json`、`trial_meta.json`、`validate_meta.json`（judge substep）
  - `run_program` の `command_id` と `command_log_ref`

## substep 構成
- `Validate.execute`: バイナリを MCP 経由で実行し、一次証跡 (`raw/` / `diagnostics.json` / `perf.json` / `quality_check.json` / `stdout.log` / `stderr.log`) を生成する **非 LLM substep**。
- `Validate.judge`: 一次証跡から判定指標を独立経路で再計算し、`LLM` 意味検査と合わせて `verdict` を確定する **LLM substep**。

## `run_id` フォーマット
- 形式: `run_<YYYYMMDD>_<seq3>`、例: `run_20260511_001`

## `validate_meta.json` 必須 key
- 共通: `attempt_count`、`verification_status`、`last_fail_reason`、`debug_mode`、`context_isolated`
- `context_isolated=false` の場合、`constraint_reason` を必須とする。
- `verification_status=pass` の場合、`judge_command_ref` で LLM 意味検査の証跡（`command_id`、`command_log_ref` 等）を必須とする。

## substep 詳細

### 4-1. Validate.execute substep
- `Validate.execute` は標準 LLM 推論を伴わず、MCP `run_program` 呼び出しと結果記録に責務を限定する。
- `run_program` の実行コマンドへ `spec.ir.yaml.case` を必ず含める（実行時入力の決定値として）。
- `run_program` の実コマンド記録は `JSONL` 形式で保存し、既定の保存先は `project_dir/mcp_command_log.jsonl` とする。
- `Validate.execute` は `node` 単位で個別実行し、他 `node` の artifact を混在させてはならない。
- `runner` の出力対象は `diagnostics.json`、`perf.json`、`raw/` 一次証跡、`stdout.log`、`stderr.log` に限定する。
- `runner` は `verdict.json`、`aggregate_verdict.json`、`summary.json`、`trial_meta.json` を書き込んではならない（これらは `Validate.judge` の責務）。
- `diagnostics.json` と `perf.json` は、標準 `JSON` parser で復元可能な UTF-8 `JSON object` として出力しなければならない。
- `Validate.execute` 完了前に `python3 tools/check_artifact_syntax.py --format json --expect-top object` を用いて `diagnostics.json` と `perf.json` と `quality_check.json` を検査し、`fail` 時は `Validate.execute fail` とする。
- `Validate.execute` は `Validate.judge` 再計算に必要な一次証跡を `runs/<run_id>/<node_key>/raw/` に保存しなければならない。
- 一次証跡の必須構成は `spec.ir.yaml.io_contract.raw_requirements.required_evidence` を canonical source とする。固定の最小構成を全 `spec` に一律適用してはならない。
- `raw_requirements.required_evidence` が `artifact=state_snapshots` かつ `required=true` を宣言する場合、`raw/state_snapshots/` は `snapshot_schema.json` で `variables[].name` と `variables[].shape_expr` と `time_variable` と `time_shape_expr` を宣言し、`min_samples` 件以上の状態ファイルへ当該項目を保持しなければならない。
- `raw_requirements.required_evidence` が `artifact=state_snapshots` を必須宣言しない場合、`raw/state_snapshots/` を必須にしてはならない。
- `raw/metrics_basis.json` は一次証跡のみを保持し、`diagnostics.json` の複写を禁止する。
- `raw/metrics_basis.json` は `io_contract.test_evidence_requirements` の全 `test_id` を対象とする per-test evidence index を保持しなければならない。各 `test_id` の entry は `required_raw_variables` を欠落なく保持する。
- 同一 `metrics_basis.json` 内で異なる `test_id` の一次証跡を相互上書きしてはならない。
- `Validate.execute` が失敗した場合、`diagnostics.json` / `perf.json` の人工生成を禁止し、当該 `node` を `fail` とする。
- `quality_check.json` は `checks.verdict_available=true` と `checks.diagnostics_match=true` と `checks.verdict_match=true` を同時に満たさなければならない。いずれかが `false` または欠落の場合は `Validate.execute fail` とする。
- `quality check` 実行は `run_quality_checks` の `preset` 指定のみを許可し、`python3 quality_check.py` など任意コマンド実行を禁止する。
- `Validate.execute` は `quality check` 成立のために `runs/<run_id>/<node_key>/` 配下へ `test` source、harness、補助 `script`、一時 `Makefile` を生成してはならない。必要 artifact が `Generate` または `Build` 出力に存在しない場合は `Validate.execute fail` とする。
- `spec.ir.yaml.impl_defaults.toolchain.build_system=make` かつ `toolchain.language=fortran` / `c` / `cpp` / `mixed` 系では、`quality check` は `source/<source_id>/src/` を `project_dir` とする `make_test` または `make_check` で実行する。`run_quality_checks` には `env={OBJDIR:<abs tmp build>, BINDIR:<abs binary/<source_binary_id>/bin>, RUNDIR:<abs runs/<run_id>/<node_safe>>}` を渡し、既存 binary を `binary/<source_binary_id>/bin/` から参照する（read-only bind の `binary/` で relink しない）。test の run 出力は `RUNDIR`（run node dir）配下に閉じ、`src/` には cross-phase audit log 以外を書かない。
- `perf.json` の仕様は `PERFORMANCE_DIAGNOSTICS.md` を参照する。
- `Validate.execute` 完了前に `python3 tools/validate_pipeline_semantics.py --stage post_execute` を実行し、`exit code 0` を必須とする。`--pipeline-root` は繰り返し指定可能とし、`spec.ir.yaml.dependency.all_nodes` が複数 `node` を保持する試行では `all_nodes` に対応する全 `pipeline_root` を指定する。

### 4-2. Validate.judge substep
- 判定 canonical source は `tests.md` と `spec.ir.yaml.io_contract` とする。
- 判定は `self_verdict`（`verdict.json`）と `aggregate_verdict`（`aggregate_verdict.json`）の 2 層で実施する。
- `Validate.judge` 開始条件は、対象 `run_id` 配下に `run_program` 実行記録と `diagnostics.json` と `perf.json` と `raw/` 一次証跡が存在し、同一 `run_id` artifact として追跡可能であることとする。
- `Validate.judge` は `raw/` 一次証跡から独立経路で判定指標を再計算し、`diagnostics.json` と整合確認しなければならない。
- 再計算入力は `raw/` のみに限定する。`diagnostics.json` を再計算入力へ流用してはならない。
- `Validate.judge` は `raw/metrics_basis.json` が `io_contract.test_evidence_requirements` の全 `test_id` を保持し、各 entry が当該 `test_id` の `required_raw_variables` を欠落なく保持していることを開始条件として検証する。不足時は `Validate.judge fail` とする。
- `Validate.judge` は再計算不能または不整合時に `fail` としなければならない。
- `Validate.judge` は固定スクリプト検査に加え、`LLM` による意味検査を必須実行し、`model` / `runner` / `raw` 一次証跡の整合性と捏造疑義を判定する。
- `LLM` 意味検査の結果は `semantic_review.json` として `runs/<run_id>/<node_key>/` 配下へ保存し、`review_method`、`decision`、`scope.model_ref`、`scope.runner_ref`、`scope.raw_refs`、`findings` を必須記録とする。
- `semantic_review.json` の `decision` が `fail` または欠落の場合、当該 `node` を `Validate.judge fail` とする。
- 直下依存 `node` に `fail` または `blocked` がある場合、上位 `node` は `self_verdict` を評価せず `aggregate_verdict=blocked` として終了する。
- `blocked` 終了時も `aggregate_verdict.json`、`summary.json`、`trial_meta.json` を必須出力とし、`blocked_reason` と `blocking_direct_deps` を記録する。
- `summary.json` は `self_summary` と `dependency_summary` を必須保持とする。`dependency_summary` は `total`、`pass`、`xfail`、`fail`、`blocked` を保持する。
- `verdict.json` は `per_test` を必須保持とし、`tests.md` の全 `test_id` を重複なく記録する。
- `summary.json` の `counts` は `verdict.json.per_test` の集計値と一致しなければならない。
- 判定入力不足時は `Validate.judge fail` とし、推定値や仮定値で `verdict` を成立させてはならない。
- `Validate.judge` 開始前と完了前に `python3 tools/validate_pipeline_semantics.py --stage pre_judge` を実行し、`fail` 時は当該 `pipeline` を `invalid` とする。
- `python3 tools/validate_pipeline_semantics.py --stage pre_judge` は `--allow-missing-orchestration` と `--allow-missing-llm-review` を指定してはならない。
- `python3 tools/validate_pipeline_semantics.py --stage pre_judge` は、`spec.ir.yaml.dependency.all_nodes` で解決された全 `node` の `pipeline_root` を `--pipeline-root` へ繰り返し指定して検証対象に含めなければならない。
- `python3 tools/validate_pipeline_semantics.py --stage pre_judge` は、`spec.ir.yaml.dependency.all_nodes` に対して `ir` または `pipeline` が未発行の `node` を検出した場合に `fail` とし、当該試行の `Validate.judge` 開始を禁止する。
- 実装品質判定（`impl_defaults.target.class=cpu`）は `threads_per_rank=1` と `threads_per_rank>1` の比較で実施し、比較対象は `diagnostics.json` と `verdict.json` とする。
- スレッド並列あり / なしの比較は `tests` の判定対象に含めず、`quality check` として扱う。
- 物理 `fail` 時は性能評価をスキップする。

## 失敗時 retry の判定基準
`Validate` 失敗時の retry 対象は `orchestration agent` が `judge` の `findings` を解釈して deterministic に決定する。判定の入力は `semantic_review.json#findings[*]` と `verdict.json#failure_class` の 2 つに限定する。

### `judge` が必須記録する分類フィールド
`Validate.judge` は、失敗を検出した場合 `semantic_review.json#findings[*]` に次のキーを必須記録する:

| field | 値域 | 意味 |
|---|---|---|
| `attribution` | `code` / `ir` / `spec` / `evidence` | 失敗の帰属先カテゴリ |
| `evidence_refs[]` | path list | 根拠とした raw / diagnostics / source / IR への参照 |
| `confidence` | `high` / `medium` / `low` | judge の確信度 |
| `description` | text | 自然言語の根拠説明 (review 用) |

`verdict.json#failure_class` は次のいずれかの値とする: `physics_fail` / `runtime_error` / `evidence_mismatch` / `structural_violation` / `pass`。

### 判定テーブル
`orchestration agent` は次の deterministic な mapping で retry 対象を決定する:

| `verdict.json#failure_class` | `attribution` (judge) | retry 対象 |
|---|---|---|
| `evidence_mismatch` | `code` | `Generate` |
| `evidence_mismatch` | `ir` | `Compile` |
| `evidence_mismatch` | `evidence` | `Validate.execute`（一次証跡の再収集） |
| `physics_fail` | `code` | `Generate` |
| `physics_fail` | `ir` | `Compile` |
| `physics_fail` | `spec` | **`Spec` (fail_closed)**: 人手介入必須 |
| `runtime_error` | `code` (always) | `Generate` |
| `structural_violation` | `code` | `Generate` |
| `structural_violation` | `ir` | `Compile` |

### Compile retry の起動契約
`Compile` への retry を起動する場合、`orchestration agent` は次を満たさなければならない:

- `semantic_review.json#findings[*].attribution=ir` を持つ finding が少なくとも 1 件存在する。
- 該当 finding の `confidence` が `high` または `medium` である（`low` の場合は `Generate` retry を先に試みる）。
- `launches/<new_agent_run_id>.request.json#repair_reason` に当該 finding の `description` と `evidence_refs[]` を引用する。
- 再投入 `Compile` は **`spec.ir.yaml` の修正対象 section を `restart` 範囲として明示** し、`ir_meta.json.last_fail_reason` で `validate_feedback:<finding_id>` を記録する。

### Spec retry の扱い
`Spec` への retry は core workflow では自動化しない（人手で `controlled_spec.md` を更新する必要があるため）。`orchestration agent` は `attribution=spec` を判定した場合、`fail_closed` で停止し、`failure_analysis.json` に詳細（finding 全文、evidence_refs、judge の `description`）を記録する。

## 設計トレードオフ
- `execute` と `judge` を同一 phase の substep として配置する理由: 「実行 → 合否判定」は本質的に一体作業であり、別 phase に分けると `judge` の入力が常に最新 `execute` 結果に依存する関係になり、phase 境界の意味が薄い。Validate に統合することで、判定経路を単純化し、`run_id` 配下に判定 artifact が完結する。
- `execute` と `judge` を substep に分けた理由: `execute` は非 LLM (MCP のみ)、`judge` は LLM 意味検査必須なので、責務と context isolation の必要性が異なる。同 phase 内で 2 substep に分けることで、`judge` が独立 LLM context で公正判定できる。
