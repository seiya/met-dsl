# Workflow Orchestration

この文書は、`workflow` 全体を統括する `orchestration agent` と、工程単位・サブ工程単位の独立エージェント実行規約を定義する。

## 目的
- `workflow` 実行を階層化し、工程責務と監査責務を分離する。
- 各 `step` / 各 `substep` を独立エージェントとして実行し、実行経路を追跡可能にする。

## 適用範囲
- `Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote`
- `node workflow` 単位の工程実行と、工程内 `substep`（例: `generate` / `verify`）の実行

## 要件
- `workflow` 実行は、必ず 1 つの `orchestration agent` を最初に起動して開始する。
- `workflow` 開始前に、`step agent` と `substep agent` を独立起動できる実行基盤の事前検査を必須実行しなければならない。事前検査が `pass` でない場合は `workflow` を開始してはならない。
- `orchestration agent` は `workflow` 全体の進行制御のみを担当し、工程本体の成果物（例: `case.resolved.yaml`、`diagnostics.json`）を直接生成してはならない。
- `workflow` 実行の代替として、ステージ進行と成果物生成を一括自動化する `script`（例: `python` / `bash`）を新規生成または実行してはならない。
- `orchestration` の責務を `script` へ委譲してはならない。`Plan` / `Generate` / `Build` / `Execute` / `Judge` の各 `step` は必ず独立 `step agent` で実行しなければならない。
- 標準 `substep` を持たない各 `step` は、`step agent` を独立起動して実行しなければならない。
- `substep` を持つ各工程は、`orchestration agent` が各 `substep` の `substep agent` を独立起動して実行しなければならない。
- `step agent` と `substep agent` は、同一 `LLM` コンテキストを共有してはならない。各 `agent_run_id` は固有の `context_id` を持ち、`context_isolated=true` を必須記録とする。
- `orchestration agent` は `substep` を持つ工程で必要な `substep` 群を起動し、完了判定を行った後に `step_result.json` を確定しなければならない。
- `orchestration agent` は `dependency.resolved.yaml` の `topo_level` 昇順と依存充足条件に基づいて `step agent` または `substep agent` の起動可否を判定しなければならない。
- すべての `agent` 実行は `agent_run_id` を持ち、入力参照・出力参照・親子関係を記録しなければならない。
- `agent_runs.jsonl` の各行は `started_at` と `status` を必須記録とし、`status` が終端状態（`pass` / `fail` / `blocked` / `timeout` / `cancel`）の場合は `finished_at` を必須記録とする。
- `step` / `substep` ロールの `agent_runs.jsonl` は `parent_agent_run_id` と `agent_backend` と `agent_model` と `context_id` と `context_isolated` と `agent_session_id` と `launch_request_ref` と `launch_response_ref` を必須記録とする。
- `launch_request_ref` と `launch_response_ref` は `workspace/orchestrations/<orchestration_id>/launches/` 配下を参照し、参照先実体が存在しなければならない。
- `agent_graph.json` の `edge` は、`orchestration -> step` または `orchestration -> substep` を正本とする。互換運用として `step -> substep` を許容してもよいが、`substep` を親ロールとする `edge` を禁止する。
- `agent` 実行の失敗、`timeout`、`cancel` はメタデータへ記録し、推測補完で継続してはならない。
- `agent_runs.jsonl` と `agent_graph.json` は、実行中イベントを逐次追記して生成しなければならない。workflow 完了後に固定値テンプレートを一括出力する運用を禁止する。
- `agent_runs.jsonl` と `agent_graph.json` と `step_result.json` を後生成または手動整形して独立実行を偽装してはならない。起動時に記録した一次証跡との突合で整合しない試行は `fail` とする。
- `orchestration agent` は、子 `agent` 起動時に `docs/WORKFLOW.md` を正本として対象 `step` または `substep` の `実行入力` と `検証入力` と `期待出力` を明示しなければならない。`step agent` を使用する工程では `step agent` も自身の契約入力と期待出力を明示しなければならない。

## 設計方針
- 単一責務: 1 つの `agent` は 1 つの責務のみを持つ。
- 階層委譲: `orchestration agent -> step agent` と `orchestration agent -> substep agent` の 2 系統で制御する。
- 契約駆動: 子 `agent` 起動時は入力契約と出力契約を固定し、契約外の読み書きを禁止する。
- 追跡可能性: すべての起動・終了イベントを時系列で保存し、再実行時に同一判断を再現可能にする。

## オーケストレーション指示契約
### 共通必須項目
- `orchestration agent` は、子 `agent` への起動要求に `orchestration_id` と `agent_run_id` と `parent_agent_run_id` と `node_key` と `step` と `substep`（存在する場合）と `plan_ref` と `pipeline_ref` と `dependency_ref` を必須記録しなければならない。
- 子 `agent` への起動要求には、`実行入力` と `検証入力` と `期待出力` と `write_root` と `read_roots` を必須記録しなければならない。
- `実行入力` は当該 `agent` が成果物を生成するために直接参照してよい入力に限定しなければならない。
- `検証入力` は当該 `agent` が pass/fail 判定、整合確認、依存確認にのみ使用してよい入力として明示しなければならない。
- `期待出力` はファイル名、保存先、更新責務を含めて明示しなければならない。親 `agent` は `期待出力` に含まれない成果物を子 `agent` へ要求してはならない。
- 親 `agent` は入力不足時に推測補完を指示してはならない。不足入力がある場合は `fail-fast` 停止を指示しなければならない。
- `step` ごとの具体的な `実行入力` と `検証入力` と `期待出力` は `docs/WORKFLOW.md` を正本とし、親 `agent` は対象 `step` 節の定義を参照して起動要求へ展開しなければならない。
- `substep` ごとの具体的な `実行入力` と `検証入力` と `期待出力` は、対応 `SKILL.md` と `docs/WORKFLOW.md` の両方を参照して決定しなければならない。`WORKFLOW.md` に明示された工程契約と矛盾する `substep` 契約を定義してはならない。
- `Build` / `Execute` / `Judge` / `Promote` のように現行標準で `substep` を定義しない `step` では、`orchestration agent` は `step` 契約をそのまま単一 `step agent` へ渡さなければならない。
- `Plan generate/verify`、`Generate generate/verify`、`Tune generate/verify` のように `substep` を持つ `step` では、`orchestration agent` は `step` 契約を分解したうえで、対応 `SKILL.md` の責務境界に一致する `substep` 契約だけを直接渡さなければならない。

