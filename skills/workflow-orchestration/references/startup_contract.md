# Workflow Orchestration Startup Contract

## 目的
- `workflow orchestration` 起動前の必須判定を最小トークンで確定する。

## 適用範囲
- `orchestration agent` 起動直後
- 子 `agent` の初回起動前

## 要件
- workflow 起動は `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]` を canonical entrypoint としなければならない。
- workflow mode は `python3 tools/run_workflow.py ... --mode <dev|prod>` で指定し、未指定時は `dev` を適用しなければならない。
- `dev` mode では verify 判定の緩和を禁止し、`issue_severity=major|critical` 検出時は fail 停止を必須とする。
- `dev` mode で fail した場合は `workspace/orchestrations/<orchestration_id>/failure_analysis.json` を保存し、失敗要因と根拠参照を必須記録とする。
- 起動前確認の canonical implementation は `tools/run_workflow.py` と `tools/orchestration_runtime.py` の組み合わせとし、`preflight` の backend 指定は `tools/run_workflow.py --llm` を通じて行わなければならない。
- 子 `agent` へ渡す要求定義と判定規則の canonical source は `docs/` と `spec/` と当該試行 artifact に限定し、`tools/` 配下の実装、検証 `script`、test code、validator code を rule source として参照してはならない。
- validator invocation は `run-gate` を原則とし、直接実行を許可する場合は read-only 検査かつ gate 非依存検査に限定する。許可対象は `validate_workspace_root.py` と `check_artifact_syntax.py` のみとし、それ以外の validator 直実行を禁止する。
- `init` と `preflight` は各 1 回以上実行しなければならない。
- `preflight.json` が `status=pass` かつ `can_launch_step_agents=true` かつ `can_launch_substep_agents=true` を満たさない場合、子 `agent` を起動してはならない。
- phase 着手前に、対象 phase が `substep agent` 必須か `step agent` 必須かを固定表で確認しなければならない。`Plan` / `Generate` / `Tune` は `substep agent`、`Build` / `Execute` / `Judge` / `Promote` は `step agent` とする。
- 最初の `commentary` で、対象 phase、使用する `SKILL`、起動する `agent` 種別、`MCP` 使用箇所を実行宣言しなければならない。
- `Plan` の子 `agent` を起動する前に、対象 `node` の直下依存 `node` が `direct dependency plan readiness` を満たすことを確認しなければならない。
- `Generate` 以降の子 `agent` を起動する前に、対象 `node` の直下依存 `node` が `direct dependency execution readiness` を満たすことを確認しなければならない。
- 子 `agent` 起動直前の live 検査は `record-launch` 実行時にのみ必須とする。
- `record-agent-run` と `write-step-result` は、`preflight.json` の整合確認を満たす場合に実行してよい。
- 起動要求本文は `launches/<agent_run_id>.prompt.txt`、起動返答本文は `launches/<agent_run_id>.reply.txt` に保存しなければならない。
- phase artifact を直接編集または `MCP` 実行する前に、`preflight` 済み、launch prompt 準備済み、child `agent` 起動済みを満たさなければならない。
- workflow の正当性確認、検証、疎通確認を目的とした仮実装であっても、親 `agent` が子 `agent` 必須 phase の本体処理を代行してはならない。

## 運用ルール
1. `tools/run_workflow.py` を実行して `workspace/orchestrations/<orchestration_id>/` の初期化と `preflight.json` 生成を行う。
2. `tools/run_workflow.py` 以外の経路で workflow を開始してはならない。
3. `METDSL_WORKFLOW_MODE=1` で起動した orchestration agent は `~/.claude/projects/` 配下の `memory/` ディレクトリ（`MEMORY.md` 等）を読んではならない。workflow 実行は決定論的に進めるため、conversation 外部の persistent state を参照しない。
4. `preflight` 判定が `pass` でない場合は `set-status --status fail` を実行して停止する。
5. 最初の `commentary` で、対象 phase、使用する `SKILL`、起動する `agent` 種別、`MCP` 使用箇所を宣言する。
6. 固定表で phase 種別を確認し、`Plan` / `Generate` / `Tune` では `substep agent`、`Build` / `Execute` / `Judge` / `Promote` では `step agent` を起動対象として確定する。
7. `Plan` の子 `agent` 起動前に、直下依存 `node` の `plan_ref` と `plan_meta.json.verification_status` を確認する。
8. `Generate` 以降の子 `agent` 起動前に、直下依存 `node` の `plan_ref` と `pipeline_ref` と `aggregate_verdict` を確認する。
9. `preflight` 済み、launch prompt 準備済み、child `agent` 起動済みを満たすまで phase artifact 編集と `MCP` 実行を開始しない。
10. 子 `agent` 起動時は `record-launch` を実行する。
11. 子 `agent` 完了後は `record-agent-run` を追記する。
12. phase 完了後は `write-step-result` を記録する。
13. 契約に反する近道へ逸脱しそうな場合は、子 `agent` 起動必須であることを明示して launch 手順へ戻る。

