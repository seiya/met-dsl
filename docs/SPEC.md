# 全体仕様: ドキュメント駆動で気象・気候計算向けサブルーチン群と runner を生成する基盤

## 最終ゴール
`Controlled Spec` と `tests` を canonical source として、`CPU` / `GPU` などのハードウェア上で各 `spec` が定義する計算課題を実装するサブルーチン群（`model`）と、input/output・実行・判定連携を担う `runner` を運用可能な品質で生成する。

## スコープ
### 対象
- 入力: 自然言語中心の `Controlled Spec`（物理・アルゴリズム定義）と `tests`（verification input・判定プロファイル）
- 出力: 実行コード（`model` + `runner`）と判定 artifact（`diagnostics.json` / `perf.json` / `verdict.json` / `aggregate_verdict.json` / `summary.json`）
- 運用: `Spec -> Plan -> Generate -> Build -> Execute -> Judge -> Tune -> Promote` の反復運用
- ハードウェア: `CPU` / `GPU` を含む（`Phase 0` は `CPU` 参照、以降 `GPU` へ拡張）
- `Controlled Spec` と `tests` の canonical source 形式は `Markdown` とし、自然言語で一意に定義できる項目は文章で記述する。

### 非対象（現時点）
- bitwise 一致の保証
- 完全自動での科学的妥当性の発見（妥当性定義は人間が与える）

## 不変原則
1. ドキュメントを canonical source とする。`Controlled Spec` と `tests` は、ドメイン研究者が単独で解釈でき、判定入力へ決定的に変換できなければならない。
2. 妥当性保証は出口で行う。`LLM` の非再現性を前提に受け入れ、最終品質は execution result に基づく判定で保証する。
3. 物理計算を担う `model` と、input/output・判定連携を担う `runner` を分離する。

## 不正防止原則（全体仕様）
1. `tests` 合格または workflow 進行を目的とした `dummy` 出力の生成を禁止する。
2. phase inputが不足する場合は当該 phase を `fail` で停止し、推測補完やプレースホルダ補完を禁止する。
3. phase 失敗時に下流 phase の開始条件を満たす目的で artifact を人工生成してはならない。
4. 明示的な指定がない場合、過去 workflow 出力（過去 `plan_id` / `pipeline_id` / `generation_id` / `build_id` / `execution_id`）の内容参照を禁止する。
5. 本原則への違反は仕様違反とし、当該 workflow 実行を無効とする。

## 運用原則（Spec-First）
1. 物理仕様の変更は `Controlled Spec` を更新してから実装へ反映する。
2. 実験条件・判定条件の変更は `tests` を更新して反映する。
3. 実装のみを直接修正して物理仕様を変更してはならない。
4. 判定不能または曖昧な仕様は仮実装で進めず、`Spec` へ差し戻して解消する。

## LLM の扱い（全体原則）
- `LLM` はモデル種類を問わず交換可能とする。
- `LLM` 利用ステージは `generate -> verify -> regenerate` を反復し、検証合格後のみ artifact を確定する。
- `verifier` は `generator` と独立したコンテキスト（別セッションまたは別エージェント）での実行を必須とする。
- ステージ内 `verify` は構造・契約・トレーサビリティ整合の確認を目的とし、物理妥当性の最終保証を代替しない。
- `LLM` 利用ステージは `<stage>_meta.json`（コード生成は `generate_meta.json`）を必須出力とする。
- メタデータの必須項目は `attempt_count`、`verification_status`、`last_fail_reason`、`context_isolated`、`debug_mode` とする。`context_isolated=false` の場合は `constraint_reason` を必須とする。
- `debug_mode` の既定値は `false` とする。`debug_mode=true` の場合のみ失敗試行 artifact の保存を許可し、`retained_failed_attempts` と保存先を記録する。

## アーキテクチャ方針
- 物理アルゴリズム（A）は物理結果に影響する選択とし、`case.resolved.yaml` で決定する。
- 実行アルゴリズム（B）は性能・計算過程に影響する選択とし、`impl.resolved.yaml` で表現する。
- `case.resolved.yaml` は実行時 input contract の決定値のみを保持し、output contract を保持しない。
- 判定対象出力の `name` と `shape_expr` と `evidence_ref` は `derived_contract.json` の `io_contract.outputs` で管理する。
- `raw` 一次証跡の必須有無は `derived_contract.json` の `raw_requirements.required_evidence` で管理し、固定の計算様式や固定の証跡構成を一律必須にしてはならない。
- A と B の分離により、物理再現性と性能探索を両立する。

## 大規模 spec 運用設計
### 目的
- 問題別 `spec` を維持しつつ、再利用可能な物理演算を `component` 単位で canonical source 化する。
- 交換可能な単位のみを `spec` として管理し、過分割を防止する。
- 生成対象が増加しても識別子・依存関係・artifact 追跡可能性を維持する。

### 適用範囲
- `spec` 階層構造（`problem` / `component` / `profile`）
- `spec` / `component` / `operation` の命名規則
- `spec` 間依存宣言と registry 整合
- 正式版 artifact（`releases/`）の配置規則

### 要件
1. `spec` は `spec_kind` を必須とし、値は `problem` / `component` / `profile` のみを許可する。
2. `spec` 配置は次の階層構造を必須とする。

