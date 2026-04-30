# Workflow Orchestration

この文書は、`workflow` 全体を統括する `orchestration agent` と、phase unit / substep unit の独立エージェント実行規約を定義する。

## 目的
- `workflow` 実行を階層化し、phase responsibilities と監査責務を分離する。
- 各 `step` / 各 `substep` を独立エージェントとして実行し、実行経路を追跡可能にする。

## 適用範囲
- `Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote`
- `node workflow` 単位の phase 実行と、phase 内 `substep`（例: `generate` / `verify`）の実行

## term rules
- `phase` は `docs/workflow/WORKFLOW_CORE.md` と `docs/workflow/phases/` 配下の契約文書で定義する workflow の論理単位を指す。
- `step` は 1 つの phase に対応するオーケストレーション上の実行単位を指す。
- `substep` は `step` を分解した下位実行単位を指す。
- `stage` は `generated_by_stage` や `<stage>_meta.json` など既存フィールド名または既存プレースホルダー名としてのみ使用する。本文では `phase` または `step` の同義語として使用してはならない。

## 要件
- `workflow` 実行は、必ず 1 つの `orchestration agent` を最初に起動して開始する。
- `workflow` 開始前に、`step agent` と `substep agent` を独立起動できる execution platform の preflight を必須実行しなければならない。preflight は `multi_agent` 機能と子 `agent` 起動可否を検証対象に含め、`pass` でない場合は `workflow` を開始してはならない。
- `backend=codex` の preflight は、`feature_states.codex_hooks=true` と `checks.codex_hooks_enabled.pass=true` と `checks.codex_home_writable.pass=true` を同時に満たさなければならない。いずれかが未充足または未定義の場合、`workflow` を開始してはならない。
- preflight は `sandbox_runtime=bwrap` と `sandbox_enforced=true` を必須条件に含めなければならない。`checks.sandbox_bwrap_available.pass=true` または `checks.sandbox_bwrap_userns.pass=true` または `checks.sandbox_bwrap_exec.pass=true` の少なくとも 1 つを満たさない場合、`workflow` を開始してはならない。
- native hook 実行時の `codex_hooks` feature 判定は、`orchestration_id` ごとに最初の 1 回だけ実行し、結果を `workspace/orchestrations/<orchestration_id>/hooks/codex_feature_check.json` へキャッシュしなければならない。
- `preflight.json` の手動編集または後編集による `pass` 化を禁止する。preflight 結果は実行時検査の一次証跡としてのみ記録しなければならない。
- 子 `agent` 起動直前に、execution platform の live probe で `multi_agent` と子 `agent` 起動可否を再検査しなければならない。live probe は `record-launch` 実行時に適用し、`fail` の場合は `record-launch` と子 `agent` 起動を禁止し、当該 `workflow` を `fail` へ遷移させなければならない。
- 各 phase の着手前に `workflow-launch-check` を実行し、required child `agent` 種別判定、execution platform 可否、session policy 可否、dependency readiness を同時に検査しなければならない。dependency readiness は `orchestration_meta.json.dependency_readiness` を canonical source とし、`direct_dependency_plan_readiness` と `direct_dependency_execution_readiness` と `detail`（`plan_ref_verified` と `pipeline_ref_verified` と `aggregate_verdict_verified`）を検査する。`dependency_readiness` 未記録または未充足のいずれも `fail_closed` とし、phase 本体へ進めてはならない。
- 各 phase の着手前に、対象 phase が `step agent` 必須か `substep agent` 必須かを phase 種別で明示判定しなければならない。`Plan` / `Generate` / `Tune` は `substep agent` 必須、`Build` / `Execute` / `Judge` / `Promote` は `step agent` 必須とする。
- phase 着手前判定で子 `agent` 必須と確定した場合、親 `agent` は `spawn_agent` 完了前に phase artifact 生成、`MCP` 実行、検証目的の仮実装、依存 code の一時内包を開始してはならない。
- `workspace/plans/` と `workspace/pipelines/` の phase artifact root は、`record-launch` と capability token と `phase_state=child_running` の 3 条件を満たした child `agent` だけが実体化できる。`orchestration agent` による直接生成を禁止する。
- child `agent` 起動前に root path 予約が必要な場合は、`workspace/orchestrations/<orchestration_id>/reservations/<node_key_safe>/<step>.json` の reservation artifact のみを生成し、`workspace/plans/` と `workspace/pipelines/` の実ディレクトリを作成してはならない。
- `orchestration agent` は `workflow` 全体の進行制御のみを担当し、phase 本体の artifact（例: `case.resolved.yaml`、`diagnostics.json`）を直接生成してはならない。
- `workflow` 実行の代替として、複数 phase の進行と artifact generation を一括自動化する `script`（例: `python` / `bash`）を新規生成または実行してはならない。
- `orchestration` の責務を `script` へ委譲してはならない。`Build` / `Execute` / `Judge` / `Promote` の各 `step` は必ず `spawn_agent` で起動した独立 `step agent` で実行しなければならない。
- `Plan` / `Generate` / `Tune` のように `substep` を持つ各 phase は、`orchestration agent` が `generate` と `verify` などの各 `substep agent` を `spawn_agent` で直接起動しなければならない。
- child `agent` に許可する phase artifact の変更は、capability token が許可した `write_root` 配下に限定しなければならない。`plan_ref` / `pipeline_ref` 配下の変更は、`.json` / `.txt` 出力については `guarded-apply-patch` を通過した canonical path、それ以外の extension については `output_manifests/<agent_run_id>.json` の `allowed_file_tool_paths` に列挙された path への `Edit` / `Write` 直接書き込みに限定し、許可 root 外の変更と manifest 外 path への書き込みを禁止する。
- `record-launch` は child `agent_run_id` ごとに `workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json` を生成し、`allowed_output_paths` と `allowed_file_tool_paths` と `allowed_tmp_root`（`workspace/tmp/<agent_run_id>`）を確定しなければならない。`allowed_file_tool_paths` は `Edit` / `Write` tool で直接書き込み可能な path 集合を保持し、既定では `allowed_output_paths` から `.json` / `.txt` の extension を除いた集合を自動収録する。`allowed_tmp_root` 配下への書き込みは `allowed_output_paths` の個別列挙なしで許可しなければならない。`guarded-apply-patch` と `record-agent-run` は当該 manifest を必須参照し、`allowed_output_paths` 外かつ `allowed_tmp_root` 外への変更、および `Edit` / `Write` による `allowed_file_tool_paths` 外（`allowed_tmp_root` 配下を除く）path への書き込みを reject しなければならない。
- `orchestration agent` の `agent_run_id` に対応する `output_manifest` は、`workspace/orchestrations/<orchestration_id>/failure_analysis.json` を `allowed_output_paths` と `allowed_file_tool_paths` の両方へ明示登録しなければならない。これにより `failure_analysis.json` への `Edit` / `Write` / `apply_patch` を許可し、`allowed_file_tool_paths` 外 path への `apply_patch` は reject しなければならない。
- `record-launch` は child `agent_run_id` ごとに `workspace/orchestrations/<orchestration_id>/read_manifests/<agent_run_id>.json` を生成し、`allowed_read_roots` と `denied_read_roots` を確定しなければならない。`orchestration_read` は当該 manifest を canonical source とし、manifest 外 path を reject しなければならない。
- `record-launch` は child `agent_run_id` ごとに `workspace/orchestrations/<orchestration_id>/sandbox_profiles/<agent_run_id>.json` を生成し、`bwrap` 実行に必要な `read_roots` と `write_roots` と runtime bind 構成を確定しなければならない。child 起動は当該 profile を用いた `bwrap` 実行のみを許可し、非 sandbox 実行を禁止しなければならない。
- `step agent` / `substep agent` は phase artifact を変更する場合、出力 path の extension で書き込み経路を分岐しなければならない。`.json` / `.txt` の出力は `apply_patch_writes` gate を通過した `guarded-apply-patch` を canonical invocation とし、`.yaml` / `.yml` / `.md` / source code 等の上記以外の extension は `output_manifests/<agent_run_id>.json.allowed_file_tool_paths` に列挙された path への `Edit` / `Write` 直接書き込みを canonical invocation とする。通常 `apply_patch` の直接実行、shell redirection、`tee`、`sed -i`、`perl -0pi`、`python` / `sh` / `bash` による file write、`allowed_file_tool_paths` 外への直接書き込みなど、いずれの canonical invocation にも含まれない file write を禁止する。
- `guarded-apply-patch` の疎通確認は dry-run または no-op patch で実施しなければならない。dummy file を作成して疎通確認してはならない。
- shell による file write は、対象 path が phase artifact かどうかを問わず、child `agent` 起動要求で明示した canonical invocation に含まれない限り禁止しなければならない。shell file write を事前 gate または `record-agent-run` が検出した場合、当該 gate または `record-agent-run` は当該 `agent_run` を reject しなければならない。reject 後は `orchestration agent` が `orchestration_meta.status=fail_closed` を記録して停止しなければならない。
- `step agent` と `substep agent` は、同一 `LLM` コンテキストを共有してはならない。各 `agent_run_id` は固有の `context_id` を持ち、`context_isolated=true` を必須記録とする。
- `orchestration agent` は `substep` を持つ phase で必要な `substep` 群を起動し、完了判定を行った後に `step_result.json` を確定しなければならない。
- `orchestration agent` は `deps.yaml` と `spec_catalog.yaml` から再構成した依存関係と依存充足条件に基づいて `step agent` または `substep agent` の起動可否を判定しなければならない。
- すべての `agent` 実行は `agent_run_id` を持ち、入力参照・出力参照・親子関係を記録しなければならない。
- `agent_runs.jsonl` の各行は `started_at` と `status` を必須記録とし、`status` が終端状態（`pass` / `fail` / `blocked` / `timeout` / `cancel`）の場合は `finished_at` を必須記録とする。
- `fail_closed` は `orchestration_meta.status` の終端状態としてのみ使用する。`agent_runs.jsonl.status` の終端語彙へ追加してはならない。
- `step` / `substep` ロールの `agent_runs.jsonl` は `parent_agent_run_id` と `agent_backend` と `agent_model` と `context_id` と `context_isolated` と `agent_session_id` と `launch_request_ref` と `launch_response_ref` と `launch_prompt_ref` と `launch_reply_ref` と `agent_result_ref` と `agent_summary_ref` を必須記録とする。
- `substep agent` の `parent_agent_run_id` は、当該 `substep` を起動した `orchestration agent_run_id` を指すことを許可する。
- `spawn_agent` の応答で得た子 `agent` 識別子は `agent_session_id` として記録しなければならない。
- `launches/<agent_run_id>.response.json` と `agents/<agent_run_id>/dialogs/child.response.json` の canonical source は、子 `agent` 起動直後に得た `spawn_agent` 実応答としなければならない。後生成、要約再構成、固定文言による代替を禁止する。
- `step` / `substep` ロールの `agent_runs.jsonl.agent_session_id` は、対応する `launch response` に含まれる子 `agent` 識別子と一致しなければならない。手書き `session_id`、連番仮値、親 `agent` 推定値を禁止する。
- `launch_request_ref` と `launch_response_ref` は `workspace/orchestrations/<orchestration_id>/launches/` 配下を参照し、参照先実体が存在しなければならない。
- `launch_prompt_ref` と `launch_reply_ref` は `workspace/orchestrations/<orchestration_id>/launches/` 配下を参照し、参照先のテキスト証跡が存在しなければならない。
- `agent_result_ref` と `agent_summary_ref` は `workspace/orchestrations/<orchestration_id>/agents/<agent_run_id>/dialogs/` 配下を参照し、起動後の最終状態、成果物参照、失敗要約を調査できる一次証跡として存在しなければならない。
- `launches/<agent_run_id>.request.json` には `launch_prompt_ref` を、`launches/<agent_run_id>.response.json` には `launch_reply_ref` を保持し、`agent_runs.jsonl` の参照値と一致させなければならない。
- `agent_graph.json` の `edge` は、`orchestration -> step` または `orchestration -> substep` を canonical source とする。互換運用として `step -> substep` を許容してもよいが、`substep` を親ロールとする `edge` を禁止する。
- `agent` 実行の失敗、`timeout`、`cancel` はメタデータへ記録し、推測補完で継続してはならない。
- `orchestration agent` は子 `agent` の完了待機中に当該子 `agent` の責務を代行してはならない。標準 `substep` を持たない phase では `step agent` も同様に子 `agent` の責務を代行してはならない。
- `workflow` の正当性確認、検証、疎通確認、暫定回避を目的としても、親 `agent` が子 `agent` 必須 phase の本体処理を代行してはならない。`leaf node` を先にローカル実装してから正規経路へ戻す運用を禁止する。
- `orchestration agent` は、子 `agent` の返却結果を評価して `issue_severity`（`minor` / `major` / `critical`）を判定しなければならない。
- `orchestration agent` は、`issue_severity` と契約逸脱範囲に基づいて再投入要否を判定し、再投入が必要な場合は `repair_strategy`（`reuse` / `restart`）を選択しなければならない。
- `orchestration agent` は、phase artifact の repair を自身で直接実施してはならない。repair が必要な場合は、対象 `step` または `substep` の child `agent` へ再委譲しなければならない。
- `repair_strategy=reuse` は、対象 `step` または `substep` の input contract と expected output を変更せず、局所修正で収束可能な場合にのみ選択してよい。
- `repair_strategy=restart` は、契約再解釈、設計再構成、広範囲再生成のいずれかが必要な場合に選択しなければならない。
- 再投入時は `repair_strategy` を問わず、新規 `agent_run_id` と新規 `context_id` を発行しなければならない。
- `repair_strategy=reuse` の場合、`agent_session_id` は再利用してよい。
- `repair_strategy=restart` の場合、`agent_session_id` は新規発行しなければならない。
- 再投入時の `launches/<agent_run_id>.request.json` は、`issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` を必須記録としなければならない。
- `agent_runs.jsonl` と `agent_graph.json` は、実行中イベントを逐次追記して生成しなければならない。workflow 完了後に固定値テンプレートを一括出力する運用を禁止する。
- `agent_runs.jsonl` と `agent_graph.json` と `step_result.json` を後生成または手動整形して独立実行を偽装してはならない。起動時に記録した一次証跡との突合で整合しない試行は `fail` とする。
- `record-launch` は、`spawn_agent` 成功直後の request/response 保存専用処理としなければならない。実起動前の予約記録、実起動失敗後の補完記録、任意 `response_payload` の後投入を禁止する。
- `record-launch` は launch response に `sandbox_runtime=bwrap` と `sandbox_enforced=true` と `sandbox_profile_ref` を記録しなければならない。`record-agent-run` は当該項目を必須検証し、未充足時は `sandbox_enforcement_violation` として reject しなければならない。
- `orchestration agent` は、子 `agent` 起動時に `docs/workflow/WORKFLOW_CORE.md` と対象 `step` に対応する `docs/workflow/phases/phase_*.md` を canonical source として、対象 `step` または `substep` の `execution input` と `verification input` と `expected output` を明示しなければならない。`step agent` を使用する phase では `step agent` も自身の契約入力と expected output を明示しなければならない。
- `orchestration agent` は、子 `agent` 起動要求に要求定義と判定規則の canonical source が `docs/` と `spec/` と当該試行 artifact であることを明示しなければならない。`tools/` 配下の実装、検証 `script`、test code、validator code を読んで rule を抽出する指示または黙示を禁止する。
- 子 `agent` の validator invocation は `run-gate` を原則とし、`python3 tools/orchestration_runtime.py run-gate --gate <gate_name> --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '<json>'` を canonical invocation としなければならない。
- validator script の直接実行は例外運用としてのみ許可する。許可条件は「対象が read-only 検査であり、`capability_token` と gate 判定を要求しないこと」を同時に満たす場合に限定し、許可対象は `validate_workspace_root.py` と `check_artifact_syntax.py` に限定する。許可条件外の直接実行は `fail` とする。
- 子 `agent` が `apply_patch` を実行する場合、`python3 tools/orchestration_runtime.py guarded-apply-patch --repo-root <repo_root> --orchestration-id <orchestration_id> --actor-role <step|substep> --agent-run-id <agent_run_id> --paths-json '["..."]' --patch-text '<patch_text>' --capability-token <capability_token>` を canonical invocation とし、`run-gate --gate apply_patch_writes` と通常 `apply_patch` の分離実行を禁止する。`guarded-apply-patch` の使用対象は `.json` / `.txt` 出力に限定する。
- `apply_patch_writes` は `guarded-apply-patch` 内部処理専用 `gate` とし、`run-gate --gate apply_patch_writes` を公開経路として使用してはならない。
- `record-agent-run` は、child `agent` が申告した `output_refs` と `apply_patch_writes` gate 記録、および `output_manifests/<agent_run_id>.json.allowed_file_tool_paths` に加えて、baseline との差分で実変更 path を検査しなければならない。実変更 path が capability token の `write_root` 配下にない、または gate 許可 path と `allowed_file_tool_paths` のいずれにも含まれない場合、`unauthorized write` として reject しなければならない。`apply_patch_writes` gate の被覆検査は `output_refs` のうち `.json` / `.txt` 出力にのみ要求し、`allowed_file_tool_paths` で許可された direct write 対象 path は当該検査の対象外とする。reject 発生時は `orchestration agent` が `orchestration_meta.status=fail_closed` を記録して停止しなければならない。
- `orchestration agent` は、子 `agent` 起動要求本文を `skills/workflow-orchestration/references/launch_prompts.md` の対応テンプレートから生成しなければならない。`step agent` には `step agent` 起動要求テンプレート、`substep agent` には `substep agent` 起動要求テンプレートを適用し、テンプレートを使わない任意の自由形式 prompt を禁止する。
- 起動要求本文のテンプレート必須項目は、省略、改名、意味変更をしてはならない。追加記述は、テンプレート必須項目と矛盾せず、対象 `step` または `substep` の契約具体化に必要な情報に限定しなければならない。
- `plan_ref` と `pipeline_ref` と `dependency_ref` は、子 `agent` 起動前に canonical path を確定しなければならない。`<agent-determined-...>` などの placeholder を起動要求へ記録してはならない。
- `launches/<agent_run_id>.request.json` の各必須フィールド値と `launches/<agent_run_id>.prompt.txt` の対応行は一致しなければならない。要約 prompt、再構成 prompt、テンプレート marker のみを残した省略 prompt を禁止する。
- `skills/*/agents/<platform>.yaml` などの表示名または説明文だけで独立 `agent` 起動契約を満たしたとみなしてはならない。起動要求本文に子 `agent` 起動ツール（`spawn_agent` または `Agent` tool、execution platform に依存）の使用義務、input contract、expected output、保存先、失敗時停止条件を明示しなければならない。execution platform ごとの起動ツール対応は `CLAUDE.md` の「execution platform 別の子 `agent` 起動ツール」を参照する。

