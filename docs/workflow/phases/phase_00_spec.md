# Phase 0: 仕様作成（人間）

### 0. 仕様作成（人間）
- execution input: workflow 外部で与える要求事項、物理要件、依存選択方針
- verification input: なし
- 出力: `spec/<spec_kind>/<domain>/<family>/<spec_id>/controlled_spec.md`、`spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md`、`spec/<spec_kind>/<domain>/<family>/<spec_id>/deps.yaml`
- `Controlled Spec` で物理アルゴリズム（A）を定義する。
- `problem spec` は依存 `component` と採用 `profile` を定義する。
- `tests` は実験条件と判定条件を定義する。
- 検証契約は `Plan` が `controlled_spec.md` と `tests.md` と `deps.yaml` から導出する。

