# 全体仕様: ドキュメント駆動で気象・気候計算向けサブルーチン群と runner を生成する基盤

## 最終ゴール
**ドキュメント的文章（`Controlled Spec` + `tests`）を正本**として、`CPU` / `GPU` などのハードウェア上で **各 `spec` が定義する計算課題を実装するサブルーチン群（`model`）と、入出力・実行・判定連携を担う `runner`** を最適化して生成できる基盤フレームワークを構築し、生成成果物を実用水準で運用する。

## スコープ
### 対象
- 入力: 自然言語中心の `Controlled Spec`（物理・アルゴリズム定義）と `tests`（検証入力・判定プロファイル）
- 出力: 実行コード（`model` + `runner`）、物理診断、性能診断、合否判定（`verdict.json` / `aggregate_verdict.json` / `summary.json`）、依存解決情報（`dependency.resolved.yaml`）
- 運用: `Spec -> Plan -> Generate -> Execute -> Judge -> Tune -> Promote` のループ
- ハードウェア: `CPU` / `GPU` を含む（`Phase 0` は `CPU` 参照、以降 `GPU` へ拡張）
- 実行時入力はユーザーが科学目的に応じて設計でき、`tests` はそのうち検証に使う既定プロファイルを与える。
- `Controlled Spec` と `tests` の正本形式は `Markdown` とし、自然言語で一意に定義できる項目は文章で記述する。
- 出力プログラミング言語に依らず、物理計算を担う `model` と、入出力・判定連携を担う `runner` を分離する。

### 非対象（現時点）
- bitwise 一致の保証
- 完全自動での科学的妥当性の発見（妥当性の定義は人間が与える）

## 不変原則
1. ドキュメントを正本とする。`Controlled Spec` と `tests` は、ドメイン研究者が単独で解釈でき、かつ判定入力へ決定的に変換できなければならない。
2. 妥当性保証は出口で行う。`LLM` の自由度・非再現性・ハルシネーションを前提として受け入れ、実行結果の妥当性は物理妥当性判定で保証する。bitwise 一致は要求しない。

## 運用原則（Spec-First）
1. 物理仕様の変更は**必ず `Controlled Spec` を更新**してから実装へ反映する。
2. 実験条件・判定条件の変更は**`tests` を更新**してテストへ反映する。
3. 実装のみを直接修正して物理仕様を変えることを禁止する（乖離防止）。
4. 判定不能・曖昧な仕様は「仮実装で進める」のではなく、`Spec` へ差し戻して解消する。

## LLM の扱い
- `LLM` はモデル種類を問わない（交換可能）。
- 最終的な品質保証は `Execute` 後の実行診断で行う。判定の正本は `diagnostics.json` / `verdict.json` / `perf.json` とする。
- `LLM` を使う各ステージは、ステージ内部で `generate -> verify -> regenerate` を反復し、検証合格後のみ成果物を確定する。
- ステージ内部の `verify` は、当該ステージの入力契約に対する出力整合性を検査する狭いスコープの判定とする。
- ステージ内部の `verify` は主に構造・契約・トレーサビリティの整合確認を目的とし、物理妥当性の最終保証を代替しない。
- ステージ内部の `verify` 合格は必要条件であり、十分条件ではない。
- `verifier` は `generator` と独立したコンテキスト（別セッションまたは別エージェント）での実行を可能な限り優先する。
- 実行環境の制約で独立コンテキストを確保できない場合は、同一コンテキスト実行を許容し、制約理由をメタデータへ記録する。
- 失敗試行の中間成果物は標準運用で永続保存せず、最終合格成果物のみを保存する。
- `LLM` 利用ステージは、ステージメタデータの出力を必須とする。正本は各ステージの `<stage>_meta.json` とし、コード生成ステージでは `generate_meta.json` とする。
- `LLM` 利用ステージのメタデータ必須項目は、`attempt_count`、`verification_status`、`last_fail_reason`、`context_isolated`、`debug_mode` とする。`context_isolated=false` の場合は `constraint_reason` を必須とする。
- `debug_mode` の既定値は `false` とする。`debug_mode=false` では失敗試行の中間成果物を保存しない。
- `debug_mode=true` の場合のみ失敗試行の成果物保存を許可する。保存時は各ステージのメタデータに `retained_failed_attempts`（保存件数）と保存先を記録する。