## 設計方針
- 単一責務: 1 つの `agent` は 1 つの責務のみを持つ。
- 階層委譲: `orchestration agent -> step agent` と `orchestration agent -> substep agent` の 2 系統で制御する。
- 契約駆動: 子 `agent` 起動時は input contract と output contract を固定し、契約外の読み書きを禁止する。
- 追跡可能性: すべての起動・終了イベントを時系列で保存し、再実行時に同一判断を再現可能にする。

## オーケストレーション指示契約
### 共通必須項目
- `orchestration agent` は、子 `agent` への起動要求に `orchestration_id` と `agent_run_id` と `parent_agent_run_id` と `node_key` と `step` と `substep`（存在する場合）と `plan_ref` と `pipeline_ref` と `dependency_ref` を必須記録しなければならない。
- 子 `agent` への起動要求本文は `skills/workflow-orchestration/references/launch_prompts.md` の対応テンプレートを基底とし、テンプレート内プレースホルダーを対象 `agent_run` の実値で置換して生成しなければならない。
- 起動要求本文と `skill_must_read_refs` は、同一 request payload から機械的に再生成可能でなければならない。手作業連結や後編集で request と prompt の値を乖離させてはならない。
- 子 `agent` への起動要求には、`execution input` と `verification input` と `expected output` と `write_root` と `read_roots` を必須記録しなければならない。
- `execution input` は当該 `agent` が artifact を生成するために直接参照してよい入力に限定しなければならない。
- `verification input` は当該 `agent` が pass/fail 判定、整合確認、依存確認にのみ使用してよい入力として明示しなければならない。
- `expected output` はファイル名、保存先、更新責務を含めて明示しなければならない。親 `agent` は `expected output` に含まれない artifact を子 `agent` へ要求してはならない。
- 親 `agent` は入力不足時に推測補完を指示してはならない。不足入力がある場合は `fail-fast` 停止を指示しなければならない。
- 子 `agent` への起動要求には `skill_name` と `skill_ref` と `skill_must_read_refs` を必須記録し、子 `agent` が起動直後に対象 `SKILL` を読める状態を保証しなければならない。
- 子 `agent` への起動要求には、`tools/` 配下の実装、検証 `script`、test code、validator code が canonical source ではないことと、要求不足時はそれらから逆算補完せず `fail-fast` 停止することを明示しなければならない。
- `step` ごとの具体的な `execution input` と `verification input` と `expected output` は `docs/workflow/WORKFLOW_CORE.md` と対応する `docs/workflow/phases/phase_*.md` を canonical source とし、親 `agent` は対象 `step` 節の定義を参照して起動要求へ展開しなければならない。
- `substep` ごとの具体的な `execution input` と `verification input` と `expected output` は、対応 `SKILL.md` と `docs/workflow/WORKFLOW_CORE.md` と対応する `docs/workflow/phases/phase_*.md` の両方を参照して決定しなければならない。`WORKFLOW_CORE.md` および `phases/` に明示された phase contract と矛盾する `substep` 契約を定義してはならない。
- `Build` / `Execute` / `Judge` / `Promote` のように現行標準で `substep` を定義しない `step` では、`orchestration agent` は `step` 契約をそのまま単一 `step agent` へ渡さなければならない。
- `Plan generate/verify`、`Generate generate/verify`、`Tune generate/verify` のように `substep` を持つ `step` では、`orchestration agent` は `step` 契約を分解したうえで、対応 `SKILL.md` の責務境界に一致する `substep` 契約だけを直接渡さなければならない。
- `Plan verify substep` の契約には、`dependency.resolved.yaml` の網羅性検証、依存辺整合検証、依存先 `node` の `plan` 文書との照合検証を必ず含めなければならない。
- `Plan verify` の起動要求では、`skill_must_read_refs` に `plan_ref` 配下の `case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` を必須記録しなければならない。不足時は起動前に `fail_closed` とする。
- `Generate verify` の起動要求では、`skill_must_read_refs` に `plan_ref` 配下の `case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` と `derived_contract.json` に加えて、`pipeline_ref` を基準とする相対パスとして `lineage.json` と `generate/<generation_id>/generate_meta.json` を必須記録しなければならない。不足時は起動前に `fail_closed` とする。
- `plan_ref` は `workspace/plans/<node_key_safe>/<plan_id>` のみとし、追加のパスセグメント（ファイルパスを含む）を付けてはならない。`<plan_id>` は `<node_key_safe>_` で始まるディレクトリ名とする。
- `pipeline_ref` は `workspace/pipelines/<node_key_safe>/<pipeline_id>` のみとし、追加のパスセグメント（`generate/` や `generate_meta.json` を含む）を付けてはならない。`<pipeline_id>` は `<node_key_safe>_` で始まるディレクトリ名とする。この制約は `pipeline_ref` フィールド値にのみ適用し、`skill_must_read_refs` には `pipeline_ref` 配下 artifact の相対パス記録を許可する。
- `dependency_ref` は phase ごとに canonical path を固定しなければならない。`Plan` は `spec/.../deps.yaml`、`Generate` 以降は `workspace/...` の phase root（`plan_ref` または `pipeline_ref`）を記録し、`spec` 直参照を禁止する。この規則は `Plan` phase の `generate` と `verify` の両 `substep` に適用し、`verify` は `generate` と同一の `dependency_ref` を受け取らなければならない。`validate_workspace_root` gate はこの規則を検証する。
- `Generate verify` の起動要求では、`generation_id` を必須記録しなければならない。`record-launch` は上記の `plan_ref` / `pipeline_ref` 形と `generation_id` と `skill_must_read_refs` 充足を検査する。
- `step agent` / `substep agent` が `pass` で終了するとき、`output_refs` の各パスは、対応する起動要求に記録された `plan_ref` または `pipeline_ref` ディレクトリ配下に含まれなければならない。`record_agent_run` がこれを検査する。

