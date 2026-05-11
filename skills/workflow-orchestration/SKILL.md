---
name: workflow-orchestration
description: 対応 execution platform で `workflow` 全体を開始し、`orchestration agent -> step agent` または `orchestration agent -> substep agent` の独立 `agent` 起動で進行制御するときに使用する。workflow 起動の canonical entrypoint は `tools/run_workflow.py` とし、`tools/orchestration_runtime.py` を使った `preflight`、launch 証跡、`agent_runs.jsonl`、`step_result.json` の記録に適用する。
---

# Workflow Orchestration

## 目的
対応 execution platform に対して、workflow 全体を親 `agent` の単一スレッド処理ではなく、独立した子 `agent` の階層起動として実行させる。core workflow は `Spec → Compile → Generate → Build → Validate` の 5-phase 構成（`Tune` / `Promote` は任意フローで core からは分離）。

## 適用範囲
- `workflow` 開始時の `orchestration_id` 発行
- `preflight.json` の生成
- `step agent` / `substep agent` の launch 証跡生成
- `agent_runs.jsonl` / `agent_graph.json` / `step_result.json` の記録

## 要件
- `orchestration agent` は phase artifactsを直接生成してはならない。
- 標準 `substep` を持たない各 `step` は `spawn_agent` で起動した独立 `step agent` へ委譲しなければならない。
- `Compile` / `Generate` / `Validate` のように `substep` を持つ phase では、`orchestration agent` が 2 つの substep (`Compile`/`Generate` は `generate` と `verify`、`Validate` は `execute` と `judge`) を別々の `substep agent` として `spawn_agent` で直接起動しなければならない。
- `Build` の `step` は、単一 `step agent` で完了させなければならない。
- 任意フロー `Tune` / `Promote` は core workflow に含めない。`Tune` は substep を持つ任意フロー、`Promote` は step の任意フローとして別 entrypoint から起動する。
- execution platform の起動可否確認と証跡書き出しは `tools/orchestration_runtime.py` を canonical source 実装として使用しなければならない。
- workflow 起動は `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]` を canonical entrypoint とし、手動 `init` / 手動 `preflight` を通常経路として使用してはならない。`<until_phase>` は `compile` / `generate` / `build` / `validate` のいずれか。
- workflow mode は `tools/run_workflow.py --mode <dev|prod>` を canonical source とし、既定値は `dev` とする。
- `dev` mode では verify 判定を厳格運用し、`issue_severity=major|critical` を検出した場合は `fail` として停止しなければならない。
- `dev` mode で fail が発生した場合、`workspace/orchestrations/<orchestration_id>/failure_analysis.json` を必須出力として原因調査結果を保存しなければならない。
- `preflight.json` の手動編集または後編集による `pass` 化を禁止する。`preflight` は `tools/orchestration_runtime.py preflight` の execution result を canonical source とする。
- `backend=codex` の preflight は、`checks.codex_hooks_enabled.pass=true` と `checks.codex_home_writable.pass=true` を同時に満たさなければならない。未充足時は workflow を開始してはならない。
- 子 `agent` 起動直前に live preflight gate を満たすことを必須とし、live 検査が `fail` の場合は `record-launch` を実行してはならない。
- 起動前の初期読込は `references/startup_contract.md` を第一参照とし、詳細契約が必要な場合のみ `docs/workflow/WORKFLOW_CORE.md` と `docs/ORCHESTRATION.md` を追加参照しなければならない。
- phase 着手前に、対象 phase が `substep agent` 必須か `step agent` 必須かを固定表で判定しなければならない。`Compile` / `Generate` / `Validate` は `substep agent`、`Build` は `step agent` とする。任意フローでは `Tune` が `substep agent`、`Promote` が `step agent`。
- 最初の phase 着手前に `python3 tools/orchestration_runtime.py workflow-launch-check --repo-root <repo_root> --orchestration-id <orchestration_id> --node-key <node_key> --step <step> --backend <backend> --require-child-agent <step|substep>` を実行しなければならない。
- `workflow-launch-check` が `status=fail_closed` を返した場合、`python3 tools/orchestration_runtime.py set-status --repo-root <repo_root> --orchestration-id <orchestration_id> --status fail_closed --reason-code <reason_code>` を実行して停止し、phase artifact を生成してはならない。
- 最初の `commentary` では、対象 phase、使用する `SKILL`、起動する `agent` 種別、`MCP` を使用する箇所を実行宣言として明示しなければならない。実行宣言と実作業が一致しない場合は停止して宣言からやり直さなければならない。
- `step agent` / `substep agent` の起動要求本文は、必ず `references/launch_prompts.md` の対応テンプレートを基底として生成しなければならない。テンプレートを使わない任意の自由形式 prompt、別テンプレートの混用、必須項目の省略または改名を禁止する。
- 子 `agent` 起動要求本文には、要求定義と判定規則の canonical source が `docs/` と `spec/` と当該試行 artifact であること、`tools/` 配下の実装、検証 `script`、test code、validator code を読んで rule を抽出してはならないことを明示しなければならない。
- `ir_ref` と `pipeline_ref` と `dependency_ref` は、起動要求生成時点で canonical path を確定しなければならない。`<agent-determined-...>` などの placeholder を禁止する。
- `Compile` phase の `generate` substep および `verify` substep を起動するとき、`dependency_ref` には `spec/<component_path>/deps.yaml` 形式の実在 path を必ず渡さなければならない。`verify` substep も `generate` substep と同一の `dependency_ref` を引き継がなければならない。`workspace/ir/` を指す値は `Compile` phase では常に誤りとする。`dependency_ref` が空または placeholder の場合は起動してはならない。`deps.yaml` の実在確認は `run-gate --gate orchestration_read` を使用すること。
- child `agent` に許可する phase artifact の変更は、capability token が許可した `write_root` 配下に限定しなければならない。`ir_ref` / `pipeline_ref` 配下の変更は `guarded-apply-patch` または対応 gate を通過した canonical path に限定し、許可 root 外の変更を禁止する。
- phase artifact を変更する場合、`step agent` / `substep agent` は出力 path の extension で書き込み経路を分岐しなければならない。`.json` と `.txt` の出力は `guarded-apply-patch` を canonical invocation とし、それ以外の extension（`.yaml` / `.yml` / `.md` / source code 等）は `output_manifests/<agent_run_id>.json` の `allowed_file_tool_paths` に列挙された path に限り `Edit` / `Write` tool で直接書き込む。`apply_patch_writes` は内部 gate であり、`run-gate --gate apply_patch_writes` と `apply-patch-gate` を公開経路として使用してはならない。通常 `apply_patch` の直接実行、shell redirection、`tee`、`sed -i`、`perl -0pi`、`python` / `sh` / `bash` による file write を禁止する。
- `guarded-apply-patch` の疎通確認は dry-run または no-op patch で実施しなければならない。dummy file を新規作成して疎通確認してはならない。
- `record-launch` は `output_manifests/<agent_run_id>.json` と `read_manifests/<agent_run_id>.json` を生成しなければならない。output manifest には `allowed_output_paths`（全許可 path）と `allowed_file_tool_paths`（`Edit` / `Write` で直接書き込み可能な path）の両方を保持し、`allowed_file_tool_paths` は既定で `allowed_output_paths` から `.json` / `.txt` を除いた集合を自動収録する。`guarded-apply-patch` と `record-agent-run` は output manifest を参照し、`orchestration-read` は read manifest を参照して manifest 外 path を reject しなければならない。
- `orchestration agent` は子 agent (= 自分以外の `agent_run_id`) が所有する以下 4 種類の internal artifact を `Read` してはならない。これらは `read_manifest_read_guard` で fail-closed にブロックされる:
  - `launches/<child_arid>.prompt.txt` — 子 prompt は `Agent` tool の input で起動時に渡しており再読不要。
  - `capabilities/<child_arid>.json` — 子のみが起動直後に自身の capability を読む。
  - `output_manifests/<child_arid>.json` / `read_manifests/<child_arid>.json` — 同上、子の自参照のみ許可。

  子の応答内容は `agent_runs.jsonl` の `agent_result_ref` / `agent_summary_ref` 経由で `agents/<child_arid>/dialogs/agent.result.json` / `agent.summary.txt` を読むこと。それ以外の workspace path は `python3 tools/orchestration_runtime.py run-gate --gate orchestration_read --agent-run-id <self_arid> --capability-token <capability_token> --args-json '{"read_path":"…"}'` を経由する。
