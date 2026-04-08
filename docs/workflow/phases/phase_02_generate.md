# Phase contract: Generate

### 2. Generate
- execution input: `case.resolved.yaml`、`algorithm.resolved.yaml`、`impl.resolved.yaml`、`dependency.resolved.yaml`
- verification input: `case.resolved.yaml`、`algorithm.resolved.yaml`、`derived_contract.json`、`dependency.resolved.yaml`、`impl.resolved.yaml`
- 出力: `generate/<generation_id>/src/`、`generate_meta.json`

#### 2-1. generate substep
- `Generate` は `node` 単位で実行し、対象 `node_key` 専用のソースを生成する。
- `generate substep` は `generate/<generation_id>/src/` と `generate_meta.json` を生成する。
- `generate substep` は `generate/<generation_id>/src/` を `project_dir` とし、`toolchain.language` に応じた `run_linter` の `preset`（例: `fortran` / `cuda_fortran` は `fortitude`、`c` / `cpp` / `cuda_c` は `cppcheck`、`mixed` は `fortitude` と `cppcheck` の 2 回の証跡、`python` は `ruff`）で `static lint` を実行しなければならない。`Makefile` の `lint` target や `compile_project` 経由での実行を要求してはならない。
- `Generate` は `controlled_spec.md` を直接入力にしてはならない。必要な演算構成は `algorithm.resolved.yaml` から解釈しなければならない。
- 言語に依らず `model`（物理計算）と `runner`（input/output・実行連携）を分離して生成する。
- `runner` は `model` を `call` / `use` / `import` で呼び出し、物理更新ロジックを重複実装してはならない。
- `runner` ソースに `verdict.json`、`aggregate_verdict.json`、`summary.json`、`trial_meta.json` の各文字列を **コメント行を含む全文** に substring として含めてはならない。静的検査はコメントを除外しない。
- `toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`runner` が `python` / `bash` / `sh` / `node` など外部インタプリタを起動してはならない。
- `model` は数値状態更新または判定対象演算を実行しなければならない。固定値返却専用、固定 `JSON` 出力専用、`no-op` 専用実装を禁止する。
- 依存を持つ `node` の `model` は、`dependency.resolved.yaml` の `direct_deps` で解決された依存 `node` の公開 `operation` 呼び出しを必須とする。
- 依存 `operation` と同等機能を依存元 `node` の `model` / `runner` に再実装してはならない。検出時は `Generate fail` とする。
- 依存先が `profile` で公開 `operation` を持たない場合、依存元 `problem` は `profile` の選択結果と拘束条件を参照する実装痕跡を必須記録とする。
- `Generate` は依存 `node` の `generate/<generation_id>/src/` 相当の実装本体を依存元 `node` の `src/` へ複製、再配置、再定義してはならない。依存先 code の内包、`component` 群のまとめ書き、依存 `module` の貼り込みを検出した場合は `Generate fail` とする。
- `Generate` は直下依存 `node` の `plan_ref` と `pipeline_ref` と `aggregate_verdict` を入力整合として確認しなければならない。依存 `node` の workflow 未完了を検出した場合、依存先 code を代替生成せず `blocked` または `fail` で停止しなければならない。
- `model` / `runner` は、判定指標（例: `mass_drift_rel`、`momx_drift_rel`、`momy_drift_rel`、`analytic_h_l2_rel`）へ物理的根拠のない任意の定数スケーリング、定数オフセット、ケース依存補正を導入してはならない。`Controlled Spec` または `tests.md` で明示定義された評価式以外の補正を禁止する。
- `toolchain.language=fortran` の `module` 名と公開 `subroutine` 名は `spec_id` 由来接頭辞を含む一意名とする。
- `toolchain.language=fortran` のソースファイル名は定義 `module` 名と一致する `<module_name>.f90` を必須とする。
- `toolchain.language=fortran` で依存 `component` を持つ `node` の `model` は依存 `spec_id` ごとに `use <spec_id>_model` と `call <spec_id>__*` を必須とし、`subroutine <spec_id>__*` の再定義を禁止する。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` の場合、生成 `src/Makefile` は `use` 依存に対応したオブジェクト依存関係を明示し、依存 `.o` を各ターゲット前提条件へ必須記述する。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` の場合、オブジェクト規則のターゲットは **literal 名**（例: `foo.o:`）で記述しなければならない。`$(OBJ)` のみのような変数展開だけをターゲットとする規則行は、静的検査の規則パーサでは採用されず、依存欠落と判定される。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` の場合、`src/Makefile` は並列ビルド（例: `make -j 4`）で依存欠落による失敗を起こしてはならない。
- 同一 `pipeline` 内で異なる `node_key` に同一 `src` を複製してはならない。共通化は共通ライブラリとして明示する。
- `target.class=cpu` でループ並列化方式の明示指定がない場合、並列化可能ループへ `OpenMP` を付与する。
- 物理更新を実装できない場合は `Generate fail` とし、代替として固定文字列や固定 `JSON` を出力してはならない。

