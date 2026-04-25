# TODO
この文書は、リポジトリ全体で管理する未完了タスクを集約する canonical source である。

## TODO 一覧

- Claude backend が hook payload から `session_id` または `agent_session_id` を取得可能になった時点で、`active_child_agent_run_id.txt` に依存した `agent_run_id` 解決を廃止し、Codex backend と同一の session 識別子ベース解決へ統一する。
  - 削除対象: `tools/codex_orchestration_runtime.py` の active file 管理ヘルパーと Claude 専用分岐、`tools/hooks/cli.py` の active file 参照分岐、active file 前提の関連テスト。
  - 完了判定: Claude/Codex の双方で hook payload の session 識別子だけで `agent_run_id` を一意解決でき、active file が生成されないことをテストで検証済みである。
