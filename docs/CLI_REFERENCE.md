# CLI Reference: `tools/orchestration_runtime.py`

## このドキュメントの位置づけ

`tools/orchestration_runtime.py` の全 subcommand の **canonical CLI reference** とする。本 file を参照すれば `--help` を呼ばずに必要な引数を確定できるよう、required / optional / JSON payload schema を網羅する。

`--help` の使用を **禁じてはいない** が、本 file が完備している場合は呼ぶ必要が無い。argparse 定義が更新されたら本 file も同期して更新する (副作用: `tools/orchestration_runtime.py` の編集レビュー時に CLI_REFERENCE への追記漏れを check する)。

関連 canonical source:
- workflow 全体の起動契約: `skills/workflow-orchestration/SKILL.md` および `skills/workflow-orchestration/references/startup_contract.md`
- 起動 prompt template: `skills/workflow-orchestration/references/launch_prompts.md`
- workspace artifact 配置: `docs/WORKSPACE_LAYOUT.md`
- hook 復旧 cheat sheet: `docs/RUNBOOK.md#hook-recovery`

## 共通規約

- `--repo-root` / `--orchestration-id` は (ほぼ) 全 subcommand で **required**。
- agent_run_id は UUID。新規発行は `python3 tools/new_agent_run_id.py` を canonical 経路とする (`python3 -c 'import uuid; …'` は hook policy で reject)。
- `node_key` の形式は `<spec_kind>/<spec_id>@<spec_version>` (例: `component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0`)。filesystem path ではない。
- `ir_id` / `pipeline_id` の形式は `<slug>_<YYYYMMDD>_<seq3>` (slug は hyphen-separated lowercase alphanumeric)。例: `flux-rsn-p0_20260425_001`。slug 中の underscore は invalid。
- ISO 8601 timestamp は UTC (`Z` suffix) を canonical とする。
- JSON 引数 (`--*-json`) は shell quoting に注意。複雑な payload は `--patch-file` のような file 指定を使う。

---

## subcommand 一覧

