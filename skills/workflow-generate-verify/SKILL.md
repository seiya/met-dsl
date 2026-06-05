---
name: workflow-generate-verify
description: Generate ステージの verify を実行し、生成コードの責務分離、input/output contract、`source_meta.json` の整合性を検査するときに使用する。`Build` 開始条件である `verification_status=pass` 判定に適用する。
---

# Workflow Generate Verify

## 目的
Generate ステージ出力の検証責務を固定し、`Build` 失敗を事前に低減する。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/source/<source_id>/src/` の検査
- `workspace/pipelines/<pipeline_id>/source/<source_id>/source_meta.json` の更新

## 要件
- 本 substep が起動できる validator gate は `skills/workflow-orchestration/references/launch_prompts.md` の「substep ↔ allowed validator gate 対応表」を canonical source とする。
- 判定規則の canonical source は `docs/workflow/WORKFLOW_CORE.md` と `docs/workflow/phases/phase_02_generate.md` と `docs/RUNBOOK.md` と `spec.ir.yaml` (`case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency` の 5 セクション) と検査対象生成物に限定する。`tools/` 配下の実装、検証 `script`、test code、validator code を読んで要求や判定規則を抽出してはならない。
- `spec.ir.yaml.case` に記載された `test case set` の全 `case_id` と全展開 `case` が、`runner` または `model` の実装経路から到達可能であることを検査する。未実装 `case`、到達不能分岐、固定 `case_id` 限定実装を `fail` とする。
- `spec.ir.yaml.case` の実行時入力が `runner` と `model` に伝播していることを検査する。少なくとも `case_id`、格子条件、時間条件、初期条件識別子、境界条件識別子、`profile` または `component` 選択結果、`test_profile_id`、`test_profile_version` の受理経路または記録経路を確認できない場合は `fail` とする。
- `spec.ir.yaml.case` で許可される選択値ごとの差分実装が固定既定値へ潰れていないことを検査する。`boundary`、`initial_profile`、`topography_profile`、`dt_rule`、`refinement`、`sweep` 展開結果などの case-dependent な入力を無視した実装を `fail` とする。
- `runner` が `model` 呼び出しに集約され、物理更新ロジックを重複実装していないことを検査する。
- `spec.ir.yaml.impl_defaults.toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`runner` から `python` / `bash` / `sh` / `node` などの外部インタプリタ起動がないことを検査する。
- `model` が対象 `node` の演算契約を実装していることを検査する。固定値返却専用、固定 `JSON` 出力専用、`no-op` 専用実装を `fail` とする。
- `model` が `spec.ir.yaml.algorithm` で要求された `steps[]` と `ordering` と `control_condition` と `iteration_contract` を満たし、`spec.ir.yaml.case` の `test case set` ごとの差分入力に対して必要な分岐または共通計算経路を欠落させていないことを検査する。
- `model` と `runner` が `spec.ir.yaml.algorithm` の `update_semantics` と `temporaries` と `derived_field_rules` と `invariants` と `splitting_policy` を欠落なく反映していることを検査する。状態更新対象の欠落、派生量計算の未実装、保存するべき invariant を破る更新順序、`splitting_policy` 不一致を `fail` とする。
- `model` と `runner` が `spec.ir.yaml.algorithm` に記載されていない追加演算、追加反復、追加条件分岐、追加依存 `operation` 呼び出しを導入していないことを検査する。`spec.ir.yaml` に存在しない実行経路を `fail` とする。
- `model` が `spec.ir.yaml.io_contract` で要求された依存 `operation` と出力指標のデータ依存（`semantic_dependency.required_sources` と `io_contract.outputs`）を満たすことを検査する。時空間ループなど特定制御構造を一律必須にしてはならない。
- `runner` の raw evidence 出力設計が `spec.ir.yaml.io_contract.raw_requirements.required_evidence` と `test_evidence_requirements` を満たすことを検査する。少なくとも `raw/metrics_basis.json` が `test_id` 単位の evidence index を保持し、全 `test_id` について `required_raw_variables` を欠落なく記録できる設計でなければならない。
- 複数 `test` の一次証跡を 1 件の summary へ潰す設計、最後に実行した `case` の値で raw evidence を上書きする設計、`diagnostics.json` の suite-level 真偽値のみを `Validate.judge` 入力へ流用する設計を検出した場合は `fail` とする。
- 出力指標が `model` execution result に依存しない定数出力、固定 `JSON` 出力、解析式直接代入を検出した場合は `fail` とする。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` は、`spec.ir.yaml.algorithm` の状態更新契約を必須検査し、欠落または `fallback_policy!=fail_closed` を検出した場合は `fail` とする。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` は、複数 `intent(out)` 指標を出力する `subroutine` が `state_variables` 配列参照を持たない場合を `metric-only scalar kernel` として `fail` とする。
- 生成コードが対象 `node_key` の input/output contract に一致することを検査する。
- 直下依存 `node` の `ir_ref` と `pipeline_ref` と `aggregate_verdict` を確認できない場合を `dependency workflow missing` として `fail` とする。
- 依存を持つ `node` は、`spec.ir.yaml.dependency.direct_deps` で解決された依存 `node` の公開 `operation` 呼び出しを実装していることを検査する。欠落時は `fail` とする。
- 依存 `operation` と同等機能の再実装を検出した場合は `fail` とする。
- 依存 `node` の `model` / `runner` / `module` / `subroutine` / `Makefile` 断片が依存元 `src/` に複製、再配置、再定義されている場合を `dependency implementation encapsulation` 違反として `fail` とする。
- `impl_defaults.toolchain.language=fortran` で依存 `component` を持つ `node` は、依存 `spec_id` ごとに `model` 内の `use <spec_id>_model` と `call <spec_id>__*` を必須検査し、`subroutine <spec_id>__*` の再定義を検出した場合は `fail` とする。
- 依存先が `profile` で公開 `operation` を持たない構成では、依存元 `problem` の実装が `profile` の選択結果と拘束条件を参照していることを検査する。欠落時は `fail` とする。
- `runner` が `verdict.json` と `aggregate_verdict.json` と `summary.json` と `trial_meta.json` を書き込む実装を検出した場合は `fail` とする。当該禁止名は `docs/workflow/phases/phase_02_generate.md` と `docs/RUNBOOK.md` のとおり、**コメントを含む runner ソース全文** への substring 照合対象であり、コメント内の例示に同一文字列があっても `fail` となる。
- `runner` の `diagnostics.json` と `perf.json` 出力が、標準 `JSON` parser で復元可能な UTF-8 `JSON object` を満たすことを検査する。`impl_defaults.toolchain.language=fortran` の `runner` が `F0.d` など leading zero を欠落し得る書式を `perf.json` または `diagnostics.json` の数値 token へ直接使用する場合は `fail` とする。
- `spec.ir.yaml.impl_defaults` の言語と `build_system` に整合する構成であることを検査する。
- `impl_defaults` の `target.class` と `target.backend` と `target.architecture` と `toolchain.language` と `toolchain.standard` と `toolchain.build_system` と `selected.backend_key` が、生成されたソース構成と `build` 用 artifact に反映されていることを検査する。言語不一致、`build_system` 不一致、未選択 backend の code path 出力、`selected.backend_key` 未反映を `fail` とする。
- `impl_defaults.abstract` と `impl_defaults.backend_overrides` で指定された並列化、レイアウト、融合、タイル、ベクトル化、非同期化などの実行アルゴリズム選択が、対象言語と target で表現可能な範囲で生成コードまたは `build` 設定へ反映されていることを検査する。指定済み knob の無視、禁止 target 向け最適化の混入、`target.class=cpu` の既定 `OpenMP` 規則違反を `fail` とする。
- 異なる `node_key` の `source/src` との完全一致を検査し、共通ライブラリ明示がない複製を `copy_based_artifact_reuse` として `fail` にする。
- `impl_defaults.toolchain.language=fortran` の場合、`module` 名とソースファイル名が `<module_name>.f90` で一致することを検査する。
- `impl_defaults.toolchain.language=fortran` の場合、`module` 名と公開 `subroutine` 名に `spec_id` 由来接頭辞が付与されていることを検査する。
- `impl_defaults.toolchain.build_system=make` かつ `impl_defaults.toolchain.language=fortran` の場合、`src/Makefile` の各オブジェクトターゲットが `use` 依存に対応した `.mod` または依存 `.o` の前提条件を明示していることを検査する。規則行のターゲットは **literal** 名で記述されていること（`$(VAR):` だけのターゲットに依存していないこと）を確認する。補足は `docs/RUNBOOK.md` の 1-2-1 を参照する。
- `impl_defaults.toolchain.build_system=make` かつ `impl_defaults.toolchain.language=fortran` の場合、`src/Makefile` が `make -j` 互換の依存関係記述を欠落していないことを検査する。
- `impl_defaults.toolchain.build_system=make` かつ `impl_defaults.toolchain.language=fortran` / `c` / `cpp` / `mixed` 系の場合、`src/Makefile` が out-of-source 出力契約（`OBJDIR ?= .` / `BINDIR ?= .` / `RUNDIR ?= .` の `?=` パラメタライズ、object/module を `$(OBJDIR)`・リンク binary を `$(BINDIR)/$(BIN)`・`test`/`check` の run 出力を `$(RUNDIR)` 配下へ出す構成。`fortran` は `gfortran -J$(OBJDIR)` での `.mod` 出力）を満たすことを検査する。build artifact を `src/` に固定出力する（`OBJDIR`/`BINDIR` を参照しない、または `.o`/`.mod`/exe を `src/` 直下へ書く）Makefile を `fail` とする。`Build` は当該 override を `compile_project` から渡すため、未パラメタライズ Makefile は `Build` で `unauthorized_write_violation` → `fail_closed` を招く（canonical source は [docs/workflow/phases/phase_02_generate.md](../../docs/workflow/phases/phase_02_generate.md) 2-1）。
- `quality check` 実行に必要な preset-compatible quality path が `Generate` 出力だけで成立することを検査する。`Validate.execute` で追加 `test` source、harness、補助 `script`、一時 `Makefile` を生成しなければ成立しない構成を `fail` とする。
- `impl_defaults.toolchain.build_system=make` かつ `impl_defaults.toolchain.language=fortran` / `c` / `cpp` / `mixed` 系の場合、`src/Makefile` に `test` または `check` target が存在し、`run_quality_checks preset=make_test` または `preset=make_check` に適合することを検査する。当該 target が `$(BINDIR)` の既存 binary を参照し（read-only `binary/` で relink しない guard `test -x $(BINDIR)/$(BIN) || $(MAKE) …`）、`cd $(RUNDIR)` の前に `mkdir -p $(RUNDIR)`（および runner が書く `raw/` 必須サブディレクトリ）で出力先を生成し、run 出力を `$(RUNDIR)` 配下へ閉じる構成であることを検査する。`src/` 直下へ relink・run 出力する target、および `$(RUNDIR)` を生成せず `cd $(RUNDIR)` する target を `fail` とする。
- `spec.ir.yaml.dependency` に存在しない依存 `node` または未宣言 `operation` への参照を生成コードが導入していないことを検査する。`direct_deps` 外の呼び出し、未解決 `component` 参照、`profile` 拘束と矛盾する実装選択を `fail` とする。
- `source_meta.json` の必須項目を検査する。`lint_command_ref.run_linter` が `verification_status=pass` のとき、`spec.ir.yaml.impl_defaults.toolchain.language` に整合する `preset` と MCP `run_linter` 成功ログ（`command_id` と `command_log_ref`）が記録されていることを検査する。本検査は既存記録の *inspect* に限定し、`run_linter` を **再実行してはならない**。`static lint` は `quality check`（`run_quality_checks`）とは別物である。
- `Generate.verify` は `run_linter` を含む `build-runtime` MCP の *write/refresh* 系 tool（`run_linter` / `compile_project` / `run_program` / `run_quality_checks`）を実行してはならない。`static lint` の実行と `lint_command_ref` 記録は `Generate.generate` の責務である（[docs/workflow/phases/phase_02_generate.md](../../docs/workflow/phases/phase_02_generate.md) 2-1）。verify は既存の `lint_command_ref.run_linter` 証跡を *inspect* するのみとする。verify が起動してよい外部 gate は 運用ルール 6/7 の `validate_workspace_root` と `validate_pipeline_semantics --stage post_generate` に限定する。verify の `allowed_output_paths` が authorize しない副次出力（`run_linter` の `mcp_command_log.jsonl` など）への書き込みを誘発する tool 呼び出しは `unauthorized_write_violation` → `fail_closed` の原因になる。
- workflow mode は `METDSL_WORKFLOW_EXEC_MODE` を canonical source とし、未設定時は `dev` を適用する。
- `dev` mode では `issue_severity=major|critical` を検出した時点で `Generate fail` とし、軽微例外扱いを禁止する。
- `debug_mode=false` の場合に `attempts/` が存在しないことを検査する。
- 検査対象 artifact の保存先ルートが `workspace/` であることを検査し、workflow ルート判定は `workspace/` のみを対象とする。

