# Workspace Layout

## このドキュメントの位置づけ

`workspace/` 配下の **canonical directory layout** を tree 図で示す。orchestration agent が `ls workspace/...` で位置確認する代わりに本 file を参照すれば足りるよう、各 path について **生成 timing** / **書き手** / **読み手** を一覧で示す。

関連 canonical source:
- orchestration 契約: `docs/ORCHESTRATION.md`
- workflow phase 契約: `docs/workflow/phases/phase_*.md`
- CLI: `docs/CLI_REFERENCE.md`
- 起動契約: `skills/workflow-orchestration/references/startup_contract.md`

## 全体構造

```
workspace/
├── orchestrations/
│   └── <orchestration_id>/                       例: orch_20260510T024428Z_a099e46d
│       ├── orchestration_meta.json               (init / set-status が更新)
│       ├── orchestration_run_write_baseline.json
│       ├── preflight.json                         (preflight が生成、手動編集禁止)
│       ├── orchestration_checkpoint.json          (write-step-result 完了で自動更新)
│       ├── failure_analysis.json                  (dev mode fail 時に必須)
│       ├── failure_analysis.runtime.<uuid12>.json (runtime safety-net sidecar)
│       ├── agent_runs.jsonl                       (record-agent-run が追記)
│       ├── agent_runs.jsonl.lock
│       ├── agent_graph.json                       (parent-child 関係 DAG)
│       ├── session_run_index.json                 (session_id ↔ agent_run_id mapping)
│       ├── phase_state.json
│       ├── phase_state_log.jsonl                  (set_status 等のイベント時系列)
│       │
│       ├── launches/
│       │   ├── orchestration.start.prompt.txt     (run_workflow.py が orchestration agent に渡した prompt)
│       │   ├── <agent_run_id>.request.json        (record-launch が書く launch request)
│       │   ├── <agent_run_id>.response.json       (record-launch が書く launch response)
│       │   ├── <agent_run_id>.prompt.txt          (子 agent prompt 本文。Agent tool 入力と 1 対 1 一致; 子は `read_manifest_read_guard` で本 file の Read を block される)
│       │   ├── <agent_run_id>.reply.txt           (record-reply が Agent tool 応答で上書き)
│       │   └── <agent_run_id>.parent_return_token (record-launch が発行、record-child-return で消費)
│       │
│       ├── agents/
│       │   └── <agent_run_id>/                     子 agent 1 件分の全 dialog
│       │       ├── dialogs/
│       │       │   ├── child.request.json          (launches/<arid>.request.json の mirror)
│       │       │   ├── child.response.json         (mirror)
│       │       │   ├── child.prompt.txt            (mirror; Agent tool 入力と 1 対 1 一致)
│       │       │   ├── child.reply.txt             (mirror)
│       │       │   ├── agent.result.json           (record-agent-run が pass 時に書く)
│       │       │   └── agent.summary.txt           (同上)
│       │       ├── gate_changed_paths.json
│       │       ├── managed_write_snapshot.json
│       │       └── run_write_baseline.json
│       │
│       ├── capabilities/
│       │   └── <agent_run_id>.json                (record-launch が生成、子 agent が起動直後に Read)
│       │       # 含む: capability_token, write_roots, denied_write_roots, ...
│       │
│       ├── output_manifests/
│       │   └── <agent_run_id>.json                (allowed_output_paths, allowed_file_tool_paths, allowed_tmp_root)
│       │
│       ├── read_manifests/
│       │   └── <agent_run_id>.json                (allowed_read_roots, denied_read_roots)
│       │
│       ├── sandbox_profiles/
│       │   └── <agent_run_id>.json                (bwrap profile)
│       │
│       ├── sandboxes/
│       │   └── ...                                (実 bwrap mount 構成スナップショット)
│       │
│       ├── access_policies/
│       │   └── <agent_run_id>.json                (manifest 派生のアクセス policy)
│       │
│       ├── access_logs/
│       │   └── <agent_run_id>.jsonl               (orchestration_read 等の access trace)
│       │
│       ├── hooks/
│       │   ├── native_hook_events.jsonl           (PreToolUse 等の全 hook 判定 trace)
│       │   └── workflow_hooks.jsonl               (pre_phase_launch, pre_command_execute 等)
│       │
│       ├── gates/
│       │   └── <agent_run_id>/                    内部 gate 結果 (子 agent 直 Read 禁止)
│       │       ├── apply_patch_writes.json
│       │       ├── validate_pipeline_semantics.json
│       │       └── ...
│       │
│       ├── steps/
│       │   └── <node_key_safe>/<step>/<agent_run_id>/
│       │       └── step_result.json               (write-step-result が生成)
│       │
│       ├── reservations/
│       │   └── <node_key_safe>/<step>/<reserved_id>.json (reserve-phase-root)
│       │
│       ├── child_returns/
│       │   └── <agent_run_id>.txt                 (record-child-return ack; deactivate-child で消費)
│       │
│       ├── active_children/
│       │   └── <agent_run_id>                     (record-launch 時 marker; deactivate-child で削除)
│       │
│       ├── cleanup_committed/
│       │   └── <agent_run_id>                     (record-timeout 等の cleanup 完了 marker)
│       │
│       └── violations/
│           └── <id>.json                          (sandbox / write 違反のスナップショット)
│
├── tmp/
│   └── <agent_run_id>/                            各 agent の allowed_tmp_root (Step 0 で TMPDIR=これ)
│       └── ...                                    (heredoc / mktemp / 中間 script の置き場)
│       # 注意: orchestration_id 配下ではなく workspace/ 直下に置く。manifest が宣言する
│       # allowed_tmp_root は "workspace/tmp/<agent_run_id>" であり、
│       # workspace/orchestrations/<orch>/tmp/... へ書くと output_manifest_write_guard で reject される。
│
├── plans/
│   └── <node_key_safe>/                           例: component__dynamics_shallow_water_flux_2d_rusanov_p0__0.1.0
│       └── <plan_id>/                             例: flux-rsn-p0_20260510_001
│           ├── case.resolved.yaml                 (plan/generate substep 出力)
│           ├── algorithm.resolved.yaml            (同上)
│           ├── impl.resolved.yaml                 (同上)
│           ├── dependency.resolved.yaml           (同上)
│           ├── algorithm.summary.md               (同上、閲覧専用)
│           ├── derived_contract.json              (plan/verify substep 出力)
│           └── plan_meta.json                     (plan/generate; verification_status は plan/verify)
│
└── pipelines/
    └── <node_key_safe>/
        └── <pipeline_id>/
            ├── generate/
            │   └── <generation_id>/
            │       ├── src/                       (生成 source code、`mcp_command_log.jsonl` を含む)
            │       └── generate_meta.json
            ├── build/
            │   └── <build_id>/
            │       ├── (out-of-source build artifacts)
            │       ├── build_meta.json            (source_generation_id を pin)
            │       └── mcp_command_log.jsonl      (compile_project の MCP audit)
            ├── execute/
            │   └── <execution_id>/
            │       └── ...                        (execute step output)
            ├── judge/
            │   └── ...
            └── lineage.json                       (phase 間の id 系譜)
```