## 判定基準
- `preflight.json` が存在し、`pass` 条件を満たしている。
- 最初の `commentary` に実行宣言が存在する。
- phase 種別と起動した `agent` 種別が固定表と一致している。
- `launches/` と `agent_runs.jsonl` と `step_result.json` の参照整合が取れている。
- 子 `agent` の起動失敗時に `set-status --status fail` が記録されている。

## node_key / ID フォーマット早見表

### `node_key` の構成

```
<spec_kind>/<spec_id>@<spec_version>
```

- `spec_kind` / `spec_id` は対象 `deps.yaml` の同名フィールドから取得する。
- `spec_version` は対象 `controlled_spec.md` の `spec_version` フィールドから取得する。
- **ファイルシステムパス**（`spec/component/dynamics/...`）とは別物。`workflow-launch-check` / `record-launch` / `reserve-phase-root` 等の `--node-key` には常にこの形式を渡す。

例:
```
deps.yaml    → spec_kind: component, spec_id: dynamics_shallow_water_flux_2d_rusanov_p0
controlled_spec.md → spec_version: 0.1.0
node_key     → component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0
node_key_safe → component__dynamics_shallow_water_flux_2d_rusanov_p0__0.1.0
```

### `plan_id` / `pipeline_id` の命名規則

形式: `<slug>_<YYYYMMDD>_<seq3>`

- `slug` は **ハイフン区切り** の英小文字・数字（アンダースコア不可）。
- 正規表現: `^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$`
- 例: `flux-rsn-p0_20260425_001` ✓　`flux_rsn_p0_20260425_001` ✗

`reserve-phase-root` の `--reserved-id` にはこの形式で渡す。

### `plan_ref` / `pipeline_ref` の形式

```
workspace/plans/<node_key_safe>/<plan_id>
workspace/pipelines/<node_key_safe>/<pipeline_id>
```

- **Plan substep** でも `pipeline_ref` は必須。パイプラインはまだ存在しないが、`reserve-phase-root --step generate` で pipeline_id を先行予約し、`workspace/pipelines/<node_key_safe>/<pipeline_id>` 形式で指定する。
- `record-launch` の `--request-json` に `pipeline_ref="none"` や空文字は渡せない。

### orchestration_agent_run_id の取得

orchestration agent 自身の `agent_run_id` は startup context の `orchestration_agent_run_id` フィールドを canonical source とする。

- `tools/run_workflow.py` が `init_orchestration()` 経由で生成し、`orchestration_meta.json` に記録済みである。
- orchestration agent は `uuid.uuid4()` などで独自生成してはならない。
- `record-agent-run`（orchestration role）の `running` 初期 entry は `init_orchestration()` が自動挿入するため、orchestration agent が手動で呼び出す必要はない。
- `record-agent-run`（orchestration role）の終端 entry（`pass` / `fail` / `fail_closed`）は orchestration agent が `set-status` 実行後に呼び出す。

## `record-launch` 手順（Claude Code backend）

Claude Code では `spawn_agent` が存在しないため、以下の順序で実行する。

```
1. agent_run_id (UUID) を生成する
2. reserve-phase-root で plan_id / pipeline_id を先行予約する（未実行なら）
   - Plan phase のみ: `--step plan`（plan_id 予約）と `--step generate`（pipeline_id 予約）の 2 回実行が必要。他の phase は 1 回のみ。
   - どちらも `--reserved-id` は同じ ID（例: `flux-rsn-p0_20260428_001`）を指定する。
3. record-launch を呼び出す（Agent tool 起動 前）
   - request-json: node_key / step / substep / plan_ref / pipeline_ref / dependency_ref /
                   skill_name / skill_ref 等の起動パラメータを含む JSON
   - response-json: {"agent_session_id": "<agent_run_id>",
                     "agent_run_id": "<agent_run_id>",
                     "started_at": "<ISO8601>",
                     "backend": "claude"}
   → capability_token / sandbox_profile / output manifest / read manifest が生成される
4. Agent tool を起動する（子 agent は capabilities/<agent_run_id>.json から
   capability_token を読んで guarded-apply-patch 等を実行する）
5. Agent tool の戻り値（最終応答テキスト）を受け取る
5.5. deactivate-child を実行して active context を orchestration agent に戻す
     python3 tools/orchestration_runtime.py deactivate-child \
       --repo-root <repo_root> \
       --orchestration-id <orchestration_id> \
       --child-run-id <agent_run_id>
6. record-reply で launches/<agent_run_id>.reply.txt に応答テキストを保存する
   python3 tools/orchestration_runtime.py record-reply \
     --repo-root <repo_root> \
     --orchestration-id <orchestration_id> \
     --agent-run-id <agent_run_id> \
     --reply-text "<Agent tool の戻り値>"
7. record-agent-run を実行して agent_runs.jsonl へ追記する
```

