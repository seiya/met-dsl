# TODO
この文書は、リポジトリ全体で管理する未完了タスクを集約する正本である。

## TODO 一覧

### スキル定義
- 対象: ワークフロー各ステージ（`Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote`）
- 要件: 各ステージごとに対応する `SKILL` を追加する。
- 完了条件: ステージ内に `generate -> verify -> regenerate` ループを持つ場合、`generate` 用と `verify` 用を別 `SKILL` として定義する。