## 主要 path の生成 timing と読み書きルール

### orchestration 単位

| path | 生成 | 書き手 | 読み手 | 備考 |
|---|---|---|---|---|
| `orchestration_meta.json` | `init` | `set-status` 等の runtime command | orchestration agent / runtime | `status`, `dependency_readiness`, `orchestration_agent_run_id` 等 |
| `preflight.json` | `preflight` | runtime のみ (probed_at 自動更新含む) | runtime / orchestration agent | 手動編集禁止。`status=pass` + `can_launch_*=true` 必須 |
| `failure_analysis.json` | dev mode fail 時 | orchestration agent (`Edit`/`Write`) | runtime (二重書きを safety-net として `failure_analysis.runtime.<uuid12>.json` へ) | `orchestration_agent_run_id` field 必須 |
| `agent_runs.jsonl` | `record-agent-run` | runtime (lock 越し append) | runtime / validator | 1 行 = 1 agent_run record |

### 子 agent 単位

| path | 生成 | 書き手 | 読み手 | 備考 |
|---|---|---|---|---|
| `launches/<arid>.prompt.txt` | `record-launch` | runtime | **本人 Read 禁止** (read_manifest_read_guard) | Agent tool 入力と 1 対 1 一致する canonical artifact (audit / replay 用) |
| `launches/<arid>.reply.txt` | `record-launch` (暫定) → `record-reply` (上書き) | runtime | runtime / validator / 親 orchestration agent | Agent tool 最終応答 |
| `launches/<arid>.parent_return_token` | `record-launch` | runtime | parent agent (record-child-return 用) | 任意 caller forge 防止 |
| `capabilities/<arid>.json` | `record-launch` | runtime | **本人のみ Read 可** | `capability_token` / `write_roots` を含む |
| `output_manifests/<arid>.json` | `record-launch` | runtime | 本人 Read 可 | `allowed_output_paths` / `allowed_file_tool_paths` / `allowed_tmp_root` |
| `read_manifests/<arid>.json` | `record-launch` | runtime | 本人 Read 可 | `allowed_read_roots` / `denied_read_roots` |
| `child_returns/<arid>.txt` | `record-child-return` | runtime | runtime (`deactivate-child` が消費) | Adv-30 token 検証付き |
| `agents/<arid>/dialogs/agent.result.json` | `record-agent-run` (pass 時) | runtime | runtime / validator / 親 orchestration agent | 子 agent の構造化結果 |
| `agents/<arid>/dialogs/agent.summary.txt` | 同上 | runtime | 同上 | 単一行禁止、根拠を含むこと |
| `gates/<arid>/<gate>.json` | gate 実行時 | runtime | **本人 Read 禁止** (内部 artifact)。stderr 経由で取得 | `2>"${TMPDIR}/last_gate_stderr.txt"` |

