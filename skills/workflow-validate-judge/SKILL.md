---
name: workflow-validate-judge
description: Validate ステージの judge substep を実行し、`tests.md` と `diagnostics.json` と依存情報に基づいて `verdict.json` と `aggregate_verdict.json` と `summary.json` を判定するときに使用する。依存 `DAG` の `blocked` 判定と `self_verdict` / `aggregate_verdict` 集約および `Validate` 失敗時の retry routing 用 `findings` 記録を行う作業に適用する。
---

# Workflow Validate Judge

## 目的
Validate phase の judge substep として、物理合否と依存集約合否を再現可能に決定し、失敗時の retry routing に必要な `findings` 分類を記録する。本 substep は独立 LLM context で動作し、execute substep が生成した一次証跡のみを根拠に判定する。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/runs/<run_id>/<node_key_safe>/` の判定処理
- `verdict.json` と `aggregate_verdict.json` と `summary.json` と `semantic_review.json` の生成

## 要件
- 判定 canonical source は対象 `node` の `tests.md` と `spec.ir.yaml.io_contract` に固定する。
- `self_verdict` は当該 `node` 単体の判定結果として `verdict.json` に保存する。
- `aggregate_verdict` は推移依存 `node` を集約して `aggregate_verdict.json` に保存する。
- 直下依存 `node` が `pass` または `xfail` でない場合、当該 `node` を `blocked` にする。
- 判定指標は `runs/<run_id>/<node_key_safe>/raw/` の実行証跡から再計算し、`diagnostics.json` と整合確認する。再計算不能または不整合は `Validate.judge fail` とする。
- 再計算入力は `raw` 一次証跡のみに限定し、`diagnostics.json` を再計算入力へ流用してはならない。
- `raw` の必須構成は `spec.ir.yaml.io_contract.raw_requirements.required_evidence` を canonical source として判定し、固定の証跡構成を一律必須にしてはならない。
- `raw/metrics_basis.json` は `io_contract.test_evidence_requirements` の全 `test_id` を対象とする per-test evidence index でなければならない。各 `test_id` の entry が `required_raw_variables` を欠落する場合、または suite 全体 summary しか持たない場合は `Validate.judge fail` とする。
- 物理 `fail` の場合は性能評価をスキップする。
- `summary.json` に `self_summary` と `dependency_summary` を必須保存し、`dependency_summary` は `total` と `pass` と `xfail` と `fail` と `blocked` を持つ。
- LLM 意味検査結果は `semantic_review.json` として `runs/<run_id>/<node_key_safe>/` 配下へ保存し、`review_method`、`decision`、`scope.model_ref`、`scope.runner_ref`、`scope.raw_refs`、`findings` を必須記録とする。
- `verdict.json` は `failure_class` を必須記録する。値域は `physics_fail` / `runtime_error` / `evidence_mismatch` / `structural_violation` / `pass` のいずれかとする。
- 失敗を検出した `semantic_review.json#findings[*]` は次のキーを必須記録する: `finding_id` (string), `attribution` (`code` / `ir` / `spec` / `evidence` のいずれか), `evidence_refs[]` (path list), `confidence` (`high` / `medium` / `low`), `description` (text)。これらは `orchestration agent` が retry 対象 (Generate / Compile / Spec / Validate.execute) を deterministic に決定するための入力となる (canonical mapping: `docs/workflow/phases/phase_04_validate.md` の「失敗時 retry の判定基準」節)。
- 判定 artifact の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。
- `Validate.judge` 開始前と完了前に `python3 tools/validate_pipeline_semantics.py --stage pre_judge --in-flight-agent-run-id <自身の agent_run_id>` を実行し、`fail` 時は `Validate.judge fail` とする。`--allow-missing-orchestration` と `--allow-missing-llm-review` を指定してはならない。
  - **`--in-flight-agent-run-id <自身の agent_run_id>` を必ず付与すること（`<...>` は自身の `agent_run_id` を literal 置換）。** `record-launch` は judge 起動時に `agent_graph.json` の自 edge を append するが、judge 自身の `agent_runs.jsonl` entry と `validate` の `step_result.json` は judge 返却後に親が書くため、judge が自身の substep 内から `pre_judge` を実行する時点では未記録である。`pre_judge` はこの自己 in-flight 例外を **active marker の有無では判定せず**（crash や backend 差で残留・欠落しうるため）、live caller が宣言した `--in-flight-agent-run-id` のみを信頼し、かつ launch request が `step=validate, substep=judge` であることを検証した上で当該 edge と `validate` step_result を許容する。flag を付けないと自身の dangling edge と未生成 step_result が violation となり fail-closed で停止する。
  - **`semantic_review.json` は「完了前」`pre_judge` の前に当該 judge 自身の実際の `decision` で書く（または上書きする）こと。** `pre_judge` は `semantic_review.json#decision != "pass"` を violation として検出するため、前の judge 試行が残した `decision=fail` を上書きしないまま「完了前」`pre_judge` を実行すると、自身の判定が pass でも stale な値で fail する。
  - **node physics は pass（`semantic_review.json#decision=pass`）でも、physics 以外の blocker（例: orchestration-record integrity による `pre_judge` fail で当該 run 内で復旧不能）により certify できない場合は、`validate` の `step_result.json` を `status=blocked` で書ける。** `write-step-result` は `decision=pass` と `status=blocked` の併存を許容する（`status=pass` には finalize された verdict が、`status=fail` には `decision!=pass` が必要なため、`fail_closed` 以外の正直な終端経路として `blocked` を用いる）。ただし `blocked` 終端時も `aggregate_verdict.json` / `summary.json` / `trial_meta.json` の生成は必須であり、これらを欠くと `write-step-result` が reject する。