```text
spec/
  registry/
    spec_catalog.yaml
  problem/
    <domain>/
      <family>/
        <spec_id>/
          controlled_spec.md
          tests.md
          deps.yaml
  component/
    <domain>/
      <family>/
        <spec_id>/
          controlled_spec.md
          tests.md
          deps.yaml
  profile/
    <domain>/
      <family>/
        <spec_id>/
          controlled_spec.md
          tests.md
          deps.yaml
releases/
  registry/
    component_catalog.yaml
  <spec_kind>/
    <domain>/
      <family>/
        <spec_id>/
          <target_architecture>/
            <toolchain_language>/
              <release_id>/
```

3. `domain` と `family` の定義は `GLOSSARY.md` の「`spec` classification 語彙」に一致させる。
4. `spec_id` はリポジトリ内で一意とし、形式は `^[a-z][a-z0-9_]{2,63}$` を必須とする。
5. `tests.md` は各 `spec` に 1 ファイルのみ配置を許可する。
6. `component_id` は `^[a-z][a-z0-9_]{2,63}$` を必須とし、推奨形式は `<domain>_<family>_<operator>_<dim>d_<scheme>` とする。
7. `operation_id` は `<component_id>__<action>` 形式を必須とする。
8. 生成コードの公開名は互換性管理を必須とし、`major` 互換を破壊する変更は別名へ分離する。
9. 全 `spec` は `deps.yaml` で依存を宣言し、直接パス参照（相対 `import`）を禁止する。
10. `problem spec` は依存 `component` と採用 `profile` を宣言しなければならない。
11. 未登録依存、未実装依存、互換性違反依存を許可しない。
12. `releases/registry/component_catalog.yaml` は `component` 単位の責務、公開 `operation`、互換性情報、実装状態を保持する。
13. 各 `tests.md` は `L0` テストを少なくとも 1 件定義しなければならない。
14. 正式版 artifact は `spec` 配下に配置してはならない。保存先は `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` を必須とする。
15. 粒度判定は次を必須基準とする。
- 差し替え可能性: 独立に差し替える意思決定がある境界のみ `component spec` 化する。
- 契約独立性: input/output contract・前提条件・失敗条件を単独記述できる単位のみ分割する。
- 検証独立性: `tests.md` に独立した合否条件を定義できる単位のみ分割する。
- 内部限定関数: 外部公開しない内部 `helper` 群は独立 `spec` 化を禁止する。

### 設計方針
- `problem spec` は統合シナリオを定義し、複数 `component` の整合性を保証する。
- `component spec` は再利用可能な物理演算契約を定義し、交換可能性と `API` 安定性を保証する。
- `profile spec` は `component` 選択規則とパラメタ拘束を定義し、運用差分を管理する。
- `spec` 横断で共有する演算は `component` として独立管理する。

### 運用ルール
- 新規 `spec` 追加時は `spec/registry/spec_catalog.yaml` への登録を必須とする。
- `spec_catalog.yaml` の必須項目は `spec_kind`、`domain`、`family`、`spec_id`、`spec_version`、`status`、`controlled_spec_path`、`tests_path` とする。
- `problem spec` の再利用境界を変更した場合は `releases/registry/component_catalog.yaml` を同時更新する。
- `component` の実装状態が `spec_defined_not_implemented` の間は、依存先 `problem spec` を `status=draft` とする。
- `workspace/` は試行 artifact の作業領域とし、正式版 artifact の canonical source に使用してはならない。
- 昇格時は `spec_catalog.yaml` の対象 `spec_id` に `official_releases` を追加し、`release_id`、`target_architecture`、`toolchain_language`、`target_backend`、`source_pipeline_id`、`source_generation_id`、`source_build_id`、`source_execution_id`、`artifact_root`、`promoted_at`、`status` を記録する。
- `official_releases` の `status=active` は各 `spec_id` の `target_architecture + toolchain_language` ごとに 1 件のみ許可する。
- 互換移行期間では旧配置 `spec/<domain>/<family>/<spec_id>/...` を `problem spec` とみなし、`spec_kind=problem` で registry 登録する。
- 互換移行期間中も新規 `spec` は `spec/<spec_kind>/<domain>/<family>/<spec_id>/...` を必須とする。

### 判定基準
- `CI` は `spec_kind` / `spec_id` / `component_id` / `operation_id` の形式検証を `pass` しなければならない。
- `CI` は各 `spec` の `tests.md` 存在検証と `L0` テスト存在検証を `pass` しなければならない。
- `CI` は `tests.md` の `spec_ref` と `controlled_spec.md` の整合性検証を `pass` しなければならない。
- `CI` は `problem spec` の依存宣言について、未登録依存・未実装依存・互換性違反依存を `fail` としなければならない。
- `CI` は `spec_catalog.yaml` と `component_catalog.yaml` の必須項目欠落を `fail` としなければならない。
- `CI` は `official_releases` の `artifact_root` が `releases/` 配下を指さない場合に `fail` としなければならない。

## 成功条件（最小）
- `Controlled Spec` から各 `spec` の計算課題を実装する `model` と `runner` の変換が再現可能である。
- `problem` / `component` / `profile` の責務境界が維持され、依存宣言が registry と整合している。
- 物理妥当性判定と性能評価に必要な artifact 契約が workflow で再現可能である。
- `impl`（B）の探索で物理合格を維持しつつ性能改善を評価できる。

## 参照
- `CONTROLLED_SPEC.md`
- `TESTS.md`
- `WORKFLOW.md`（入口） / `workflow/WORKFLOW_CORE.md` / `workflow/phases/`
- `IMPL_PLAN_SPEC.md`
- `PHYSICAL_VALIDATION.md`
- `GLOSSARY.md`
