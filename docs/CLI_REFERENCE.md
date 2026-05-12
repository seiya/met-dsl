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
| `mark-dependency-readiness` | dependency_readiness detail flag を更新し top-level を派生 | [mark-dependency-readiness](#mark-dependency-readiness) |
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

**呼び出し規約**

- **同一 `agent_run_id` の二重登録は不可。** 既存行と同じ `agent_run_id` で `record-agent-run` を再 invoke すると `ValueError: duplicate agent_run_id: <id>` を raise する。idempotent ではないため、再試行する場合は `python3 tools/new_agent_run_id.py` で新規 `agent_run_id` を採番し直し、`record-launch` から sequence をやり直す。回復手順の詳細は [docs/RUNBOOK.md#duplicate-agent_run_id-recovery](RUNBOOK.md#duplicate-agent_run_id-recovery)。
- **orchestration agent 自身の entry は orchestration 起動直後に 1 回 append する**。`agent_role=orchestration`、`status=running` で append し、終了時に同じ `agent_run_id` の行を更新しない。orchestration の terminal 状態は `set-status` で `orchestration_meta.json` 側に表現する（finalize 操作の正規経路は [set-status](#set-status) を参照）。

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

`orchestration_meta.json` の `status` / `reason_code` / `reason_detail` / `blocking_policy_scope` を更新。**orchestration finalize / finalization の正規 entrypoint** であり、`finalize_orchestration` 等の別 subcommand は存在しない。`pass` / `fail` / `fail_closed` への遷移が orchestration 全体の terminal 操作となり、`agent_runs.jsonl` の orchestration 行を後から更新する経路は無い（orchestration の終了状態は本コマンド経由で `orchestration_meta.json` 側に表現する）。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--status` | yes | `pass` / `fail` / `fail_closed` / `blocked` / 等 |
| `--reason-code` | no | snake_case 識別子 (例: `compile_verify_shape_expr_invalid`) |
| `--reason-detail` | no | 自由文 |
| `--blocking-policy-scope` | no | `sandbox` / `verify` / `dependency` 等 |

**呼び出し規約**

- 同一 terminal 値の再呼び出しは at-least-once retry を許容する **idempotent な操作**。replay は status-specific の precondition (`pass` の preflight/completion 検証、`fail_closed` の reason_code 必須) を再実行しない (lock 取得後の terminal-replay 検出が status-specific 検証より先に行われる)。**replay 時に phase_state_log.jsonl に canonical `set_status` event が無い場合 (元の forward 呼び出しが meta + marker commit 後に log append で失敗した場合)、persisted meta の reason_code / reason_detail / blocking_policy_scope から backfill されてから replay 完了する**。backfill された event は `backfilled: true` フラグ付き:
  - `cleanup_committed/<orch_arid>.json` marker 未書き込み → cleanup retry (`_cleanup_agent_tmp_root` + marker 書き込みのみ再実行、narrative fields は不変)。`phase_state_log.jsonl` に `event=set_status_cleanup_retry`。
  - marker 書き込み済み (fully committed) → no-op replay (既存 meta をそのまま返す)。`phase_state_log.jsonl` に `event=set_status_noop_replay`。defensive retry や response loss 後の reissue が `ValueError` にならないことを保証。
- narrative fields (`reason_code` / `reason_detail` / `blocking_policy_scope`) は 1 回目の `set-status` で固定する。再呼び出しは narrative を上書きしない。narrative の追記は `workspace/orchestrations/<orchestration_id>/failure_analysis.json` を直接編集する (orchestration agent の `allowed_file_tool_paths` に登録済み)。
- terminal 間遷移で唯一許容されるのは `fail` → `fail_closed` (live preflight gate fail 後に fail_closed を確定する流れ)。それ以外の terminal-to-terminal 遷移は `ValueError` で reject される。
- 並行 terminalizer から呼ばれることを想定し、read-check-write-cleanup-marker の critical section は `orchestration_meta.json.lock` 上の fcntl `LOCK_EX` で serialize される (POSIX 環境)。`write_preflight` の `orchestration_meta.json` 更新と `mark-dependency-readiness` の更新は同じロックを共有するため、`mark-dependency-readiness` で verified された flag が並行 preflight に上書きされる race は発生しない。
- `phase_state_log.jsonl` の canonical `set_status` event は **commit 後の `orchestration_meta.json` から読み返した値** (`.strip()` 正規化後、`fail → fail_closed` 昇格後等) を記録する。raw call arguments でなく persisted state を audit するため、forward 書き込みと replay backfill が同一 shape になり、recovery / postmortem tooling での乖離が起きない。

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

### dependency_readiness (orchestration_meta.json) の canonical field

`workflow-launch-check` が参照する依存 readiness の canonical key を以下に定義する。`preflight` サブコマンドが orchestration_meta 初期化時に書き込み、`mark-dependency-readiness` (下記) が verification 完了後に flag を更新する。

| key | step | 内容 |
|---|---|---|
| `dependency_readiness.direct_dependency_compile_readiness` | `compile` | 直下依存 node の `ir_ref` と `ir_meta.json.verification_status` が満たされているか |
| `dependency_readiness.direct_dependency_execution_readiness` | `generate` / `build` / `validate` | 直下依存 node の `ir_ref` / `pipeline_ref` / 最新 `aggregate_verdict` が満たされているか |
| `dependency_readiness.detail.ir_ref_verified` | `compile` 以降 | |
| `dependency_readiness.detail.pipeline_ref_verified` | `generate` 以降 | |
| `dependency_readiness.detail.aggregate_verdict_verified` | `validate` | |

未記録または `true` 以外の値が記録されている場合、`workflow-launch-check` は `fail_closed` (`reason_detail=direct_dependency_<step>_readiness_not_pass` または `dependency_readiness_detail_not_pass:<key>`) を返す。stored `dep_set_fingerprint` と現在計算した fingerprint が一致しない場合は `reason_detail=dep_set_fingerprint_stale` で reject される。**さらに launch-time で deps.yaml が存在する場合、gate は live recompute を行い、persisted booleans でなく recomputed booleans を authoritative として判定する**: `orchestration_meta.json` を直接編集して flag を forge しても、live recompute が false を返せば gate は reject する。live recompute が失敗した場合、production は **fail-closed** で具体的な reason を返す: `deps_yaml_missing_or_unparseable` (deps.yaml 不在または YAML パース失敗) と `deps_yaml_malformed_schema` (schema が malformed) は区別される (Codex round 25 F2)。test scaffolding が persisted booleans への legacy fallback を必要とする場合は環境変数 `METDSL_DEP_READINESS_ALLOW_PERSISTED_FALLBACK=1` で opt-in する (production 環境では設定しない)。PyYAML は deps.yaml / spec_catalog.yaml を parse する経路 (`_read_deps_yaml`, `_load_spec_catalog_from_bytes`) のみで lazy resolve される (Codex round 27 F1)。未 install 時はこれら関数の最初の YAML touch で `RuntimeError` (install hint 付き) が呼び出し元に伝播し silent fail-closed を防ぐ一方、`set-status` / `record-timeout` / `workflow-launch-check` (leaf 経路) などの recovery / non-dep commands は PyYAML 不在でも実行可能であり、control-plane outage を回避する。`_dependency_ready` の meta read + fingerprint recompute は `mark-dependency-readiness` / `write_preflight` / `update_orchestration_status` と同じ `orchestration_meta.json.lock` 上の fcntl `LOCK_EX` で serialize されており (Codex round 27 F2)、writer と reader が同時に走っても torn read で `dep_set_fingerprint_stale` を誤検出することはない。参照: `skills/workflow-orchestration/SKILL.md` items 60-62。

**初期値の計算ルール** (`_compute_initial_dependency_readiness`):
- `orchestration_meta.spec_ref` 配下に `deps.yaml` が存在し `dependencies.components` と `dependencies.profiles` の双方が空 → 全フラグ `true` (vacuous truth; leaf node 用)
- `spec_ref` 未設定、`deps.yaml` 不在、または非空依存が記載 → 全フラグ **`false` (fail-closed)**。`workflow-launch-check` は phase 起動を block する。
- 非自明な依存を持つ node は、orchestration agent が SKILL.md item 60-61 の手順で検証した後、`mark-dependency-readiness` で明示的に flag を立てる。

**dep_set_fingerprint と invalidation 規約**:
- 各 `dependency_readiness` には `dep_set_fingerprint` が記録される。fingerprint は以下を SHA-256 でまとめたもの:
  - `spec_ref` (normalized)
  - `<spec_ref>/deps.yaml` の bytes
  - **deps-relevant な catalog subset**: deps.yaml に登場する `(spec_kind, spec_id)` pair のみについて、catalog の sorted version list を deterministic JSON で表現した bytes (全 catalog bytes ではない。Codex round 19 F1 — 無関係な spec publication で全 orchestration を invalidate しない)
  - 各 certified direct dep について、`(spec_kind, spec_id, spec_version, stage)` 識別子 + 該当 stage の最新 workspace artifact bytes (`ir_meta.json` / `binary_meta.json` / `aggregate_verdict.json`)。識別子 prefix は Codex round 19 F2 — artifact bytes が偶然一致しても certified version drift を検出する
- 検査タイミング:
  - `write_preflight` 再実行時: fingerprint mismatch 検出で `dependency_readiness` を初期値 (leaf=trivial-true / non-leaf=fail-closed) にリセット (`phase_state_log.jsonl` に `event=dependency_readiness_invalidated` で記録)。
  - `_dependency_ready` (launch-time gate): fingerprint mismatch を `reason=dep_set_fingerprint_stale` で reject。preflight 再実行を待たず即時検出。
- これにより以下のすべての drift シナリオで gate が通過することを防ぐ: spec_ref 差し替え / deps.yaml 編集 / spec_catalog.yaml の drift (新 matching version 追加 / 既存 version 削除 / constraint 解決が ambiguous に変化) / **post-mark の dep artifact regression** (新しい ir_meta.json / binary_meta.json / aggregate_verdict.json で verification_status や verdict が変化)。
- fingerprint が一致する場合:
  - leaf node (computed が trivial-true) → 上書き recompute (idempotent)。
  - non-leaf node → 既存値を保持 (`mark-dependency-readiness` で立てた flag が preflight 再実行で消えないことを保証)。
- existing が未設定 → 上記初期値ルールで初期化。

---

## mark-dependency-readiness

`orchestration_meta.dependency_readiness` を **runtime による artifact 検証**から再計算し、**全 detail flag を毎回 overwrite** する。CLI は caller assertion ではなく、検証要求として動作する。`<spec_ref>/deps.yaml` を解析し、`spec/registry/spec_catalog.yaml` で各 dep を `(spec_kind, spec_id, spec_version)` に resolve した後、全 stage の workspace artifact を実 inspect する。

| arg | 必須 | 説明 |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |

**毎回 full-recompute (per-stage 部分更新は許可しない)**: 全 detail flag を毎回 artifact 検証結果で上書きする。部分更新を許容すると、ある stage で過去に立った `true` flag が後続の dependency regression (新しい artifact が fail に転じる等) を生き残ってしまい、`workflow-launch-check` が stale な persisted boolean を信用して gate を通してしまうため。

**path-safety validation**: `spec_kind` / `spec_id` / `spec_version` の各 token は workspace path に補間されるため、`[A-Za-z0-9._+-]` 範囲外の文字、`..` 部分文字列、path separator (`/`, `\\`) を含む値は受理しない。deps.yaml で unsafe token が登場すれば well_formed=False で全 stage fail-closed、spec_catalog.yaml の unsafe entry は indexing 時に skip される (resolve はその id に対して None を返す)。

**within-mark consistency**: `mark-dependency-readiness` は `_compute_dep_readiness_and_fingerprint` 単一 pass で全 artifact bytes を **一度だけ** 読み、その同一 snapshot から readiness booleans と `dep_set_fingerprint` の両方を派生させる。これにより検証 read と fingerprint read が別タイミングで異なる byte 状態を観測する within-mark TOCTOU window を閉じる。`_dependency_set_fingerprint` (gate 用) も同じ walker を使うため、同一 on-disk state は必ず同一 hash を生む。

**build-variant ambiguity rejection**: 範囲・不等価 constraint (例: `>=1.0.0`) が build metadata だけ異なる複数 catalog entry (`1.0.0+cpu`, `1.0.0+gpu`) にマッチするとき、`_matching_dep_versions` は空 tuple を返し fail-closed にする。workspace artifact root は full version string で key 化されるため、`+cpu` と `+gpu` の選択を range 制約に委ねるのは ambiguity となる。特定 variant を pin するには `==1.0.0+cpu` のような exact-string constraint を使う。

**version 解決ルール**: 各 dep の `(spec_kind, spec_id, version_constraint)` を `spec/registry/spec_catalog.yaml` の全 catalog version に対して評価する。constraint は AND 結合の `>=`/`>`/`<=`/`<`/`==`/`!=` 演算子をサポートし、版本値は semver-style (`X.Y.Z[-prerelease][+build]`) を受理する。演算子セマンティクス:
- 順序演算子 (`>`, `>=`, `<`, `<=`) は SemVer-numeric precedence (§11 prerelease、§10 build metadata を無視)。
- 等価演算子 (`==`, `!=`) は build metadata を含む **正規化文字列の完全一致** で評価する。workspace artifact root が full version string で key 化されるため、`==1.0.0+cpu` が `1.0.0+gpu` に silent match することを防ぐ。

**per-stage verification (same-version coherence + certified version pinning)**: 各 stage を独立に判定するのではなく、**同一 catalog version に対する cumulative chain** を要求する。さらに per-dep で **certified version を 1 つだけ** 選ぶ:
- 各 dep の matched catalog versions について、cumulative level (ir=1, ir+pipeline=2, ir+pipeline+verdict=3) を計算。
- 最大 level を達成した version の中で **最も高い version** を certified version として選択。
- 全 stage の readiness flag は certified version の level から派生。
- `dep_set_fingerprint` は certified version の artifact bytes のみを hash する。

これにより:
- cross-version mixing (ir が版本 A、pipeline が版本 B) を防ぐ
- per-dep の canonical version (`certified_deps` field) が `meta.dependency_readiness` に永続化され、downstream consumer は同 version で resolve できる
- non-certified version の artifact churn (例: 新版が部分的に published / 旧版が cleanup) で readiness が誤って invalidate されることを防ぐ
- certified version の artifact regression は fingerprint mismatch で invalidate される

constraint にマッチする version が 1 つも無ければ fail-closed。`>=0.1.0 <1.0.0` のような range は新版公開 (例: 0.2.0) で artifact 未整備でも旧版 (0.1.0) で完備された chain で引き続き launchable (0.1.0 が certified)。

**stage 別の verification 条件** (各 dep の **最新 (canonical id 順) artifact のみ** を評価、ALL deps pass で stage true):

| stage | 条件 |
|---|---|
| `ir_ref` | `workspace/ir/<kind>__<id>__<version>/*/ir_meta.json` の **最新ファイル** の `verification_status == "pass"` |
| `pipeline_ref` | `workspace/pipelines/<kind>__<id>__<version>/` 配下で「最新の pipeline_id ディレクトリ」(`_latest_pipeline_dir`) を選び、その pipeline 内の最新 `binary/*/binary_meta.json` の `verification_status == "pass"` |
| `aggregate_verdict` | 同じ最新 pipeline ディレクトリ内で、**pipeline_ref で選ばれた binary** に `trial_meta.json.source_binary_id` が一致する verdict のみを候補とし、その最新 `aggregate_verdict ∈ {"pass", "xfail"}` (docs/GLOSSARY.md)。当該 binary に対応する verdict が無ければ fail-closed (Codex round 24: 旧 binary の passing verdict を新 binary に流用させない) |

`pipeline_ref` と `aggregate_verdict` は **同じ pipeline_id** に bind される (Codex round 11 F2 + round 24)。これにより「新しい pipeline の binary は pass / 古い pipeline の verdict は pass」というクロス pipeline mixing で execution_readiness が誤って通過することを防ぐ。

「最新」は **canonical id suffix (`<slug>_<YYYYMMDD>_<seq3>`) から parse した `(date, seq)`** の順序で決定する (mtime ではない、raw path lex でもない、slug でもない)。canonical な suffix を持たない directory (例: stray `zzz/`、`dep_a_001` のような short form、`dep_a_0.1.0_002` の version 混入形) は **selector で filter out** される (Codex round 23 F2 / round 31 F2: reader と writer の grammar を lock-step に揃え、非 canonical 名による gate bypass を防ぐ)。同一 `(date, seq)` を持つ複数の canonical id (slug 違い) が同じ親 directory 配下に存在する場合は **明示的な collision として fail-closed** にする (Codex round 35 F1: slug-tiebreaker による silent な選択を許さず、stderr に `freshness_id_collision at (date=…, seq=…): …` を emit して `_select_max_by_id_extracted` が None を返す)。mtime を信用しない理由は touch / copy / restore / clock skew で容易に偽造されるため。historical 過去 run の passing artifact が残っていても、新しい id の run が fail なら gate は通さない。unresolvable dep (catalog 未登録、または constraint 解決不能) が一つでも存在する場合は全 stage を fail-closed にする。`deps.yaml` の `dependencies` block は **canonical 2 key `{components, profiles}` を厳密に要求** する: 両 key とも明示的に list 形で存在し、未知 key (例: 単数形タイポ `component:`、追加 section `extras:`) が一切無いことが leaf trivial-true の前提。各 list item は **dict 形 (`{component_id|profile_id, version_constraint?}`) のみ** を受理し、bare string (`"dep_a"`、`"profile/foo"`、`"../dep_a"` 等) は schema malformed として一律 fail-closed (silent normalization による wrong-dep certification 防止)。

派生規則:
- `direct_dependency_compile_readiness = detail.ir_ref_verified`
- `direct_dependency_execution_readiness = detail.ir_ref_verified AND detail.pipeline_ref_verified AND detail.aggregate_verdict_verified`

`dep_set_fingerprint` (`spec_ref + deps.yaml` の SHA-256) も毎回 refresh する。書き込みは `orchestration_meta.json.lock` 上の fcntl `LOCK_EX` で serialize される。`phase_state_log.jsonl` に `event=mark_dependency_readiness` と `verified` / `detail` を記録する。

**verification 失敗時の挙動**: 以下のいずれかが検出された場合、runtime は **raise する前に `dependency_readiness` を fail-closed payload で上書きし**、`phase_state_log.jsonl` に `event=mark_dependency_readiness_failed` で `reason` 付きで記録する。CLI 経由 (`tools/orchestration_runtime.py mark-dependency-readiness`) では traceback を吐かず stderr に reason を出し exit 1 を返す (Codex round 26 F2)。
- `reason=deps_yaml_missing_or_unparseable`: `deps.yaml` 不在、または YAML パース失敗。
- `reason=deps_yaml_malformed_schema`: `deps.yaml` はパースできるが schema が malformed (`dependencies` 直下が `{components, profiles}` の完全一致でない、list 型でない、各 entry の `*_id` 欠落、`version_constraint` が非 string、path-traversal token 等)。
- `reason=spec_catalog_corrupt` (Codex round 33 F2 + round 34 F2 + round 35 F2): `spec/registry/spec_catalog.yaml` が不在、unreadable、zero-byte、YAML パース失敗、または top-level schema が malformed (`specs:` list 欠落、`dict` でない等)。これは repository-wide outage であり ordinary な dependency miss と区別される。`mark-dependency-readiness` は CLI 経由でも `ValueError` を `print` し exit 1 を返す。

`workflow-launch-check` の `_dependency_ready` 経路が返す追加 reason:
- `reason=pyyaml_unavailable` (Codex round 28 F1): PyYAML 未 install で live recompute 不能。leaf orchestration (`certified_deps == []` かつ persisted byte-only fingerprint が一致) のみ launch を許可、それ以外は fail-closed。
- `freshness_id_collision` (stderr 出力のみ、Codex round 35 F1): 同一 `(date, seq)` を持つ canonical id 衝突。`_select_max_by_id_extracted` が `None` を返すため、gate 上は `direct_dependency_<step>_readiness_not_pass` / `dependency_readiness_detail_not_pass:<key>` として現れる。原因特定には runtime の stderr (`freshness_id_collision at (date=…, seq=…): <colliding paths>`) を参照する。

distinct な reason 設計により、observability tooling は「spec 定義の defect」を「ordinary な negative verification」から識別できる。エラー前に passing 状態であった orchestration が、後続の `workflow-launch-check` で launch 可能なまま残存することを防ぐ。

**設計上の trust boundary**: CLI を呼んだだけでは flag を立てられない。caller が boolean を渡す形ではなく、runtime が version_constraint を resolve して特定された catalog version の **canonical id 順 (`<slug>_<YYYYMMDD>_<seq3>` の `(date, seq)`) で選んだ workspace artifact** を inspect する (Codex round 26 F1: catalog cache も content-keyed なので mtime 偽造に影響されない)。stale artifact / version mismatch / verdict=fail / constraint ambiguous のいずれかが検出されれば flag は false のまま。さらに毎回 full-overwrite、`dep_set_fingerprint` 一致確認 (launch-time でも実施)、catalog cache の content-keyed invalidation、verification 失敗時の即時 fail-closed persist、fingerprint への per-dep artifact bytes 組み込みにより、(a) CLI 呼び出しで gate bypass、(b) 古い passing artifact で新規 launch unblock、(c) constraint と異なる version の artifact 採用、(d) 部分更新で stale `true` 残存、(e) spec_ref 差し替え / deps.yaml 編集後の stale state 残存、(f) preflight 再実行までの間 out-of-band edit で gate bypass、(g) verification 失敗で passing state が survive、(h) post-mark の dep artifact regression で stale `true` が gate を通過、(i) 長寿命プロセスで catalog cache が in-process edit を反映せず resolution drift、を全て防ぐ。

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
