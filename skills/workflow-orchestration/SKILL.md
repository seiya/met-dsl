---
name: workflow-orchestration
description: 対応 execution platform で `workflow` 全体を開始し、`orchestration agent -> step agent` または `orchestration agent -> substep agent` の独立 `agent` 起動で進行制御するときに使用する。`tools/codex_orchestration_runtime.py` を使った `preflight`、launch 証跡、`agent_runs.jsonl`、`step_result.json` の記録に適用する。
---

# Workflow Orchestration

## 目的
対応 execution platform に対して、workflow 全体を親 `agent` の単一スレッド処理ではなく、独立した子 `agent` の階層起動として実行させる。

## 適用範囲
- `workflow` 開始時の `orchestration_id` 発行
- `preflight.json` の生成
- `step agent` / `substep agent` の launch 証跡生成
- `agent_runs.jsonl` / `agent_graph.json` / `step_result.json` の記録

## 要件
- `orchestration agent` は phase artifactsを直接生成してはならない。
- 標準 `substep` を持たない各 `step` は `spawn_agent` で起動した独立 `step agent` へ委譲しなければならない。
- `Plan` / `Generate` / `Tune` のように `substep` を持つ phase では、`orchestration agent` が `generate` と `verify` を別々の `substep agent` として `spawn_agent` で直接起動しなければならない。
- `Build` / `Execute` / `Judge` / `Promote` の `step` は、単一 `step agent` で完了させなければならない。
- execution platform の起動可否確認と証跡書き出しは `tools/codex_orchestration_runtime.py` を canonical source 実装として使用しなければならない。
- `preflight.json` の手動編集または後編集による `pass` 化を禁止する。`preflight` は `tools/codex_orchestration_runtime.py preflight` の execution result を canonical source とする。
- 子 `agent` 起動直前に live preflight gate を満たすことを必須とし、live 検査が `fail` の場合は `record-launch` を実行してはならない。
- 起動前の初期読込は `references/startup_contract.md` を第一参照とし、詳細契約が必要な場合のみ `docs/WORKFLOW.md` と `docs/ORCHESTRATION.md` を追加参照しなければならない。
- phase 着手前に、対象 phase が `substep agent` 必須か `step agent` 必須かを固定表で判定しなければならない。`Plan` / `Generate` / `Tune` は `substep agent`、`Build` / `Execute` / `Judge` / `Promote` は `step agent` とする。
- 最初の `commentary` では、対象 phase、使用する `SKILL`、起動する `agent` 種別、`MCP` を使用する箇所を実行宣言として明示しなければならない。実行宣言と実作業が一致しない場合は停止して宣言からやり直さなければならない。
- `step agent` / `substep agent` の起動要求本文は、必ず `references/launch_prompts.md` の対応テンプレートを基底として生成しなければならない。テンプレートを使わない任意の自由形式 prompt、別テンプレートの混用、必須項目の省略または改名を禁止する。
- 子 `agent` 起動要求本文には、要求定義と判定規則の canonical source が `docs/` と `spec/` と当該試行 artifact であること、`tools/` 配下の実装、検証 `script`、test code、validator code を読んで rule を抽出してはならないことを明示しなければならない。
- `plan_ref` と `pipeline_ref` と `dependency_ref` は、起動要求生成時点で canonical path を確定しなければならない。`<agent-determined-...>` などの placeholder を禁止する。
- `step agent` / `substep agent` の起動要求本文には、input contract、expected output、保存先、失敗時停止条件、`spawn_agent` 義務を明示しなければならない。
- `step agent` / `substep agent` の起動要求本文には、`skill_name` と `skill_ref` と `skill_must_read_refs` を必須記録し、子 `agent` が起動直後に対象 `SKILL` を読める状態にしなければならない。
- `Plan verify` と `Generate verify` の起動要求では、`skill_must_read_refs` に `plan_ref` 配下の `case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` と `derived_contract.json` を必須記録しなければならない。
- `launch` 記録時に保存する prompt は、request payload の必須フィールド値と一致するテンプレート完全体でなければならない。要約 prompt や marker のみ保持した簡略 prompt を禁止する。
- `record-launch` に保存する `launch response` は、`spawn_agent` 成功直後の実応答完全体でなければならない。後生成、固定文言、要約文のみの代替を禁止する。
- `launch response` は子 `agent` 識別子を必須記録し、`record-agent-run` の `agent_session_id` は当該識別子と一致しなければならない。
- 上位 `node` の `Plan` を起動する前に、直下依存 `node` の `plan_ref` と `plan_meta.json.verification_status` を確認し、`direct dependency plan readiness` を満たすことを必須とする。
- 上位 `node` の `Generate` / `Build` / `Execute` / `Judge` を起動する前に、直下依存 `node` の `plan_ref` と `pipeline_ref` と最新 `aggregate_verdict` を確認し、`direct dependency execution readiness` を満たすことを必須とする。
- 直下依存 `node` が未完了の場合、依存先 code を上位 `node` の `src/` へ内包する代替実装を指示してはならない。
- phase artifact を直接編集または `MCP` 実行する前に、`preflight` 済み、launch prompt 準備済み、child `agent` 起動済みの 3 条件を満たさなければならない。いずれかが未充足の場合は phase 本体の編集と実行を開始してはならない。
- workflow の正当性確認、検証、疎通確認を目的とした仮実装であっても、親 `agent` が子 `agent` 必須 phase の本体処理を代行してはならない。
- 子 `agent` 起動ごとに、起動要求本文を `launches/<agent_run_id>.prompt.txt`、起動返答本文を `launches/<agent_run_id>.reply.txt` へ保存し、`agent_runs.jsonl` の `launch_prompt_ref` と `launch_reply_ref` に参照を記録しなければならない。
- 各 `step agent` / `substep agent` の完了時に、`agents/<agent_run_id>/dialogs/agent.result.json` と `agents/<agent_run_id>/dialogs/agent.summary.txt` を保存し、`agent_runs.jsonl` の `agent_result_ref` と `agent_summary_ref` に参照を記録しなければならない。
- `agent.summary.txt` は最終 `status` と主要 `output_refs` または失敗原因を含む調査用ログとし、単一行の `pass` / `fail` のみで終えてはならない。
- `openai.yaml` の表示名だけで orchestration 契約を満たしたとみなしてはならない。
- 子 `agent` の返却結果を評価した後、`issue_severity`（`minor` / `major` / `critical`）を判定し、再投入が必要な場合は `repair_strategy`（`reuse` / `restart`）を選択しなければならない。
- `repair_strategy=reuse` は契約不変の局所修正に限定し、`repair_strategy=restart` は契約再解釈または広範囲再生成が必要な場合に選択しなければならない。
- 再投入時の起動要求には、`issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` を必須記録しなければならない。
- 再投入時は `repair_strategy` を問わず新規 `agent_run_id` を発行し、`repair_strategy=reuse` の場合のみ `agent_session_id` 再利用を許可する。