- `orchestration agent` は UUID を独自生成してはならない。自身の `agent_run_id` は `tools/run_workflow.py` の `init_orchestration()` が `orchestration_meta.json` に記録済みであり、子 `agent_run_id` を発行する場合は `python3 tools/new_agent_run_id.py` を canonical 経路として使用する。`python3 -c 'import uuid; …'` は `forbid_python_inline_write` で fail-closed にブロックされ、`cat /proc/sys/kernel/random/uuid` および `uuidgen` は session sandbox の approval 要求で都度停止するため使用しない（詳細は `references/startup_contract.md` の `orchestration_agent_run_id` 取得手順を参照）。
- shell による file write は、対象 path が phase artifact かどうかを問わず禁止対象とし、事前 gate または `record-agent-run` が検出した場合は当該 gate または `record-agent-run` が当該 `agent_run` を reject しなければならない。reject 後は `orchestration agent` が `orchestration_meta.status=fail_closed` を記録して停止しなければならない。
- `step agent` / `substep agent` の起動要求本文には、input contract、expected output、保存先、失敗時停止条件、`spawn_agent` 義務を明示しなければならない。
- `step agent` / `substep agent` の起動要求本文には、`skill_name` と `skill_ref` と `skill_must_read_refs` を必須記録し、子 `agent` が起動直後に対象 `SKILL` を読める状態にしなければならない。
- `Compile verify` の起動要求では、`skill_must_read_refs` に `ir_ref` 配下の `spec.ir.yaml` を必須記録しなければならない。
- `Generate verify` の起動要求では、`skill_must_read_refs` に `ir_ref` 配下の `spec.ir.yaml` を必須記録しなければならない。
- `Validate execute` / `Validate judge` の起動要求では、`skill_must_read_refs` に `ir_ref` 配下の `spec.ir.yaml`、対象 `pipeline_ref` 配下の `source/<source_id>/source_meta.json`、`binary/<binary_id>/binary_meta.json` を必須記録しなければならない。
- `launch` 記録時に保存する prompt は、request payload の必須フィールド値と一致するテンプレート完全体でなければならない。要約 prompt や marker のみ保持した簡略 prompt を禁止する。
- `record-launch` に保存する `launch response` は、`spawn_agent` 成功直後の実応答完全体でなければならない。後生成、固定文言、要約文のみの代替を禁止する。
- `launch response` は子 `agent` 識別子を必須記録し、`record-agent-run` の `agent_session_id` は当該識別子と一致しなければならない。
- 上位 `node` の `Compile` を起動する前に、直下依存 `node` の `ir_ref` と `ir_meta.json.verification_status` を確認し、`direct dependency compile readiness` を満たすことを必須とする。
- 上位 `node` の `Generate` / `Build` / `Validate` を起動する前に、直下依存 `node` の `ir_ref` と `pipeline_ref` と最新 `aggregate_verdict` を確認し、`direct dependency execution readiness` を満たすことを必須とする。
- `workflow-launch-check` の dependency readiness 判定を通すため、`orchestration_meta.json.dependency_readiness` に `direct_dependency_compile_readiness` と `direct_dependency_execution_readiness` と `detail.ir_ref_verified` と `detail.pipeline_ref_verified` と `detail.aggregate_verdict_verified` を必須記録する。未記録または未充足の場合、`workflow-launch-check` は `fail_closed` を返す。
- 直下依存 `node` が未完了の場合、依存先 code を上位 `node` の `src/` へ内包する代替実装を指示してはならない。
- phase artifact を直接編集または `MCP` 実行する前に、`preflight` 済み、launch prompt 準備済み、child `agent` 起動済みの 3 条件を満たさなければならない。いずれかが未充足の場合は phase 本体の編集と実行を開始してはならない。
- child `agent` 起動前に path 予約が必要な場合は、`python3 tools/orchestration_runtime.py reserve-phase-root --repo-root <repo_root> --orchestration-id <orchestration_id> --node-key <node_key> --step <step> --reserved-id <id> --reserved-by-agent-run-id <agent_run_id>` を使用し、`workspace/ir/` と `workspace/pipelines/` の実体化を禁止する。
- workflow の正当性確認、検証、疎通確認を目的とした仮実装であっても、親 `agent` が子 `agent` 必須 phase の本体処理を代行してはならない。
- 子 `agent` 起動ごとに、起動要求本文を `launches/<agent_run_id>.prompt.txt`、起動返答本文を `launches/<agent_run_id>.reply.txt` へ保存し、`agent_runs.jsonl` の `launch_prompt_ref` と `launch_reply_ref` に参照を記録しなければならない。
- 各 `step agent` / `substep agent` の完了時に、`agents/<agent_run_id>/dialogs/agent.result.json` と `agents/<agent_run_id>/dialogs/agent.summary.txt` を保存し、`agent_runs.jsonl` の `agent_result_ref` と `agent_summary_ref` に参照を記録しなければならない。
- `agent.summary.txt` は最終 `status` と主要 `output_refs` または失敗原因を含む調査用ログとし、単一行の `pass` / `fail` のみで終えてはならない。
- 子 `agent` は、担当 `SKILL.md` が定める段階別 validator invocation を `python3 tools/orchestration_runtime.py run-gate --gate validate_pipeline_semantics --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '<json>'` 経由で当該 phase 完了前に実行し、成功または失敗の要約を `agent.summary.txt` に記録しなければならない。
- `openai.yaml` の表示名だけで orchestration 契約を満たしたとみなしてはならない。
- 子 `agent` の返却結果を評価した後、`issue_severity`（`minor` / `major` / `critical`）を判定し、再投入が必要な場合は `repair_strategy`（`reuse` / `restart`）を選択しなければならない。
- `orchestration agent` は phase artifact の repair を自身で直接実施してはならない。repair が必要な場合は、対象 `step` または `substep` の child `agent` へ再委譲しなければならない。
- `repair_strategy=reuse` は契約不変の局所修正に限定し、`repair_strategy=restart` は契約再解釈または広範囲再生成が必要な場合に選択しなければならない。
- 再投入時の起動要求には、`issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` を必須記録しなければならない。
- `Validate` 失敗時の retry 先（`Generate` / `Compile` / `Spec` / `Validate.execute`）は、`semantic_review.json#findings[*].attribution` と `verdict.json#failure_class` の組合せから deterministic に決定する（canonical mapping: `docs/workflow/phases/phase_04_validate.md` の「失敗時 retry の判定基準」節）。`Build` 失敗時は `binary_meta.json#failure_category` から deterministic に `Generate` の `repair_strategy` を決定する（canonical mapping: `docs/workflow/phases/phase_03_build.md` の retry trigger 節）。
- 再投入時は `repair_strategy` を問わず新規 `agent_run_id` を発行し、`repair_strategy=reuse` の場合のみ `agent_session_id` 再利用を許可する。
- `substep` を持つ phase の `write-step-result` において、`substep_agent_run_ids` は当該 `step` の `agent_runs.jsonl` 上の全 `substep` を欠落なく列挙しなければならない。終端 `status` が `pass` 以外の `substep` であっても `agent_run_id` を省略してはならない。補足は `docs/ORCHESTRATION.md` の運用ルール 20 と `docs/RUNBOOK.md` 1-3 を参照する。
- `retry_decisions` を含む `step_result.json` では、`repair_target_agent_run_id -> new_agent_run_id` の置換関係から `effective pass substep` 集合を一意に復元できなければならない。旧 failed run は履歴保持のため `substep_agent_run_ids` に残し、`status=pass` 判定対象からは除外する。`status=pass` の `step_result.json` では、各 `new_agent_run_id` は `effective pass substep` 集合へ残る最終採用 `pass` run に限り、後続 retry で再置換される連鎖 retry の中間 run を残してはならない。
- `orchestration_checkpoint.json` は `write-step-result` が `status=pass` で完了した後に `tools/orchestration_runtime.py` により自動更新される。`orchestration_checkpoint.json` を手動編集してはならない。
- `resume_enabled=true` を設定した orchestration では、`check-step-completed` の結果のみをスキップ判定の canonical source とする。`step_result.json` の直接参照でスキップを判断してはならない。
- `verify-checkpoint-integrity` で `stale` が検出された `step` をスキップしてはならない。

