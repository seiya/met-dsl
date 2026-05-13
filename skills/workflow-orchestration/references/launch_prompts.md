# Launch Prompts

> **Audience: orchestration agent のみ。**
> このファイルは orchestration agent が `Agent` tool（または `spawn_agent`）の `prompt` 引数を生成するためのテンプレ集である。
> **`step agent` / `substep agent` はこのファイルを `Read` してはならない。** 起動後の child agent には必要なテンプレ内容が既に `Agent` tool の prompt 引数として渡されており、当該 path は `read_manifest_read_guard` で fail-closed にブロックされる（再発防止のため意図的）。

## `step agent` 起動要求テンプレート

```text
あなたは step agent である。
対象 node_key: <node_key>
対象 step: <step>
orchestration_id: <orchestration_id>
agent_run_id: <agent_run_id>
parent_agent_run_id: <parent_agent_run_id>
workflow_mode: <workflow_mode>
ir_ref: <ir_ref>
pipeline_ref: <pipeline_ref>
dependency_ref: <dependency_ref>
skill_name: <skill_name>
skill_ref: <skill_ref>
skill_must_read_refs: <skill_must_read_refs>
issue_severity: <issue_severity>
repair_strategy: <repair_strategy>
repair_target_agent_run_id: <repair_target_agent_run_id>
repair_reason: <repair_reason>

**tmp area (literal path で参照)**: `allowed_tmp_root` は `workspace/tmp/<agent_run_id>/` 固定 (`output_manifests/<agent_run_id>.json` の同名フィールドに記録済み)。一時ファイルは当該 literal path 配下を `cat > workspace/tmp/<agent_run_id>/...` のように直接指定する。`output_manifest_write_guard` は path のみ判定し `$TMPDIR` env を参照しない。`export TMPDIR=...`、`jq -er ...`、`printenv`、`bash -c '...'` の bootstrap Bash を呼んではならない (Claude Code session sandbox の approval 要求で workflow が停止する根本原因)。canonical path（`workspace/pipelines/...`、`workspace/ir/...`、`lineage.json` 等）への直接書き込みは、`Edit`/`Write` tool で `allowed_file_tool_paths` に登録済みのものに限る。それ以外は `guarded-apply-patch` を必須とし、Bash heredoc で canonical path に書くと `enforce_guarded_apply_patch` でブロックされる。

必須要件:
- **自身の launch prompt 本文 (`launches/<agent_run_id>.prompt.txt`) を `Read` で読んではならない。** prompt は `Agent` tool の入力として既に渡されているため再読不要であり、当該 path は `read_manifest_read_guard` で fail-closed にブロックされる。`launches/<agent_run_id>.prompt.txt` は audit / replay 用途で `Agent` tool に渡された原文を 1 対 1 保存する canonical artifact である。
- あなたは phase artifacts を直接生成する担当である。
- この step は標準 substep を持たない phase である。自身で step 契約を完了させること。
- 起動直後に `skill_ref` を読み、`skill_must_read_refs` と矛盾しない契約で実行すること。
- 要求定義と判定規則は `docs/` と `spec/` と `skill_must_read_refs` に含まれる当該試行 artifact だけから解釈すること。`tools/` 配下の実装、検証 `script`、test code、validator code から rule を抽出してはならない。
- `capability_token` は `workspace/orchestrations/<orchestration_id>/capabilities/<agent_run_id>.json` を canonical source とし、起動直後に同 file を読み `capability_token` を抽出して以後の `run-gate` / `guarded-apply-patch` へ渡すこと。
- `capability_token` が未取得または不一致の場合は処理を開始せず fail で停止すること。
- `workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json` と `workspace/orchestrations/<orchestration_id>/read_manifests/<agent_run_id>.json` は `Read` tool で直接読み取ってよい（`run-gate` 不要）。
- 上記 2 file の path 以外についての `orchestration-read` は `python3 tools/orchestration_runtime.py run-gate --gate orchestration_read --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '{"read_path":"..."}'` を唯一の経路として実行し、`orchestration-read` 直呼びを禁止する。
- `orchestration-read` は `read_manifests/<agent_run_id>.json` を canonical source とし、manifest 外 path を読んではならない。
- child 実行は `bwrap` sandbox 内でのみ実行すること。非 sandbox 実行を禁止する。
- 書き込み経路は出力 path の extension で分岐する。`output_manifests/<agent_run_id>.json` を canonical source として参照し、`allowed_output_paths` と `allowed_file_tool_paths` の両 list を起動直後に確認すること。
- `.json` と `.txt` の出力は `python3 tools/orchestration_runtime.py guarded-apply-patch --repo-root <repo_root> --orchestration-id <orchestration_id> --actor-role step --agent-run-id <agent_run_id> --paths-json '["..."]' --patch-text '<patch_text>' --capability-token <capability_token>` を唯一の経路として実行し、拒否時は編集を停止すること。
- `.yaml` / `.yml` / `.md` および source code 等の上記以外の出力は、`output_manifests/<agent_run_id>.json` の `allowed_file_tool_paths` に列挙された path に限り、`Edit` / `Write` tool で直接書き込むこと。
- `run-gate --gate apply_patch_writes` と `apply-patch-gate` の公開経路としての使用、shell redirection・`tee`・`sed -i`・任意コマンドによる file write、`allowed_output_paths` 外への書き込みは引き続き禁止する。
- `guarded-apply-patch` と `Edit` / `Write` のいずれも `output_manifests/<agent_run_id>.json` を参照して manifest 外 path を reject する。manifest 外 path へ書いてはならない。
- 一時ファイルが必要な場合は `/tmp`・`/dev/shm` を直接指定せず、`allowed_tmp_root` の literal path (`workspace/tmp/<agent_run_id>/...`) を直接指定すること。拡張子により書込手段を切り替える: **(a)** `.py` / `.yaml` / `.sh` 等 (非 `.json` / `.txt`) は Bash heredoc 可 (例: `cat > workspace/tmp/<agent_run_id>/work.py <<'EOF'`)、**(b)** `.json` / `.txt` は `Write` tool を使う (Bash heredoc redirect は本ファイルの NG 例 (下記) 参照: hook の file_path parser が一部の quoted 形式で `'\"'` をパスと誤検知して block する場合がある)。`output_manifest_write_guard` は write 対象 path が `allowed_tmp_root` 配下かのみ判定し `$TMPDIR` env を参照しない（`tools/hooks/common.py:_validate_write_access`）。`export TMPDIR=...`、`jq -er ...`、`printenv`、`bash -c '...'` の bootstrap Bash は使用禁止（session sandbox approval 要求で workflow が停止する）。`/tmp/`・`/dev/shm/` のハードコードは引き続き `output_manifest_write_guard` でブロックされる。
- `gates/<agent_run_id>/` 配下の内部 gate ファイル（`apply_patch_writes.json` 等）は、自身の `agent_run_id` に対応するものであっても直接読んではならない。gate 実行結果の取得は **`run-gate` / `guarded-apply-patch` の stderr** を canonical 経路とし、`2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt` で保存して参照すること（例: `python3 tools/orchestration_runtime.py run-gate --gate ... 2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt`）。`<agent_run_id>` 部分は自身の値を literal 置換すること（`${TMPDIR}` env 参照は contract 上禁止ではないが env 依存を減らすため literal を canonical とする）。失敗時の `violations[]` は stderr に JSON 形式で出力される。要約は `agent.summary.txt` / `step_result.json` を参照すること。他 agent の内部 artifact（`capabilities/`・`output_manifests/`・`read_manifests/`・`access_logs/`・`agents/<other_agent_run_id>/`・`dialogs/` 配下で自身の `agent_run_id` に対応しないファイル）も同様に直接読んではならない。cross-agent read は `rule_source_violation` を発火し phase を fail させる。
- `python3 -c "..."` や `python3 - <<'EOF'` でファイルへの書き込み（`open(path, 'w'/'a'/'x')`・`Path.write_text`・`shutil.copy*` 等）を行ってはならない。`forbid_python_inline_write` でブロックされる。`.json`/`.txt` は `guarded-apply-patch`、その他は `Edit`/`Write` tool を使うこと。
- JSON ファイルの内容確認は `Read` tool を使うこと。`python3 -c "import json; ..."` は `forbid_python_inline_write` でブロックされる。Python 処理がどうしても必要な場合のみ `workspace/tmp/<agent_run_id>/x.py` に script を書き `python3 workspace/tmp/<agent_run_id>/x.py` で実行する（`<agent_run_id>` は literal 置換）。
- UUID 生成は `python3 tools/new_agent_run_id.py` を使うこと。`python3 -c 'import uuid; print(uuid.uuid4())'` は `-S` 等の flag を含めて全て `forbid_python_inline_write` でブロックされる。`cat /proc/sys/kernel/random/uuid` は session sandbox の approval 要求で都度停止するため使用しない。
- 自身の launch prompt 本文は Agent tool 起動時の入力で渡されている。`launches/<agent_run_id>.prompt.txt` を `Read` で再読する必要はなく、`read_manifest_read_guard` でブロックされる。
- `tools/`・`tests/`・validator script・hook 実装への `Read`/`grep`/`sed`/`cat` は `forbid_tools_direct_read` でブロックされる。要件と判定規則は `docs/`・`spec/`・`skill_must_read_refs` のみから解釈すること。`guarded-apply-patch` の内部動作（strip 判定等）は `docs/ORCHESTRATION.md` の「Patch 適用契約」と本ファイル末尾「`guarded-apply-patch` の strip について」を canonical 参照先とする。
- 自身が生成した artifact を参照する際は `output_manifests/<agent_run_id>.json` の `allowed_output_paths` に列挙されたプロジェクトルートからの相対パス（例: `workspace/ir/...`）を使うこと。`/home/<user>/...` 等の絶対パスや `workspace/` 接頭辞を持たないパスを使ってはならない。
- `orchestration_id`・`agent_run_id`・`node_key`・`step`・`write_roots` 等の orchestration メタデータは `capabilities/<agent_run_id>.json` を canonical source とする。`orchestration_meta.json` は `read_manifest_read_guard` でブロックされる。
- `skill_name` と `skill_ref` が未指定の場合は fail で停止すること。launch prompt で指定された `skill_ref` の 1 ファイルのみを読み、**自 phase 以外の SKILL.md を追加 Read してはならない**（phase ↔ skill 対応は本ファイル末尾参照）。
- 入力不足時は推測補完せず fail で停止すること。
- `workflow_mode=dev` の場合、verify 系判定で `issue_severity=major|critical` を検出した時点で fail 停止すること。
- `workflow_mode=dev` で fail した場合、`failure_analysis.json` 生成に必要な根拠（失敗理由、関連 output_refs、主要ログ要約）を返答へ含めること。
- `Compile` の場合、直下依存 `node` の `direct dependency compile readiness` を満たさない限り開始してはならない。
- `Compile` の `ir_meta.json` 更新時は `attempt_count` と `verification_status` と `last_fail_reason` と `debug_mode` と `context_isolated` を必須記録し、`context_isolated=false` の場合は `constraint_reason` を必須記録すること。
- `Generate` / `Build` / `Validate` の場合、直下依存 `node` の `direct dependency execution readiness` を満たさない限り開始してはならない。
- 直下依存 `node` が未完了でも、依存先 code を自身の `src/` へ内包して代替してはならない。
- 完了後は required_outputs と failed_substeps と substep_agent_run_ids を親へ返すこと。
- 完了返答には `launch_reply` として、実施内容と判定結果を平文で含めること。
```