## 運用ルール
1. `python3 tools/codex_orchestration_runtime.py init --repo-root <repo_root> --orchestration-id <orchestration_id> --spec-ref <spec_ref> --dependency-ref <dependency_ref>` を実行し、`workspace/orchestrations/<orchestration_id>/` を初期化する。
2. `python3 tools/codex_orchestration_runtime.py preflight --repo-root <repo_root> --orchestration-id <orchestration_id> --backend <backend>` を実行し、`preflight.json` を生成する。`backend` 未指定時は既定値 `codex` を使用する。
3. `preflight.json` の `can_launch_step_agents=true` と `can_launch_substep_agents=true` を満たさない場合は workflow を開始しない。
4. `orchestration agent` は `references/startup_contract.md` を読んで起動条件を確定し、最初の `commentary` で対象 phase、使用する `SKILL`、起動する `agent` 種別、`MCP` 使用箇所を実行宣言する。
5. 実行宣言後に phase 種別を固定表で再確認し、`Plan` / `Generate` / `Tune` では `substep agent`、`Build` / `Execute` / `Judge` / `Promote` では `step agent` を選択する。
6. 起動要求本文と `skill_must_read_refs` は、`tools/codex_orchestration_runtime.py` の `prepare_launch_request_payload` と `render_launch_prompt_text` に相当する canonical 生成規則で組み立てる。手作業連結での field 欠落、verify 必須 ref 欠落、prompt と request の値不一致を禁止する。
7. `record-launch` 実行前に、`plan_ref` と `pipeline_ref` と `dependency_ref` に placeholder が残存していないこと、`verify` 起動要求の `skill_must_read_refs` が必須 resolved artifact を網羅していること、起動要求本文に non-canonical な `tools/` / validator 参照禁止が含まれていることを検査する。
8. `Plan` の子 `agent` 起動前に、対象 `node` の直下依存 `node` ごとの `plan_ref` と `plan_meta.json.verification_status` を照合し、`direct dependency plan readiness` 不成立なら子 `agent` を起動せず `blocked` または `fail` を記録する。
9. `Generate` 以降の子 `agent` 起動前に、対象 `node` の直下依存 `node` ごとの `plan_ref` と `pipeline_ref` と `aggregate_verdict` を照合し、`direct dependency execution readiness` 不成立なら子 `agent` を起動せず `blocked` または `fail` を記録する。
10. phase 本体へ進む前に、`preflight` 済み、launch prompt 準備済み、child `agent` 起動済みの 3 条件を確認する。未充足なら編集、`MCP` 実行、phase artifact 生成を開始してはならない。
11. 生成した起動要求本文で子 `agent` を起動する。起動成功直後の実 `spawn_agent` 応答だけを `record-launch` で保存し、`launch_prompt_ref` と `launch_reply_ref` も同時に記録する。実起動前の仮記録、起動失敗後の補完記録、任意 `response_payload` の後投入を禁止する。
12. 子 `agent` 完了後は `python3 tools/codex_orchestration_runtime.py record-agent-run --repo-root <repo_root> --orchestration-id <orchestration_id> --agent-run-json '<json>'` を実行し、`agent_runs.jsonl` へ 1 行追記する。`record-agent-run` により `agent.result.json` と `agent.summary.txt` も同時に保存しなければならない。
13. `substep` を持つ phase では、返却結果を評価して `issue_severity` と `repair_strategy` を決定する。再投入が必要な場合は `repair_target_agent_run_id` と `repair_reason` を起動要求へ付与して再起動し、`record-launch` を追加する。
14. `repair_strategy=reuse` の再投入では、対象 `substep` の契約を変更せず差分修正だけを要求する。`repair_strategy=restart` の再投入では、対象 `substep` の契約入力から再生成させる。
15. 契約に反する近道を取りたくなった場合は、子 `agent` 起動必須であることを `commentary` で明示し、launch 手順へ戻る。ローカル実装を継続してはならない。
16. 標準 `substep` を持たない phase では `step agent` 完了後に、`substep` を持つ phase では `orchestration agent` 集約完了後に、`python3 tools/codex_orchestration_runtime.py write-step-result --repo-root <repo_root> --orchestration-id <orchestration_id> --node-key <node_key> --step <step> --agent-run-id <agent_run_id> --result-json '<json>'` を実行する。再投入を実施した場合は `step_result.json` に `retry_decisions` を含める。
17. workflow 終了時は `python3 tools/codex_orchestration_runtime.py set-status --repo-root <repo_root> --orchestration-id <orchestration_id> --status <status>` を実行し、`orchestration_meta.json` を終端状態へ更新する。
18. `preflight.json` を手動編集または後編集して `status` と `can_launch_*` を変更してはならない。検査条件の変化は `preflight` 再実行でのみ反映する。
19. `record-launch` 実行時に live preflight gate が `fail` の場合、当該起動を停止し、`set-status --status fail` のみを許可する。