| subcommand | 用途 | section |
|---|---|---|
| `init` | orchestration 開始 / orchestration_meta.json 生成 | [init](#init) |
| `preflight` | execution platform 起動可否判定 | [preflight](#preflight) |
| `preflight-status` | 既存 preflight.json を読み返す | [preflight-status](#preflight-status) |
| `record-launch` | 子 agent 起動証跡 + capability_token + manifest 生成 | [record-launch](#record-launch) |
| `record-child-return` | Adv-20: Agent tool return ack 記録 | [record-child-return](#record-child-return) |
| `deactivate-child` | active_children marker 解除 | [deactivate-child](#deactivate-child) |
| `record-reply` | launches/<arid>.reply.txt を Agent tool 応答で上書き | [record-reply](#record-reply) |
| `record-agent-run` | agent_runs.jsonl へ 1 行追記 + agent.result.json/agent.summary.txt 保存 | [record-agent-run](#record-agent-run) |
| `record-timeout` | API stream idle timeout の canonical 復旧 | [record-timeout](#record-timeout) |
| `set-status` | orchestration_meta.json status 更新 | [set-status](#set-status) |
| `write-step-result` | step_result.json 生成と検証 | [write-step-result](#write-step-result) |
| `reserve-phase-root` | ir_id / pipeline_id の予約 (path 実体化はしない) | [reserve-phase-root](#reserve-phase-root) |
| `workflow-launch-check` | phase 着手前 gate (依存 readiness, agent 種別) | [workflow-launch-check](#workflow-launch-check) |
| `run-gate` | validator gate (validate_pipeline_semantics 等) を capability 越しに実行 | [run-gate](#run-gate) |
| `guarded-apply-patch` | `.json` / `.txt` への canonical 書き込み | [guarded-apply-patch](#guarded-apply-patch) |
| `orchestration-read` | manifest 外 path の gate-mediated read (通常は `run-gate --gate orchestration_read` 経由) | [orchestration-read](#orchestration-read) |
| `read-checkpoint` | orchestration_checkpoint.json 取得 | [read-checkpoint](#read-checkpoint) |
| `verify-checkpoint-integrity` | checkpoint と artifact hash の照合 | [verify-checkpoint-integrity](#verify-checkpoint-integrity) |
| `check-step-completed` | resume_enabled で当該 step の完了を確認 | [check-step-completed](#check-step-completed) |

---

## init

orchestration を開始し `workspace/orchestrations/<orchestration_id>/orchestration_meta.json` を生成する。通常は `tools/run_workflow.py` 経由で起動するため直接呼び出すことは少ない。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | repo root への絶対パス |
| `--orchestration-id` | yes | `orch_YYYYMMDDTHHMMSSZ_<8hex>` 形式 |
| `--spec-ref` | no | 対象 spec への path |
| `--source-dependency-ref` | no | `spec/.../deps.yaml` |
| `--status` | no | 既定 `running` |
| `--agent-backend` | no | `codex` / `claude` / `cursor`。既定 `codex` |
| `--resume-from-checkpoint` | no | flag。既存 orchestration の resume を有効化 (`resume_enabled=true`) |

返値: `orchestration_agent_run_id` を含む JSON object (stdout)。

---

## preflight

execution platform 起動可否を probe し `preflight.json` を書く。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--backend` | no | `codex` / `claude` / `cursor`。既定 `codex` |
| `--agent-command` | no | backend 起動 command (path 上書き) |
| `--codex-command` | no | 既定 `codex` |
| `--claude-command` | no | 既定 `claude` |

返値: `status`, `can_launch_step_agents`, `can_launch_substep_agents`, `feature_states`, `checks` を含む JSON。

## preflight-status

既存 `preflight.json` の現在状態を JSON で返す。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |

---

## record-launch

子 agent 起動の最重要 entry point。capability_token・sandbox_profile・output_manifest・read_manifest を生成し `launches/<child_agent_run_id>.{request,response,prompt,reply}.txt` を書く。

**Claude Code では `Agent` tool 起動の前**に呼ぶこと (子 agent が起動直後に capabilities/<arid>.json を Read する必要があるため)。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--parent-agent-run-id` | yes | orchestration agent (親) の UUID |
| `--child-agent-run-id` | yes | 事前発行した子 UUID。Claude Code では `agent_session_id` も同値となる |
| `--request-json` | yes | 子起動 request payload (下記 schema) |
| `--response-json` | yes | spawn 応答 payload (下記 schema) |
| `--relation-type` | no | 既定 `launch` |

### `--request-json` payload (主要 field)

| field | required | 内容 |
|---|---|---|
| `agent_role` | yes | `step` または `substep` |
| `node_key` | yes | `<spec_kind>/<spec_id>@<spec_version>` |
| `step` | yes | `compile` / `generate` / `build` / `validate` (core 5-phase)。Tune / Promote は任意フローで別 entrypoint |
| `substep` | substep agent では yes | Compile / Generate: `generate` / `verify`。Validate: `execute` / `judge` |
| `orchestration_id` | yes | |
| `agent_run_id` | yes | child_agent_run_id と一致 |
| `parent_agent_run_id` | yes | |
| `workflow_mode` | yes | `dev` / `prod` |
| `ir_ref` | yes | `workspace/ir/<node_key_safe>/<ir_id>` (Compile phase 含めて全 phase で必須) |
| `pipeline_ref` | yes | `workspace/pipelines/<node_key_safe>/<pipeline_id>` (Compile phase でも必須。未生成なら先に `reserve-phase-root --step generate` で予約) |
| `dependency_ref` | yes | Compile: `spec/.../deps.yaml`、Generate 以降: workspace 内 phase root |
| `skill_name` | yes | `workflow-<step>` または `workflow-<step>-<substep>` |
| `skill_ref` | yes | `skills/<skill_name>/SKILL.md` |
| `allowed_output_paths` または `required_outputs` または `output_refs` | step/substep では 1 つ必須 | 書き込み許可 path のリスト |
| `allowed_file_tool_paths` | optional | `Edit` / `Write` 直接書き込み path。`allowed_output_paths` の subset |
| `run_id` | Validate step では yes | 実行 ID (1 launch につき 1 つ pin) |
| `source_id` | Generate substep / Validate / Build (cross-phase Make) では yes | Generate 出力を識別 |
| `source_binary_id` | Validate step では yes | 使用する `binary_id` |

### `--response-json` payload (Claude Code)

```json
{
  "agent_run_id": "<child_agent_run_id>",
  "agent_session_id": "<child_agent_run_id>",
  "started_at": "<ISO8601>",
  "backend": "claude"
}
```

`sandbox_runtime` / `sandbox_enforced` / `sandbox_profile_ref` は record-launch が自動付与する。

---

## record-child-return

Adv-20: orchestration agent が `Agent` tool の return を観測した証跡 (`child_returns/<arid>.txt`) を記録。`deactivate-child` の前提。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--agent-run-id` | yes | 子 agent の UUID |
| `--return-token` | yes | Adv-30: `workspace/orchestrations/<orch>/launches/<arid>.parent_return_token` の値。`$(cat <path>)` で渡す |
| `--reply-excerpt` | no | 任意短文 (200 chars 截断)。audit 用 |

---

## deactivate-child

active context を orchestration agent に切り戻す。`record-child-return` の ack が無いと `ValueError` で reject される。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--child-run-id` | yes | 子 agent の UUID |

---

## record-reply

`launches/<arid>.reply.txt` を `Agent` tool の最終応答テキストで上書き。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--agent-run-id` | yes | 子 agent の UUID |
| `--reply-text` | 1 つ必須 | 直接テキスト |
| `--reply-from-stdin` | 1 つ必須 | flag。stdin から読む (大きい reply 用) |

---

## record-agent-run

`agent_runs.jsonl` へ 1 行追記。step/substep role では `agent.result.json` と `agent.summary.txt` も保存。capability の write_root に含まれない `unauthorized write` を検出した場合 reject。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--agent-run-json` | yes | 下記 schema |

### `--agent-run-json` payload

| field | required | 内容 |
|---|---|---|
| `agent_run_id` | yes | UUID |
| `agent_role` | yes | `orchestration` / `step` / `substep` |
| `agent_backend` | yes | `claude` / `codex` / `cursor` |
| `status` | yes | `running` / `pass` / `fail` / `blocked` / `timeout` / `cancel` |
| `started_at` | yes | ISO 8601 |
| `agent_session_id` | step/substep では yes | Claude Code では `agent_run_id` と同値 |
| `context_id` | step/substep では yes | unique UUID |
| `context_isolated` | step/substep では yes | `true` (Claude Code) |
| `node_key` | step/substep では yes | |
| `finished_at` | terminal status では yes | ISO 8601 |
| `output_refs` | `pass` では yes | 書き込んだ artifact path のリスト |
| `issue_severity` | optional | `minor` / `major` / `critical` |

---

## record-timeout

`Agent` tool の API stream idle timeout 等の canonical 復旧経路。`launches/<arid>.{request,response}.json` を読んで `record-agent-run` を `status=timeout` で代行 invoke。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--agent-run-id` | yes | 子 agent の UUID |
| `--reason` | yes | 理由テキスト (例: `"API stream idle timeout after 600s"`) |
| `--force-reason` | no | Adv-26: active marker check を bypass。`record-child-return → deactivate-child` が成功しない wedge ケース専用 |

---

## set-status

`orchestration_meta.json` の `status` / `reason_code` / `reason_detail` / `blocking_policy_scope` を更新。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--status` | yes | `pass` / `fail` / `fail_closed` / `blocked` / 等 |
| `--reason-code` | no | snake_case 識別子 (例: `compile_verify_shape_expr_invalid`) |
| `--reason-detail` | no | 自由文 |
| `--blocking-policy-scope` | no | `sandbox` / `verify` / `dependency` 等 |

---

## write-step-result

`workspace/orchestrations/<orch>/steps/<node_key_safe>/<step>/<arid>/step_result.json` を生成し validation を実行。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--node-key` | yes | |
| `--step` | yes | |
| `--agent-run-id` | yes | step を執行した primary agent (orchestration agent for substep-aware phases) |
| `--result-json` | yes | 下記 schema |

### `--result-json` payload

| field | required | 内容 |
|---|---|---|
| `status` | yes | `pass` / `fail` / `blocked` / `timeout` / `cancel` |
| `required_outputs` | yes | list[str] |
| `executor_agent_run_id` | yes | UUID |
| `substep_agent_run_ids` | yes | list[str]。substep を持つ phase は全 substep の UUID を欠落なく含める |
| `failed_substeps` | optional | list[str] |
| `retry_decisions` | optional | list[object]。各 item: `{issue_severity, repair_strategy, repair_target_agent_run_id, new_agent_run_id, repair_reason}` |
| `validation_stage` | compile/generate/build/validate の terminal status では yes | `compile` / `post_generate` / `post_build` / `post_execute` / `pre_judge` / `full` のいずれか (step 別の許容値あり) |

---

## reserve-phase-root

`ir_id` または `pipeline_id` を予約。実 directory は作成しない (子 agent が作成する)。Compile phase の launch 前に `--step compile` (ir_id 予約) と `--step generate` (pipeline_id 予約) の両方が必要。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--node-key` | yes | |
| `--step` | yes | `compile` で ir_id 予約、`generate` で pipeline_id 予約 |
| `--reserved-id` | yes | `<slug>_<YYYYMMDD>_<seq3>` (slug 中の underscore は invalid; hyphen を使う) |
| `--reserved-by-agent-run-id` | yes | 予約 ID を使う agent の UUID |

---

## workflow-launch-check

phase 着手前 gate。execution platform 可用性、session policy、依存 readiness、必要な child agent 種別を check する。最初の phase 着手前に 1 回実行。`status=fail_closed` を返したら `set-status` で停止する。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--node-key` | yes | |
| `--step` | yes | `compile` / `generate` / `build` / `validate` (core 5-phase) |
| `--backend` | no | 既定 `codex` |
| `--require-child-agent` | yes | `step` または `substep`。Compile / Generate / Validate は `substep`、Build は `step` |
| `--launch-request-json` | no | downstream artifact check 用の launch request payload |

返値: `{"status": "pass"|"fail_closed", "next_action": "...", ...}`。

---

## run-gate

validator gate を capability_token 越しに実行。validator 直接呼び出しを禁じる context での canonical 経路。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--gate` | yes | `validate_pipeline_semantics` / `check_artifact_syntax` / `validate_workspace_root` / `orchestration_read` |
| `--agent-run-id` | yes | 子 agent の UUID |
| `--args-json` | yes | gate 別 schema (下記) |
| `--capability-token` | yes | `capabilities/<agent_run_id>.json#capability_token` |

### `--args-json` schema (gate 別)

| gate | schema |
|---|---|
| `orchestration_read` | `{"read_path": "docs/..."}` |
| `validate_workspace_root` | `{"paths": ["workspace"]}` (optional, defaults to repo workspace) |
| `check_artifact_syntax` | `{"expect_top": "object", "paths": ["workspace/.../file.yaml", ...]}` |
| `validate_pipeline_semantics` | `{"stage": "compile|post_generate|post_build|post_execute|pre_judge|full", "ir_ref": "workspace/ir/..." (compile stage), "pipeline_root": "workspace/pipelines/..." または list, "source_id": "<id>" (optional)}` |

key は CLI flag に変換される (`pipeline_root` → `--pipeline-root`)。

stderr の最終行に gate 結果 JSON (`status`, `violations`, ...) が出力される。`2>"${TMPDIR}/last_gate_stderr.txt"` で保存して参照する。

---

## guarded-apply-patch

`.json` / `.txt` 出力の唯一の canonical 書き込み経路。allowed_output_paths に列挙された path に unified diff を適用する。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--actor-role` | yes | `step` / `substep` / `orchestration` |
| `--agent-run-id` | yes | UUID |
| `--paths-json` | yes | JSON list of path strings (例: `'["workspace/ir/.../ir_meta.json"]'`) |
| `--patch-text` | 1 つ必須 | unified diff 本文 (inline) |
| `--patch-file` | 1 つ必須 | unified diff を含む file への path (大型 patch では OS ARG_MAX 回避のため必須) |
| `--capability-token` | yes | |

---

## orchestration-read

manifest 外 path の gate-mediated read。通常は `run-gate --gate orchestration_read --args-json '{"read_path": "..."}'` 経由で呼ぶ。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--agent-run-id` | yes | UUID |
| `--read-path` | yes | 読む path |
| `--capability-token` | yes | |

---

## read-checkpoint

`workspace/orchestrations/<orch>/orchestration_checkpoint.json` を読んで返す。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |

---

## verify-checkpoint-integrity

checkpoint に記録された artifact hash と現状を照合。`stale` 検出時はその step を skip してはならない。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |

---

## check-step-completed

`resume_enabled=true` の orchestration で、対象 step の完了状態を確認 (canonical な skip 判定経路)。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--node-key` | yes | |
| `--step` | yes | |
| `--skip-integrity-check` | no | flag (testing only) |

返値: `{"completed": bool, "integrity": "ok"|"stale"|...}`。`completed=true && integrity=ok` のときに限り skip 可。
