# Phase 0: Spec（人手）

## 概要
`Controlled Spec` と `tests` と `deps` を人手で記述し、core workflow の起点を成立させる phase。LLM 利用 phase ではなく、orchestration の対象外。

## I/O 契約
- execution input: workflow 外部で与える要求事項、物理要件、依存選択方針
- verification input: なし
- 出力:
  - `spec/<spec_kind>/<domain>/<family>/<spec_id>/controlled_spec.md`
  - `spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md`
  - `spec/<spec_kind>/<domain>/<family>/<spec_id>/deps.yaml`

## 必須要件
- `Controlled Spec` で物理アルゴリズム（A）の意図を定義する。
- `problem spec` は依存 `component` と採用 `profile` を `deps.yaml` で宣言する。
- `tests.md` は実験条件と判定条件、および `test_id` ごとの要求証跡を定義する。
- 自然言語表記を canonical source とし、`Compile` phase が構造化 IR (`spec.ir.yaml`) へ統合する。
- `controlled_spec.md` の `spec_version` を必須記録とする。spec を更新する場合は `spec_version` を更新する。

## 後段との接続
- `Compile` phase は `controlled_spec.md` + `tests.md` + `deps.yaml` + `spec/registry/spec_catalog.yaml` を入力に `spec.ir.yaml` を生成する。
- `Generate` 以降の段は `spec.ir.yaml` を canonical source とし、`controlled_spec.md` を直接読まない。
- 仕様変更は `controlled_spec.md` / `tests.md` / `deps.yaml` のいずれかの更新で表現し、実装側の修正のみで仕様を変えてはならない（Spec-First 原則）。
