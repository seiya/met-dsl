---
name: workflow-generate-verify
description: Generate ステージの verify を実行し、生成コードの責務分離、input/output contract、`generate_meta.json` の整合性を検査するときに使用する。`Build` 開始条件である `verification_status=pass` 判定に適用する。
---

# Workflow Generate Verify

## 目的
Generate ステージ出力の検証責務を固定し、`Build` 失敗を事前に低減する。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/generate/<generation_id>/src/` の検査
- `workspace/pipelines/<pipeline_id>/generate/<generation_id>/generate_meta.json` の更新

## 要件
- `case.resolved.yaml` に記載された `test case set` の全 `case_id` と全展開 `case` が、`runner` または `model` の実装経路から到達可能であることを検査する。未実装 `case`、到達不能分岐、固定 `case_id` 限定実装を `fail` とする。
- `case.resolved.yaml` の実行時入力が `runner` と `model` に伝播していることを検査する。少なくとも `case_id`、格子条件、時間条件、初期条件識別子、境界条件識別子、`profile` または `component` 選択結果、`test_profile_id`、`test_profile_version` の受理経路または記録経路を確認できない場合は `fail` とする。
- `case.resolved.yaml` で許可される選択値ごとの差分実装が固定既定値へ潰れていないことを検査する。`boundary`、`initial_profile`、`topography_profile`、`dt_rule`、`refinement`、`sweep` 展開結果などの case-dependent な入力を無視した実装を `fail` とする。
- `runner` が `model` 呼び出しに集約され、物理更新ロジックを重複実装していないことを検査する。
- `toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`runner` から `python` / `bash` / `sh` / `node` などの外部インタプリタ起動がないことを検査する。
- `model` が対象 `node` の演算契約を実装していることを検査する。固定値返却専用、固定 `JSON` 出力専用、`no-op` 専用実装を `fail` とする。
- `model` が `algorithm.resolved.yaml` で要求された `steps[]` と `ordering` と `control_condition` と `iteration_contract` を満たし、`case.resolved.yaml` の `test case set` ごとの差分入力に対して必要な分岐または共通計算経路を欠落させていないことを検査する。
- `model` と `runner` が `algorithm.resolved.yaml` の `update_semantics` と `temporaries` と `derived_field_rules` と `invariants` と `splitting_policy` を欠落なく反映していることを検査する。状態更新対象の欠落、派生量計算の未実装、保存するべき invariant を破る更新順序、`splitting_policy` 不一致を `fail` とする。
- `model` と `runner` が `algorithm.resolved.yaml` に記載されていない追加演算、追加反復、追加条件分岐、追加依存 `operation` 呼び出しを導入していないことを検査する。`resolved artifact` に存在しない実行経路を `fail` とする。
- `model` が `derived_contract.json` で要求された依存 `operation` と出力指標のデータ依存（`semantic_dependency.required_sources` と `io_contract.outputs`）を満たすことを検査する。時空間ループなど特定制御構造を一律必須にしてはならない。
- `runner` の raw evidence 出力設計が `derived_contract.json` の `raw_requirements.required_evidence` と `test_evidence_requirements` を満たすことを検査する。少なくとも `raw/metrics_basis.json` が `test_id` 単位の evidence index を保持し、全 `test_id` について `required_raw_variables` を欠落なく記録できる設計でなければならない。
- 複数 `test` の一次証跡を 1 件の summary へ潰す設計、最後に実行した `case` の値で raw evidence を上書きする設計、`diagnostics.json` の suite-level 真偽値のみを Judge 入力へ流用する設計を検出した場合は `fail` とする。
- 出力指標が `model` execution result に依存しない定数出力、固定 `JSON` 出力、解析式直接代入を検出した場合は `fail` とする。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` は、`algorithm.resolved.yaml` の状態更新契約を必須検査し、欠落または `fallback_policy!=fail_closed` を検出した場合は `fail` とする。
- `problem` かつ `spec_id` が `2d` または `3d` を含む `node` は、複数 `intent(out)` 指標を出力する `subroutine` が `state_variables` 配列参照を持たない場合を `metric-only scalar kernel` として `fail` とする。
- 生成コードが対象 `node_key` の input/output contract に一致することを検査する。
- 直下依存 `node` の `plan_ref` と `pipeline_ref` と `aggregate_verdict` を確認できない場合を `dependency workflow missing` として `fail` とする。
- 依存を持つ `node` は、`dependency.resolved.yaml` の `direct_deps` で解決された依存 `node` の公開 `operation` 呼び出しを実装していることを検査する。欠落時は `fail` とする。
- 依存 `operation` と同等機能の再実装を検出した場合は `fail` とする。
- 依存 `node` の `model` / `runner` / `module` / `subroutine` / `Makefile` 断片が依存元 `src/` に複製、再配置、再定義されている場合を `dependency implementation encapsulation` 違反として `fail` とする。
- `toolchain.language=fortran` で依存 `component` を持つ `node` は、依存 `spec_id` ごとに `model` 内の `use <spec_id>_model` と `call <spec_id>__*` を必須検査し、`subroutine <spec_id>__*` の再定義を検出した場合は `fail` とする。
- 依存先が `profile` で公開 `operation` を持たない構成では、依存元 `problem` の実装が `profile` の選択結果と拘束条件を参照していることを検査する。欠落時は `fail` とする。
- `runner` が `verdict.json` と `aggregate_verdict.json` と `summary.json` と `trial_meta.json` を書き込む実装を検出した場合は `fail` とする。
- `runner` の `diagnostics.json` と `perf.json` 出力が、標準 `JSON` parser で復元可能な UTF-8 `JSON object` を満たすことを検査する。`toolchain.language=fortran` の `runner` が `F0.d` など leading zero を欠落し得る書式を `perf.json` または `diagnostics.json` の数値 token へ直接使用する場合は `fail` とする。
- `impl.resolved.yaml` の言語と `build_system` に整合する構成であることを検査する。
- `impl.resolved.yaml` の `target.class` と `target.backend` と `target.architecture` と `toolchain.language` と `toolchain.standard` と `toolchain.build_system` と `selected.backend_key` が、生成されたソース構成と `build` 用 artifact に反映されていることを検査する。言語不一致、`build_system` 不一致、未選択 backend の code path 出力、`selected.backend_key` 未反映を `fail` とする。
- `impl.resolved.yaml` の `abstract` と `backend_overrides` で指定された並列化、レイアウト、融合、タイル、ベクトル化、非同期化などの実行アルゴリズム選択が、対象言語と target で表現可能な範囲で生成コードまたは `build` 設定へ反映されていることを検査する。指定済み knob の無視、禁止 target 向け最適化の混入、`target.class=cpu` の既定 `OpenMP` 規則違反を `fail` とする。
- 異なる `node_key` の `generate/src` との完全一致を検査し、共通ライブラリ明示がない複製を `copy_based_artifact_reuse` として `fail` にする。
- `toolchain.language=fortran` の場合、`module` 名とソースファイル名が `<module_name>.f90` で一致することを検査する。
- `toolchain.language=fortran` の場合、`module` 名と公開 `subroutine` 名に `spec_id` 由来接頭辞が付与されていることを検査する。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` の場合、`src/Makefile` の各オブジェクトターゲットが `use` 依存に対応した `.mod` または依存 `.o` の前提条件を明示していることを検査する。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` の場合、`src/Makefile` が `make -j` 互換の依存関係記述を欠落していないことを検査する。
- `quality check` 実行に必要な preset-compatible quality path が `Generate` 出力だけで成立することを検査する。`Execute` で追加 `test` source、harness、補助 `script`、一時 `Makefile` を生成しなければ成立しない構成を `fail` とする。
- `toolchain.build_system=make` かつ `toolchain.language=fortran` / `c` / `cpp` / `mixed` 系の場合、`src/Makefile` に `test` または `check` target が存在し、`run_quality_checks preset=make_test` または `preset=make_check` に適合することを検査する。
- `dependency.resolved.yaml` に存在しない依存 `node` または未宣言 `operation` への参照を生成コードが導入していないことを検査する。`direct_deps` 外の呼び出し、未解決 `component` 参照、`profile` 拘束と矛盾する実装選択を `fail` とする。
- `generate_meta.json` の必須項目を検査する。
- `debug_mode=false` の場合に `attempts/` が存在しないことを検査する。
- 検査対象 artifact の保存先ルートが `workspace/` であることを検査し、workflow ルート判定は `workspace/` のみを対象とする。

## 運用ルール
1. 検査結果に基づき `generate_meta.json` の `verification_status` を更新する。
2. `fail` の場合は `last_fail_reason` に規約違反内容と修正対象を記録する。
3. `verification_status=fail` の場合は regenerate を要求し、同一 `plan_id` で新しい `generation_id` を発行する。
4. `verification_status=pass` の場合のみ `Build` を開始する。
5. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
6. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Generate fail` とする。

## 判定基準
- 検査項目がすべて `pass` の場合のみ `verification_status=pass` とする。
- 検査結果が再現可能なファイル参照を持つ。
- 判定規則が `docs/WORKFLOW.md` と `docs/RUNBOOK.md` に整合する。