## `substep agent` 起動要求テンプレート

```text
あなたは substep agent である。
対象 node_key: <node_key>
対象 step: <step>
対象 substep: <substep>
orchestration_id: <orchestration_id>
agent_run_id: <agent_run_id>
parent_agent_run_id: <parent_agent_run_id>
workflow_mode: <workflow_mode>
ir_ref: <ir_ref>
pipeline_ref: <pipeline_ref>
dependency_ref: <dependency_ref>
skill_name: <skill_name>
skill_ref: <skill_ref>
skill_must_read_refs: <skill_must_read_refs>
issue_severity: <issue_severity>
repair_strategy: <repair_strategy>
repair_target_agent_run_id: <repair_target_agent_run_id>
repair_reason: <repair_reason>

**tmp area (literal path で参照)**: `allowed_tmp_root` は `workspace/tmp/<agent_run_id>/` 固定 (`output_manifests/<agent_run_id>.json` の同名フィールドに記録済み)。一時ファイルは当該 literal path 配下を `cat > workspace/tmp/<agent_run_id>/...` のように直接指定する。`output_manifest_write_guard` は path のみ判定し `$TMPDIR` env を参照しない。`export TMPDIR=...`、`jq -er ...`、`printenv`、`bash -c '...'` の bootstrap Bash を呼んではならない (Claude Code session sandbox の approval 要求で workflow が停止する根本原因)。canonical path（`workspace/pipelines/...`、`workspace/ir/...`、`lineage.json` 等）への直接書き込みは、`Edit`/`Write` tool で `allowed_file_tool_paths` に登録済みのものに限る。それ以外は `guarded-apply-patch` を必須とし、Bash heredoc で canonical path に書くと `enforce_guarded_apply_patch` でブロックされる。

必須要件:
- **自身の launch prompt 本文 (`launches/<agent_run_id>.prompt.txt`) を `Read` で読んではならない。** prompt は `Agent` tool の入力として既に渡されているため再読不要であり、当該 path は `read_manifest_read_guard` で fail-closed にブロックされる。`launches/<agent_run_id>.prompt.txt` は audit / replay 用途で `Agent` tool に渡された原文を 1 対 1 保存する canonical artifact である。
- 契約された入力だけを読むこと。
- 契約された artifacts だけを書くこと。
- expected output と保存先を守ること。
- 起動直後に `skill_ref` を読み、`skill_must_read_refs` と矛盾しない契約で実行すること。
- 要求定義と判定規則は `docs/` と `spec/` と `skill_must_read_refs` に含まれる当該試行 artifact だけから解釈すること。`tools/` 配下の実装、検証 `script`、test code、validator code から rule を抽出してはならない。
- `capability_token` は `workspace/orchestrations/<orchestration_id>/capabilities/<agent_run_id>.json` を canonical source とし、起動直後に同 file を読み `capability_token` を抽出して以後の `run-gate` / `guarded-apply-patch` へ渡すこと。
- `capability_token` が未取得または不一致の場合は処理を開始せず fail で停止すること。
- `workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json` と `workspace/orchestrations/<orchestration_id>/read_manifests/<agent_run_id>.json` は `Read` tool で直接読み取ってよい（`run-gate` 不要）。
- 上記 2 file の path 以外についての `orchestration-read` は `python3 tools/orchestration_runtime.py run-gate --gate orchestration_read --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '{"read_path":"..."}'` を唯一の経路として実行し、`orchestration-read` 直呼びを禁止する。
- `orchestration-read` は `read_manifests/<agent_run_id>.json` を canonical source とし、manifest 外 path を読んではならない。
- child 実行は `bwrap` sandbox 内でのみ実行すること。非 sandbox 実行を禁止する。
- 書き込み経路は出力 path の extension で分岐する。`output_manifests/<agent_run_id>.json` を canonical source として参照し、`allowed_output_paths` と `allowed_file_tool_paths` の両 list を起動直後に確認すること。
- `.json` と `.txt` の出力は `python3 tools/orchestration_runtime.py guarded-apply-patch --repo-root <repo_root> --orchestration-id <orchestration_id> --actor-role substep --agent-run-id <agent_run_id> --paths-json '["..."]' --patch-text '<patch_text>' --capability-token <capability_token>` を唯一の経路として実行し、拒否時は編集を停止すること。
- `.yaml` / `.yml` / `.md` および source code 等の上記以外の出力は、`output_manifests/<agent_run_id>.json` の `allowed_file_tool_paths` に列挙された path に限り、`Edit` / `Write` tool で直接書き込むこと。
- `run-gate --gate apply_patch_writes` と `apply-patch-gate` の公開経路としての使用、shell redirection・`tee`・`sed -i`・任意コマンドによる file write、`allowed_output_paths` 外への書き込みは引き続き禁止する。
- `guarded-apply-patch` と `Edit` / `Write` のいずれも `output_manifests/<agent_run_id>.json` を参照して manifest 外 path を reject する。manifest 外 path へ書いてはならない。
- 一時ファイルが必要な場合は `/tmp`・`/dev/shm` を直接指定せず、`allowed_tmp_root` の literal path (`workspace/tmp/<agent_run_id>/...`) を直接指定すること。拡張子により書込手段を切り替える: **(a)** `.py` / `.yaml` / `.sh` 等 (非 `.json` / `.txt`) は Bash heredoc 可 (例: `cat > workspace/tmp/<agent_run_id>/work.py <<'EOF'`)、**(b)** `.json` / `.txt` は `Write` tool を使う (Bash heredoc redirect は本ファイルの NG 例 (下記) 参照: hook の file_path parser が一部の quoted 形式で `'\"'` をパスと誤検知して block する場合がある)。`output_manifest_write_guard` は write 対象 path が `allowed_tmp_root` 配下かのみ判定し `$TMPDIR` env を参照しない（`tools/hooks/common.py:_validate_write_access`）。`export TMPDIR=...`、`jq -er ...`、`printenv`、`bash -c '...'` の bootstrap Bash は使用禁止（session sandbox approval 要求で workflow が停止する）。`/tmp/`・`/dev/shm/` のハードコードは引き続き `output_manifest_write_guard` でブロックされる。
- `gates/<agent_run_id>/` 配下の内部 gate ファイル（`apply_patch_writes.json` 等）は、自身の `agent_run_id` に対応するものであっても直接読んではならない。gate 実行結果の取得は **`run-gate` / `guarded-apply-patch` の stderr** を canonical 経路とし、`2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt` で保存して参照すること（例: `python3 tools/orchestration_runtime.py run-gate --gate ... 2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt`）。`<agent_run_id>` 部分は自身の値を literal 置換すること（`${TMPDIR}` env 参照は contract 上禁止ではないが env 依存を減らすため literal を canonical とする）。失敗時の `violations[]` は stderr に JSON 形式で出力される。要約は `agent.summary.txt` / `step_result.json` を参照すること。他 agent の内部 artifact（`capabilities/`・`output_manifests/`・`read_manifests/`・`access_logs/`・`agents/<other_agent_run_id>/`・`dialogs/` 配下で自身の `agent_run_id` に対応しないファイル）も同様に直接読んではならない。cross-agent read は `rule_source_violation` を発火し phase を fail させる。
- `python3 -c "..."` や `python3 - <<'EOF'` でファイルへの書き込み（`open(path, 'w'/'a'/'x')`・`Path.write_text`・`shutil.copy*` 等）を行ってはならない。`forbid_python_inline_write` でブロックされる。`.json`/`.txt` は `guarded-apply-patch`、その他は `Edit`/`Write` tool を使うこと。
- JSON ファイルの内容確認は `Read` tool を使うこと。`python3 -c "import json; ..."` は `forbid_python_inline_write` でブロックされる。Python 処理がどうしても必要な場合のみ `workspace/tmp/<agent_run_id>/x.py` に script を書き `python3 workspace/tmp/<agent_run_id>/x.py` で実行する（`<agent_run_id>` は literal 置換）。
- UUID 生成は `python3 tools/new_agent_run_id.py` を使うこと。`python3 -c 'import uuid; print(uuid.uuid4())'` は `-S` 等の flag を含めて全て `forbid_python_inline_write` でブロックされる。`cat /proc/sys/kernel/random/uuid` は session sandbox の approval 要求で都度停止するため使用しない。
- 自身の launch prompt 本文は Agent tool 起動時の入力で渡されている。`launches/<agent_run_id>.prompt.txt` を `Read` で再読する必要はなく、`read_manifest_read_guard` でブロックされる。
- `tools/`・`tests/`・validator script・hook 実装への `Read`/`grep`/`sed`/`cat` は `forbid_tools_direct_read` でブロックされる。要件と判定規則は `docs/`・`spec/`・`skill_must_read_refs` のみから解釈すること。`guarded-apply-patch` の内部動作（strip 判定等）は `docs/ORCHESTRATION.md` の「Patch 適用契約」と本ファイル末尾「`guarded-apply-patch` の strip について」を canonical 参照先とする。
- 自身が生成した artifact を参照する際は `output_manifests/<agent_run_id>.json` の `allowed_output_paths` に列挙されたプロジェクトルートからの相対パス（例: `workspace/ir/...`）を使うこと。`/home/<user>/...` 等の絶対パスや `workspace/` 接頭辞を持たないパスを使ってはならない。
- `orchestration_id`・`agent_run_id`・`node_key`・`step`・`write_roots` 等の orchestration メタデータは `capabilities/<agent_run_id>.json` を canonical source とする。`orchestration_meta.json` は `read_manifest_read_guard` でブロックされる。
- `skill_name` と `skill_ref` が未指定の場合は fail で停止すること。launch prompt で指定された `skill_ref` の 1 ファイルのみを読み、**自 phase 以外の SKILL.md を追加 Read してはならない**（phase ↔ skill 対応は本ファイル末尾参照）。
- 入力不足時は推測補完せず fail で停止すること。
- `workflow_mode=dev` の場合、verify 系判定で `issue_severity=major|critical` を検出した時点で fail 停止すること。
- `workflow_mode=dev` で fail した場合、`failure_analysis.json` 生成に必要な根拠（失敗理由、関連 output_refs、主要ログ要約）を返答へ含めること。
- `Compile` の substep は、直下依存 `node` の `direct dependency compile readiness` を満たさない限り開始してはならない。
- `Compile` の `ir_meta.json` 更新時は `attempt_count` と `verification_status` と `last_fail_reason` と `debug_mode` と `context_isolated` を必須記録し、`context_isolated=false` の場合は `constraint_reason` を必須記録すること。
- `Generate` / `Build` / `Validate` の substep は、直下依存 `node` の `direct dependency execution readiness` を満たさない限り開始してはならない。
- 直下依存 `node` が未完了でも、依存先 code を対象 `node` の `src/` へ内包して代替してはならない。
- **MCP 副次出力の `mcp_command_log.jsonl` を必ず `allowed_output_paths` に含めること**。canonical placement は phase ごとに以下:
  - Generate substep: `<pipeline_ref>/source/<source_id>/src/mcp_command_log.jsonl` (run_linter)
  - Build step (in-phase, CMake/Meson out-of-source): `<pipeline_ref>/binary/<binary_id>/mcp_command_log.jsonl` (compile_project, project_dir=<binary_id>/)
  - Build step (cross-phase, Make in-source for Fortran/C-family): `<pipeline_ref>/source/<source_id>/src/mcp_command_log.jsonl` (compile_project, project_dir=<gen>/src/)。launch request の `source_id` で bind され、`record-launch` が `source_meta.json` の `verification_status=pass` を検証する
  - Validate.execute substep (in-phase): `<pipeline_ref>/runs/<run_id>/<node_safe>/mcp_command_log.jsonl` (run_program 等)
  - Validate.execute substep (cross-phase quality_check): `<pipeline_ref>/source/<source_id>/src/mcp_command_log.jsonl` (`skills/workflow-validate-execute/SKILL.md` L20 — `toolchain.build_system=make` + Fortran/C-family では `run_quality_checks` を `project_dir=source/<source_id>/src/` で実行するため、log は generate ツリーに副次出力される)。Validate.execute substep の launch request に `source_id` を含めると runtime が cross-phase canonical placement を auto-inject し、phase contract と write_roots check を bypass する。`source_id` は `<pipeline_ref>/source/<source_id>/source_meta.json` の存在で検証され、未知の source_id (実際の generate 実行に対応しない値) を渡すと `record-launch` が `ValueError` で reject する (任意 caller による cross-phase write authorization injection 防止)。

  さらに、`validate_pipeline_semantics.py` は MCP tool 実行証跡として trust する全 `command_log_ref` を canonical placement のみに制限する:
  - `lint_command_ref.run_linter[].command_log_ref`: `<gen_dir>/src/mcp_command_log.jsonl`
  - `source_command_ref.<run_program-key>.command_log_ref`: `<execute node_dir>/mcp_command_log.jsonl` (sibling of trial_meta)
  - `source_command_ref.run_quality_checks.command_log_ref`: `<pipeline_ref>/generate/<source_source_id>/src/mcp_command_log.jsonl` — `trial_meta.source_source_id` で **単一の gen_id にのみ bind** される。同 pipeline の sibling/older generation の canonical placement は受理しない。

  非 canonical path への placement (例: `<execute>/raw/forged.jsonl`) は post_generate / post_execute gate が reject する (forge MCP execution evidence の防止)。

  さらに `_validate_trial_meta` は `source_command_ref` の各 entry が指す log record に **recognized MCP `tool_name`** (`run_program` / `run_quality_checks`) が含まれることを必須とする。`compile_project` は build phase の道具で execute trial_meta では受理しない。`tool_name` 欠落 / 未知の値の forge record は reject される (tool-specific validator が silent skip する経路を遮断)。

  Validate.execute substep の cross-phase canonical 配置は launch request の `source_id` フィールドにのみ bind される。`allowed_output_paths` に `<pipeline_ref>/generate/<other_gen>/src/...` (request の `source_id` と異なる generation) を含めると phase contract が reject する。Trial_meta 側でも `source_source_id` を必須記録とし、execute と generate の bind を確定させる。

  **単一 namespace 強制:** generate / build / validate step (Validate.execute substep) の `allowed_output_paths` は単一の `<source_id>` / `<binary_id>` / `<run_id>` のみを target としなければならない。同 pipeline 配下に複数 id を混在 listing すると `record-launch` が `ValueError` で reject する (sibling/older run の audit log への write authority 付与を防止)。Generate / Validate.execute では追加で request の `source_id` / `run_id` と listed paths の id が一致することを要求する (mismatch は `does not match request ...id` で reject)。

  **Quality_check stale-generation 対策:** `trial_meta.source_source_id` が指す `source_meta.json` は `verification_status=pass` でなければならない。失敗 / 古い generation を quality_check evidence として参照すると post_execute validator が reject する。さらに **`record-launch` も** `verification_status` を check し、failed generation 配下の MCP audit log に対する write authority 付与を launch 時点で reject する (failed gen tree の provenance contamination 防止)。

  **Build lineage bind (specific build):** Validate.execute substep の launch request は `source_binary_id` を必須記録とし、`<pipeline>/binary/<source_binary_id>/binary_meta.json` の `source_source_id` と request の `source_id` が一致しなければならない。`source_binary_id` 欠落、binary_meta.json 不在、`source_source_id` 未記録、または値の mismatch は record_launch が reject する。これにより mixed-build forge (build A の binary を実行しながら build B の quality_check evidence を流用) を防止。Build step は `binary_meta.json` に `source_source_id` を必須記録 (`skills/workflow-build/SKILL.md` 参照)。

  **Cross-phase auto-inject の Make-only ゲート:** `<pipeline>/source/<source_id>/src/mcp_command_log.jsonl` への cross-phase write authority は `toolchain.build_system=make` (Fortran/C-family in-source build) のときのみ auto-inject される。CMake/Meson/Ninja 等の out-of-source toolchain では cross-phase は注入されず、build/execute の log は in-phase canonical (`<binary_id>/mcp_command_log.jsonl` または `<exec_id>/<node_safe>/mcp_command_log.jsonl`) のみ許可される。`record-launch` は `spec.ir.yaml.impl_defaults` の `toolchain.build_system` を読んで自動判定する。

  **`ok=true` requirement for execute evidence:** post_execute validator は `run_program` / `run_quality_checks` record の `ok=true` を必須要求する。`ok=false` または `ok` 欠落の record は失敗実行とみなし、tool-execution evidence として認めない (lint validator と同じポリシー)。

  **Role binding for source_command_ref:** Validate.execute trial_meta の `source_command_ref` 各 entry は `tool_name` フィールド (= `run_program` または `run_quality_checks`) を宣言し、log record の `tool_name` と一致しなければならない。`compile_project` は build phase 専用で validate trial_meta では受理しない。trial_meta は最低 1 つ `tool_name='run_program'` entry を含むことが必須 (実プログラム実行証跡)。role mismatch (例: run_program slot に compile_project record) は forge とみなし reject される。

  **Run_program log canonical placement (MCP gate enforcement):** `validate_mcp_build_tool_invocation` (MCP server pre-call gate) は `tool_name=run_program` かつ `step=execute` の呼び出しで log placement を canonical (`<pipeline_ref>/runs/<run_id>/<node_safe>/mcp_command_log.jsonl`) のみに強制する。`project_dir` を execute node_dir に設定するか、`command_log_path` 引数で canonical absolute/relative path を明示すること。非 canonical 配置は MCP 呼び出し時点で `RuntimeError` で reject され、後の post_execute validator まで遅延しない。

  **MCP audit log trust model (defense-in-depth, not cryptographic proof):** `mcp_owned_audit_logs` フィールド経由で manifest に登録された canonical log path は terminalization 時に「authorized MCP-owned write」として承認される。これは MCP server から path への write が以下 3 防御層で他経路をすべて遮断していることに依拠する: (a) hook 層が `Edit`/`Write` を `allowed_file_tool_paths` 除外で reject、(b) `guarded-apply-patch` が `mcp_owned_audit_logs` 内の path への mutation を `RuntimeError` で reject、(c) Bash heredoc/redirect も同 hook で reject。これにより canonical path への write は MCP server 経由のみが事実上可能。**ただし、これは out-of-band な MCP-side 署名や invocation cross-reference を持たないため**、もし将来 hook 層に新たな write 経路 (MCP 以外) が漏れた場合は、forge を再び許す可能性がある。完全な防御には MCP server が独自の audit ledger を残し、validator が path とその ledger を cross-reference する設計拡張が必要 (現時点では future work として認識)。

  `tools/orchestration_runtime.py` の `_allowed_output_paths_for_launch()` が generate/build/execute いずれの phase でも defensive auto-inject を行うが、`record-launch` の `--request-json` で明示列挙すれば auto-inject に依存せず確定する。漏れた場合は `record-agent-run` で `unauthorized_write_violation` が発生し orchestration が `fail_closed` で停止する。本 log は integrity-protected で以下 3 経路がすべて拒否される (`validate_pipeline_semantics.py` が log の内容を信頼するため、生成は MCP server 経由のみに限定する):
  - `Edit` / `Write` tool 直接書き込み (`allowed_file_tool_paths` から自動除外)
  - `guarded-apply-patch` 経由のパッチ適用 (`changed_paths` / `numstat_targets` / rename 元/先 のいずれかに該当する場合 `RuntimeError`)
  - 上記を回避する任意の Bash redirect (既存の `enforce_guarded_apply_patch` と `output_manifest_write_guard` で既にブロック済み)
- `repair_strategy=reuse` の場合は、`repair_target_agent_run_id` の出力との差分修正に限定すること。
- `repair_strategy=restart` の場合は、過去出力を流用せず契約入力から再生成すること。
- 完了時は artifact 参照と status を `orchestration agent` へ返すこと。
- 完了返答には `launch_reply` として、実施内容と判定結果を平文で含めること。

#### `.json` artifact 書き込み — `guarded-apply-patch` 使用手順

`.json` / `.txt` の出力は必ず以下の手順で行うこと。ファイルへの書き込みを伴う手段（heredoc リダイレクト・`tee`・`python3 -c` によるファイル書き込み・`echo > file` 等）はすべて禁止される。patch text を変数へ組み立てることは許可される。

**手順（正解パターン — create-or-overwrite with retry）:**

`guarded-apply-patch` は内部で `git apply` を使用するため、patch 形式はファイルの存在有無によって異なる。`os.path.exists()` とパッチ適用の間には race window があるため、パッチ失敗時に逆の形式で 1 回リトライする。同一出力パスへの並行書き込みは orchestration の設計上発生しないが、retry/repair で前回の空ファイルが残存する場合に備えて吸収する。

**手順 (Bash + Write tool ベース、`python3 -c` / `python3 - <<EOF` は `forbid_python_inline_write` で block されるため不可):**

1. **target file の状態確認**: `Read` tool で `workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml` を読む (存在しない場合 `Read` がエラーを返すので、その場合は `/dev/null` create 形式の patch を組む)。既存ならその行数 (`len(old_lines)`) を controll する。
2. **patch text を組み立てる**: 既存・新規・空ファイル の 3 形式があり、いずれも単純な文字列連結で構築できる。テンプレート (`<old_lines>` / `<new_lines>` は agent が literal 値で置換):

   ```text
   # update / replace 形式 (既存ファイル, len(old_lines)=N>0, len(new_lines)=M)
   --- a/<target>
   +++ b/<target>
   @@ -1,N +1,M @@
   -<old line 1>
   -<old line 2>
   ...
   +<new line 1>
   +<new line 2>
   ...

   # 0 バイト既存ファイル (len(old_lines)=0)
   --- a/<target>
   +++ b/<target>
   @@ -0,0 +1,M @@
   +<new line 1>
   ...

   # ファイル不在: /dev/null create hunk
   --- /dev/null
   +++ b/<target>
   @@ -0,0 +1,M @@
   +<new line 1>
   ...
   ```

3. **patch text を `workspace/tmp/<agent_run_id>/guarded_patch_input.txt` へ書き込む**: `Write` tool を使う (literal path は `allowed_tmp_root` 配下のため `output_manifest_write_guard` を通過する。`.json` / `.txt` への Bash heredoc redirect は本ファイルの NG 例 (下記参照) で禁止しているため、`Write` tool が canonical 経路)。
4. **guarded-apply-patch を実行**:

   ```bash
   python3 tools/orchestration_runtime.py guarded-apply-patch \
     --repo-root . \
     --orchestration-id <orchestration_id> \
     --actor-role <substep|step> \
     --agent-run-id <agent_run_id> \
     --paths-json '["workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml"]' \
     --patch-file workspace/tmp/<agent_run_id>/guarded_patch_input.txt \
     --capability-token <capability_token>
   ```

5. **race window retry**: コマンドが失敗した場合、`os.path.exists()` と `git apply` の間の race を吸収するため、逆の patch 形式 (update ↔ create) を `Write` tool で再構築して再度 `guarded-apply-patch` を実行する。失敗判定は Bash の終了コード (`echo $?` または出力に含まれる error 文字列) で行う。retry は最大 1 回まで。同一出力パスへの並行書き込みは orchestration の設計上発生しないが、retry/repair で前回の空ファイルが残存する場合に備えて吸収する。

**禁止される代替パターン:**
- `python3 -c "..."` / `python3 - <<'EOF'` で patch を build して subprocess.run する形式 → `forbid_python_inline_write` で **無条件 block** (workflow mode の `tools/hooks/common.py:_validate_workflow_bash_policy` 内 regex `python3?\s+-\s*<<`)。
- `VAR=$(...)` の shell var 割り当て + command substitution → `Bash(python3 ...)` allowlist 一致を壊し session approval 要求。
- `tee` / `cat <<EOF >file` 等で patch を書き出す → `output_manifest_write_guard` または `enforce_guarded_apply_patch` で block。

**禁止パターン（NG — hook がブロックする）:**

```bash
# NG: workspace/ prefix なしのパス
echo "$CONTENT" > ir/spec.ir.yaml

