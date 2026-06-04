# Workflow Orchestration Startup Contract

## 目的
- `workflow orchestration` 起動前の必須判定を最小トークンで確定する。

## 適用範囲
- `orchestration agent` 起動直後
- 子 `agent` の初回起動前

## tmp area の使い方（必須前提）

orchestration agent / 子 agent の `allowed_tmp_root` は `workspace/tmp/<agent_run_id>/` 固定で、`record-launch` 時に `output_manifests/<agent_run_id>.json` の `allowed_tmp_root` フィールドへ記録される。一時ファイル書き込みは当該 literal path 配下を **直接** 指定すれば `output_manifest_write_guard` を通過する。

```
# orchestration agent
workspace/tmp/<orchestration_agent_run_id>/

# 子 agent
workspace/tmp/<agent_run_id>/
```

- `<orchestration_agent_run_id>` は `orchestration_meta.json#orchestration_agent_run_id`、子 agent の `<agent_run_id>` は `record-launch` 時に発行した値。
- agent は当該 literal path を直接 `cat > workspace/tmp/<arid>/x.patch <<EOF` 等で使う。`$TMPDIR` env への参照は許容するが必須ではない (`output_manifest_write_guard` は write 対象 path のみを判定し env を参照しない、cf. `tools/hooks/common.py:_validate_write_access` の `allowed_tmp_root` 分岐)。
- **bootstrap Bash 禁止**: `export TMPDIR=...`、`jq -er ...`、`printenv`、`bash -c '...'`、`env` (read-only debug 用途以外) を呼んではならない。Claude Code session sandbox の approval 要求で workflow が連続停止する原因になる。env (`METDSL_ORCHESTRATION_ID` / `ORCHESTRATION_AGENT_RUN_ID` / `TMPDIR`) は `tools/run_workflow.py` が subprocess に inherit 済み。
- tmp 外への直接書き込み（`workspace/<canonical>/...` など）は `guarded-apply-patch` 経由を必須とし、`Edit`/`Write` tool は `allowed_file_tool_paths` 登録済みパスにのみ使用する。Bash heredoc で canonical path に直接書くと `enforce_guarded_apply_patch` でブロックされる。