## 運用ルール
0. **Step 0 (TMPDIR セットアップ)**: orchestration agent は最初の `Bash` 実行**前に** `export TMPDIR=$(jq -er '.allowed_tmp_root' "workspace/orchestrations/$METDSL_ORCHESTRATION_ID/output_manifests/$ORCHESTRATION_AGENT_RUN_ID.json")` を実行する (`METDSL_ORCHESTRATION_ID` と `ORCHESTRATION_AGENT_RUN_ID` は `tools/run_workflow.py` が export 済み)。これを skip すると以降の `cat > $TMPDIR/...` 等の heredoc が `output_manifest_write_guard` でブロックされる。詳細手順は `references/startup_contract.md` の Step 0 を canonical source とする。子 `agent` も同等の Step 0 を起動直後に実施する義務がある (`references/launch_prompts.md` 参照)。
1. `python3 tools/run_workflow.py <spec_ref> <until_phase> --llm <backend>` を実行し、`workspace/orchestrations/<orchestration_id>/` の初期化、`preflight.json` 生成、起動 prompt 生成を一括で行う。
2. `tools/run_workflow.py` 以外の経路で workflow を開始してはならない。例外運用で `tools/orchestration_runtime.py` を直接実行する場合は、理由と差分を記録し、通常運用へ復帰しなければならない。
3. `preflight.json` の `probed_at` フィールドは、`record-launch` 実行時に TTL キャッシュの判定に使用され、TTL 期限切れ後の live probe 成功時に `tools/orchestration_runtime.py` によって自動更新される。この自動更新は `status` / `can_launch_*` 等の判定結果フィールドを変更しないため、手動編集禁止ルールの適用外とする。
4. `preflight.json` の `can_launch_step_agents=true` と `can_launch_substep_agents=true` を満たさない場合は workflow を開始しない。
5. `orchestration agent` は `references/startup_contract.md` を読んで起動条件を確定し、最初の `commentary` で対象 phase、使用する `SKILL`、起動する `agent` 種別、`MCP` 使用箇所を実行宣言する。
6. 実行宣言後に phase 種別を固定表で再確認し、`Compile` / `Generate` / `Validate` では `substep agent`、`Build` では `step agent` を選択する。
7. 起動要求本文と `skill_must_read_refs` は、`tools/orchestration_runtime.py` の `prepare_launch_request_payload` と `render_launch_prompt_text` に相当する canonical 生成規則で組み立てる。手作業連結での field 欠落、verify 必須 ref 欠落、prompt と request の値不一致を禁止する。
8. `record-launch` 実行前に、`ir_ref` と `pipeline_ref` と `dependency_ref` に placeholder が残存していないこと、`step=compile` の substep 起動要求（`generate` / `verify`）の `dependency_ref` が `spec/` で始まり `/deps.yaml` で終わる文字列であること（非空・非 placeholder を確認）、`verify` 起動要求の `skill_must_read_refs` が必須 IR section を網羅していること、起動要求本文に non-canonical な `tools/` / validator 参照禁止が含まれていることを検査する。
9. `Compile` の子 `agent` 起動前に、対象 `node` の直下依存 `node` ごとの `ir_ref` と `ir_meta.json.verification_status` を照合し、`direct dependency compile readiness` 不成立なら子 `agent` を起動せず `blocked` または `fail` を記録する。
10. `Generate` 以降の子 `agent` 起動前に、対象 `node` の直下依存 `node` ごとの `ir_ref` と `pipeline_ref` と `aggregate_verdict` を照合し、`direct dependency execution readiness` 不成立なら子 `agent` を起動せず `blocked` または `fail` を記録する。
11. phase 本体へ進む前に、`preflight` 済み、launch prompt 準備済み、child `agent` 起動済みの 3 条件を確認する。未充足なら編集、`MCP` 実行、phase artifact 生成を開始してはならない。
12. 生成した起動要求本文で子 `agent` を起動する。起動成功直後の実 `spawn_agent` 応答だけを `record-launch` で保存し、`launch_prompt_ref` と `launch_reply_ref` も同時に記録する。実起動前の仮記録、起動失敗後の補完記録、任意 `response_payload` の後投入を禁止する。
13. 子 `agent` 完了後は、次の順序で記録処理を実行する。順序を逆転させてはならない。`record-child-return` → `deactivate-child` の handshake は Adv-14/16/20 ガードに必須で、欠落すると runtime が `ValueError` で停止する。
    1. `RETURN_TOKEN=$(cat workspace/orchestrations/<orchestration_id>/launches/<agent_run_id>.parent_return_token)` で `record-launch` が発行した parent-bound token を取得する。
    2. `python3 tools/orchestration_runtime.py record-child-return --repo-root <repo_root> --orchestration-id <orchestration_id> --agent-run-id <agent_run_id> --return-token "$RETURN_TOKEN"` を実行し、Agent tool の return を観測した証跡 `child_returns/<agent_run_id>.txt` を記録する (Adv-30: 任意 caller による forge を防ぐ token 検証付き)。
    3. `deactivate-child` を実行して active context を orchestration agent へ切り戻す（ack 不在 or token 不一致では拒否される）。
    4. `record-reply` で `launches/<agent_run_id>.reply.txt` を Agent tool の最終応答テキストで上書き保存する。
    5. `python3 tools/orchestration_runtime.py record-agent-run --repo-root <repo_root> --orchestration-id <orchestration_id> --agent-run-json '<json>'` を実行して `agent_runs.jsonl` へ 1 行追記する。
    `record-agent-run` により `agent.result.json` と `agent.summary.txt` も同時に保存しなければならない。`record-agent-run` は、申告した `output_refs` と `apply_patch_writes` internal gate 記録と output manifest 記録に加えて baseline との差分で実変更 path を検査するため、capability token の `write_root` または gate 許可 path または manifest 許可 path に含まれない `unauthorized write` が存在する場合は当該 `agent_run` を reject する。reject 発生後は `orchestration agent` が `orchestration_meta.status=fail_closed` を記録して停止しなければならない。