## 参照
- 起動最小契約: `references/startup_contract.md`
- launch 要求テンプレート: `references/launch_prompts.md`

## 判定基準
- `orchestration agent` が phase artifactsを直接生成していない。
- `workspace/orchestrations/<orchestration_id>/preflight.json` が存在し、`pass` 条件を満たしている。
- 最初の `commentary` に、対象 phase、使用 `SKILL`、起動 `agent` 種別、`MCP` 使用箇所の実行宣言が存在する。
- `agent_runs.jsonl` に `orchestration` と、必要に応じて `step` / `substep` の各ロールが記録されている。
- `Plan` / `Generate` / `Tune` が `substep agent`、`Build` / `Execute` / `Judge` / `Promote` が `step agent` で起動されている。
- `step` / `substep` の各 `agent_run` に対応する `agent.result.json` と `agent.summary.txt` が存在し、`agent_runs.jsonl` の参照値と一致している。
- `launches/` の要求と応答が `agent_runs.jsonl` の `launch_request_ref` / `launch_response_ref` と一致する。
- `launches/` と `agents/<agent_run_id>/dialogs/child.response.json` の応答が、同一の `spawn_agent` 実応答を保持している。
- `agent_runs.jsonl.agent_session_id` が、対応する `launch response` の子 `agent` 識別子と一致している。
- `launches/` の prompt と reply が `agent_runs.jsonl` の `launch_prompt_ref` / `launch_reply_ref` と一致する。
- `launches/` の prompt が `references/launch_prompts.md` の対応テンプレートを基底としており、テンプレート必須項目の欠落または意味変更が存在しない。
- `launches/` の request に placeholder ref が存在しない。
- `verify` の `launches/` request が、必須 resolved artifact を `skill_must_read_refs` へ記録している。
- 子 `agent` の `launches/` prompt が、`tools/` 配下の実装、検証 `script`、test code、validator code を rule source として読むことを禁止している。
- `step_result.json` が `executor_agent_run_id` と `substep_agent_run_ids` を保持している。
- 再投入を実施した場合、該当 `launch` 要求に `issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` が含まれている。
- 子 `agent` の全 `launch` 要求に `skill_name` と `skill_ref` と `skill_must_read_refs` が含まれている。
- `repair_strategy=reuse` と `repair_strategy=restart` の選択が、`ORCHESTRATION.md` の判定条件と一致している。
- 子 `agent` 必須 phase で、child `agent` 起動前の phase artifact 直接編集、`MCP` 実行、検証目的の仮実装が存在しない。
- `agent.summary.txt` が、単一行の定型 `pass` / `fail` のみではなく、最終状態と主要 `output_refs` または失敗原因を含んでいる。