## 要件
- workflow 起動は `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]` を canonical entrypoint としなければならない。
- workflow mode は `python3 tools/run_workflow.py ... --mode <dev|prod>` で指定し、未指定時は `dev` を適用しなければならない。
- `dev` mode では verify 判定の緩和を禁止し、`issue_severity=major|critical` 検出時は fail 停止を必須とする。
- `dev` mode で fail した場合は `workspace/orchestrations/<orchestration_id>/failure_analysis.json` を保存し、失敗要因と根拠参照を必須記録とする。この file は orchestration agent が `Edit`/`Write` tool で直接書き込む（`failure_analysis.json` は `allowed_file_tool_paths` に登録済み）。`tools/run_workflow.py` は orchestration agent が書かなかった場合のみ同 path へ safety-net として書き込む（既存ファイルは上書きしない）。orchestration agent が既に書いていた場合は runtime 収集データを `failure_analysis.runtime.<uuid12>.json`（例: `failure_analysis.runtime.3a7f9c2e14b0.json`）へ別書きする。uuid12 は実行ごとに一意な 12 文字 hex で、並行実行時の上書き衝突を防ぐ。`tools/run_workflow.py` が返す `analysis_ref` は常に current-run データを指す（canonical が有効なら `failure_analysis.json`、stale/invalid なら sidecar）。一時ファイルを経由する場合は必ず `allowed_tmp_root` (= `workspace/tmp/<orchestration_agent_run_id>/`) 配下の literal path を使用し、`/tmp/` ハードコードは禁止する。
- 起動前確認の canonical implementation は `tools/run_workflow.py` と `tools/orchestration_runtime.py` の組み合わせとし、`preflight` の backend 指定は `tools/run_workflow.py --llm` を通じて行わなければならない。
- 子 `agent` へ渡す要求定義と判定規則の canonical source は `docs/` と `spec/` と当該試行 artifact に限定し、`tools/` 配下の実装、検証 `script`、test code、validator code を rule source として参照してはならない。
- validator invocation は `run-gate` を原則とし、直接実行を許可する場合は read-only 検査かつ gate 非依存検査に限定する。許可対象は `validate_workspace_root.py` と `check_artifact_syntax.py` のみとし、それ以外の validator 直実行を禁止する。
- `init` と `preflight` は各 1 回以上実行しなければならない。
- `preflight.json` が `status=pass` かつ `can_launch_step_agents=true` かつ `can_launch_substep_agents=true` を満たさない場合、子 `agent` を起動してはならない。
- Claude backend では `preflight.json#checks` に `claude_mcp_build_runtime_registered: pass=true` **かつ** `claude_mcp_build_runtime_permission_granted: pass=true` が含まれることを判定対象とする (server 登録 ∧ tool permission 付与の AND)。`probe_execution_platform` が AND 評価済みであり、orchestration agent 側で `claude mcp list` を再実行する必要はない。いずれかが `pass=false` の時は `status=fail` で停止済みのため、当該分岐に達することはない (Generate/Build/Validate 子 agent 起動の前段で必ず検知される)。permission 未付与時の remediation は `.claude/settings.json` の `permissions.allow` への `mcp__build-runtime` 追加 (詳細は [CLAUDE.md](../../../CLAUDE.md) preflight 節)。
- phase 着手前に、対象 phase が `substep agent` 必須か `step agent` 必須かを固定表で確認しなければならない。`Compile` / `Generate` / `Validate` は `substep agent`、`Build` は `step agent` とする。
- 最初の `commentary` で、対象 phase、使用する `SKILL`、起動する `agent` 種別、`MCP` 使用箇所を実行宣言しなければならない。
- `Compile` の子 `agent` を起動する前に、対象 `node` の直下依存 `node` が `direct dependency compile readiness` を満たすことを確認しなければならない。
- `Generate` 以降の子 `agent` を起動する前に、対象 `node` の直下依存 `node` が `direct dependency execution readiness` を満たすことを確認しなければならない。
- 子 `agent` 起動直前の live 検査は `record-launch` 実行時にのみ必須とする。
- `record-agent-run` と `write-step-result` は、`preflight.json` の整合確認を満たす場合に実行してよい。
- 起動要求本文は `launches/<agent_run_id>.prompt.txt`、起動返答本文は `launches/<agent_run_id>.reply.txt` に保存しなければならない。
- phase artifact を直接編集または `MCP` 実行する前に、`preflight` 済み、launch prompt 準備済み、child `agent` 起動済みを満たさなければならない。
- workflow の正当性確認、検証、疎通確認を目的とした仮実装であっても、親 `agent` が子 `agent` 必須 phase の本体処理を代行してはならない。

## 運用ルール
1. `tools/run_workflow.py` を実行して `workspace/orchestrations/<orchestration_id>/` の初期化と `preflight.json` 生成を行う。
2. `tools/run_workflow.py` 以外の経路で workflow を開始してはならない。
3. `METDSL_WORKFLOW_MODE=1` で起動した orchestration agent は `~/.claude/projects/` 配下の `memory/` ディレクトリ（`MEMORY.md` 等）を読んではならない。workflow 実行は決定論的に進めるため、conversation 外部の persistent state を参照しない。以下の **Claude Code auto-read 系ファイル**が起動直後に自動 Read された場合、`audit_detail.policy=auto_read_expected_block` で benign 分類されるが **これは想定動作**であり workflow の継続に影響しない。エラーとして扱わず、再試行やこれらのファイルへの追加参照を試みてはならない。

   許容対象は **(A) harness 強制 auto-read（全 agent role 適用）** と **(B) orchestration agent のみ許容** の 2 ブロックに分かれる。実装は `tools/hooks/common.py` の `_HARNESS_AUTO_READ_TOLERATED_REPO_RELPATHS` / `_HARNESS_AUTO_READ_TOLERATED_REPO_PREFIXES` および `_AUTO_READ_TOLERATED_REPO_RELPATHS` に対応する。

   **(A) harness 強制 auto-read（全 agent role 適用）**

   Claude Code harness が agent role に依らず startup 直後に Read するファイル群。`orchestration agent` / `step agent` / `substep agent` のいずれでも benign 扱いとする。harness の動作であり、agent が能動的にこれらを Read することは禁止する。
   - `.claude/settings.json`
   - `.cursor/mcp.json`（Claude Code の MCP discovery が起動直後に自動 Read する）
   - `mcp_servers/README.md`（同上）
   - `mcp_servers/mcp_servers.example.json`（同上）
   - `mcp_servers/tools/` 配下の全ファイル（MCP tool 定義の auto-discovery。実装は prefix-tolerate しており harness が読むのは `*.json` のみ）

   **(B) orchestration agent のみ許容**

   `orchestration agent` の startup 時に Claude Code harness が project state を read する経路。`substep agent` には適用されない（substep の harness は project state を再 read しないため）。
   - `~/.claude/projects/.../memory/MEMORY.md`
   - `README.md`（プロジェクトルート）
   - `TODO.md`（プロジェクトルート）
   - `CLAUDE.md`（プロジェクトルート）
   - プロジェクトルート直下の `MEMORY.md`

   **substep agent はブロック (B) のファイルを Read してはならない**（substep にとっては通常エラーで `read_manifest_read_guard` が発火する）。ブロック (A) は harness 経由でのみ許容され、agent prompt から能動的に Read することは全 role で禁止する。