## 運用ルール
1. `workflow` 開始時に `orchestration_id` を発行し、`workspace/orchestrations/<orchestration_id>/orchestration_meta.json` を作成する。
2. `workflow` 開始前に事前検査結果を `workspace/orchestrations/<orchestration_id>/preflight.json` へ記録し、`can_launch_step_agents=true` と `can_launch_substep_agents=true` を満たさない場合は `fail` として停止する。
3. `orchestration agent` は `step agent` または `substep agent` の起動要求ごとに `launches/<agent_run_id>.request.json` と `launches/<agent_run_id>.response.json` を保存し、`agent_runs.jsonl` の `launch_request_ref` と `launch_response_ref` へ参照を記録する。
4. `orchestration agent` は `dependency.resolved.yaml` を読み、`node_key` と `topo_level` に基づく実行キューを確定する。
5. `orchestration agent` は起動対象ごとに `step agent` または `substep agent` を発行し、`node_key`、`step`、`plan_ref`、`pipeline_ref`、`dependency_ref` を入力として渡す。
6. `orchestration agent` は `step` を持つ工程では対象 `step` の `実行入力` と `検証入力` と `期待出力` を明示し、`substep` を持つ工程では対象 `substep` の `実行入力` と `検証入力` と `期待出力` を明示しなければならない。
7. `substep` を持つ工程では、`orchestration agent` が `generate` と `verify` などの `substep agent` を逐次起動する。
8. `substep agent` は自身の成果物と `<stage>_meta.json` を生成し、`agent_output_ref` を `orchestration agent` へ返却する。
9. `orchestration agent` は `substep` を持つ工程で全 `substep` の必須成果物を検証し、`workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/step_result.json` へ `step_result.json` を出力する。この場合の `agent_run_id` は `orchestration agent_run_id` とする。
10. `step agent` は標準 `substep` を持たない工程で自身の成果物を検証し、`workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/step_result.json` へ `step_result.json` を出力する。
11. `orchestration agent` は `step_result.json` を受け取り、次 `step` の起動可否を判定する。
12. `node` 実行は `dependency.resolved.yaml` の `topo_level` 昇順で逐次実行する。依存関係を持つ `node` は依存 `node` の完了前に起動してはならない。同一 `topo_level` の独立 `node` も並列実行してはならない。
13. `step agent` または `substep agent` が `fail` / `timeout` / `cancel` の場合、当該 `node` の当該 `step` を `fail` とし、下流 `step` 起動を禁止する。
14. `orchestration agent` は各 `agent` 実行イベントを `workspace/orchestrations/<orchestration_id>/agent_runs.jsonl` へ追記しなければならない。
15. `orchestration agent` は親子関係を `workspace/orchestrations/<orchestration_id>/agent_graph.json` へ保存し、`parent_agent_run_id` と `child_agent_run_id` と `relation_type` を必須記録とする。
16. `Promote` 以外の `agent` は `workspace/` 配下以外へ書き込んではならない。
17. `workflow` 実行時に `step` / `substep` の実処理を `script` で代行した場合は `fail` とし、当該試行を破棄しなければならない。

## 判定基準
- `workflow` ごとに `orchestration_id` が発行され、`orchestration_meta.json` が存在する。
- 各 `step` または各 `substep` が独立 `agent_run_id` を持つ。
- `step` と `substep` の `context_id` が重複せず、全件で `context_isolated=true` が記録される。
- `step` と `substep` の `agent_runs.jsonl` に `agent_session_id` と `launch_request_ref` と `launch_response_ref` が記録され、参照先実体が存在する。
- `preflight.json` が存在し、`can_launch_step_agents=true` と `can_launch_substep_agents=true` を満たす。
- `agent_graph.json` で `orchestration -> step` または `orchestration -> substep` の親子関係を追跡できる。
- `agent_runs.jsonl` から `queued` / `running` / `pass` / `fail` / `blocked` / `timeout` / `cancel` の遷移を追跡できる。
- `step_result.json` の `executor_agent_run_id` が当該ディレクトリ名と一致し、`substep_agent_run_ids` が親子関係と整合する。標準 `substep` を持たない工程では `substep_agent_run_ids=[]` を許可する。
- `step_result.json` の `required_outputs` が `WORKFLOW.md` の工程契約と一致する。
- 失敗試行で推測補完や人工成果物生成を行わず、当該 `step` を停止している。
- 各 `step_result.json` の `executor_agent_run_id` が `orchestration` または `step` ロールの実行記録と対応し、`script` 実行ログのみで工程完了を主張していない。