## 運用ルール
1. 検査結果に基づき `source_meta.json` の `verification_status` を更新する。
2. `fail` の場合は `last_fail_reason` に規約違反内容と修正対象を記録する。`Validate` retry を起点とする場合は `source_meta.json` の `attribution_hint` に `code` を必須記録する（routing 整合）。
3. `verification_status=fail` の場合は regenerate を要求し、同一 `ir_id` で新しい `source_id` を発行する。
4. `verification_status=pass` の場合のみ `Build` を開始する。
5. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
6. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Generate fail` とする。
7. verify 完了前に `python3 tools/validate_pipeline_semantics.py --stage post_generate --pipeline-root workspace/pipelines/<node_key_safe>/<pipeline_id>/` を実行する。検証対象の `source_id` を固定する場合は `--source-id <source_id>` を付与する。`exit code 0` を必須とし、`fail` 時は `source_meta.json` の `verification_status=pass` を付与してはならない。
8. `dev` mode で `fail` した場合は、`failure_analysis.json` 作成に必要な根拠（違反規約、対象 artifact、失敗理由）を `last_fail_reason` に記録する。

## 判定基準
- 検査項目がすべて `pass` の場合のみ `verification_status=pass` とする。
- 検査結果が再現可能なファイル参照を持つ。
- 判定規則が `docs/workflow/WORKFLOW_CORE.md` と `docs/workflow/phases/phase_02_generate.md` と `docs/RUNBOOK.md` に整合する。