# NG: python3 -c によるインラインファイル書き込み (intent_detected=write)
python3 -c "import json; open('workspace/ir/.../spec.ir.yaml','w').write(json.dumps({}))"

# NG: python3 -c による JSON 読み取り (intent_detected=json_read) — Read tool または jq を使う
python3 -c "import json; print(json.load(open('workspace/orchestrations/<oid>/output_manifests/<id>.json'))['allowed_tmp_root'])"

# NG: python3 -c による UUID 生成 (intent_detected=uuid) — python3 tools/new_agent_run_id.py を使う
python3 -c "import uuid; print(uuid.uuid4())"

# NG: heredoc リダイレクト（直接ファイル指定）
cat <<EOF > workspace/ir/.../spec.ir.yaml
{"key": "value"}
EOF

# NG: workspace/tmp/<agent_run_id>/ 配下であっても、.json/.txt 出力 path への
# heredoc redirect は禁止 (hook は file_path を解釈できず '\"' をパスと誤検知してブロックする)。
# 加えて TMPFILE=$(mktemp ...) のような shell var 割り当ては allowlist 一致を壊し
# session approval を要求するため使用しない。
cat > workspace/tmp/<agent_run_id>/work.json << 'EOF'
{"key": "value"}
EOF
# → patch text を Write tool で workspace/tmp/<arid>/guarded_patch_input.txt に書き込んでから
# guarded-apply-patch --patch-file に渡すこと (本ファイル「`.json` artifact 書き込み — `guarded-apply-patch` 使用手順」節 参照)
```

**重要:** `.json` / `.txt` の出力は本ファイル「`.json` artifact 書き込み — `guarded-apply-patch` 使用手順」節の `guarded-apply-patch` 手順**以外の手段をすべて禁止する**。patch text を `Write` tool で `workspace/tmp/<agent_run_id>/guarded_patch_input.txt` に書き込んだのち、`guarded-apply-patch --patch-file` 経由で適用すること。

**重要:** `--paths-json` と `--patch-text` の `+++ b/` パスはいずれも `workspace/` で始まるプロジェクトルート相対パスとすること。`plans/...`（`workspace/` 接頭辞なし）や絶対パスは `output_manifest_write_guard` でブロックされる。

---

#### `guarded-apply-patch` の strip について

`guarded-apply-patch` に `--strip` という CLI 引数は存在しない。`--paths-json` で渡した `changed_paths` を oracle として `-p1` → `-p0` の順で `git apply --check` を内部試行し、すべての `changed_paths` を被覆できる strip を自動選択する。agent が strip を指定する必要はない。

**エラー `cannot determine patch strip level` が出た場合の対処:**

1. `--paths-json` の path と patch ヘッダ（`+++ b/...`）の prefix を照合する。
   - strip=0（-p0）適用時: `--- workspace/foo/bar.json` + `+++ workspace/foo/bar.json` → changed_path は `workspace/foo/bar.json`
   - strip=1（-p1）適用時: `--- a/workspace/foo/bar.json` + `+++ b/workspace/foo/bar.json` → changed_path は `workspace/foo/bar.json`
2. path の前後に余計な `/` や相対パス記号（`./`）が混入していないか確認する。
3. 新規ファイル作成なら `--- /dev/null` / `+++ b/<path>` 形式にする。

`tools/orchestration_runtime.py` を grep してこのロジックを確認しようとしてはならない（`forbid_tools_direct_read` でブロックされる）。正規参照先はこの段落と `docs/ORCHESTRATION.md#patch-適用契約` である。

