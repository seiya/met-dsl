# Workflow Orchestration

この文書は、`workflow` 全体を統括する `orchestration agent` と、phase unit / substep unit の独立エージェント実行規約を定義する。

## 目的
- `workflow` 実行を階層化し、phase responsibilities と監査責務を分離する。
- 各 `step` / 各 `substep` を独立エージェントとして実行し、実行経路を追跡可能にする。

## 適用範囲
- `Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote`
- `node workflow` 単位の phase 実行と、phase 内 `substep`（例: `generate` / `verify`）の実行

## term rules
- `phase` は `docs/WORKFLOW.md` で定義する workflow の論理単位を指す。
- `step` は 1 つの phase に対応するオーケストレーション上の実行単位を指す。
- `substep` は `step` を分解した下位実行単位を指す。
- `stage` は `generated_by_stage` や `<stage>_meta.json` など既存フィールド名または既存プレースホルダー名としてのみ使用する。本文では `phase` または `step` の同義語として使用してはならない。

## 要件
- `workflow` 実行は、必ず 1 つの `orchestration agent` を最初に起動して開始する。
- `workflow` 開始前に、`step agent` と `substep agent` を独立起動できる execution platform の preflight を必須実行しなければならない。preflight は `multi_agent` 機能と子 `agent` 起動可否を検証対象に含め、`pass` でない場合は `workflow` を開始してはならない。
- `preflight.json` の手動編集または後編集による `pass` 化を禁止する。preflight 結果は実行時検査の一次証跡としてのみ記録しなければならない。
- 子 `agent` 起動直前に、`Codex CLI` の live probe で `multi_agent` と子 `agent` 起動可否を再検査しなければならない。live probe は `record-launch` 実行時に適用し、`fail` の場合は `record-launch` と子 `agent` 起動を禁止し、当該 `workflow` を `fail` へ遷移させなければならない。
- `orchestration agent` は `workflow` 全体の進行制御のみを担当し、phase 本体の artifact（例: `case.resolved.yaml`、`diagnostics.json`）を直接生成してはならない。
- `workflow` 実行の代替として、複数 phase の進行と artifact generation を一括自動化する `script`（例: `python` / `bash`）を新規生成または実行してはならない。
- `orchestration` の責務を `script` へ委譲してはならない。`Build` / `Execute` / `Judge` / `Promote` の各 `step` は必ず `spawn_agent` で起動した独立 `step agent` で実行しなければならない。
- `Plan` / `Generate` / `Tune` のように `substep` を持つ各 phase は、`orchestration agent` が `generate` と `verify` などの各 `substep agent` を `spawn_agent` で直接起動しなければならない。
- `step agent` と `substep agent` は、同一 `LLM` コンテキストを共有してはならない。各 `agent_run_id` は固有の `context_id` を持ち、`context_isolated=true` を必須記録とする。
- `orchestration agent` は `substep` を持つ phase で必要な `substep` 群を起動し、完了判定を行った後に `step_result.json` を確定しなければならない。
- `orchestration agent` は `dependency.resolved.yaml` の `topo_level` 昇順と依存充足条件に基づいて `step agent` または `substep agent` の起動可否を判定しなければならない。
- すべての `agent` 実行は `agent_run_id` を持ち、入力参照・出力参照・親子関係を記録しなければならない。
- `agent_runs.jsonl` の各行は `started_at` と `status` を必須記録とし、`status` が終端状態（`pass` / `fail` / `blocked` / `timeout` / `cancel`）の場合は `finished_at` を必須記録とする。
- `step` / `substep` ロールの `agent_runs.jsonl` は `parent_agent_run_id` と `agent_backend` と `agent_model` と `context_id` と `context_isolated` と `agent_session_id` と `launch_request_ref` と `launch_response_ref` と `launch_prompt_ref` と `launch_reply_ref` を必須記録とする。
- `substep agent` の `parent_agent_run_id` は、当該 `substep` を起動した `orchestration agent_run_id` を指すことを許可する。
- `spawn_agent` の応答で得た子 `agent` 識別子は `agent_session_id` として記録しなければならない。
- `launch_request_ref` と `launch_response_ref` は `workspace/orchestrations/<orchestration_id>/launches/` 配下を参照し、参照先実体が存在しなければならない。
- `launch_prompt_ref` と `launch_reply_ref` は `workspace/orchestrations/<orchestration_id>/launches/` 配下を参照し、参照先のテキスト証跡が存在しなければならない。
- `launches/<agent_run_id>.request.json` には `launch_prompt_ref` を、`launches/<agent_run_id>.response.json` には `launch_reply_ref` を保持し、`agent_runs.jsonl` の参照値と一致させなければならない。
- `agent_graph.json` の `edge` は、`orchestration -> step` または `orchestration -> substep` を canonical source とする。互換運用として `step -> substep` を許容してもよいが、`substep` を親ロールとする `edge` を禁止する。
- `agent` 実行の失敗、`timeout`、`cancel` はメタデータへ記録し、推測補完で継続してはならない。
- `orchestration agent` は子 `agent` の完了待機中に当該子 `agent` の責務を代行してはならない。標準 `substep` を持たない phase では `step agent` も同様に子 `agent` の責務を代行してはならない。
- `orchestration agent` は、子 `agent` の返却結果を評価して `issue_severity`（`minor` / `major` / `critical`）を判定しなければならない。
- `orchestration agent` は、`issue_severity` と契約逸脱範囲に基づいて再投入要否を判定し、再投入が必要な場合は `repair_strategy`（`reuse` / `restart`）を選択しなければならない。
- `repair_strategy=reuse` は、対象 `step` または `substep` の input contract と expected output を変更せず、局所修正で収束可能な場合にのみ選択してよい。
- `repair_strategy=restart` は、契約再解釈、設計再構成、広範囲再生成のいずれかが必要な場合に選択しなければならない。
- 再投入時は `repair_strategy` を問わず、新規 `agent_run_id` と新規 `context_id` を発行しなければならない。
- `repair_strategy=reuse` の場合、`agent_session_id` は再利用してよい。
- `repair_strategy=restart` の場合、`agent_session_id` は新規発行しなければならない。
- 再投入時の `launches/<agent_run_id>.request.json` は、`issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` を必須記録としなければならない。
- `agent_runs.jsonl` と `agent_graph.json` は、実行中イベントを逐次追記して生成しなければならない。workflow 完了後に固定値テンプレートを一括出力する運用を禁止する。
- `agent_runs.jsonl` と `agent_graph.json` と `step_result.json` を後生成または手動整形して独立実行を偽装してはならない。起動時に記録した一次証跡との突合で整合しない試行は `fail` とする。
- `orchestration agent` は、子 `agent` 起動時に `docs/WORKFLOW.md` を canonical source として対象 `step` または `substep` の `execution input` と `verification input` と `expected output` を明示しなければならない。`step agent` を使用する phase では `step agent` も自身の契約入力と expected output を明示しなければならない。
- `skills/*/agents/openai.yaml` の表示名または説明文だけで独立 `agent` 起動契約を満たしたとみなしてはならない。起動要求本文に `spawn_agent` の使用義務、input contract、expected output、保存先、失敗時停止条件を明示しなければならない。

