# 全体ワークフロー: Spec→Plan→Generate→Execute→Judge→Tune

この文書はプロジェクト全体の流れを単独で把握できるようにまとめる。
用語は `docs/GLOSSARY.md` を参照。

## 0. 仕様作成（人間）
- Controlled Spec（文章＋構造化ブロック）で物理アルゴリズム（A）を決定する。
- 実行アルゴリズム（B）はここでは固定しない（将来探索する）。

成果物:
- `spec/*.md`

## 1. Plan生成（決定的）
### 1-1) 物理Plan（case.resolved.yaml）
- Spec/caseから物理アルゴリズム（A）と入力条件を決定し、sweep/refinementも展開する。
- この層は「物理結果を保証するために決定的」である必要がある。

### 1-2) 実装Plan（impl.resolved.yaml）
- targetや環境に応じて、実行アルゴリズム（B）を決める。
- Phase 1では固定値でもよい。
- Phase 2以降は tuner が複数候補を生成し探索する。

## 2. 生成（LLM）
- LLMは交換可能。
- 入力: case.resolved（A固定）と impl.resolved（Bの指定、または候補集合）
- 出力: 実装コード（simulate）と付随ドキュメント
- 生成はテンプレ補完・小粒度パッチを基本とする。

## 3. 実行（simulate）
- 入力: case.resolved.yaml と（任意で）impl.resolved.yaml
- 出力: diagnostics.json と perf.json
 - perf.json仕様は `docs/PERFORMANCE_DIAGNOSTICS.md`

## 4. 判定（runner）
- 物理判定: diagnostics を用いて checks/thresholds を評価
- 性能判定（任意）: perf を用いて performance regression を評価
- 物理fail時は性能評価をスキップする。

## 5. チューニング（tuner: Phase 2+）
- 同一 case.resolved に対し複数 impl.resolved を生成し、物理合格を満たす範囲で性能目的関数を最大化する。
- 詳細は `docs/TUNING_MODEL.md`


補足:
- implの仕様: `docs/IMPL_PLAN_SPEC.md`
- AIチューニング運用: `docs/AI_TUNING_WORKFLOW.md`