---

#### phase ↔ skill 対応表

| step | substep | skill_name | skill_ref |
|---|---|---|---|
| plan | generate | workflow-compile-generate | skills/workflow-compile-generate/SKILL.md |
| plan | verify | workflow-compile-verify | skills/workflow-compile-verify/SKILL.md |
| generate | generate | workflow-generate-generate | skills/workflow-generate-generate/SKILL.md |
| generate | verify | workflow-generate-verify | skills/workflow-generate-verify/SKILL.md |
| tune | generate | workflow-tune-generate | skills/workflow-tune-generate/SKILL.md |
| tune | verify | workflow-tune-verify | skills/workflow-tune-verify/SKILL.md |
| build | — | workflow-build | skills/workflow-build/SKILL.md |
| execute | — | workflow-validate-execute | skills/workflow-validate-execute/SKILL.md |
| judge | — | workflow-validate-judge | skills/workflow-validate-judge/SKILL.md |
| promote | — | workflow-promote | skills/workflow-promote/SKILL.md |

**ネガティブ制約:** 自 phase 以外の SKILL.md を Read してはならない（例: generate substep が `skills/workflow-compile-verify/SKILL.md` を読む行為は `rule_source_violation` を発火する）。launch prompt の `skill_ref` で渡された 1 ファイルだけを読むこと。