## 設計方針
- 単一責務: 1 つの `agent` は 1 つの責務のみを持つ。
- 階層委譲: `orchestration agent -> step agent` と `orchestration agent -> substep agent` の 2 系統で制御する。
- 契約駆動: 子 `agent` 起動時は input contract と output contract を固定し、契約外の読み書きを禁止する。
- 追跡可能性: すべての起動・終了イベントを時系列で保存し、再実行時に同一判断を再現可能にする。

## オーケストレーション指示契約
### 共通必須項目
- `orchestration agent` は、子 `agent` への起動要求に `orchestration_id` と `agent_run_id` と `parent_agent_run_id` と `node_key` と `step` と `substep`（存在する場合）と `plan_ref` と `pipeline_ref` と `dependency_ref` を必須記録しなければならない。
- 子 `agent` への起動要求には、`execution input` と `verification input` と `expected output` と `write_root` と `read_roots` を必須記録しなければならない。
- `execution input` は当該 `agent` が artifact を生成するために直接参照してよい入力に限定しなければならない。
- `verification input` は当該 `agent` が pass/fail 判定、整合確認、依存確認にのみ使用してよい入力として明示しなければならない。
- `expected output` はファイル名、保存先、更新責務を含めて明示しなければならない。親 `agent` は `expected output` に含まれない artifact を子 `agent` へ要求してはならない。
- 親 `agent` は入力不足時に推測補完を指示してはならない。不足入力がある場合は `fail-fast` 停止を指示しなければならない。
- 子 `agent` への起動要求には `skill_name` と `skill_ref` と `skill_must_read_refs` を必須記録し、子 `agent` が起動直後に対象 `SKILL` を読める状態を保証しなければならない。
- `step` ごとの具体的な `execution input` と `verification input` と `expected output` は `docs/WORKFLOW.md` を canonical source とし、親 `agent` は対象 `step` 節の定義を参照して起動要求へ展開しなければならない。
- `substep` ごとの具体的な `execution input` と `verification input` と `expected output` は、対応 `SKILL.md` と `docs/WORKFLOW.md` の両方を参照して決定しなければならない。`WORKFLOW.md` に明示された phase contractと矛盾する `substep` 契約を定義してはならない。
- `Build` / `Execute` / `Judge` / `Promote` のように現行標準で `substep` を定義しない `step` では、`orchestration agent` は `step` 契約をそのまま単一 `step agent` へ渡さなければならない。
- `Plan generate/verify`、`Generate generate/verify`、`Tune generate/verify` のように `substep` を持つ `step` では、`orchestration agent` は `step` 契約を分解したうえで、対応 `SKILL.md` の責務境界に一致する `substep` 契約だけを直接渡さなければならない。

