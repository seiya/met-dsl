# Phase contract: Execute

### 4. Execute
- execution input: `build/<build_id>/bin/`、`case.resolved.yaml`
- verification input: `derived_contract.json`、`dependency.resolved.yaml`、`build/<build_id>/bin/`
- 出力: `diagnostics.json`、`perf.json`、`quality_check.json`、`raw/`、`stdout.log`、`stderr.log`、`run_program` の `command_id` と `command_log_ref`
- `Execute` は標準 `substep` を持たない単一 `step` とする。
- `Execute` は `MCP` サーバー経由で実行する。
- `Execute` は `run_program` を使用し、実行コマンドへ `case.resolved.yaml` を必ず含める。
- `run_program` の実コマンド記録は `JSONL` 形式で保存し、既定の保存先は `project_dir/mcp_command_log.jsonl` とする。
- `Execute` の試行メタデータは `command_id` と `command_log_ref`（または `command_log_path`）を追跡可能に記録する。
- `Execute` は `node` 単位で個別実行し、他 `node` の artifact を混在させてはならない。
- `runner` の出力対象は `diagnostics.json`、`perf.json`、`raw/` 一次証跡、`stdout.log`、`stderr.log` に限定する。
- `runner` は `verdict.json`、`aggregate_verdict.json`、`summary.json`、`trial_meta.json` を書き込んではならない。
- `diagnostics.json` と `perf.json` は、標準 `JSON` parser で復元可能な UTF-8 `JSON object` として出力しなければならない。不正 `JSON`、先頭 0 欠落数値、言語依存整形による非互換 token を禁止する。
- `Execute` 完了前に `python3 tools/check_artifact_syntax.py --format json --expect-top object` を用いて `diagnostics.json` と `perf.json` と `quality_check.json` と `trial_meta.json` を検査し、`fail` 時は `Execute fail` としなければならない。
- `Execute` 完了前に `python3 tools/validate_pipeline_semantics.py --stage post_execute` を実行し、`exit code 0` を必須としなければならない。`--pipeline-root` は繰り返し指定可能とし、`dependency.resolved.yaml` が `all_nodes` を保持する試行では `all_nodes` に対応する全 `pipeline_root` を指定しなければならない。`fail` 時は `Execute fail` とし、`Judge` を開始してはならない。
- `Execute` は `Judge` 再計算に必要な一次証跡を `execution_id/<node_key>/raw/` に保存しなければならない。
- 一次証跡の必須構成は `derived_contract.json` の `raw_requirements.required_evidence` を canonical source とする。固定の最小構成を全 `spec` に一律適用してはならない。
- `raw_requirements.required_evidence` は `metrics_basis.json` と `execution_trace.json` と `state_snapshots` などの `artifact` ごとに必須有無を宣言しなければならない。
- `raw_requirements.required_evidence` が `artifact=state_snapshots` かつ `required=true` を宣言する場合、`raw/state_snapshots/` は `snapshot_schema.json` で `variables[].name` と `variables[].shape_expr` と `time_variable` と `time_shape_expr` を宣言し、`min_samples` 件以上の状態ファイルへ当該項目を保持しなければならない。
- `raw_requirements.required_evidence` が `artifact=state_snapshots` を必須宣言しない場合、`raw/state_snapshots/` を必須にしてはならない。スカラー目的 `spec` を含む任意の計算課題を許容しなければならない。
- `python3 tools/validate_pipeline_semantics.py` の `--stage post_execute`、`--stage pre_judge`、および省略時（`--stage full` 相当）の invocation は、`derived_contract.json` で宣言された `state_snapshots` の変数名と形状式、および `time_variable` の形状式が `raw/state_snapshots/snapshot_schema.json` と各 `snapshot*.json` に一致することを検証しなければならない。
- `raw/metrics_basis.json` は一次証跡のみを保持し、`diagnostics.json` の複写を禁止する。
- `raw/metrics_basis.json` は `test_evidence_requirements` の全 `test_id` を対象とする per-test evidence index を保持しなければならない。各 `test_id` の entry は `required_raw_variables` を欠落なく保持し、suite 全体 summary のみで代替してはならない。
- 同一 `metrics_basis.json` 内で異なる `test_id` の一次証跡を相互上書きしてはならない。単一の最後勝ち `case` 結果で複数 `test` の raw evidence を代表させる構成を禁止する。
- `Build` または `Execute` が失敗した場合、`diagnostics.json` / `perf.json` の人工生成を禁止し、当該 `node` を `fail` とする。
- `quality_check.json` は `checks.verdict_available=true` と `checks.diagnostics_match=true` と `checks.verdict_match=true` を同時に満たさなければならない。いずれかが `false` または欠落の場合は `Execute fail` とする。
- `quality check` 実行は `run_quality_checks` の `preset` 指定のみを許可し、`python3 quality_check.py` など任意コマンド実行を禁止する。
- `Execute` は `quality check` 成立のために `execute/<execution_id>/<node_key>/` 配下へ `test` source、harness、補助 `script`、一時 `Makefile` を生成してはならない。必要 artifact が `Generate` または `Build` 出力に存在しない場合は `Execute fail` とする。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` / `c` / `cpp` / `mixed` 系では、`quality check` は `generate/<generation_id>/src/` を `project_dir` とする `make_test` または `make_check` で実行しなければならない。
- `perf.json` の仕様は `PERFORMANCE_DIAGNOSTICS.md` を参照する。