## アーキテクチャ方針（現時点）
- **物理アルゴリズム（A）**: 物理結果に影響する選択。`case.resolved.yaml` で決定し、決定的である必要がある。
- **実行アルゴリズム（B）**: 計算過程や性能に影響する選択。`impl.resolved.yaml` で表現し、探索可能とする。
- この分離により「物理再現性」と「性能探索」を両立する。

## 大規模 `spec` 運用設計
### 目的
- 問題別 `spec` を維持しつつ、再利用可能な物理演算を `component` 単位で正本化する。
- 粒度の過分割を防ぎ、交換可能な単位だけを `spec` として管理する。
- 生成対象が増加しても、`spec` の識別子・依存関係・生成物の追跡可能性を維持する。

### 適用範囲
- `spec` の階層構造（`problem` / `component` / `profile`）
- `spec`・`component`・`operation` の命名規則
- `spec` 間依存と再利用規則
- 既存 `component` / `operation` の登録台帳

### 要件
1. `spec` は `spec_kind` を必須とし、値は `problem` / `component` / `profile` のいずれかのみを許可する。
2. `spec` 配置は階層構造を必須とする。最小構成を次に固定する。

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

3. `domain` と `family` の定義は `GLOSSARY.md` の「`spec` 分類語彙」を参照する。
4. 正式版成果物は `spec` 配下に配置してはならない。保存先は `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` を必須とする。
5. `spec_id` はリポジトリ内で一意とし、形式は `^[a-z][a-z0-9_]{2,63}$` を必須とする。
6. `tests.md` は各 `spec` に 1 ファイルのみ配置を許可する。配置先は `spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md` とする。
7. `component_id` は `^[a-z][a-z0-9_]{2,63}$` を必須とし、推奨形式を `<domain>_<family>_<operator>_<dim>d_<scheme>` とする。
8. `operation_id` は `component_id` を接頭辞に持つ形式（`<component_id>__<action>`）を必須とする。
9. 生成コードの公開名は互換性管理を必須とする。`major` 互換が破壊される変更は別名に分離する。
10. すべての `spec` は `deps.yaml` で依存を宣言し、直接パス参照（相対 `import`）を禁止する。
11. `problem spec` は依存 `component` と採用 `profile` を宣言しなければならない。解決結果は `case.resolved.yaml` に固定し、実行時の再解決を禁止する。
12. 依存解決は `component_id` + `version constraint` と `profile_id` + `version constraint` で行う。未登録依存、未実装依存、互換性違反依存をエラーとする。
13. `releases/registry/component_catalog.yaml` は `component` 単位の責務・公開 `operation`・互換性情報・実装状態を保持する。
14. 未実装 `component` / `operation` を参照する `problem spec` は生成工程へ進めず、依存解決エラーとする。
15. 各 `tests.md` は `L0` テストを少なくとも 1 件定義しなければならない。`L1` 以上の要否は `spec_kind` ではなく検証目的で決定する。
16. 粒度判定は次を必須基準とする。
- 差し替え可能性: 独立に差し替える意思決定がある境界のみ `component spec` 化する。
- 契約独立性: 入出力契約・前提条件・失敗条件を単独記述できる単位のみ分割する。
- 検証独立性: `tests.md` に独立した合否条件を定義できる単位のみ分割する。
- 内部限定関数: 外部公開しない内部 helper 群は独立 `spec` 化を禁止する。
17. `Plan` は `dependency.resolved.yaml` を必須出力とし、`node_key`、`direct_deps`、`transitive_deps`、`topo_level` を必須記録する。
18. 実行順序は `dependency.resolved.yaml` の `topo_level` 昇順に固定する。親 `node` は直下依存 `node` がすべて `pass` または `xfail` になるまで `Judge` を開始してはならない。
19. 判定成果物は `verdict.json`（当該 `node` の `self_verdict`）と `aggregate_verdict.json`（依存を含む集約判定）を必須とする。
20. `summary.json` は `self_summary` と `dependency_summary` を必須保持し、`dependency_summary` には `total`、`pass`、`xfail`、`fail`、`blocked` を必須記録する。
21. `problem` の `Promote` は `self_verdict=pass` に加え、推移依存を含む `aggregate_verdict.overall=pass` を必須条件とする。

