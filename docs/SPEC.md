# 全体仕様: ドキュメント駆動で気象モデルを生成する基盤

## 最終ゴール
**ドキュメント的文章（Controlled Spec + physical_tests）を正本**として、CPU/GPU などのハードウェア上で**準最適化された気象モデル**を生成できる基盤フレームワークと、実際に生成されたモデル群を実用水準で運用する。

## スコープ
### 対象
- 入力: Controlled Spec（物理・アルゴリズム定義）と physical_tests（妥当性検証用の入力・判定プロファイル）
- 出力: 実行コード（model + runner）、物理診断、性能診断、合否判定
- 運用: Spec→Plan→Generate→Execute→Judge→Tune のループ
- ハードウェア: CPU / GPU を含む（Phase 0 は CPU 参照、以降 GPU へ拡張）
- 実行時入力はユーザーが科学目的に応じて設計でき、`physical_tests` はそのうち妥当性検証に使う既定プロファイルを与える。
- 言語に依らず、物理計算を担う model と、入出力・判定連携を担う runner を分離する。

### 非対象（現時点）
- bitwise 一致の保証
- 完全自動での科学的妥当性の発見（妥当性の定義は人間が与える）
- すべての気象・気候モデルの網羅

## 運用原則（Spec-First）
1. 物理仕様の変更は**必ず Controlled Spec を更新**してから実装へ反映する。
2. 実験条件・判定条件の変更は**physical_tests を更新**してテストへ反映する。
3. 実装のみを直接修正して物理仕様を変えることを禁止する（乖離防止）。
4. 判定不能・曖昧な仕様は「仮実装で進める」のではなく、Spec へ差し戻して解消する。

## LLM の扱い
- LLM はモデル種類を問わない（交換可能）。
- 品質はテストで担保する。

## アーキテクチャ方針（現時点）
- **物理アルゴリズム（A）**: 物理結果に影響する選択。`case.resolved.yaml` で決定し、決定的である必要がある。
- **実行アルゴリズム（B）**: 計算過程や性能に影響する選択。`impl.resolved.yaml` で表現し、探索可能とする。
- この分離により「物理再現性」と「性能探索」を両立する。

## 大規模 `spec` 運用設計
### 目的
- 複数の `spec` を並行運用し、`spec` 間で再利用する `component` / `operation` を決定的に解決する。
- 生成対象が増加しても、`spec` の識別子・依存関係・生成物の追跡可能性を維持する。

### 適用範囲
- `spec` の保管構造
- `spec`・`component`・`operation` の命名規則
- `spec` 間依存と再利用規則
- 既存 `component` / `operation` の登録台帳

### 要件
1. `spec` 配置は階層構造を必須とする。最小構成を次に固定する。

```text
spec/
  registry/
    spec_catalog.yaml
    component_catalog.yaml
  <domain>/
    <component>/
      <spec_id>/
        controlled_spec.md
        physical_tests/
          <suite_id>.yaml
        deps.yaml
```

2. すべての `spec` は階層構造へ配置する。
   - `domain` と `component` の定義は `GLOSSARY.md` の「`spec` 分類語彙」を参照する。
3. `spec_id` はリポジトリ内で一意とし、形式は `^[a-z][a-z0-9_]{2,63}$` を必須とする。
4. `suite_id` は `spec_id` を接頭辞に持つ形式（`<spec_id>_<purpose>`）を必須とする。
5. `component_id` は `^[a-z][a-z0-9_]{2,63}$` を必須とし、推奨形式を `<domain>_<component>_<operator>_<dim>d_<scheme>` とする。
6. `operation_id` は `component_id` を接頭辞に持つ形式（`<component_id>__<action>`）を必須とする。
7. 生成コードの公開名は互換性管理を必須とする。`major` 互換が破壊される変更は別名に分離する。
8. 各 `spec` は `deps.yaml` で依存 `component` を宣言し、直接パス参照（相対 import）を禁止する。
9. 依存解決は `component_id` + `version constraint` で行い、解決結果を `case.resolved.yaml` に固定する。
10. `registry/component_catalog.yaml` は `component` 単位の責務・公開 `operation`・互換性情報・実装状態を保持する。
11. 未実装 `component` / `operation` を参照する `spec` は生成工程へ進めず、依存解決エラーとする。

### 設計方針
- `spec` は「物理仕様の定義」に限定し、再利用資産の正本は `registry/component_catalog.yaml` に集約する。
- `component` の責務は物理演算単位で分割する。`runner` は台帳上の公開 `operation` のみ呼び出す。
- `spec` 横断で共有する演算（境界処理、フラックス、時間積分、診断）は `component` として独立管理する。
- `component` の API 変更は `major` を更新し、旧 `major` を同時運用可能にする。

### 運用ルール
- 新規 `spec` 追加時は `registry/spec_catalog.yaml` への登録を必須とする。
- `spec` 更新で再利用境界が変わる場合は `registry/component_catalog.yaml` を同時更新する。
- `deps.yaml` の依存解決結果は `case.resolved.yaml` に記録し、実行時の再解決を禁止する。
- `component` の実装状態が `spec_defined_not_implemented` の間は、依存先 `spec` を `status=draft` 扱いに固定する。

### 判定基準
- `spec` の CI は `spec_id` / `component_id` / `operation_id` の形式検証を pass しなければならない。
- `spec` の CI は未登録依存、未実装依存、互換性違反依存を fail としなければならない。
- 生成実行ログ（`trial_meta.json`）には `spec_id`、`spec_version`、`component_id@api_version` の解決結果を必須記録とする。
- `component_catalog.yaml` の公開 `operation` と生成コードの公開シンボルは 1 対 1 で対応しなければならない。

## 成功条件（最小）
- Controlled Spec → 実行モデルの変換が再現可能である。
- runner が model を呼び出す構成が維持され、物理更新ロジックが二重実装されない。
- 物理妥当性判定により合否が決定的に再現される。
- impl（B）の探索で、物理合格を維持しつつ性能が改善できる。

## 参照
- Controlled Spec の書き方: `CONTROLLED_SPEC.md`
- 物理妥当性判定: `PHYSICAL_VALIDATION.md`
- 用語・Artifacts: `GLOSSARY.md`
- 全体フロー: `WORKFLOW.md`