---

#### substep ↔ allowed validator gate 対応表

`orchestration agent` が launch prompt 本文に明示・列挙してよい `validate_pipeline_semantics --stage <X>` invocation を `(step, substep)` ごとに canonical 化する。下表の "allowed_stage" 列で許可された `--stage` 以外を launch prompt に記載してはならない。再発防止プラン (Issue 1) を canonical source とする。

| step | substep | allowed `validate_pipeline_semantics --stage` | 備考 |
|---|---|---|---|
| compile | generate | (なし) | gate 呼び出しは `validate_workspace_root` / `check_artifact_syntax --expect-top object` に限定。`io_contract` 関連は `Compile.verify` 責務。 |
| compile | verify | `compile` | `io_contract` 導出後の verify 完了前に必須。 |
| generate | generate | (なし) | `--stage post_generate` は `Generate.verify` 責務。 |
| generate | verify | `post_generate` | |
| build | — | `post_build` | MCP `compile_project` 呼び出し後に invoke。 |
| validate | execute | `post_execute` | `run_program` / `run_quality_checks` 結果の判定に invoke。 |
| validate | judge | `pre_judge` | `aggregate_verdict` 確定前の最終 validation。 |

`--stage full` は end-to-end validation を行う debug 用 stage であり、上記いずれの (step, substep) でも明示的には allow-list に含めない (定常 workflow は per-phase stage を canonical とする)。canonical な `--stage` 値の網羅一覧は `tools/validate_pipeline_semantics.py` の argparse `choices` (`compile` / `post_generate` / `post_build` / `post_execute` / `pre_judge` / `full`) を一次 source とする。

