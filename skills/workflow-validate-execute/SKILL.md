---
name: workflow-validate-execute
description: Validate ステージの execute substep を実行し、`build` artifact を `MCP` サーバー経由の `run_program` で実行して `diagnostics.json` と `perf.json` と実行ログを生成するときに使用する。`quality check` を `run_quality_checks` で実行する作業に適用する。
---

# Workflow Validate Execute

## 目的
Validate phase の execute substep として、判定可能なランタイム artifact を生成する。本 substep は非 LLM（MCP のみ）で動作し、合否判定は同 phase の `Validate.judge` substep が独立 LLM context で担う。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/runs/<run_id>/<node_key_safe>/` の artifact generation
- `runner` 実行と `quality check` 実行

## 要件
- 本 phase が起動できる validator gate は `skills/workflow-orchestration/references/launch_prompts.md` の「substep ↔ allowed validator gate 対応表」を canonical source とする。
- `run` は `MCP` サーバーの `run_program` を使用する。
- `run_program` の実行コマンドは `spec.ir.yaml.case` を入力引数として必ず含まなければならない。
- `quality check` は `MCP` サーバーの `run_quality_checks` を使用する。
- `quality check` 成立のために `runs/<run_id>/<node_key_safe>/` 配下へ `test` source、harness、補助 `script`、一時 `Makefile` を追加生成してはならない。
- `spec.ir.yaml.impl_defaults.toolchain.build_system=make` かつ `toolchain.language=fortran` / `c` / `cpp` / `mixed` 系の場合、`quality check` は `source/<source_id>/src/` に対する `run_quality_checks preset=make_test` または `preset=make_check` で実行しなければならない。適合経路が存在しない場合は `Validate.execute fail` とする。
- 上記 `run_quality_checks` は `env` で out-of-source dir を渡さなければならない: `env={OBJDIR:<abs>/workspace/tmp/<agent_run_id>/build, BINDIR:<abs>/<pipeline>/binary/<source_binary_id>/bin, RUNDIR:<abs>/workspace/tmp/<agent_run_id>/qc_run}`。`binary/` と `source/` は Validate.execute では read-only bind されるため `make test` は relink してはならず（Makefile の `test -x $(BINDIR)/$(BIN) || $(MAKE) …` guard で既存 binary を参照）、`make test` の binary 再実行が吐く `diagnostics.json` / `perf.json` / `raw/*` は **tmp（`workspace/tmp/<agent_run_id>/qc_run`、`run_program` の `run/` とは別 subdir）** 配下に閉じる。`RUNDIR` を canonical run node dir へ向けてはならない: canonical run node dir は本 substep の write_root だが、canonical `.json` は write_root 配下にあるだけでは authorize されず `guarded-apply-patch` の gate evidence を必須とするため、`make test` の binary 直書きは gate-authored copy を上書きし終端 `record-agent-run` で `unauthorized_write_violation` → `fail_closed` を招く。`Validate.execute` 中の `src/` 書き込みは cross-phase audit log（`source/<source_id>/src/mcp_command_log.jsonl`）のみ許可され、それ以外（`.o`/`.mod`/exe/`raw/`/`diagnostics.json`/`perf.json` の `src/` 出力）は `unauthorized_write_violation` を招く。
- `runner` が `model` を呼び出し、`diagnostics.json` と `perf.json` を出力する。
- `runner`（`run_program` が実行する binary）に canonical run dir (`runs/<run_id>/<node_key_safe>/`) へ `diagnostics.json` / `perf.json` を**直書きさせてはならない**。これらは canonical `.json` であり `guarded-apply-patch` の gate evidence を必須とするため、binary 直書きは終端 `record-agent-run` で `unauthorized_write_violation` → `fail_closed` を招く（`mcp_command_log.jsonl` のような MCP-owned audit log とは扱いが異なる）。launch は `run_program` の作業/出力先（`project_dir` または case 出力先）を `allowed_tmp_root`（`workspace/tmp/<exec_agent_run_id>/run/`）へ向け、binary は `diagnostics.json` / `perf.json` / `raw/` / log を tmp（auto-authorize）へ落とす。`command_log_path` には canonical `runs/<run_id>/<node_key_safe>/mcp_command_log.jsonl` を明示する（`project_dir` を tmp にしても MCP log placement gate を満たすため）。
- execute agent は **`run/` tmp tree**（`workspace/tmp/<agent_run_id>/run/`、= `spec.ir.yaml.case` を引数とする `run_program` 実行の出力）の `diagnostics.json` / `perf.json` を読み、canonical `runs/<run_id>/<node_key_safe>/diagnostics.json` / `perf.json` を `guarded-apply-patch`（create-form）で再 author する。**runner のプログラム出力（`diagnostics.json` / `perf.json` / `raw/metrics_basis.json` / `raw/state_snapshots/*`）は必ず `run/` から promote し、`qc_run/`（`run_quality_checks` / make test 再実行の出力）から promote してはならない** — `qc_run/` を promote すると canonical 証跡が required `run_program` invocation（`spec.ir.yaml.case` とその `command_log`）に対応せず、`Validate.judge` が provenance 不一致の証跡を消費する。`qc_run/` の出力は quality-check 比較（`quality_check.json` の verdict 算出）のためにのみ参照する。`trial_meta.json` と `quality_check.json` は **runner 出力ではなく agent が author するメタデータ**（`trial_meta.json` は MCP command refs / `source_source_id` / `source_command_ref` から構成、`quality_check.json` は比較結果から構成。runner は両 file を直接出力してはならない）であり、tmp からの promote ではなく `guarded-apply-patch` で生成する。`raw/` 配下の `.json`（`metrics_basis.json` / `state_snapshots/*.json` 等）**も canonical `.json` であり `guarded-apply-patch` で再 author する**（`Write` tool で書くと `enforce_guarded_apply_patch` で reject され、仮に書けても終端 `record-agent-run` が gate evidence 欠落で `unauthorized_write_violation` を検出する）。`Write` tool で書くのは `raw/` 配下の**非 `.json`** ファイル・`stdout.log`・`stderr.log` に限り、いずれも `allowed_file_tool_paths` 内 path へ書く。
- `run_program` と `run_quality_checks` の binary 出力はいずれも tmp（`run/` / `qc_run/`）へ落とし、canonical run node dir へ直書きさせてはならない。全 canonical `.json`（`diagnostics.json` / `perf.json` / `trial_meta.json` / `quality_check.json` / `raw/metrics_basis.json` / `raw/state_snapshots/*`）の `guarded-apply-patch` 再 author は **両 MCP 呼び出し（`run_program` と `run_quality_checks`）完了後の最終 step** として実行し、後続の binary 再実行が gate-authored copy を上書きしない順序を保証すること。`run_program` 後に再 author しても `run_quality_checks`（`make test`）の binary 再実行が canonical を上書きする ordering defect が `unauthorized_write_violation` の典型原因である。
- `runner` が `verdict.json` と `aggregate_verdict.json` と `summary.json` と `trial_meta.json` を直接出力してはならない（これらは `Validate.judge` の責務）。
- agent が author する `quality_check.json` は `checks.verdict_available=true` と `checks.diagnostics_match=true` と `checks.verdict_match=true` を同時に満たし、**かつ top-level `status` フィールドへ literal `"pass"`** を記録しなければならない。`status` が `"pass"` 以外または欠落（例: `verdict:"pass"` のみ）の場合、`post_execute` gate が `quality_check.json:status must be pass` で reject する。
- `Validate.execute` 完了条件は `diagnostics.json` と `perf.json` が標準 `JSON` parser で復元可能な UTF-8 `JSON object` であることを含む。不正 `JSON` を検出した場合は `Validate.execute fail` とし、`Validate.judge` を開始してはならない。
- `Validate.execute` 完了前に `python3 tools/check_artifact_syntax.py --format json --expect-top object` を実行し、`diagnostics.json` と `perf.json` と `quality_check.json` と `trial_meta.json` の構文妥当性を検査しなければならない。
- `runs/<run_id>/<node_key_safe>/raw/` に `Validate.judge` 再計算用の実行証跡を必須保存する。必須構成は `spec.ir.yaml.io_contract.raw_requirements.required_evidence` を canonical source とする。
- `raw_requirements.required_evidence` が `artifact=state_snapshots` を必須宣言する場合のみ、状態スナップショットを必須保存する。
- `raw` は一次証跡のみを保存し、`diagnostics.json` の複写を `metrics_basis` として保存してはならない。
- `stdout.log` と `stderr.log` と `trial_meta.json` を必須保存する。
- `trial_meta.json` に `runner_command` と `process_trace_ref` と `raw_artifact_refs` を必須記録する。
- `trial_meta.json` には `source_source_id` を必須記録する。値は本 `Validate.execute` が `quality_check` で参照する `<pipeline>/source/<source_id>/` の id とし、当該 `source_meta.json` の `verification_status=pass` でなければならない (failed / stale source を quality_check evidence として参照することを禁止する)。
- Validate の launch request は `source_binary_id` を必須記録とし、本 `Validate.execute` が binary を取得する `<pipeline>/binary/<source_binary_id>/` の id を指定する。`record-launch` は `<source_binary_id>/binary_meta.json` の `source_source_id` と request の `source_id` が一致することを cross-reference 検証し、mismatch は reject する (mixed-binary forge 防止)。
- `trial_meta.json` の `source_command_ref` 各 entry は `tool_name` フィールドを必須宣言とし、`run_program` または `run_quality_checks` のいずれかを指定する (`compile_project` は build phase の道具で、binary_meta.json に記録されるため execute trial_meta では受理しない)。entry の `tool_name` は対応する MCP `command_log` record の `tool_name` と一致しなければならない。少なくとも 1 つの entry は `tool_name='run_program'` でなければならない (実プログラム実行証跡)。
- `run_program` の MCP `command_log` 出力は `<run node_dir>/mcp_command_log.jsonl` (= `<pipeline>/runs/<run_id>/<node_key_safe>/mcp_command_log.jsonl`) を canonical placement とし、`source_command_ref.<run_program-key>.command_log_ref` は当該 path のみ許可する。`run_program` 呼び出し時は `command_log_path` 引数または `project_dir` 設定で本 path に log が落ちるよう構成する。
- `run_quality_checks` の MCP `command_log` 出力は cross-phase canonical placement (`<pipeline>/source/<source_source_id>/src/mcp_command_log.jsonl`) のみ許可する。非 canonical placement (例: `raw/` 配下の任意 `.jsonl`) は `post_execute` validator で reject される。
- `impl_defaults.target.class=cpu` の品質比較は `threads_per_rank=1` と `threads_per_rank>1` の execution result を比較対象として保存する。
- `quality check` の比較 canonical source は `diagnostics.json` と `verdict.json` とし、`stdout` 差分のみで合否を確定してはならない。
- workflow artifact の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。
- `Validate.execute` 完了前に `python3 tools/validate_pipeline_semantics.py --stage post_execute --pipeline-root <pipeline_root> --run-id <run_id>` を実行し、`fail` 時は `Validate.execute fail` とする。`--pipeline-root` は繰り返し指定可能とし、`spec.ir.yaml.dependency.all_nodes` を保持する試行では `all_nodes` に対応する全 `pipeline_root` を指定しなければならない。`--run-id` には本試行の `run_id` を指定し、検証を当該 run へ scope する（`append-only` の pipeline に残る過去 retry の壊れた sibling run で fail しないため）。

## 運用ルール
1. `run_id` を発行し、artifact を `run_id` 単位で分離保存する。`run_id` は固定 literal `run_` prefix を持つ `run_<YYYYMMDD>_<seq3>` 形式（例: `run_20260605_001`）とし、`ir_id` / `pipeline_id` の `<slug>_<YYYYMMDD>_<seq3>` 形式を流用してはならない（`run-rsn-p0_20260605_001` 等のハイフン slug 形式は `record-launch` の phase contract が reject し、通過しても `post_execute` の run 発見が silent fail する）。
2. 判定入力の混在を避けるため、`run_id` を跨いだ artifact 参照を禁止する。
3. `node_key` ごとに artifact ディレクトリを分離する。
4. 実行失敗時は `trial_meta.json` に環境情報と失敗原因を記録する。
5. `raw` 実行証跡が欠落する場合は `Validate.execute fail` とし、`Validate.judge` を開始してはならない。
6. 出力先が `workspace/` でない場合は `Validate.execute fail` とし、当該 `run` を無効化する。
7. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
8. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Validate.execute fail` とする。
9. `python3 tools/validate_pipeline_semantics.py --stage post_execute --pipeline-root <pipeline_root> --run-id <run_id>` を実行し、`fail` 時は `Validate.judge` を開始してはならない。
10. 完了前に `python3 tools/check_artifact_syntax.py --format json --expect-top object` を `diagnostics.json` と `perf.json` と `quality_check.json` と `trial_meta.json` へ実行し、`fail` 時は `Validate.execute fail` とする。

## 判定基準
- `diagnostics.json` と `perf.json` とログ群が揃っている。
- `raw` 実行証跡と `trial_meta.json` の参照情報が整合している。
- `node_key` 単位で artifact が分離されている。
- 実行方式が `MCP run_program` と `MCP run_quality_checks` に限定される。
- `python3 tools/validate_pipeline_semantics.py --stage post_execute` が `exit code 0` を返す。
