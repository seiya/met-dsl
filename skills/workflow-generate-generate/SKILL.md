---
name: workflow-generate-generate
description: Generate ステージの generate を実行し、`spec.ir.yaml` から `model` と `runner` を分離した実装コードを作成するときに使用する。`source_id` 発行と `source_meta.json` 出力に適用する。
---

# Workflow Generate Generate

## 目的
Generate ステージの生成責務を固定し、`Build` 可能な実装 artifact を作成する。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/source/<source_id>/src/` に実装コードを生成する作業
- `source_meta.json` を生成する作業

## 要件
- 作業開始直後に `ir_ref` の `spec.ir.yaml` を読み、`io_contract.raw_requirements.required_evidence` の全要素について `artifact` と `required` と `min_samples` と（`artifact=state_snapshots` かつ `required=true` のとき）`schema.variables[].name` と `time_variable` を列挙し、`runner` の `raw/` 出力設計・`raw/metrics_basis.json` の索引設計と突合してから実装に入る。
- 入力は `spec.ir.yaml` (`case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency` の 5 セクション) とする。
- `controlled_spec.md` を直接入力にしてはならない。演算構成の要求定義は `spec.ir.yaml.algorithm` から解釈しなければならない。
- 実装コードは `model` と `runner` を分離し、`runner` は `model` を `call` / `use` / `import` で利用する。
- `runner` に物理更新ロジックを重複実装しない。
- `spec.ir.yaml.impl_defaults.toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`runner` から `python` / `bash` / `sh` / `node` などの外部インタプリタを起動してはならない。
- `model` は対象 `node` の演算契約を実装し、固定値返却専用、固定 `JSON` 出力専用、`no-op` 専用実装を禁止する。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` は、`spec.ir.yaml.algorithm` の状態更新契約を入力として読み取り、`state_variables` を更新計算へ必須利用する。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` は、`case_id` 分岐とスカラー定数代入のみで複数の `diagnostics` 指標を生成してはならない。
- `spec.ir.yaml.algorithm` の状態更新契約が欠落、または `state_variables` と `required_update_paths` が欠落する場合は `Generate fail` とし、推測補完で生成を継続してはならない。
- `spec.ir.yaml.algorithm` の `steps[]` と `ordering` と `control_condition` と `iteration_contract` を満たす実装構成を生成しなければならない。
- 直下依存 `node` の `ir_ref` と `pipeline_ref` と `aggregate_verdict` を確認し、`direct dependency execution readiness` を満たさない場合は生成を開始してはならない。
- 依存を持つ `node` は、`spec.ir.yaml.dependency.direct_deps` で解決された依存 `node` の公開 `operation` を呼び出す実装を必須とする。
- 依存 `operation` と同等機能を依存元 `node` の `model` / `runner` に再実装してはならない。
- 依存 `node` の `model` / `runner` / `module` / `subroutine` / `Makefile` 断片を依存元 `src/` へ複製、再配置、再定義してはならない。
- `impl_defaults.toolchain.language=fortran` で依存 `component` を持つ `node` の `model` は、依存 `spec_id` ごとに `use <spec_id>_model` と `call <spec_id>__*` を必須とし、`subroutine <spec_id>__*` の再定義を禁止する。
- 依存先が `profile` で公開 `operation` を持たない構成では、依存元 `problem` が `profile` の選択結果と拘束条件を参照する実装にしなければならない。
- `runner` は `diagnostics.json` と `perf.json` と `raw/` 一次証跡のみを出力対象とし、`verdict.json` と `aggregate_verdict.json` と `summary.json` と `trial_meta.json` を書き込んではならない。
- `runner` ソース（`Fortran` では慣例として `*_runner.f90`）に上記 4 ファイル名を **コメントを含むソース全体** の substring として含めてはならない。部分一致検査はコメントを除外しない。
- `runner` が出力する `diagnostics.json` と `perf.json` は、標準 `JSON` parser で復元可能な UTF-8 `JSON object` でなければならない。`impl_defaults.toolchain.language=fortran` の場合、`F0.d` など leading zero を欠落し得る数値整形を `JSON` 数値 token へ直接使用してはならない。
- `impl_defaults.target.class=cpu` かつ並列化方式未指定のとき、並列化可能ループへ `OpenMP` を既定適用する。
- 生成 artifact は対象 `node_key` と整合する構成にする。
- 生成 artifact は `node_key` ごとの差分を保持し、共通ライブラリ明示なしに `src` 全体を複製してはならない。
- `impl_defaults.toolchain.language=fortran` の場合、`module` 名とソースファイル名を一致させ、`<module_name>.f90` 形式で出力する。
- `impl_defaults.toolchain.language=fortran` の場合、`module` 名と公開 `subroutine` 名に `spec_id` 由来接頭辞を付与し、名前衝突を回避する。
- `impl_defaults.toolchain.build_system=make` かつ `impl_defaults.toolchain.language=fortran` の場合、`src/Makefile` は `use` 依存に対応したオブジェクト依存関係を明示し、依存 `module` の `.mod` または依存 `.o` を各オブジェクトターゲットの前提条件へ必須記述する。オブジェクト規則は **literal** ターゲット名（例: `foo.o:`）で書く。ターゲットが `$(VAR)` のみで literal が残らない行は静的解析から除外され、依存欠落扱いとなる。
- `impl_defaults.toolchain.build_system=make` かつ `impl_defaults.toolchain.language=fortran` の場合、`src/Makefile` は `make -j` で依存欠落による不定失敗を起こしてはならない。
- `quality check` 実行に必要な preset-compatible quality path は `Generate` 出力だけで成立しなければならない。`Validate.execute` で追加 `test` source、harness、補助 `script`、一時 `Makefile` を生成する前提を禁止する。
- `impl_defaults.toolchain.build_system=make` かつ `impl_defaults.toolchain.language=fortran` / `c` / `cpp` / `mixed` 系の場合、`src/Makefile` は `run_quality_checks preset=make_test` または `preset=make_check` で使用できる `test` または `check` target を必須定義する。
- `source_meta.json` に `attempt_count` と `verification_status` と `last_fail_reason` と `debug_mode` と `lint_command_ref` を記録する。
- `static lint` は MCP `run_linter` のみで実行する。`project_dir` は `source/<source_id>/src` とする。`spec.ir.yaml.impl_defaults.toolchain.language` に応じて `preset` を選ぶ（例: `fortran` / `cuda_fortran` は `fortitude`、`c` / `cpp` / `cuda_c` は `cppcheck`、`python` は `ruff`、`mixed` は `fortitude` と `cppcheck` を別々に実行し、それぞれの `command_id` と `command_log_ref` を `lint_command_ref.run_linter` 配列へ記録する）。`Makefile` の `lint` target や `compile_project` 経由でリンターを起動してはならない。
- `lint_command_ref.run_linter` は `preset` と MCP ログの `command_id` と `command_log_ref` を対応付けた object の配列とする。`quality check` 用の `run_quality_checks` とは別手順である。
- `source_meta.json` の `verification_status` は `fail_closed` を前提とし、検証未実施や判定不能を `pass` にしてはならない。
- workflow artifact の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。
- 対象 `pipeline` ルートに `lineage.json` を必須配置する（`workspace/pipelines/<node_key_safe>/<pipeline_id>/lineage.json`）。フィールド要件は `docs/workflow/WORKFLOW_CORE.md` を canonical source とし、`post_generate` 検査で欠落は `fail` となる。