**recording-layer との区別:** `skills/workflow-orchestration/SKILL.md` line 116 は `step_result.json#validation_stage` に**記録してよい**値として step 単位の広めの集合 (`full` を含む) を定義しており、これは write-step-result 時の recording-layer contract である。本表は launch-prompt 時の invocation-layer contract であり、recording-layer よりも厳格な per-substep 制約を課す。両者は別 layer の contract であり、本表で per-substep に絞られた結果として recording される `validation_stage` 値も自動的に SKILL.md line 116 の許容集合に含まれる (例: `compile/verify` で実行可能なのは `compile` のみ → SKILL.md `compile`/`full` 集合の subset)。

**negative constraint:** 上記表に許可されていない `--stage` の `validate_pipeline_semantics` 呼び出しを本 `(step, substep)` の launch prompt に記載してはならない。例: `Compile.generate` 用 prompt に `validate_pipeline_semantics --stage compile` を含めると `Compile.verify` 責務を侵害し `noncanonical_phase_write_attempt` を発火する。MCP tool 名 (`compile_project` 等) の単なる言及 (説明文・negative constraint 等) は本 lint の対象外とする。

`record-launch` は `_validate_launch_prompt_text` 内で `launch_prompt_ref` のテキストに対して per-(step, substep) の allowed-stage 集合を照合する。actionable な invocation 行 (`python3` / `tools/validate_pipeline_semantics.py` / `--gate validate_pipeline_semantics` を含む行) のみを scan し、direct CLI 形式と canonical run-gate JSON 形式 (`--args-json '{"stage": "..."}'`) の両方を抽出して allowed-stage 外なら `ValueError` で reject する (`tools/orchestration_runtime.py::_lint_launch_prompt_gate_allowlist` と `ALLOWED_VALIDATE_PIPELINE_STAGES` が canonical 実装)。緊急 rollback 用に env `METDSL_ENFORCE_GATE_ALLOWLIST=0` で lint を無効化できる (default は有効)。

