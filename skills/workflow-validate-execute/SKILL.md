---
name: workflow-validate-execute
description: Validate ステージの execute substep を実行し、`build` artifact を `MCP` サーバー経由の `run_program` で実行して `diagnostics.json` と `perf.json` と実行ログを生成するときに使用する。`quality check` を `run_quality_checks` で実行する作業に適用する。
---

# Workflow Validate Execute

## 目的
Validate phase の execute substep として、判定可能なランタイム artifact を生成する。本 substep は非 LLM（MCP のみ）で動作し、合否判定は同 phase の `Validate.judge` substep が独立 LLM context で担う。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/runs/<run_id>/<node_key>/` の artifact generation
- `runner` 実行と `quality check` 実行

## 要件
- 本 phase が起動できる validator gate は `skills/workflow-orchestration/references/launch_prompts.md` の「substep ↔ allowed validator gate 対応表」を canonical source とする。
- `run` は `MCP` サーバーの `run_program` を使用する。
- `run_program` の実行コマンドは `spec.ir.yaml.case` を入力引数として必ず含まなければならない。
- `quality check` は `MCP` サーバーの `run_quality_checks` を使用する。
- `quality check` 成立のために `runs/<run_id>/<node_key>/` 配下へ `test` source、harness、補助 `script`、一時 `Makefile` を追加生成してはならない。
- `spec.ir.yaml.impl_defaults.toolchain.build_system=make` かつ `toolchain.language=fortran` / `c` / `cpp` / `mixed` 系の場合、`quality check` は `source/<source_id>/src/` に対する `run_quality_checks preset=make_test` または `preset=make_check` で実行しなければならない。適合経路が存在しない場合は `Validate.execute fail` とする。
- `runner` が `model` を呼び出し、`diagnostics.json` と `perf.json` を出力する。
- `runner` が `verdict.json` と `aggregate_verdict.json` と `summary.json` と `trial_meta.json` を直接出力してはならない（これらは `Validate.judge` の責務）。
- `Validate.execute` 完了条件は `diagnostics.json` と `perf.json` が標準 `JSON` parser で復元可能な UTF-8 `JSON object` であることを含む。不正 `JSON` を検出した場合は `Validate.execute fail` とし、`Validate.judge` を開始してはならない。
- `Validate.execute` 完了前に `python3 tools/check_artifact_syntax.py --format json --expect-top object` を実行し、`diagnostics.json` と `perf.json` と `quality_check.json` と `trial_meta.json` の構文妥当性を検査しなければならない。
- `runs/<run_id>/<node_key>/raw/` に `Validate.judge` 再計算用の実行証跡を必須保存する。必須構成は `spec.ir.yaml.io_contract.raw_requirements.required_evidence` を canonical source とする。
- `raw_requirements.required_evidence` が `artifact=state_snapshots` を必須宣言する場合のみ、状態スナップショットを必須保存する。
- `raw` は一次証跡のみを保存し、`diagnostics.json` の複写を `metrics_basis` として保存してはならない。
- `stdout.log` と `stderr.log` と `trial_meta.json` を必須保存する。
- `trial_meta.json` に `runner_command` と `process_trace_ref` と `raw_artifact_refs` を必須記録する。
- `trial_meta.json` には `source_source_id` を必須記録する。値は本 `Validate.execute` が `quality_check` で参照する `<pipeline>/source/<source_id>/` の id とし、当該 `source_meta.json` の `verification_status=pass` でなければならない (failed / stale source を quality_check evidence として参照することを禁止する)。
- Validate の launch request は `source_binary_id` を必須記録とし、本 `Validate.execute` が binary を取得する `<pipeline>/binary/<source_binary_id>/` の id を指定する。`record-launch` は `<source_binary_id>/binary_meta.json` の `source_source_id` と request の `source_id` が一致することを cross-reference 検証し、mismatch は reject する (mixed-binary forge 防止)。
- `trial_meta.json` の `source_command_ref` 各 entry は `tool_name` フィールドを必須宣言とし、`run_program` または `run_quality_checks` のいずれかを指定する (`compile_project` は build phase の道具で、binary_meta.json に記録されるため execute trial_meta では受理しない)。entry の `tool_name` は対応する MCP `command_log` record の `tool_name` と一致しなければならない。少なくとも 1 つの entry は `tool_name='run_program'` でなければならない (実プログラム実行証跡)。
- `run_program` の MCP `command_log` 出力は `<run node_dir>/mcp_command_log.jsonl` (= `<pipeline>/runs/<run_id>/<node_key>/mcp_command_log.jsonl`) を canonical placement とし、`source_command_ref.<run_program-key>.command_log_ref` は当該 path のみ許可する。`run_program` 呼び出し時は `command_log_path` 引数または `project_dir` 設定で本 path に log が落ちるよう構成する。
- `run_quality_checks` の MCP `command_log` 出力は cross-phase canonical placement (`<pipeline>/source/<source_source_id>/src/mcp_command_log.jsonl`) のみ許可する。非 canonical placement (例: `raw/` 配下の任意 `.jsonl`) は `post_execute` validator で reject される。
- `impl_defaults.target.class=cpu` の品質比較は `threads_per_rank=1` と `threads_per_rank>1` の execution result を比較対象として保存する。
- `quality check` の比較 canonical source は `diagnostics.json` と `verdict.json` とし、`stdout` 差分のみで合否を確定してはならない。
- workflow artifact の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。
- `Validate.execute` 完了前に `python3 tools/validate_pipeline_semantics.py --stage post_execute` を実行し、`fail` 時は `Validate.execute fail` とする。`--pipeline-root` は繰り返し指定可能とし、`spec.ir.yaml.dependency.all_nodes` を保持する試行では `all_nodes` に対応する全 `pipeline_root` を指定しなければならない。

## 運用ルール
1. `run_id` を発行し、artifact を `run_id` 単位で分離保存する。
2. 判定入力の混在を避けるため、`run_id` を跨いだ artifact 参照を禁止する。
3. `node_key` ごとに artifact ディレクトリを分離する。
4. 実行失敗時は `trial_meta.json` に環境情報と失敗原因を記録する。
5. `raw` 実行証跡が欠落する場合は `Validate.execute fail` とし、`Validate.judge` を開始してはならない。
6. 出力先が `workspace/` でない場合は `Validate.execute fail` とし、当該 `run` を無効化する。
7. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
8. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Validate.execute fail` とする。
9. `python3 tools/validate_pipeline_semantics.py --stage post_execute` を実行し、`fail` 時は `Validate.judge` を開始してはならない。
10. 完了前に `python3 tools/check_artifact_syntax.py --format json --expect-top object` を `diagnostics.json` と `perf.json` と `quality_check.json` と `trial_meta.json` へ実行し、`fail` 時は `Validate.execute fail` とする。

## 判定基準
- `diagnostics.json` と `perf.json` とログ群が揃っている。
- `raw` 実行証跡と `trial_meta.json` の参照情報が整合している。
- `node_key` 単位で artifact が分離されている。
- 実行方式が `MCP run_program` と `MCP run_quality_checks` に限定される。
- `python3 tools/validate_pipeline_semantics.py --stage post_execute` が `exit code 0` を返す。