### 設計方針
- `problem spec` は統合シナリオを定義する。目的は、複数 `component` を組み合わせた整合性と回帰判定である。
- `component spec` は再利用可能な物理演算の契約を定義する。目的は、交換可能性と `API` 安定性の担保である。
- `profile spec` は `component` 選択規則とパラメタ拘束を定義する。目的は、スキーム選択の差分管理である。
- `spec` 横断で共有する演算（境界処理、フラックス、時間積分、診断）は `component` として独立管理する。
- `component` の `API` 変更は `major` を更新し、旧 `major` を同時運用可能にする。

### 運用ルール
- 新規 `spec` 追加時は `spec/registry/spec_catalog.yaml` への登録を必須とする。
- `spec_catalog.yaml` の各エントリは `spec_kind`、`domain`、`family`、`spec_id`、`spec_version`、`status`、`controlled_spec_path`、`tests_path` を必須とする。
- `problem spec` で再利用境界が変わる場合は `releases/registry/component_catalog.yaml` を同時更新する。
- `component` の実装状態が `spec_defined_not_implemented` の間は、依存先 `problem spec` を `status=draft` 扱いに固定する。
- `workspace` は試行成果物の作業領域とし、正式版成果物の正本に使ってはならない。
- `dependency.resolved.yaml` は `plan_id` の正本に含め、`case.resolved.yaml` / `impl.resolved.yaml` と同時に `immutable` 管理する。
- ユーザーからプログラミング言語の明示指定がない場合、`target.class=cpu` では `fortran`、`target.class=gpu` では `cuda_fortran` を必ず採用し、`impl.resolved.yaml` へ補完後の値を明示記録する。
- `toolchain.language` の既定値からの逸脱は、ユーザーがプログラミング言語を明示指定した場合にのみ許可する。
- 採用する実装は `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` へ昇格保存する。
- 昇格時は `spec_catalog.yaml` の対象 `spec_id` に `official_releases` を追加し、`release_id` / `target_architecture` / `toolchain_language` / `target_backend` / `source_pipeline_id` / `source_generation_id` / `source_build_id` / `source_execution_id` / `artifact_root` / `promoted_at` / `status` を必須記録する。
- `official_releases` の `status=active` は各 `spec_id` の `target_architecture + toolchain_language` ごとに 1 件のみ許可し、切替は新規 `release` 追加と旧 `release` の `deprecated` 化で管理する。
- 互換移行期間では、旧配置 `spec/<domain>/<family>/<spec_id>/...` を `problem spec` とみなし、`spec_kind=problem` として台帳に登録する。
- 互換移行期間中も、新規追加 `spec` は新配置 `spec/<spec_kind>/<domain>/<family>/<spec_id>/...` を必須とする。

### 判定基準
- `spec` の `CI` は `spec_kind` / `spec_id` / `component_id` / `operation_id` の形式検証を `pass` しなければならない。
- `CI` は各 `spec` の `tests.md` 存在検証と `L0` テスト存在検証を `pass` しなければならない。
- `CI` は `tests.md` の `spec_ref` と `controlled_spec.md` の整合性検証を `pass` しなければならない。
- `problem spec` の `CI` は `component` 依存と `profile` 依存の未登録・未実装・互換性違反を `fail` としなければならない。
- `CI` は `dependency.resolved.yaml` の `DAG` が非循環であることを `pass` しなければならない。
- `CI` は `aggregate_verdict.json` の依存集合と `dependency.resolved.yaml` の推移依存集合が一致することを `pass` しなければならない。
- 生成実行ログ（`trial_meta.json`）には `spec_kind`、`spec_id`、`spec_version`、`component_id@api_version`、`profile_id@version` の解決結果を必須記録とする。
- `component_catalog.yaml` の公開 `operation` と生成コードの公開シンボルは 1 対 1 で対応しなければならない。

## 成功条件（最小）
- `Controlled Spec` から、各 `spec` が定義する計算課題を実装するサブルーチン群（`model`）と `runner` の変換が再現可能である。
- `runner` が `model` を呼び出す構成が維持され、物理更新ロジックが二重実装されない。
- 物理妥当性判定により合否が決定的に再現される。
- 依存を含む集約判定（`aggregate_verdict`）と依存集約サマリー（`dependency_summary`）が決定的に再現される。
- `impl`（B）の探索で、物理合格を維持しつつ性能が改善できる。

## 参照
- `CONTROLLED_SPEC.md`
- `TESTS.md`
- `PHYSICAL_VALIDATION.md`
- `GLOSSARY.md`
- `WORKFLOW.md`