#### `repair_strategy=reuse` 時の追加契約

`repair_strategy=reuse` での再投入は、`record-agent-run` の `apply_patch_writes` 証跡を `repair_target_agent_run_id` から継承する (`docs/ORCHESTRATION.md` の repair / retry 節を canonical source とする)。launch prompt 本文の `guarded-apply-patch` 関連の constraint 行はそのまま保持してよい — child が実際に同 path を再書き込みする場合は通常通り gate を経由し、何も書かなければ継承証跡で coverage を満たす。継承の信頼性は runtime 側の同一 identity 検証 (`(node_key, step, substep)` 一致) で担保される。

---

#### `allowed_tmp_root` の利用契約

`record-launch` は `workspace/tmp/<agent_run_id>/` を作成して `output_manifests/<agent_run_id>.json` の `allowed_tmp_root` フィールドに記録する。**agent はこの literal path を直接使う**ことで `output_manifest_write_guard` を通過する (write 対象 path のみを判定し `$TMPDIR` env は参照しない、`tools/hooks/common.py:_validate_write_access`)。

**禁止される bootstrap Bash:**

- `export TMPDIR=$(jq -er ...)`、`export TMPDIR=...` — Claude Code session sandbox の approval 要求で workflow が停止する根本原因。
- `jq -er ...` / `printenv` / `bash -c '...'` — 同上。
- `python3 -c "import json; ..."` — `forbid_python_inline_write` (intent_detected=`json_read`) でブロックされる。