14. `substep` を持つ phase では、返却結果を評価して `issue_severity` と `repair_strategy` を決定する。再投入が必要な場合は `repair_target_agent_run_id` と `repair_reason` を起動要求へ付与して再起動し、`record-launch` を追加する。
15. `repair_strategy=reuse` の再投入では、対象 `substep` の契約を変更せず差分修正だけを要求する。`repair_strategy=restart` の再投入では、対象 `substep` の契約入力から再生成させる。
16. 契約に反する近道を取りたくなった場合は、子 `agent` 起動必須であることを `commentary` で明示し、launch 手順へ戻る。ローカル実装を継続してはならない。
17. 標準 `substep` を持たない phase では `step agent` 完了後に、`substep` を持つ phase では `orchestration agent` 集約完了後に、`python3 tools/orchestration_runtime.py write-step-result --repo-root <repo_root> --orchestration-id <orchestration_id> --node-key <node_key> --step <step> --agent-run-id <agent_run_id> --result-json '<json>'` を実行する。再投入を実施した場合は `step_result.json` に `retry_decisions` を含める。`substep_agent_run_ids` には当該 `step` の `agent_runs.jsonl` に記録された **全** `substep` の `agent_run_id` を欠落なく含め、`fail` / `cancel` 等で終端した ID を省略してはならない。`status=pass` の `step_result` では、`retry_decisions` に記録する各 `new_agent_run_id` は `effective pass substep` 集合へ残る最終採用 `pass` run に限り、後続 retry で再置換される中間 run を含めてはならない。`effective pass substep` 集合の各 run が `pass` であり、`required_outputs` が当該集合の `output_refs` のみで被覆されることを必須とする。retry 前の failed run または superseded run の `output_refs` に依存してはならない。
18. workflow 終了時は `python3 tools/orchestration_runtime.py set-status --repo-root <repo_root> --orchestration-id <orchestration_id> --status <status>` を実行し、`orchestration_meta.json` を終端状態へ更新する。
19. `orchestration_meta.json` の `resume_enabled=true` が設定されている orchestration では、各 `step` / `node` の起動前に次の確認を行ってよい。`python3 tools/orchestration_runtime.py check-step-completed --repo-root <repo_root> --orchestration-id <orchestration_id> --node-key <node_key> --step <step>` を実行する。`completed=true` かつ `integrity=ok` が返却された場合、当該 `step` の起動をスキップし、返却された `ir_ref` / `pipeline_ref` / `output_refs` を後続 `step` へ渡す。`completed=false` または整合性が失敗している場合、当該 `step` を通常どおり実行する。スキップした `step` は `python3 tools/orchestration_runtime.py record-agent-run` で `agent_role=skipped_by_checkpoint` として新規 `agent_run_id` を発行し、`skipped_step` と `reason=checkpoint_integrity_ok` を含む最小限のエントリを `agent_runs.jsonl` へ追記しなければならない。`resume_enabled=false`（デフォルト）の orchestration では本ルールを適用せず、チェックポイントが存在しても全 `step` を新規実行しなければならない。
20. `preflight.json` を手動編集または後編集して `status` と `can_launch_*` を変更してはならない。検査条件の変化は `preflight` 再実行でのみ反映する。
21. `record-launch` 実行時に live preflight gate が `fail` の場合、当該起動を停止し、`set-status --status fail` のみを許可する。
22. `Compile` / `Generate` / `Build` / `Validate` の各子 `agent` 完了記録において、`step_result.json` の `validation_stage` フィールドに `validate_pipeline_semantics.py` で実行した `--stage` 値を記録しなければならない。`status=pass` の `step_result` では、当該 `step` に対応する許容 `validation_stage` 値（`compile`: `compile`/`full`、`generate`: `post_generate`/`post_build`/`full`、`build`: `post_build`/`full`、`validate`: `post_execute`/`pre_judge`/`full`）が記録されていない場合、`write-step-result` は失敗する。任意フローの `Tune` / `Promote` の `step_result` には `validation_stage` を要求しない。
23. 子 `Agent` tool が API stream idle timeout 等で途中切断された場合は、`record-agent-run` を `status=timeout` で手書きせず、次の順序で finalize する。各 step が欠落すると Adv-14/16/20 ガードにより `ValueError` で停止する。
    1. `record-child-return --agent-run-id <arid>`: Agent tool の return（タイムアウト終了応答を含む）を観測した証跡を記録。
    2. `deactivate-child --child-run-id <arid>`: active marker を解除（ack を消費）。
    3. `record-timeout --agent-run-id <arid> --reason "<text>"`: 終端 entry を記録、部分書き込み整合チェックと `workspace/tmp/<arid>/` 削除を実行。

    Agent tool が wedge して return を一切観測できない例外ケース（プロセス kill 等）に限り、`record-timeout --force-reason "<operator override 内容>"` で marker check を bypass できる。`forced=True` と `forced_reason` が audit 記録に残る。通常フローを優先し、`--force-reason` は最後の escape hatch として使う。詳細は `docs/RUNBOOK.md#substep-timeout-recovery` を参照。