`record-launch` を Agent tool より前に呼ぶ理由: capability_token と output manifest を子 agent が実行開始前に参照できるようにするため。

## `record-launch` コマンドテンプレート（Claude Code backend）

コマンドを構築するとき以下をそのままコピーして値を埋める。`--help` を実行して引数を調べてはならない。

```bash
STARTED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
python3 tools/orchestration_runtime.py record-launch \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --parent-agent-run-id <orchestration_agent_run_id> \
  --child-agent-run-id <agent_run_id> \
  --request-json '{
    "agent_role": "<substep|step>",
    "node_key": "<node_key>",
    "step": "<step>",
    "substep": "<substep_or_omit_for_step_agent>",
    "orchestration_id": "<orchestration_id>",
    "agent_run_id": "<agent_run_id>",
    "parent_agent_run_id": "<orchestration_agent_run_id>",
    "workflow_mode": "<dev|prod>",
    "plan_ref": "<plan_ref>",
    "pipeline_ref": "<pipeline_ref>",
    "dependency_ref": "<dependency_ref>",
    "skill_name": "<skill_name>",
    "skill_ref": "<skill_ref>",
    "allowed_output_paths": ["<出力ファイルのパス一覧>"]
  }' \
  --response-json "{
    \"agent_run_id\": \"<agent_run_id>\",
    \"agent_session_id\": \"<agent_run_id>\",
    \"started_at\": \"$STARTED_AT\",
    \"backend\": \"claude\"
  }"
```

- `--parent-agent-run-id` と `--child-agent-run-id` はそれぞれ独立した位置引数であり、`--request-json` の内側のフィールドとは別に指定する。
- `--response-json` は必須の独立引数。`--request-json` の中に含めてはならない。
- `allowed_output_paths` は step/substep agent では必須フィールド。省略すると exit code 1 で失敗する。
- `substep` フィールドは step agent（substep なし）の場合は省略する。

## `record-launch --request-json` の最小必須フィールド

| フィールド | 説明 |
|---|---|
| `agent_role` | `"substep"` または `"step"` |
| `node_key` | `<spec_kind>/<spec_id>@<spec_version>` 形式 |
| `step` | `"plan"` / `"generate"` / `"build"` / `"execute"` / `"judge"` 等 |
| `substep` | `"generate"` / `"verify"`（substep agent の場合）|
| `orchestration_id` | orchestration ID |
| `agent_run_id` | 子 agent の UUID |
| `parent_agent_run_id` | 親（orchestration）agent の UUID |
| `workflow_mode` | `"dev"` または `"prod"` |
| `plan_ref` | `workspace/plans/<node_key_safe>/<plan_id>` 形式 |
| `pipeline_ref` | `workspace/pipelines/<node_key_safe>/<pipeline_id>` 形式（Plan phase でも必須）|
| `dependency_ref` | phase 別 canonical path。`Plan` は `spec/.../deps.yaml`、`Generate` 以降は `workspace/...` の phase root（`plan_ref` または `pipeline_ref`） |
| `skill_name` | 命名規則: `workflow-{step}-{substep}`（例: `"workflow-plan-generate"`）|
| `skill_ref` | 命名規則: `skills/{skill_name}/SKILL.md`（例: `"skills/workflow-plan-generate/SKILL.md"`）|
| `skill_must_read_refs` | 子 `SKILL.md` を読んで導出してはならない。orchestration `SKILL.md` の「`Plan verify` の起動要求」「`Generate verify` の起動要求」の各項を canonical source とする |
| `allowed_output_paths` | 子 agent が書き込める全出力 path のリスト。step/substep では必須。`guarded-apply-patch` と `apply_patch_writes` gate がこのリストを参照する |

## `record-agent-run --agent-run-json` の最小必須フィールド

| フィールド | 説明 | 備考 |
|---|---|---|
| `agent_run_id` | UUID | |
| `agent_role` | `"orchestration"` / `"step"` / `"substep"` | |
| `agent_backend` | `"claude"` / `"codex"` / `"cursor"` | |
| `status` | `"running"` / `"pass"` / `"fail"` 等 | |
| `started_at` | ISO8601 | |
| `agent_session_id` | step/substep では必須。Claude Code では agent_run_id と同値 | |
| `context_id` | step/substep では必須 | |
| `context_isolated` | step/substep では必須（常に `true`）| |
| `node_key` | step/substep では必須 | |
| `output_refs` | pass 終端時に必須 | |

## execution platform 別の補足

execution platform ごとの子 `agent` 起動ツールと `preflight` 引数の対応は `CLAUDE.md` の「execution platform 別の子 `agent` 起動ツール」を参照する。