### phase artifact

| path | 生成 phase | 書き手 | 読み手 | 備考 |
|---|---|---|---|---|
| `workspace/plans/.../<plan_id>/algorithm.resolved.yaml` | plan/generate | substep agent (Edit/Write) | 後続 phase 全部 | `temporaries[].shape_expr` の表現規則は `spec/schema/plan/shape_expr.schema.json` |
| `workspace/plans/.../<plan_id>/derived_contract.json` | plan/verify | substep agent (guarded-apply-patch) | generate 以降 | |
| `workspace/plans/.../<plan_id>/plan_meta.json` | plan/generate / verify | substep agent (guarded-apply-patch) | runtime / validator | `verification_status` は verify が pass の時のみ付与 |
| `workspace/pipelines/.../<pipeline_id>/generate/<gen>/src/` | generate | step/substep agent | 後続 phase | |
| `workspace/pipelines/.../<pipeline_id>/build/<build_id>/build_meta.json` | build | step agent (guarded-apply-patch) | execute / validator | `source_generation_id` を pin |
| `workspace/pipelines/.../<pipeline_id>/lineage.json` | 各 phase が追加 | (write-step-result 経由) | runtime / validator | phase id 系譜 |

## node_key_safe の生成規則

`node_key_safe = node_key.replace("/", "__").replace("@", "__")`。

例: `component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0` → `component__dynamics_shallow_water_flux_2d_rusanov_p0__0.1.0`。

## tmp / TMPDIR

各 agent の `allowed_tmp_root` は **`workspace/tmp/<agent_run_id>/`** (workspace 直下、orchestration_id 配下では無い)。runtime canonical source は `tools/orchestration_runtime.py` の `init_orchestration` (orchestration agent 用) と `record_launch` (子 agent 用) で、いずれも `workspace/tmp/<agent_run_id>` を `output_manifests/<agent_run_id>.json#allowed_tmp_root` に書き込む。

Step 0 で `export TMPDIR=$(jq -er '.allowed_tmp_root' "workspace/orchestrations/<orch>/output_manifests/<agent_run_id>.json")` を実行する (`startup_contract.md` 参照)。manifest が宣言する path 以外 (`/tmp/`、`/dev/shm/`、`workspace/orchestrations/<orch>/tmp/...` 等) を tmp として使うと `output_manifest_write_guard` でブロックされる。