## 参照
- 起動最小契約: `references/startup_contract.md`
- launch 要求テンプレート: `references/launch_prompts.md`
- CLI 全 subcommand reference: `docs/CLI_REFERENCE.md` (本 file 経由で `--help` を呼ばずに引数を確定する canonical source)
- workspace artifact 配置の tree 図と読み書きルール: `docs/WORKSPACE_LAYOUT.md`

## 判定基準
- `orchestration agent` が phase artifactsを直接生成していない。
- `workspace/orchestrations/<orchestration_id>/preflight.json` が存在し、`pass` 条件を満たしている。
- 最初の `commentary` に、対象 phase、使用 `SKILL`、起動 `agent` 種別、`MCP` 使用箇所の実行宣言が存在する。
- `agent_runs.jsonl` に `orchestration` と、必要に応じて `step` / `substep` の各ロールが記録されている。
- `Compile` / `Generate` / `Validate` が `substep agent`、`Build` が `step agent` で起動されている。
- `step` / `substep` の各 `agent_run` に対応する `agent.result.json` と `agent.summary.txt` が存在し、`agent_runs.jsonl` の参照値と一致している。
- `launches/` の要求と応答が `agent_runs.jsonl` の `launch_request_ref` / `launch_response_ref` と一致する。
- `launches/` と `agents/<agent_run_id>/dialogs/child.response.json` の応答が、同一の `spawn_agent` 実応答を保持している。
- `agent_runs.jsonl.agent_session_id` が、対応する `launch response` の子 `agent` 識別子と一致している。
- `launches/` の prompt と reply が `agent_runs.jsonl` の `launch_prompt_ref` / `launch_reply_ref` と一致する。
- `launches/` の prompt が `references/launch_prompts.md` の対応テンプレートを基底としており、テンプレート必須項目の欠落または意味変更が存在しない。
- `launches/` の request に placeholder ref が存在しない。
- `verify` の `launches/` request が、必須 IR section を `skill_must_read_refs` へ記録している。
- 子 `agent` の `launches/` prompt が、`tools/` 配下の実装、検証 `script`、test code、validator code を rule source として読むことを禁止している。
- `step_result.json` が `executor_agent_run_id` と `substep_agent_run_ids` を保持している。`substep` を持つ `step` では `substep_agent_run_ids` が当該 `step` の全 `substep` の `agent_run_id` を網羅している。
- `step_result.json` が `retry_decisions` を保持する場合、`effective pass substep` 集合を一意に復元でき、`status=pass` 判定および `required_outputs` 被覆判定が当該集合だけに基づいている。`status=pass` の場合、各 `new_agent_run_id` は最終採用された `pass` run であり、中間 retry run を含んでいない。
- 再投入を実施した場合、該当 `launch` 要求に `issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` が含まれている。
- 子 `agent` の全 `launch` 要求に `skill_name` と `skill_ref` と `skill_must_read_refs` が含まれている。
- `repair_strategy=reuse` と `repair_strategy=restart` の選択が、`ORCHESTRATION.md` の判定条件と一致している。
- `resume_enabled=true` の orchestration で `check-step-completed` を使用した場合、スキップした `step` が `agent_runs.jsonl` に `agent_role=skipped_by_checkpoint` で記録されている。
- スキップした `step` の `output_refs` が後続 `step` の `ir_ref` / `pipeline_ref` と整合している。
- `verify-checkpoint-integrity` で `valid=false` が返却されたとき、該当 `step` を再実行している。
- 子 `agent` 必須 phase で、child `agent` 起動前の phase artifact 直接編集、`MCP` 実行、検証目的の仮実装が存在しない。
- child `agent` の phase artifact 変更が、`.json` / `.txt` については `guarded-apply-patch` を、それ以外の extension については `output_manifests/<agent_run_id>.json.allowed_file_tool_paths` 内 path への `Edit` / `Write` 直接書き込みのみを使用しており、shell file write や manifest 外 path への書き込みを含む `unauthorized write` が `record-agent-run` または事前 gate で reject されている。
- `agent.summary.txt` が、単一行の定型 `pass` / `fail` のみではなく、最終状態と主要 `output_refs` または失敗原因を含んでいる。
