# Phase 2: Generate

## 概要
`spec.ir.yaml` を canonical 入力として、対象 `node` の `model`（物理計算）と `runner`（input/output・実行連携）のソースコードを生成する phase。`controlled_spec.md` の直接読み込みは禁止する。

## I/O 契約
- execution input: `spec.ir.yaml`
- verification input: `spec.ir.yaml`、生成された `source/<source_id>/src/`
- 出力: `workspace/pipelines/<node_key_safe>/<pipeline_id>/source/<source_id>/src/`、`source_meta.json`

## substep 構成
- `Generate.generate`: `spec.ir.yaml` を読みソース一式を生成する LLM substep。
- `Generate.verify`: ソースが `spec.ir.yaml` の `case` / `algorithm` / `io_contract` / `dependency` / `impl_defaults` セクションに整合することを検証する独立 LLM substep。

## `source_id` フォーマット
- 形式: `src_<YYYYMMDD>_<seq3>`、例: `src_20260511_001`

## `source_meta.json` 必須 key
- 共通: `attempt_count`、`verification_status`、`last_fail_reason`、`debug_mode`、`context_isolated`
- `context_isolated=false` の場合、`constraint_reason` を必須とする。
- `verification_status=pass` の場合、`lint_command_ref.run_linter[]`（`command_id`、`command_log_ref`、`preset`）を必須とする。

## substep 詳細