#### 2-2. verify substep
- `Generate verify` 完了前に `python3 tools/validate_pipeline_semantics.py --stage post_generate --pipeline-root workspace/pipelines/<node_key_safe>/<pipeline_id>/` を実行し、検証対象の `generation_id` を固定する場合は `--generation-id <generation_id>` を付与しなければならない。`exit code 0` を必須とする。`fail` 時は `generate_meta.json` の `verification_status=pass` を付与してはならない。
- `pipeline_id` は `<slug>_<date>_<seq3>` 形式を必須とし、`slug` は `spec_id` 由来の短い可読 token、`date` は `YYYYMMDD`、`seq3` は同日内 3 桁連番とする。
- `generation_id` は `gen_<date>_<seq3>` 形式を必須とし、`date` は `YYYYMMDD`、`seq3` は同日内 3 桁連番とする。
- `Generate verify` は、`model` が `case_id` 分岐と固定数値代入のみで判定指標を構成する実装を検出した場合に `fail` とする。
- `Generate verify` は `case.resolved.yaml` を入力として、記載された `test case set` の全 `case_id` と全展開 `case` が `runner` または `model` の実装経路から到達可能であることを検証しなければならない。未実装 `case`、到達不能分岐、固定 `case_id` 限定実装を検出した場合は `fail` とする。
- `Generate verify` は `case.resolved.yaml` の実行時入力を `runner` と `model` が受理していることを検証しなければならない。少なくとも `case_id`、格子条件、時間条件、初期条件識別子、境界条件識別子、`profile` または `component` 選択結果、`tests.md` 由来の `test_profile_id` と `test_profile_version` に対応する入力伝播または記録経路を確認できない場合は `fail` とする。
- `Generate verify` は `case.resolved.yaml` で許可される選択値ごとの差分実装が、固定定数または単一既定値に潰されていないことを検証しなければならない。`boundary`、`initial_profile`、`topography_profile`、`dt_rule`、`refinement`、`sweep` 展開結果などの case-dependent な入力を無視した実装を検出した場合は `fail` とする。
- `Generate verify` は `case.resolved.yaml` と `algorithm.resolved.yaml` と `derived_contract.json` と `dependency.resolved.yaml` と `impl.resolved.yaml` を入力として、`test case set` 網羅、演算構成、依存 `operation`、出力指標のデータ依存を検証しなければならない。制御構造の形式を固定要件にしてはならず、判定は `case.resolved.yaml` の `test case set` と `algorithm.resolved.yaml` の `steps` と `ordering` と `control_condition` と `iteration_contract` に基づいて実施しなければならない。
- `Generate verify` は `algorithm.resolved.yaml` の `update_semantics` と `temporaries` と `derived_field_rules` と `invariants` と `splitting_policy` が生成コードへ反映されていることを検証しなければならない。状態更新対象の欠落、派生量計算の未実装、保存するべき invariant を破る更新順序、`splitting_policy` 不一致を検出した場合は `fail` とする。
- `Generate verify` は `algorithm.resolved.yaml` に記載されていない追加演算、追加反復、追加条件分岐、追加依存 `operation` 呼び出しを生成コードが導入していないことを検証しなければならない。`controlled_spec.md` 由来情報の推測再導入や、`resolved artifact` に存在しない実行経路を検出した場合は `fail` とする。
- `Generate verify` は `model` 出力と無関係な定数出力、固定 `JSON` 出力、解析式直接代入による `diagnostics` 生成を検出した場合に `fail` とする。
- `Generate verify` は、`intent(out)` 変数の最終式木が `derived_contract.json` の `semantic_dependency.required_sources` と `io_contract.outputs` で宣言された出力変数群へ到達することを検証しなければならない。
- `Generate verify` は `derived_contract.json` の `raw_requirements.required_evidence` と `test_evidence_requirements` を `runner` の raw evidence 出力設計と照合し、全 `test_id` ごとに Judge 再計算に必要な raw evidence が保持されることを検証しなければならない。`runner` が suite 全体 summary のみを書き出す構成、複数 `test` の一次証跡を 1 件へ上書き集約する構成、`required_raw_variables` の欠落を検出した場合は `fail` とする。
- `Generate verify` は、`spec` の目的に依存しない固定計算様式（例: 常に `flux` や常に時刻積分）を一律必須にしてはならない。判定は `derived_contract.json` の要求計算種別に基づいて実施しなければならない。
- `Generate verify` は `impl.resolved.yaml` の `target.class` と `target.backend` と `target.architecture` と `toolchain.language` と `toolchain.standard` と `toolchain.build_system` と `selected.backend_key` が、生成されたソース構成と `build` 用 artifact に反映されていることを検証しなければならない。言語不一致、`build_system` 不一致、未選択 backend の code path 出力、`selected.backend_key` 未反映を検出した場合は `fail` とする。
- `Generate verify` は `impl.resolved.yaml` の `abstract` と `backend_overrides` で指定された並列化、レイアウト、融合、タイル、ベクトル化、非同期化などの実行アルゴリズム選択が、対象言語と target で表現可能な範囲で生成コードまたは `build` 設定へ反映されていることを検証しなければならない。指定済み knob の無視、禁止 target 向け最適化の混入、`target.class=cpu` の既定 `OpenMP` 規則違反を検出した場合は `fail` とする。
- `Generate verify` は `dependency.resolved.yaml` に存在しない依存 `node` または未宣言 `operation` への参照を生成コードが導入していないことを検証しなければならない。`direct_deps` 外の呼び出し、未解決 `component` 参照、`profile` 拘束と矛盾する実装選択を検出した場合は `fail` とする。