4. `preflight` 判定が `pass` でない場合は `set-status --status fail` を実行して停止する。
5. 最初の `commentary` で、対象 phase、使用する `SKILL`、起動する `agent` 種別、`MCP` 使用箇所を宣言する。
6. 固定表で phase 種別を確認し、`Compile` / `Generate` / `Validate` では `substep agent`、`Build` では `step agent` を起動対象として確定する。
7. `Compile` の子 `agent` 起動前に、直下依存 `node` の `ir_ref` と `ir_meta.json.verification_status` を確認する。
8. `Generate` 以降の子 `agent` 起動前に、直下依存 `node` の `ir_ref` と `pipeline_ref` と `aggregate_verdict` を確認する。
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

### `ir_id` / `pipeline_id` の命名規則

形式: `<slug>_<YYYYMMDD>_<seq3>`

- `slug` は **ハイフン区切り** の英小文字・数字（アンダースコア不可）。
- 正規表現: `^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$`
- 例: `flux-rsn-p0_20260425_001` ✓　`flux_rsn_p0_20260425_001` ✗

`reserve-phase-root` の `--reserved-id` にはこの形式で渡す。

### `ir_ref` / `pipeline_ref` の形式

```
workspace/ir/<node_key_safe>/<ir_id>
workspace/pipelines/<node_key_safe>/<pipeline_id>
```

- **Compile substep** でも `pipeline_ref` は必須。パイプラインはまだ存在しないが、`reserve-phase-root --step generate` で pipeline_id を先行予約し、`workspace/pipelines/<node_key_safe>/<pipeline_id>` 形式で指定する。
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
   - canonical 経路: `python3 tools/new_agent_run_id.py` を bare 実行し、Bash 出力に印字された UUID を後続コマンドへ literal 文字列として埋め込む。
   - `CHILD_ARID=$(python3 tools/new_agent_run_id.py)` の **2-step shell var 割り当て形式は使わない** — 先頭 `CHILD_ARID=` が `Bash(python3 tools/new_agent_run_id.py)` allowlist 一致を壊し session approval を要求する。
   - `cat /proc/sys/kernel/random/uuid` / `uuidgen` は session sandbox の approval 要求で都度停止するため使用しない。
   - `python3 -c 'import uuid; …'` は `forbid_python_inline_write` でブロックされる。
2. reserve-phase-root で ir_id / pipeline_id を先行予約する（未実行なら）
   - Compile phase のみ: `--step compile`（ir_id 予約）と `--step generate`（pipeline_id 予約）の 2 回実行が必要。他の phase は 1 回のみ。
   - どちらも `--reserved-id` は同じ ID（例: `flux-rsn-p0_20260428_001`）を指定する。
3. record-launch を呼び出す（Agent tool 起動 前）
   - request-json: node_key / step / substep / ir_ref / pipeline_ref / dependency_ref /
                   skill_name / skill_ref 等の起動パラメータを含む JSON
   - response-json: {"agent_session_id": "<agent_run_id>",
                     "agent_run_id": "<agent_run_id>",
                     "started_at": "<ISO8601>",
                     "backend": "claude"}
   → capability_token / sandbox_profile / output manifest / read manifest が生成される
4. Agent tool を起動する（子 agent は capabilities/<agent_run_id>.json から
   capability_token を読んで guarded-apply-patch 等を実行する）