## 運用ルール
1. 判定入力は同一 `run_id` 配下 artifact のみに限定する。
2. 判定入力は `diagnostics.json` と `perf.json` と `raw` 実行証跡の同時存在を必須とし、いずれか欠落時は `Validate.judge` を開始しない。
3. `aggregate_verdict.json` は `spec.ir.yaml.dependency` の依存集合と一致させる。
4. `impl_defaults.target.class=cpu` の品質比較結果は `quality check` として記録し、`tests` 判定と分離する。
5. `quality check` の比較 canonical source は `diagnostics.json` と `verdict.json` とし、`stdout` 差分のみで合否を確定してはならない。
6. 判定失敗時は `summary.json` に失敗 classification を明示し、`semantic_review.json#findings[*].attribution` で戻り先ステージを指定する。
7. 出力先が `workspace/` でない場合は `Validate.judge fail` とする。
8. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
9. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Validate.judge fail` とする。
10. `python3 tools/validate_pipeline_semantics.py --stage pre_judge` の `exit code` が `0` でない場合、または `--allow-missing-orchestration` / `--allow-missing-llm-review` を指定した場合は、`verdict.json` と `aggregate_verdict.json` を確定してはならない。
11. `attribution=spec` を判定した場合は、`fail_closed` で停止して `failure_analysis.json` に詳細 (finding 全文、evidence_refs、description) を記録するよう `orchestration agent` に通知する（自動 retry はしない）。

## 判定基準
- 判定根拠が `tests.md` と `spec.ir.yaml.io_contract` と `diagnostics.json` に追跡できる。
- 判定根拠が `raw` 実行証跡から再計算可能である。
- `blocked` 判定条件が依存状態と一致する。
- `aggregate_verdict.json` と `summary.json` が `spec.ir.yaml.dependency` と整合する。
- `verdict.json#failure_class` と `semantic_review.json#findings[*].attribution` の組合せが `docs/workflow/phases/phase_04_validate.md` の retry 判定テーブルで一意に解釈可能である。
- `python3 tools/validate_pipeline_semantics.py --stage pre_judge` が `exit code 0` を返す。