## 運用ルール
1. `workflow` 開始時に `orchestration_id` を発行し、`workspace/orchestrations/<orchestration_id>/orchestration_meta.json` を作成する。
2. `workflow` 開始前に preflight 結果を `workspace/orchestrations/<orchestration_id>/preflight.json` へ記録し、`can_launch_step_agents=true` と `can_launch_substep_agents=true` と `sandbox_enforced=true` を同時に満たさない場合は `fail` として停止する。
3. 各 phase の着手前に phase 種別を確認し、`Plan` / `Generate` / `Tune` では `substep agent`、`Build` / `Execute` / `Judge` / `Promote` では `step agent` を起動対象として確定する。判定結果と不一致の実行経路を開始してはならない。
4. `orchestration agent` は `step agent` または `substep agent` の起動要求ごとに `launches/<agent_run_id>.request.json` と `launches/<agent_run_id>.response.json` と `launches/<agent_run_id>.prompt.txt` と `launches/<agent_run_id>.reply.txt` を保存し、`agent_runs.jsonl` の `launch_request_ref` と `launch_response_ref` と `launch_prompt_ref` と `launch_reply_ref` へ参照を記録する。
5. `record-launch` に保存する `response.json` と `child.response.json` は、`spawn_agent` 実応答の完全保存とし、子 `agent` 識別子を欠落させてはならない。
6. 各 `step agent` と各 `substep agent` の完了時には、`agents/<agent_run_id>/dialogs/agent.result.json` と `agents/<agent_run_id>/dialogs/agent.summary.txt` を保存し、`agent_runs.jsonl` の `agent_result_ref` と `agent_summary_ref` から追跡可能にしなければならない。
7. `agent.summary.txt` には、少なくとも最終 `status` と失敗要因または主要成果物参照を含め、調査時に `agent_runs.jsonl` だけでは不足する文脈を補完しなければならない。単一行の定型 `pass` / `fail` のみを禁止する。
8. `launches/<agent_run_id>.prompt.txt` は `skills/workflow-orchestration/references/launch_prompts.md` の対応テンプレートを具体化した本文としなければならない。テンプレート必須項目の欠落、別テンプレート混用、自由形式への全面置換を禁止する。
9. `orchestration agent` は `deps.yaml` と `spec_catalog.yaml` と `dependency.resolved.yaml` を照合し、`spec` 依存関係に基づく実行キューを確定する。`dependency.resolved.yaml` は整合確認と依存参照に使用し、実行順序決定の canonical source にしてはならない。
10. `orchestration agent` は起動対象ごとに `step agent` または `substep agent` を発行し、`node_key` と `step` と `plan_ref` と `pipeline_ref` と `dependency_ref` を入力として渡す。
11. `orchestration agent` は上位 `node` の `Plan` を起動する前に、直下依存 `node` ごとの `plan_ref` と `plan_meta.json.verification_status` を照合し、`direct dependency plan readiness` を満たさない場合は起動してはならない。
12. `orchestration agent` は上位 `node` の `Generate` 以降を起動する前に、直下依存 `node` ごとの `plan_ref` と `pipeline_ref` と最新 `aggregate_verdict` を照合し、`direct dependency execution readiness` を満たさない場合は起動してはならない。
13. `direct dependency plan readiness` または `direct dependency execution readiness` を満たさない場合、`orchestration agent` は当該 `node` を `blocked` または `fail` として記録し、依存 `node` の未完了を親 `node` の `Plan` または `Generate` で代替してはならない。
14. `orchestration agent` は `step` を持つ phase では対象 `step` の `execution input` と `verification input` と `expected output` を明示し、`substep` を持つ phase では対象 `substep` の `execution input` と `verification input` と `expected output` を明示しなければならない。
15. `substep` を持つ phase では、`orchestration agent` が `generate` と `verify` などの `substep agent` を逐次起動する。
16. `substep agent` は自身の artifact と対応 phase のメタデータを生成し、`agent_output_ref` を `orchestration agent` へ返却する。
17. `orchestration agent` は子 `agent` の返却結果を評価し、`issue_severity` と再投入要否を確定する。再投入が必要な場合は `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` を確定する。
18. 再投入が必要で `repair_strategy=reuse` の場合、`orchestration agent` は同一 `agent_session_id` の継続修正を許可してよい。この場合も新規 `agent_run_id` を発行し、`relation_type` を `reuse` として `record-launch` 記録を追加しなければならない。
19. 再投入が必要で `repair_strategy=restart` の場合、`orchestration agent` は新規 `agent_session_id` を持つ `substep agent` を再起動し、`relation_type` を `restart` として `record-launch` 記録を追加しなければならない。
20. `orchestration agent` は `substep` を持つ phase で全 `substep` の必須 artifact を検証し、`workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/step_result.json` へ `step_result.json` を出力する。この場合の `agent_run_id` は `orchestration agent_run_id` とする。`step_result.json` の `substep_agent_run_ids` は、当該 `step` で起動して `agent_runs.jsonl` に記録された **全** `substep` の `agent_run_id` を欠落なく列挙しなければならない。`pass` 以外で終端した `substep`（`fail` / `cancel` 等）を省略してはならない。
21. `step_result.json` は、再投入を実施した場合に `retry_decisions` 配列を保持し、各要素へ `issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `new_agent_run_id` と `repair_reason` を記録しなければならない。`retry_decisions` は retry 履歴の canonical source とし、`repair_target_agent_run_id` から `new_agent_run_id` への置換関係を一意に復元できなければならない。`status=pass` の `write-step-result` に記録する `retry_decisions` では、各 `new_agent_run_id` は `effective pass substep` 集合へ最終採用された `pass` run に限らなければならない。後続 `retry_decisions.repair_target_agent_run_id` で再度置換される中間 run を `new_agent_run_id` として残してはならない。
22. `noncanonical_phase_write_attempt` を起因とする再投入では、`repair_strategy=restart` を必須とし、`reuse` を選択してはならない。違反 run の output を後続 run で再利用してはならない。
23. `substep` を持つ phase の `step_result.json` における `status=pass` 判定は、`substep_agent_run_ids` 全件ではなく `retry_decisions` を適用した最終採用集合、すなわち `effective pass substep` 集合に対して行わなければならない。`effective pass substep` 集合は、`substep_agent_run_ids` に含まれる `agent_run_id` のうち、後続 `retry_decisions.repair_target_agent_run_id` として置換されていない run と、置換先 `new_agent_run_id` を反映した最終 run の集合と定義する。`status=pass` の `step_result.json` では、`retry_decisions` は `effective pass substep` 集合へ残る最終 run だけを保持しなければならない。中間 retry run を経由した連鎖置換を `status=pass` の `step_result.json` に残してはならない。
24. `status=pass` の `step_result` では、`effective pass substep` 集合に含まれる各 run が `pass` で終端していなければならない。retry 前の failed run は `substep_agent_run_ids` と `retry_decisions` に履歴として保持してよいが、`status=pass` 判定対象へ含めてはならない。
25. `status=pass` の `step_result` における `required_outputs` 被覆判定は、`effective pass substep` 集合の `output_refs` のみを対象に行わなければならない。retry 前の failed run または superseded run の `output_refs` に依存して `required_outputs` を満たしてはならない。
26. `step agent` は標準 `substep` を持たない phase で自身の artifact を検証し、`workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/step_result.json` へ `step_result.json` を出力する。
27. `orchestration agent` は `step_result.json` を受け取り、次 `step` の起動可否を判定する。
28. `node` 実行は `deps.yaml` と `spec_catalog.yaml` から再構成した依存順で逐次実行する。依存関係を持つ `node` は依存 `node` の完了前に起動してはならない。独立 `node` の並列実行は、workflow 入力または orchestration 指示で明示的に許可された場合にのみ開始してよい。明示指示がない場合、`orchestration agent` は独立 `node` を逐次起動しなければならない。
28. `step agent` または `substep agent` が `fail` / `timeout` / `cancel` の場合、当該 `node` の当該 `step` を `fail` とし、下流 `step` 起動を禁止する。
29. `orchestration agent` は各 `agent` 実行イベントを `workspace/orchestrations/<orchestration_id>/agent_runs.jsonl` へ追記しなければならない。
30. `orchestration agent` は親子関係を `workspace/orchestrations/<orchestration_id>/agent_graph.json` へ保存し、`parent_agent_run_id` と `child_agent_run_id` と `relation_type` を必須記録とする。
31. `Promote` 以外の `agent` は `workspace/` 配下以外へ書き込んではならない。
32. `workflow` 実行時に `step` / `substep` の実処理を `script` で代行した場合は `fail` とし、当該試行を破棄しなければならない。
33. 再投入時は新規 `agent_run_id` を発行し、既存 `launch` 証跡や `agent_runs` 行を上書きしてはならない。`agent_session_id` の扱いは `repair_strategy` 規則に従う。
34. `preflight.json` の手動編集または後編集で `status` と `can_launch_*` を変更してはならない。変更が必要な場合は `preflight` を再実行して新しい検査結果を記録しなければならない。
35. 子 `agent` 起動直前の live probe が `fail` の場合、`record-launch` を実行してはならない。`orchestration_meta.status=fail` を記録して停止しなければならない。`record-agent-run`（`step` / `substep`）と `write-step-result` は `preflight.json` の整合確認を満たす場合のみ実行してよい。
36. `record-launch` が実行する live probe は、`METDSL_PREFLIGHT_TTL_SECONDS` で設定した TTL（デフォルト 30 分）以内に成功済みのプローブが存在する場合はスキップされる。この最適化は `METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT=1` が明示設定されている場合は無効化され、常に live probe が実行される（後方互換）。
37. `preflight.json` の `probed_at` フィールドは live probe 成功時に自動更新される。`status` / `can_launch_*` を含むその他フィールドの変更は引き続き禁止する。
38. native hook の実行結果は `workspace/orchestrations/<orchestration_id>/hooks/native_hook_events.jsonl` へ追記し、`backend` と `event` と `action` と `reason` を追跡可能にしなければならない。
39. native hook 実行 payload に `orchestration_id` が存在しない場合の動作は `METDSL_MISSING_ORCHESTRATION_ID_POLICY` で制御する。`strict` の場合はすべての hook event でエラーとして失敗させなければならない。未設定（既定）の場合は `workspace/orchestrations/_global/hooks/native_hook_events.jsonl` へのフォールバック記録を許可する。`tools/run_workflow.py` はワークフロー起動時に `METDSL_MISSING_ORCHESTRATION_ID_POLICY=strict` を設定し、orchestration_id なしの hook 実行を禁止しなければならない。
40. 子 `agent` 必須 phase で契約に反する近道へ逸脱しそうな場合、`orchestration agent` は当該 phase が子 `agent` 起動必須であることを明示し、正規の起動手順へ復帰しなければならない。逸脱を理由とするローカル継続実装を禁止する。
41. `write-step-result` が `status=pass` で完了した後、`orchestration_checkpoint.json` が `tools/orchestration_runtime.py` により自動更新される。`orchestration_checkpoint.json` の手動編集を禁止する。
42. `resume_enabled=true` の orchestration において、`orchestration agent` は `check-step-completed` を各 `step` 起動前に実行し、`completed=true` かつ `integrity=ok` の場合のみ当該 `step` のスキップを許可する。
43. チェックポイントによりスキップした `step` は `agent_runs.jsonl` に `agent_role=skipped_by_checkpoint` として記録しなければならない。
44. `resume_enabled=false` の orchestration（未設定を含む）では `orchestration_checkpoint.json` を信頼して `step` をスキップしてはならない。`docs/workflow/WORKFLOW_CORE.md` の該当ハードニング規範における「明示的な指定」は `orchestration_meta.json` の `resume_enabled=true` のみが満たす。
45. `write-step-result` の `status=pass` では、`Generate` / `Build` / `Execute` / `Judge` の `step_result.json` に `validation_stage` を必須記録しなければならない。許容値は `Generate: post_generate|full`、`Build: post_build|full`、`Execute: post_execute|pre_judge|full`、`Judge: pre_judge|full` とする。`Plan` と `Tune` は `validation_stage` を必須にしない。
46. `codex_feature_check.json` の `status_kind=probe_error`（timeout や command failure）の結果は永続固定してはならない。`METDSL_HOOK_FEATURE_RETRY_TTL_SECONDS`（既定 30 秒）経過後に再プローブを許可しなければならない。
47. `workspace/tmp/<agent_run_id>/` は各 `orchestration agent` / `substep agent` / `step agent` の一時作業領域として使用できる。`orchestration agent` の `agent_run_id` は `orchestration_meta.json` の `orchestration_agent_run_id` と一致する。ファイル書き込みは `output_manifest` の `allowed_tmp_root`（`workspace/tmp/<agent_run_id>`）で許可される。`record-agent-run` は当該 `agent_run` 記録後に `workspace/tmp/<agent_run_id>/` を自動削除する。`tools/run_workflow.py` は `init` 成功後に環境変数 `TMPDIR` を `workspace/tmp/<orchestration_agent_run_id>/` に設定し、当該 `run_workflow` プロセス終了時に当該ディレクトリのみ削除する。`bwrap` による child `agent` 起動では、`sandbox_profile` の `rendered_command` に `workspace/tmp/<agent_run_id>` を読み書き可能にバインドし `TMPDIR` を同一絶対パスへ設定する `bubblewrap` 引数を含めなければならない。並行 orchestration 間で `workspace/tmp/` 全体を共有掃除してはならない。

## 判定基準
- `workflow` ごとに `orchestration_id` が発行され、`orchestration_meta.json` が存在する。
- 各 `step` または各 `substep` が独立 `agent_run_id` を持つ。
- `step` と `substep` の `context_id` が重複せず、全件で `context_isolated=true` が記録される。
- `step` と `substep` の `agent_runs.jsonl` に `agent_session_id` と `launch_request_ref` と `launch_response_ref` と `launch_prompt_ref` と `launch_reply_ref` と `agent_result_ref` と `agent_summary_ref` が記録され、参照先実体が存在する。
- `launches/<agent_run_id>.response.json` と `agents/<agent_run_id>/dialogs/child.response.json` が `spawn_agent` 実応答の同一内容を保持し、子 `agent` 識別子を欠落させていない。
- `agent_runs.jsonl.agent_session_id` が、対応 `launch response` の子 `agent` 識別子と一致する。
- `launches/<agent_run_id>.request.json` の `launch_prompt_ref` と `launches/<agent_run_id>.response.json` の `launch_reply_ref` が `agent_runs.jsonl` の参照値と一致する。
- `launches/<agent_run_id>.prompt.txt` が `skills/workflow-orchestration/references/launch_prompts.md` の対応テンプレートを基底としており、テンプレート必須項目の欠落または意味変更が存在しない。
- 子 `agent` の全 `launches/<agent_run_id>.request.json` に `skill_name` と `skill_ref` と `skill_must_read_refs` が記録されている。
- `preflight.json` が存在し、`can_launch_step_agents=true` と `can_launch_substep_agents=true` を満たす。
- `backend=codex` の preflight では、`feature_states.codex_hooks=true` と `checks.codex_hooks_enabled.pass=true` と `checks.codex_home_writable.pass=true` が記録されている。
- preflight では `sandbox_runtime=bwrap` と `sandbox_enforced=true` と `checks.sandbox_bwrap_available.pass=true` と `checks.sandbox_bwrap_userns.pass=true` が記録されている。
- `preflight.json` の `pass` 条件と、子 `agent` 起動直前 live probe の `pass` 条件が同時に満たされる。
- 各 phase の実行記録から、`Plan` / `Generate` / `Tune` は `substep agent`、`Build` / `Execute` / `Judge` / `Promote` は `step agent` を使用したことを追跡できる。
- `agent_graph.json` で `orchestration -> step` または `orchestration -> substep` の親子関係を追跡できる。
- `agent_runs.jsonl` から `queued` / `running` / `pass` / `fail` / `blocked` / `timeout` / `cancel` の遷移を追跡できる。
- `hooks/native_hook_events.jsonl` から native hook の decision を追跡できる。
- `step_result.json` の `executor_agent_run_id` が当該ディレクトリ名と一致し、`substep_agent_run_ids` が親子関係と整合する。標準 `substep` を持たない phase では `substep_agent_run_ids=[]` を許可する。
- `substep` を持つ `step` では、`agent_runs.jsonl` に記録された当該 `step` の全 `substep` の `agent_run_id` が、いずれかの `step_result.json` の `substep_agent_run_ids` に含まれる。欠落時は `tools/orchestration_runtime.py` の orchestration 完了検査で `fail` となる。
- `step_result.json` の `required_outputs` が `docs/workflow/WORKFLOW_CORE.md` および対応する `docs/workflow/phases/phase_*.md` の phase contract と一致する。
- `step_result.json` が `retry_decisions` を保持する場合、`repair_target_agent_run_id` と `new_agent_run_id` の置換関係から `effective pass substep` 集合を一意に復元できる。
- `step_result.json` が `status=pass` の場合、各 `retry_decisions.new_agent_run_id` は `effective pass substep` 集合へ残る最終採用 `pass` run であり、後続 retry で再置換される中間 run を含んでいない。
- `step_result.json` が `status=pass` の場合、`effective pass substep` 集合に含まれる各 run が `pass` で終端している。
- `step_result.json` が `status=pass` の場合、`required_outputs` は `effective pass substep` 集合の `output_refs` のみで被覆されている。retry 前の failed run または superseded run の `output_refs` に依存していない。
- `step_result.json` が `status=pass` の `Generate` / `Build` / `Execute` / `Judge` では、`validation_stage` が phase ごとの許容値に一致する。
- 再投入を実施した `substep` は、対応する `launches/<agent_run_id>.request.json` に `issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` を保持している。
- `repair_strategy=reuse` の再投入を実施した場合、対象 `agent_run` の `agent_session_id` は `repair_target_agent_run_id` の `agent_session_id` と一致する。
- `repair_strategy=restart` の再投入を実施した場合、対象 `agent_run` の `agent_session_id` は `repair_target_agent_run_id` の `agent_session_id` と一致しない。
- `step_result.json` が `retry_decisions` を保持する場合、各 `new_agent_run_id` が `substep_agent_run_ids` と `agent_graph.json` の親子関係に含まれている。
- 失敗試行で推測補完や人工 artifact generation を行わず、当該 `step` を停止している。
- 子 `agent` 必須 phase で、親 `agent` による検証目的の仮実装、依存 code の一時内包、`MCP` 実行代行が存在しない。
- 各 `step_result.json` の `executor_agent_run_id` が `orchestration` または `step` ロールの実行記録と対応し、`script` 実行ログのみで phase 完了を主張していない。
- `agent.summary.txt` が、単一行の定型 `pass` / `fail` のみではなく、最終状態と主要 `output_refs` または失敗要因を保持している。