5. Agent tool の戻り値（最終応答テキスト）を受け取る
5.4. record-child-return で Agent tool return 観測の証跡を残す（Adv-20/Adv-30 ガード必須）
     - return-token は `$(cat ...)` をインライン引数として渡す。`VAR=$(cat ...)` の 2-step
       形式は使わない（先頭 `VAR=` が `Bash(python3 ...)` allowlist 一致を壊し session
       approval を要求する）。
     python3 tools/orchestration_runtime.py record-child-return \
       --repo-root <repo_root> \
       --orchestration-id <orchestration_id> \
       --agent-run-id <agent_run_id> \
       --return-token "$(cat <repo_root>/workspace/orchestrations/<orchestration_id>/launches/<agent_run_id>.parent_return_token)"
5.5. deactivate-child を実行して active context を orchestration agent に戻す
     （上の record-child-return 完了後でなければ ValueError で拒否される）
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

コマンドを構築するときは下記テンプレートをそのままコピーして値を埋める。本テンプレートで列挙する 4 つは頻出 subcommand であり、payload schema の完全仕様は `docs/CLI_REFERENCE.md` (Tier-A) を canonical source とする。稀少 subcommand (例: `init` / `preflight` / `record-timeout` / `read-checkpoint` 等) は `python3 tools/orchestration_runtime.py <sub> --help` を canonical source とし、`docs/CLI_REFERENCE_RARE.md` に overview を置く。tool 単位の使い分けは `CLAUDE.md` の「CLI 仕様の確認規約」節を参照。

**started_at の取扱い**: `STARTED_AT=$(date ...)` の **2-step shell var 割り当ては使わない**（先頭 `STARTED_AT=` が `Bash(python3 ...)` allowlist 一致を壊し session approval を要求する）。`--response-json` の `started_at` 値には `$(date -u +"%Y-%m-%dT%H:%M:%SZ")` を **インライン** command substitution で埋め込む。コマンド全体は `python3 tools/orchestration_runtime.py …` で始まるため `Bash(python3 tools/orchestration_runtime.py *)` allowlist と一致する。`date -u *` は補強として allowlist に追加済み (`.claude/settings.json`)。

```bash
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
    "ir_ref": "<ir_ref>",
    "pipeline_ref": "<pipeline_ref>",
    "dependency_ref": "<dependency_ref>",
    "skill_name": "<skill_name>",
    "skill_ref": "<skill_ref>",
    "allowed_output_paths": ["<出力ファイルのパス一覧>"]
  }' \
  --response-json "{
    \"agent_run_id\": \"<agent_run_id>\",
    \"agent_session_id\": \"<agent_run_id>\",
    \"started_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",
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
| `ir_ref` | `workspace/ir/<node_key_safe>/<ir_id>` 形式 |
| `pipeline_ref` | `workspace/pipelines/<node_key_safe>/<pipeline_id>` 形式（Compile phase でも必須）|
| `dependency_ref` | phase 別 canonical path。`Compile` は `spec/.../deps.yaml`、`Generate` 以降は `workspace/...` の phase root（`ir_ref` または `pipeline_ref`） |
| `skill_name` | 命名規則: `workflow-{step}-{substep}`（例: `"workflow-compile-generate"`）|
| `skill_ref` | 命名規則: `skills/{skill_name}/SKILL.md`（例: `"skills/workflow-compile-generate/SKILL.md"`）|
| `skill_must_read_refs` | 子 `SKILL.md` を読んで導出してはならない。orchestration `SKILL.md` の「`Compile verify` の起動要求」「`Generate verify` の起動要求」の各項を canonical source とする |
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

## `reserve-phase-root` コマンドテンプレート（Claude Code backend）

コマンドを構築するときは下記テンプレートをそのままコピーして値を埋める。本テンプレートで列挙する 4 つは頻出 subcommand であり、payload schema の完全仕様は `docs/CLI_REFERENCE.md` (Tier-A) を canonical source とする。稀少 subcommand (例: `init` / `preflight` / `record-timeout` / `read-checkpoint` 等) は `python3 tools/orchestration_runtime.py <sub> --help` を canonical source とし、`docs/CLI_REFERENCE_RARE.md` に overview を置く。tool 単位の使い分けは `CLAUDE.md` の「CLI 仕様の確認規約」節を参照。

```bash
python3 tools/orchestration_runtime.py reserve-phase-root \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --node-key <node_key> \
  --step <plan|generate> \
  --reserved-id <reserved_id> \
  --reserved-by-agent-run-id <agent_run_id>