## 運用ルール
1. `source_id` を発行し、出力先を `workspace/pipelines/<pipeline_id>/source/<source_id>/` に固定する。
2. `debug_mode=false` では `attempts/` を作成しない。
3. `debug_mode=true` の場合のみ失敗試行を `attempts/<attempt_id>/` に保存する。
4. `verification_status=pass` の artifact のみ `Build` に引き渡す。
5. 出力先が `workspace/` でない場合は `Generate fail` とする。
6. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
7. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Generate fail` とする。
8. ソース生成後、`MCP` の `run_linter` で `static lint` を成功させ、`source_meta.json` の `lint_command_ref` を埋めてから `Generate verify` へ渡す。
9. `MCP` `run_linter` は `cwd=<src>/` で実行され、副次的に `<src>/mcp_command_log.jsonl` を書き出す。orchestration agent は当該 path を `allowed_output_paths` に含めて `record-launch` を呼ぶこと。`tools/orchestration_runtime.py` の `_allowed_output_paths_for_launch()` が defensive auto-inject も実施するが、明示列挙が canonical。本 log は integrity-protected log として `allowed_file_tool_paths` から自動除外され、child agent は `Edit` / `Write` で直接書き込めない。`validate_pipeline_semantics.py` が `tool_name=run_linter` / `ok=true` などの記録を信頼するため、log の生成は MCP `run_linter` 経由のみに限定される。
10. `Build` 失敗からの retry を受けた場合 (`launches/<agent_run_id>.request.json#repair_reason` に `binary_meta.json` の `failure_category` / `failure_source_refs[]` / `failure_excerpt` が引用されている場合)、`docs/workflow/phases/phase_03_build.md` の retry trigger 節に従い、`failure_source_refs[]` が指すソースに限定して修正する（`repair_strategy=restart` の場合を除く）。

## 判定基準
- `model` と `runner` の責務分離が保持される。
- 出力ファイル集合が `docs/workflow/WORKFLOW_CORE.md` と `docs/workflow/phases/phase_02_generate.md` と `docs/RUNBOOK.md` の契約に一致する。
- `source_meta.json` の必須項目が欠落しない。