### 2-1. Generate.generate substep
- `Generate` は `node` 単位で実行し、対象 `node_key` 専用のソースを生成する。
- `generate substep` は `source/<source_id>/src/` と `source_meta.json` を生成する。
- `Generate` は `controlled_spec.md` を直接入力にしてはならない。必要な演算構成は `spec.ir.yaml` の `algorithm` セクションから解釈する。
- 言語に依らず `model`（物理計算）と `runner`（input/output・実行連携）を分離して生成する。
- `runner` は `model` を `call` / `use` / `import` で呼び出し、物理更新ロジックを重複実装してはならない。
- `runner` ソースに `verdict.json`、`aggregate_verdict.json`、`summary.json`、`trial_meta.json` の各文字列を **コメント行を含む全文** に substring として含めてはならない。
- `runner` が JSON 出力（`diagnostics.json` / `perf.json` / `raw/metrics_basis.json` / `raw/state_snapshots/*.json`）へ書く token は標準 JSON parser で復元可能でなければならない。`impl_defaults.toolchain.language=fortran` では数値へ leading-zero を欠落し得る `F0.d` 書式を、論理値へ `T`/`F` を生む `L` 系 edit descriptor（`L1` 等）を JSON token へ直接使用してはならない。boolean は literal `true` / `false` を出力する。canonical な serialization 規則は [docs/PERFORMANCE_DIAGNOSTICS.md](../../PERFORMANCE_DIAGNOSTICS.md) §6 を参照する。post_generate 静的解析がこの違反を検出して fail させる。
- `runner` が出力する `perf.json` は [docs/PERFORMANCE_DIAGNOSTICS.md](../../PERFORMANCE_DIAGNOSTICS.md) §2 の必須フィールドを欠落なく持たなければならない: `case_id` / `target` / `walltime_sec` / `steps` / `cells_updated` / `throughput_cells_per_sec` / `parallelism`（object: `mpi_ranks` / `threads_per_rank` / `gpu_devices` / `parallel_degree_total`）。これらは IR から導出するのではなく canonical doc が一律に要求する runner 出力契約であり、`Validate.execute` の `post_execute` gate が `walltime_sec` / `throughput_cells_per_sec` / `parallelism` を必須検証する。独自最小 schema（例: `{case_count, wall_seconds}`）は fail する。
- `runner` が出力する `raw/metrics_basis.json` は `io_contract.test_evidence_requirements` の全 `test_id` を対象とする **`per_test` list（または `tests` object）** を持ち、各 entry が `test_id` と当該 `required_raw_variables` を欠落なく保持しなければならない（`Validate.judge` の per-test 再計算に必要）。per-test 索引を持たない独自構造（例: 単一 `evidence[]`）は `post_execute` gate（`must contain per_test list or tests object`）で reject される。
- `spec.ir.yaml.impl_defaults.toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`runner` が `python` / `bash` / `sh` / `node` など外部インタプリタを起動してはならない。
- `model` は数値状態更新または判定対象演算を実行しなければならない。固定値返却専用、固定 `JSON` 出力専用、`no-op` 専用実装を禁止する。
- 依存を持つ `node` の `model` は、`spec.ir.yaml.dependency.direct_deps` で解決された依存 `node` の公開 `operation` 呼び出しを必須とする。
- 依存 `operation` と同等機能を依存元 `node` の `model` / `runner` に再実装してはならない。検出時は `Generate fail` とする。
- 依存先が `profile` で公開 `operation` を持たない場合、依存元 `problem` は `profile` の選択結果と拘束条件を参照する実装痕跡を必須記録とする。
- `Generate` は依存 `node` の `source/<source_id>/src/` 相当の実装本体を依存元 `node` の `src/` へ複製、再配置、再定義してはならない。検出時は `Generate fail` とする。
- `Generate` は直下依存 `node` の `ir_ref` と `pipeline_ref` と `aggregate_verdict` を入力整合として確認しなければならない。依存 `node` の workflow 未完了を検出した場合、依存先 code を代替生成せず `blocked` または `fail` で停止する。
- `model` / `runner` は、判定指標へ物理的根拠のない任意の定数スケーリング、定数オフセット、ケース依存補正を導入してはならない。`controlled_spec.md` または `tests.md` で明示定義された評価式以外の補正を禁止する。
- `impl_defaults.toolchain.language=fortran` の `module` 名と公開 `subroutine` 名は `spec_id` 由来接頭辞を含む一意名とする。
- `impl_defaults.toolchain.language=fortran` のソースファイル名は定義 `module` 名と一致する `<module_name>.f90` を必須とする。
- `impl_defaults.toolchain.language=fortran` で依存 `component` を持つ `node` の `model` は依存 `spec_id` ごとに `use <spec_id>_model` と `call <spec_id>__*` を必須とし、`subroutine <spec_id>__*` の再定義を禁止する。
- `impl_defaults.toolchain.build_system=make` かつ `impl_defaults.toolchain.language=fortran` の場合、生成 `src/Makefile` は `use` 依存に対応したオブジェクト依存関係を明示し、依存 `.o` を各ターゲット前提条件へ必須記述する。
- `impl_defaults.toolchain.build_system=make` かつ `impl_defaults.toolchain.language=fortran` の場合、オブジェクト規則のターゲット・前提条件は **literal `.o`/`.mod` basename へ解決できる形**で記述する。**out-of-source build（`$(OBJDIR)` override）の正当性のため、依存オブジェクトの生成規則ターゲットが `$(OBJDIR)/foo.o:` のように `$(OBJDIR)/` prefix 付きなら、それを消費する前提条件も同じ `$(OBJDIR)/` prefix（`$(OBJDIR)/foo.o` / `$(OBJDIR)/foo.mod`、または `$(OBJDIR)/` へ展開される変数）で書くこと。bare basename（prefix 無しの `foo.o` / `foo.mod`）は `OBJDIR` override 時に生成規則を持たない別ターゲットとなり `make -j` が `No rule to make target` で停止するため fail とする**（`$(OBJDIR)/` prefix は cosmetic ではなく、post_generate 静的解析が prefix 不一致を検出する）。in-source 専用に全規則を bare で書く場合はターゲット・前提条件をともに bare で揃える（例: `foo.o:`）。前提条件・ターゲットを変数（`MODEL_OBJ = $(OBJDIR)/foo_model.o` を参照する `$(MODEL_OBJ)` 等）で書く場合も、静的解析が同一 Makefile 内の単純変数定義（`=` / `:=` / `?=` / `+=`）を展開して `$(OBJDIR)/` prefix 込みで解決するため許容される。ただし変数は**当該規則より前**で定義すること（make は前提条件を読み込み時に即時展開するため後置定義の forward reference は空展開され依存欠落となる）。同 Makefile 内に定義が無い未定義変数（展開不能で literal basename が残らない）を使うと依存欠落と判定される。
- `impl_defaults.toolchain.build_system=make` かつ `impl_defaults.toolchain.language=fortran` の場合、`src/Makefile` は並列ビルド（例: `make -j 4`）で依存欠落による失敗を起こしてはならない。
- **out-of-source 出力契約（`build_system=make`）:** `src/Makefile` は `OBJDIR ?= .` / `BINDIR ?= .` / `RUNDIR ?= .` を `?=` デフォルトでパラメタライズする。object/module は `$(OBJDIR)`（`gfortran` は `-J$(OBJDIR)` / `-I$(OBJDIR)`）、実行 binary は `$(BINDIR)/$(BIN)`、`test`/`check` の run 出力は `$(RUNDIR)` 配下に出す。生成 `src/` は build artifact を持たない pristine 状態を保ち（cross-phase MCP audit log のみ）、`Build` は object を per-run tmp、exe を `binary/<binary_id>/bin/` に出す override を `compile_project` から渡す（[phase_03_build.md](phase_03_build.md)）。`?=` デフォルト `.` でローカルの素の `make` は in-source 動作を維持する。
- 同一 `pipeline` 内で異なる `node_key` に同一 `src` を複製してはならない。共通化は共通ライブラリとして明示する。
- `impl_defaults.target.class=cpu` でループ並列化方式の明示指定がない場合、並列化可能ループへ `OpenMP` を付与する。
- 物理更新を実装できない場合は `Generate fail` とし、代替として固定文字列や固定 `JSON` を出力してはならない。
- `Generate.generate` は `source/<source_id>/src/` を `project_dir` とし、`impl_defaults.toolchain.language` に応じた `run_linter` の `preset`（例: `fortran` / `cuda_fortran` は `fortitude`、`c` / `cpp` / `cuda_c` は `cppcheck`、`mixed` は `fortitude` と `cppcheck` の 2 回の証跡、`python` は `ruff`）で `static lint` を実行する。`Makefile` の `lint` target や `compile_project` 経由での実行を要求してはならない。`run_linter` を含む `build-runtime` MCP server の登録、及び子 `Agent` session への tool permission 付与は workflow 起動前提として `preflight` が verify 済み (Claude backend では `claude_mcp_build_runtime_registered` ∧ `claude_mcp_build_runtime_permission_granted` の両 check、詳細は [CLAUDE.md](../../../CLAUDE.md) preflight 節)。
- **fortitude C003 ↔ `-std=f2008` conflict（Fortran source 生成時の必須回避）:** fortitude lint rule **C003** は F2018 の spec-list 付き `implicit none (type, external)` を要求するが、`impl_defaults.toolchain.standard=f2008`（Makefile `FFLAGS=-std=f2008`）下ではこの形式が `Error: Fortran 2018: IMPLICIT NONE with spec list` で `compile_error` になる。両者は構造的 conflict。回避は plain `implicit none`（spec-list なし）を使い C003 のみ無効化する。**無効化手段は生成 source への inline `! allow(C003)` ディレクティブ**（`implicit none` の直前行に置く）を canonical とする。`src/<source_id>/src/fortitude.toml` に `[check] ignore=["C003"]` を置く方法は output manifest の source-extension 制限で reject されるため使わない。
- `Generate` は `Validate.execute` が `run_quality_checks` の `preset` だけで実行可能な preset-compatible quality path を正式出力へ含めなければならない。不足時は `Generate fail` とする。
- `impl_defaults.toolchain.build_system=make` かつ `impl_defaults.toolchain.language=fortran` / `c` / `cpp` / `mixed` 系では、`source/<source_id>/src/Makefile` に `test` または `check` target を必須定義する。欠落時は `Generate fail` とする。当該 target は `$(BINDIR)` の既存 binary を参照（`Validate.execute` の read-only `binary/` で relink しない guard `test -x $(BINDIR)/$(BIN) || $(MAKE) …` を置く）し、`cd $(RUNDIR)` の **前に** `mkdir -p $(RUNDIR)`（および runner が書く `raw/` 必須サブディレクトリ）で出力先を生成してから実行し、run 出力を `$(RUNDIR)` 配下へ閉じる。`$(RUNDIR)` は未作成の run node dir を指し得るため `mkdir -p` を欠くと `cd` が失敗する。

### 2-2. Generate.verify substep
`Generate.verify` 完了前に `python3 tools/validate_pipeline_semantics.py --stage post_generate --pipeline-root workspace/pipelines/<node_key_safe>/<pipeline_id>/` を実行し、検証対象の `source_id` を固定する場合は `--source-id <source_id>` を付与する。`exit code 0` を必須とする。

`Generate.verify` は `run_linter` を含む `build-runtime` MCP の write 系 tool（`run_linter` / `compile_project` / `run_program` / `run_quality_checks`）を実行してはならない（`static lint` は 2-1 の `Generate.generate` 責務）。verify が起動してよい外部 gate は read-only の `validate_workspace_root.py` と `validate_pipeline_semantics --stage post_generate` に限定し、`lint_command_ref.run_linter` は既存記録の *検査* 対象に限定する。verify の `allowed_output_paths` は `run_linter` の副次出力 (`mcp_command_log.jsonl`) を authorize しないため、verify から `run_linter` を実行するとその書き込みが manifest 外となり `unauthorized_write_violation` → `fail_closed` を招く。

`Generate.verify` 必須検証項目:

#### G1. case 被覆性
- `spec.ir.yaml.case.test_case_set` の全 `case_id` と全展開 `case` が `runner` または `model` の実装経路から到達可能であること。
- 未実装 `case`、到達不能分岐、固定 `case_id` 限定実装を検出した場合は `fail` とする。
- `model` が `case_id` 分岐と固定数値代入のみで判定指標を構成する実装を検出した場合に `fail` とする。

#### G2. case 入力受理
- `spec.ir.yaml.case.test_case_set[].inputs` の各実行時入力を `runner` と `model` が受理していること。
- 少なくとも `case_id`、格子条件、時間条件、初期条件識別子、境界条件識別子、`profile` または `component` 選択結果、`tests.md` 由来の `test_profile_id` と `test_profile_version` に対応する入力伝播または記録経路を確認できない場合は `fail`。

#### G3. case 依存差分の実装
- `case.test_case_set[].inputs` で許可される選択値ごとの差分実装が、固定定数または単一既定値に潰されていないこと。
- `boundary`、`initial_profile`、`topography_profile`、`dt_rule`、`refinement`、`sweep` 展開結果などの case-dependent な入力を無視した実装を検出した場合は `fail`。

#### G4. algorithm 反映
- `spec.ir.yaml.algorithm.steps` と `ordering` と `control_condition` と `iteration_contract` に基づき、`test case set` 網羅、演算構成、依存 `operation`、出力指標のデータ依存が生成コードに反映されていること。
- `algorithm.update_semantics` と `temporaries` と `derived_field_rules` と `invariants` と `splitting_policy` が生成コードへ反映されていること。状態更新対象の欠落、派生量計算の未実装、保存するべき invariant を破る更新順序、`splitting_policy` 不一致を検出した場合は `fail`。
- `algorithm` に記載されていない追加演算、追加反復、追加条件分岐、追加依存 `operation` 呼び出しを生成コードが導入していないこと。`controlled_spec.md` 由来情報の推測再導入を検出した場合は `fail`。

#### G5. io_contract 整合
- `model` 出力と無関係な定数出力、固定 `JSON` 出力、解析式直接代入による `diagnostics` 生成を検出した場合に `fail` とする。
- `intent(out)` 変数の最終式木が `spec.ir.yaml.io_contract.semantic_dependency.required_sources` と `io_contract.outputs` で宣言された出力変数群へ到達することを検証する。
- `spec.ir.yaml.io_contract.raw_requirements.required_evidence` と `test_evidence_requirements` を `runner` の raw evidence 出力設計と照合し、全 `test_id` ごとに `Validate.judge` 再計算に必要な raw evidence が保持されることを検証する。`runner` が suite 全体 summary のみを書き出す構成、複数 `test` の一次証跡を 1 件へ上書き集約する構成、`required_raw_variables` の欠落を検出した場合は `fail`。

#### G6. impl_defaults 反映
- `spec.ir.yaml.impl_defaults.target.class` と `target.backend` と `target.architecture` と `toolchain.language` と `toolchain.standard` と `toolchain.build_system` と `selected.backend_key` が、生成されたソース構成と `build` 用 artifact に反映されていること。
- `impl_defaults.abstract` と `backend_overrides` で指定された並列化、レイアウト、融合、タイル、ベクトル化、非同期化などの実行アルゴリズム選択が、対象言語と target で表現可能な範囲で生成コードまたは `build` 設定へ反映されていることを検証する。指定済み knob の無視、禁止 target 向け最適化の混入、`target.class=cpu` の既定 `OpenMP` 規則違反を検出した場合は `fail`。

#### G7. dependency 整合
- `spec.ir.yaml.dependency` に存在しない依存 `node` または未宣言 `operation` への参照を生成コードが導入していないことを検証する。`direct_deps` 外の呼び出し、未解決 `component` 参照、`profile` 拘束と矛盾する実装選択を検出した場合は `fail`。

## 失敗時挙動
- `Generate fail` の retry は同 phase 内 retry を既定とする。
- `Generate.verify` が「IR 自体に誤りがある」と判定した場合は、`source_meta.json.last_fail_reason` に「ir_inconsistency」を記録し、`orchestration agent` が `Compile` まで戻すかを判定する。