```

- `--step compile` で ir_id を、`--step generate` で pipeline_id を予約する。Compile phase は両方を 1 回ずつ実行し、`--reserved-id` には**同じ ID**（例: `flux-rsn-p0_20260509_001`）を指定する。他 phase は `--step generate` の 1 回のみ。
- `--reserved-id` は `<slug>_<YYYYMMDD>_<seq3>` 形式（slug はハイフン区切り小文字英数。アンダースコアを slug 内に含めてはならない）。
- `--reserved-by-agent-run-id` は当該 ID を実際に使う子 agent の UUID。

## `record-agent-run` コマンドテンプレート（Claude Code backend）

コマンドを構築するときは下記テンプレートをそのままコピーして値を埋める。本テンプレートで列挙する 4 つは頻出 subcommand であり、payload schema の完全仕様は `docs/CLI_REFERENCE.md` (Tier-A) を canonical source とする。稀少 subcommand (例: `init` / `preflight` / `record-timeout` / `read-checkpoint` 等) は `python3 tools/orchestration_runtime.py <sub> --help` を canonical source とし、`docs/CLI_REFERENCE_RARE.md` に overview を置く。tool 単位の使い分けは `CLAUDE.md` の「CLI 仕様の確認規約」節を参照。

```bash
python3 tools/orchestration_runtime.py record-agent-run \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --agent-run-json '{
    "agent_run_id": "<agent_run_id>",
    "agent_role": "<orchestration|step|substep>",
    "agent_backend": "claude",
    "status": "<running|pass|fail|fail_closed|blocked|timeout|cancel>",
    "started_at": "<ISO8601>",
    "finished_at": "<ISO8601>",
    "agent_session_id": "<agent_run_id>",
    "context_id": "<agent_run_id>",
    "context_isolated": true,
    "node_key": "<node_key>",
    "step": "<step>",
    "substep": "<substep_or_omit_for_step_agent>",
    "output_refs": ["<path>", ...]
  }'
```

- 終端 status (`pass` / `fail` / `fail_closed` / `blocked` / `timeout` / `cancel`) では `finished_at` を必須とする。
- `pass` 終端では `output_refs` を必須とする。
- step/substep role では `agent_session_id` / `context_id` / `context_isolated` / `node_key` を必須とする。Claude Code では `agent_session_id` と `context_id` は `agent_run_id` と同値で記録する。
- orchestration role の `running` 初期 entry は `init_orchestration()` が自動挿入するため orchestration agent からは呼び出さない。終端 entry のみ orchestration agent が `set-status` 実行後に追記する。

## `set-status` コマンドテンプレート（Claude Code backend）

コマンドを構築するときは下記テンプレートをそのままコピーして値を埋める。本テンプレートで列挙する 4 つは頻出 subcommand であり、payload schema の完全仕様は `docs/CLI_REFERENCE.md` (Tier-A) を canonical source とする。稀少 subcommand (例: `init` / `preflight` / `record-timeout` / `read-checkpoint` 等) は `python3 tools/orchestration_runtime.py <sub> --help` を canonical source とし、`docs/CLI_REFERENCE_RARE.md` に overview を置く。tool 単位の使い分けは `CLAUDE.md` の「CLI 仕様の確認規約」節を参照。

```bash
python3 tools/orchestration_runtime.py set-status \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --status <running|pass|fail|fail_closed> \
  --reason-code <reason_code_or_omit> \
  --reason-detail <reason_detail_or_omit> \
  --blocking-policy-scope <scope_or_omit>
```

- `--reason-code` / `--reason-detail` / `--blocking-policy-scope` は `fail` / `fail_closed` 時に必要。`pass` では省略可。
- 省略する optional flag は **flag ごと外す**（空文字や `omit` 文字列を値として渡してはならない）。例: `pass` 時は `--reason-code` の行ごと削除する。
- orchestration agent は本コマンドを実行した後に `record-agent-run` で orchestration role の終端 entry を追記する。

## execution platform 別の補足

execution platform ごとの子 `agent` 起動ツールと `preflight` 引数の対応は `CLAUDE.md` の「execution platform 別の子 `agent` 起動ツール」を参照する。
