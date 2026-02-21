# TODO
この文書は、リポジトリ全体で管理する未完了タスクを集約する正本である。

## TODO 一覧

### 実行基盤
- 状態: `open`
- 対象: 実行に使用する `MCP` サーバー
- 要件: `OpenMP` を有効化したスレッド並列実行をサポートする。
- 完了条件: `run_program` で `threads_per_rank` を指定した場合、`target.class=cpu` の実行に `OpenMP` スレッド数として反映される。

### スキル定義
- 状態: `open`
- 対象: ワークフロー各ステージ（`Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote`）
- 要件: 各ステージごとに対応する `SKILL` を追加する。
- 完了条件: ステージ内に `generate -> verify -> regenerate` ループを持つ場合、`generate` 用と `verify` 用を別 `SKILL` として定義する。