## 運用ルール
1. `workflow` 開始時に `orchestration_id` を発行し、`workspace/orchestrations/<orchestration_id>/orchestration_meta.json` を作成する。
2. `workflow` 開始前にpreflight結果を `workspace/orchestrations/<orchestration_id>/preflight.json` へ記録し、`can_launch_step_agents=true` と `can_launch_substep_agents=true` を満たさない場合は `fail` として停止する。
3. `orchestration agent` は `step agent` または `substep agent` の起動要求ごとに `launches/<agent_run_id>.request.json` と `launches/<agent_run_id>.response.json` と `launches/<agent_run_id>.prompt.txt` と `launches/<agent_run_id>.reply.txt` を保存し、`agent_runs.jsonl` の `launch_request_ref` と `launch_response_ref` と `launch_prompt_ref` と `launch_reply_ref` へ参照を記録する。
4. `orchestration agent` は `dependency.resolved.yaml` を読み、`node_key` と `topo_level` に基づく実行キューを確定する。
5. `orchestration agent` は起動対象ごとに `step agent` または `substep agent` を発行し、`node_key`、`step`、`plan_ref`、`pipeline_ref`、`dependency_ref` を入力として渡す。
6. `orchestration agent` は `step` を持つ phase では対象 `step` の `execution input` と `verification input` と `expected output` を明示し、`substep` を持つ phase では対象 `substep` の `execution input` と `verification input` と `expected output` を明示しなければならない。
7. `substep` を持つ phase では、`orchestration agent` が `generate` と `verify` などの `substep agent` を逐次起動する。
8. `substep agent` は自身の artifact と対応 phase のメタデータを生成し、`agent_output_ref` を `orchestration agent` へ返却する。
9. `orchestration agent` は子 `agent` の返却結果を評価し、`issue_severity` と再投入要否を確定する。再投入が必要な場合は `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` を確定する。
10. 再投入が必要で `repair_strategy=reuse` の場合、`orchestration agent` は同一 `agent_session_id` の継続修正を許可してよい。この場合も新規 `agent_run_id` を発行し、`relation_type` を `reuse` として `record-launch` 記録を追加しなければならない。
11. 再投入が必要で `repair_strategy=restart` の場合、`orchestration agent` は新規 `agent_session_id` を持つ `substep agent` を再起動し、`relation_type` を `restart` として `record-launch` 記録を追加しなければならない。
12. `orchestration agent` は `substep` を持つ phase で全 `substep` の必須 artifact を検証し、`workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/step_result.json` へ `step_result.json` を出力する。この場合の `agent_run_id` は `orchestration agent_run_id` とする。
13. `step_result.json` は、再投入を実施した場合に `retry_decisions` 配列を保持し、各要素へ `issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `new_agent_run_id` と `repair_reason` を記録しなければならない。
14. `step agent` は標準 `substep` を持たない phase で自身の artifact を検証し、`workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/step_result.json` へ `step_result.json` を出力する。
15. `orchestration agent` は `step_result.json` を受け取り、次 `step` の起動可否を判定する。
16. `node` 実行は `dependency.resolved.yaml` の `topo_level` 昇順で逐次実行する。依存関係を持つ `node` は依存 `node` の完了前に起動してはならない。同一 `topo_level` の独立 `node` も並列実行してはならない。
17. `step agent` または `substep agent` が `fail` / `timeout` / `cancel` の場合、当該 `node` の当該 `step` を `fail` とし、下流 `step` 起動を禁止する。
18. `orchestration agent` は各 `agent` 実行イベントを `workspace/orchestrations/<orchestration_id>/agent_runs.jsonl` へ追記しなければならない。
19. `orchestration agent` は親子関係を `workspace/orchestrations/<orchestration_id>/agent_graph.json` へ保存し、`parent_agent_run_id` と `child_agent_run_id` と `relation_type` を必須記録とする。
20. `Promote` 以外の `agent` は `workspace/` 配下以外へ書き込んではならない。
21. `workflow` 実行時に `step` / `substep` の実処理を `script` で代行した場合は `fail` とし、当該試行を破棄しなければならない。
22. 再投入時は新規 `agent_run_id` を発行し、既存 `launch` 証跡や `agent_runs` 行を上書きしてはならない。`agent_session_id` の扱いは `repair_strategy` 規則に従う。
23. `preflight.json` の手動編集または後編集で `status` と `can_launch_*` を変更してはならない。変更が必要な場合は `preflight` を再実行して新しい検査結果を記録しなければならない。
24. 子 `agent` 起動直前の live probe が `fail` の場合、`record-launch` を実行してはならない。`orchestration_meta.status=fail` を記録して停止しなければならない。`record-agent-run`（`step` / `substep`）と `write-step-result` は `preflight.json` の整合確認を満たす場合のみ実行してよい。

## 判定基準
- `workflow` ごとに `orchestration_id` が発行され、`orchestration_meta.json` が存在する。
- 各 `step` または各 `substep` が独立 `agent_run_id` を持つ。
- `step` と `substep` の `context_id` が重複せず、全件で `context_isolated=true` が記録される。
- `step` と `substep` の `agent_runs.jsonl` に `agent_session_id` と `launch_request_ref` と `launch_response_ref` と `launch_prompt_ref` と `launch_reply_ref` が記録され、参照先実体が存在する。
- `launches/<agent_run_id>.request.json` の `launch_prompt_ref` と `launches/<agent_run_id>.response.json` の `launch_reply_ref` が `agent_runs.jsonl` の参照値と一致する。
- 子 `agent` の全 `launches/<agent_run_id>.request.json` に `skill_name` と `skill_ref` と `skill_must_read_refs` が記録されている。
- `preflight.json` が存在し、`can_launch_step_agents=true` と `can_launch_substep_agents=true` を満たす。
- `preflight.json` の `pass` 条件と、子 `agent` 起動直前 live probe の `pass` 条件が同時に満たされる。
- `agent_graph.json` で `orchestration -> step` または `orchestration -> substep` の親子関係を追跡できる。
- `agent_runs.jsonl` から `queued` / `running` / `pass` / `fail` / `blocked` / `timeout` / `cancel` の遷移を追跡できる。
- `step_result.json` の `executor_agent_run_id` が当該ディレクトリ名と一致し、`substep_agent_run_ids` が親子関係と整合する。標準 `substep` を持たない phase では `substep_agent_run_ids=[]` を許可する。
- `step_result.json` の `required_outputs` が `WORKFLOW.md` の phase contractと一致する。
- 再投入を実施した `substep` は、対応する `launches/<agent_run_id>.request.json` に `issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` を保持している。
- `repair_strategy=reuse` の再投入を実施した場合、対象 `agent_run` の `agent_session_id` は `repair_target_agent_run_id` の `agent_session_id` と一致する。
- `repair_strategy=restart` の再投入を実施した場合、対象 `agent_run` の `agent_session_id` は `repair_target_agent_run_id` の `agent_session_id` と一致しない。
- `step_result.json` が `retry_decisions` を保持する場合、各 `new_agent_run_id` が `substep_agent_run_ids` と `agent_graph.json` の親子関係に含まれている。
- 失敗試行で推測補完や人工 artifact generation を行わず、当該 `step` を停止している。
- 各 `step_result.json` の `executor_agent_run_id` が `orchestration` または `step` ロールの実行記録と対応し、`script` 実行ログのみで phase 完了を主張していない。
