# Phase contract: Judge

### 5. Judge
- execution input: `tests.md`、`derived_contract.json`、同一 `execution_id` 配下の `raw/`
- verification input: `dependency.resolved.yaml`、同一 `execution_id` 配下の `diagnostics.json` / `perf.json` / `quality_check.json` / `raw/`、対象 `generation_id` の `model` / `runner`
- 出力: `semantic_review.json`、`verdict.json`、`aggregate_verdict.json`、`summary.json`、`trial_meta.json`
- `Judge` は標準 `substep` を持たない単一 `step` とする。
- 判定 canonical source は `tests.md` とする。
- 判定は `self_verdict`（`verdict.json`）と `aggregate_verdict`（`aggregate_verdict.json`）の 2 層で実施する。
- `Judge` 開始条件は、対象 `execution_id` 配下に `run_program` 実行記録と `diagnostics.json` と `perf.json` と `raw/` 一次証跡が存在し、同一 `execution_id` artifact として追跡可能であることとする。
- `Judge` は `raw/` 一次証跡から独立経路で判定指標を再計算し、`diagnostics.json` と整合確認しなければならない。
- `Judge` 再計算入力は `raw/` のみに限定する。`diagnostics.json` を再計算入力へ流用してはならない。
- `Judge` は `raw/metrics_basis.json` が `test_evidence_requirements` の全 `test_id` を保持し、各 entry が当該 `test_id` の `required_raw_variables` を欠落なく保持していることを開始条件として検証しなければならない。不足時は `Judge fail` とする。
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
- `python3 tools/validate_pipeline_semantics.py --stage pre_judge` は、`problem node` の `model` で `intent(out)` 変数が固定値代入のみで構成される実装と、`runner` の `diagnostics.json` が `model` 呼び出し結果を参照しない固定値埋め込み実装を検出した場合に `fail` とする。
- `Judge` 開始前と `Judge` 完了前に `python3 tools/validate_pipeline_semantics.py --stage pre_judge` を実行し、`fail` 時は当該 `pipeline` を `invalid` とする。
- `python3 tools/validate_pipeline_semantics.py --stage pre_judge` は `--allow-missing-orchestration` と `--allow-missing-llm-review` を指定してはならない。
- `Judge` 開始前の `python3 tools/validate_pipeline_semantics.py --stage pre_judge` は、対象 `dependency.resolved.yaml` の `all_nodes` で解決された全 `node` の `pipeline_root` を `--pipeline-root` へ繰り返し指定して検証対象に含めなければならない。起点 `problem` の単独 `pipeline_root` のみを対象にしてはならない。
- `python3 tools/validate_pipeline_semantics.py --stage pre_judge` は、`dependency.resolved.yaml` の `all_nodes` に対して `plan` または `pipeline` が未発行の `node` を検出した場合に `fail` とし、当該試行の `Judge` 開始を禁止しなければならない。
- 実装品質判定（`target.class=cpu`）は `threads_per_rank=1` と `threads_per_rank>1` の比較で実施し、比較対象は `diagnostics.json` と `verdict.json` とする。
- スレッド並列あり / なしの比較は `tests` の判定対象に含めず、`quality check` として扱う。
- 物理 `fail` 時は性能評価をスキップする。

