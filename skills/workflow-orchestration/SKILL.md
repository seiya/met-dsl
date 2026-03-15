---
name: workflow-orchestration
description: `Codex CLI` で `workflow` 全体を開始し、`orchestration agent -> step agent` または `orchestration agent -> substep agent` の独立 `agent` 起動で進行制御するときに使用する。`tools/codex_orchestration_runtime.py` を使った `preflight`、launch 証跡、`agent_runs.jsonl`、`step_result.json` の記録に適用する。
---

# Workflow Orchestration

## 目的
`Codex CLI` に対して、workflow 全体を親 `agent` の単一スレッド処理ではなく、独立した子 `agent` の階層起動として実行させる。

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
- `Codex CLI` の起動可否確認と証跡書き出しは `tools/codex_orchestration_runtime.py` を canonical source 実装として使用しなければならない。
- `preflight.json` の手動編集または後編集による `pass` 化を禁止する。`preflight` は `tools/codex_orchestration_runtime.py preflight` の execution result を canonical source とする。
- 子 `agent` 起動直前に live preflight gate を満たすことを必須とし、live 検査が `fail` の場合は `record-launch` を実行してはならない。
- 起動前の初期読込は `references/startup_contract.md` を第一参照とし、詳細契約が必要な場合のみ `docs/WORKFLOW.md` と `docs/ORCHESTRATION.md` を追加参照しなければならない。
- `step agent` / `substep agent` の起動要求本文には、input contract、expected output、保存先、失敗時停止条件、`spawn_agent` 義務を明示しなければならない。
- `step agent` / `substep agent` の起動要求本文には、`skill_name` と `skill_ref` と `skill_must_read_refs` を必須記録し、子 `agent` が起動直後に対象 `SKILL` を読める状態にしなければならない。
- 子 `agent` 起動ごとに、起動要求本文を `launches/<agent_run_id>.prompt.txt`、起動返答本文を `launches/<agent_run_id>.reply.txt` へ保存し、`agent_runs.jsonl` の `launch_prompt_ref` と `launch_reply_ref` に参照を記録しなければならない。
- `openai.yaml` の表示名だけで orchestration 契約を満たしたとみなしてはならない。
- 子 `agent` の返却結果を評価した後、`issue_severity`（`minor` / `major` / `critical`）を判定し、再投入が必要な場合は `repair_strategy`（`reuse` / `restart`）を選択しなければならない。
- `repair_strategy=reuse` は契約不変の局所修正に限定し、`repair_strategy=restart` は契約再解釈または広範囲再生成が必要な場合に選択しなければならない。
- 再投入時の起動要求には、`issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` を必須記録しなければならない。
- 再投入時は `repair_strategy` を問わず新規 `agent_run_id` を発行し、`repair_strategy=reuse` の場合のみ `agent_session_id` 再利用を許可する。

## 運用ルール
1. `python3 tools/codex_orchestration_runtime.py init --repo-root <repo_root> --orchestration-id <orchestration_id> --spec-ref <spec_ref> --dependency-ref <dependency_ref>` を実行し、`workspace/orchestrations/<orchestration_id>/` を初期化する。
2. `python3 tools/codex_orchestration_runtime.py preflight --repo-root <repo_root> --orchestration-id <orchestration_id>` を実行し、`preflight.json` を生成する。
3. `preflight.json` の `can_launch_step_agents=true` と `can_launch_substep_agents=true` を満たさない場合は workflow を開始しない。
4. `orchestration agent` は `references/startup_contract.md` を読んで起動条件を確定し、`references/launch_prompts.md` の `step agent` 用または `substep agent` 用テンプレートに従って子 `agent` を起動する。起動要求と起動応答は `record-launch` で保存し、`launch_prompt_ref` と `launch_reply_ref` も同時に記録する。
5. 子 `agent` 完了後は `python3 tools/codex_orchestration_runtime.py record-agent-run --repo-root <repo_root> --orchestration-id <orchestration_id> --agent-run-json '<json>'` を実行し、`agent_runs.jsonl` へ 1 行追記する。
6. `substep` を持つ phase では、返却結果を評価して `issue_severity` と `repair_strategy` を決定する。再投入が必要な場合は `repair_target_agent_run_id` と `repair_reason` を起動要求へ付与して再起動し、`record-launch` を追加する。
7. `repair_strategy=reuse` の再投入では、対象 `substep` の契約を変更せず差分修正だけを要求する。`repair_strategy=restart` の再投入では、対象 `substep` の契約入力から再生成させる。
8. 標準 `substep` を持たない phase では `step agent` 完了後に、`substep` を持つ phase では `orchestration agent` 集約完了後に、`python3 tools/codex_orchestration_runtime.py write-step-result --repo-root <repo_root> --orchestration-id <orchestration_id> --node-key <node_key> --step <step> --agent-run-id <agent_run_id> --result-json '<json>'` を実行する。再投入を実施した場合は `step_result.json` に `retry_decisions` を含める。
9. workflow 終了時は `python3 tools/codex_orchestration_runtime.py set-status --repo-root <repo_root> --orchestration-id <orchestration_id> --status <status>` を実行し、`orchestration_meta.json` を終端状態へ更新する。
10. `preflight.json` を手動編集または後編集して `status` と `can_launch_*` を変更してはならない。検査条件の変化は `preflight` 再実行でのみ反映する。
11. `record-launch` 実行時に live preflight gate が `fail` の場合、当該起動を停止し、`set-status --status fail` のみを許可する。

## 参照
- 起動最小契約: `references/startup_contract.md`
- launch 要求テンプレート: `references/launch_prompts.md`

## 判定基準
- `orchestration agent` が phase artifactsを直接生成していない。
- `workspace/orchestrations/<orchestration_id>/preflight.json` が存在し、`pass` 条件を満たしている。
- `agent_runs.jsonl` に `orchestration` と、必要に応じて `step` / `substep` の各ロールが記録されている。
- `launches/` の要求と応答が `agent_runs.jsonl` の `launch_request_ref` / `launch_response_ref` と一致する。
- `launches/` の prompt と reply が `agent_runs.jsonl` の `launch_prompt_ref` / `launch_reply_ref` と一致する。
- `step_result.json` が `executor_agent_run_id` と `substep_agent_run_ids` を保持している。
- 再投入を実施した場合、該当 `launch` 要求に `issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` が含まれている。
- 子 `agent` の全 `launch` 要求に `skill_name` と `skill_ref` と `skill_must_read_refs` が含まれている。
- `repair_strategy=reuse` と `repair_strategy=restart` の選択が、`ORCHESTRATION.md` の判定条件と一致している。