**正しい一時ファイル書き込み:**

- **`.json` / `.txt` ファイル**: `Write` tool で `workspace/tmp/<agent_run_id>/<name>.{json,txt}` を直接書き込む。Bash heredoc redirect は `cat > "path" <<EOF` の quoted 形式で hook の file_path parser が `'\"'` をパスと誤検知する既知のリスクがあるため避ける。
- **`.py` / `.yaml` / `.sh` 等**: Bash heredoc で OK。

```bash
# gate stderr 退避 (非 .json/.txt path への redirect は安全)
python3 tools/orchestration_runtime.py run-gate --gate ... 2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt

# 一時 python script
cat > workspace/tmp/<agent_run_id>/build_patch.py <<'EOF'
# script body ...
EOF
python3 workspace/tmp/<agent_run_id>/build_patch.py
```

`.json` / `.txt` patch file の書込は `Write` tool 経由を canonical とする (本ファイル「`.json` artifact 書き込み — `guarded-apply-patch` 使用手順」節 参照)。

`<agent_run_id>` は launch prompt の対応フィールドで literal 置換すること。`$TMPDIR` env は `tools/run_workflow.py` が subprocess に inherit させているため `${TMPDIR}/...` 形式の参照も結果的に動作するが、env 依存を最小化するため literal path を canonical とする。`/tmp/`・`/dev/shm/`・`$(mktemp)` 無引数は引き続き `output_manifest_write_guard` でブロックされる。
