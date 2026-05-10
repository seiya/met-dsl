# Launch Prompts

## `step agent` 起動要求テンプレート

```text
あなたは step agent である。
対象 node_key: <node_key>
対象 step: <step>
orchestration_id: <orchestration_id>
agent_run_id: <agent_run_id>
parent_agent_run_id: <parent_agent_run_id>
workflow_mode: <workflow_mode>
plan_ref: <plan_ref>
pipeline_ref: <pipeline_ref>
dependency_ref: <dependency_ref>
skill_name: <skill_name>
skill_ref: <skill_ref>
skill_must_read_refs: <skill_must_read_refs>
issue_severity: <issue_severity>
repair_strategy: <repair_strategy>
repair_target_agent_run_id: <repair_target_agent_run_id>
repair_reason: <repair_reason>

**Step 0（最初の Bash で必ず実行）** — 1 行コマンド:
    export TMPDIR=$(jq -er '.allowed_tmp_root' "workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json")

以降の `cat > ...`、`mkdir -p ...`、heredoc は **すべて** `$TMPDIR/` 配下で行う。canonical path（`workspace/pipelines/...`、`workspace/plans/...`、`lineage.json` 等）への直接書き込みは、`Edit`/`Write` tool で `allowed_file_tool_paths` に登録済みのものに限る。それ以外は `guarded-apply-patch` を必須とし、Bash heredoc で書くと `enforce_guarded_apply_patch` でブロックされる。

必須要件:
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
- 一時ファイルが必要な場合は `/tmp`・`/dev/shm` を直接指定せず、`$TMPDIR` 環境変数を展開した path のみを使用すること（例: `"${TMPDIR}/work.json"` または `$(mktemp -p "${TMPDIR}")`）。**起動直後の最初の Bash ステップ**で `output_manifests/<agent_run_id>.json` の `allowed_tmp_root` を読み `export TMPDIR=<allowed_tmp_root>` を実行すること（詳細手順は本ファイル末尾「`$TMPDIR` の設定主体と起動手順」参照）。`$TMPDIR` 未設定のまま `/tmp/`・`/dev/shm/` をハードコードすると `output_manifest_write_guard` でブロックされる。
- `gates/<agent_run_id>/` 配下の内部 gate ファイル（`apply_patch_writes.json` 等）は、自身の `agent_run_id` に対応するものであっても直接読んではならない。gate 実行結果の取得は **`run-gate` / `guarded-apply-patch` の stderr** を canonical 経路とし、`2>"${TMPDIR}/last_gate_stderr.txt"` で保存して参照すること（例: `python3 tools/orchestration_runtime.py run-gate --gate ... 2>"${TMPDIR}/last_gate_stderr.txt"`）。失敗時の `violations[]` は stderr に JSON 形式で出力される。要約は `agent.summary.txt` / `step_result.json` を参照すること。他 agent の内部 artifact（`capabilities/`・`output_manifests/`・`read_manifests/`・`access_logs/`・`agents/<other_agent_run_id>/`・`dialogs/` 配下で自身の `agent_run_id` に対応しないファイル）も同様に直接読んではならない。cross-agent read は `rule_source_violation` を発火し phase を fail させる。
- `python3 -c "..."` や `python3 - <<'EOF'` でファイルへの書き込み（`open(path, 'w'/'a'/'x')`・`Path.write_text`・`shutil.copy*` 等）を行ってはならない。`forbid_python_inline_write` でブロックされる。`.json`/`.txt` は `guarded-apply-patch`、その他は `Edit`/`Write` tool を使うこと。
- JSON ファイルの内容確認は `Read` tool を使うこと。`python3 -c "import json; ..."` は `forbid_python_inline_write` でブロックされる。Python 処理がどうしても必要な場合のみ `${TMPDIR}/x.py` に script を書き `python3 ${TMPDIR}/x.py` で実行する。
- UUID 生成は `cat /proc/sys/kernel/random/uuid` を使うこと。`python3 -c 'import uuid; print(uuid.uuid4())'` は `-S` 等の flag を含めて全て `forbid_python_inline_write` でブロックされる。
- 自身の launch prompt 本文は Agent tool 起動時の入力で渡されている。`launches/<agent_run_id>.prompt.txt` を `Read` で再読する必要はなく、`read_manifest_read_guard` でブロックされる。
- `tools/`・`tests/`・validator script・hook 実装への `Read`/`grep`/`sed`/`cat` は `forbid_tools_direct_read` でブロックされる。要件と判定規則は `docs/`・`spec/`・`skill_must_read_refs` のみから解釈すること。`guarded-apply-patch` の内部動作（strip 判定等）は `docs/ORCHESTRATION.md` の「Patch 適用契約」と本ファイル末尾「`guarded-apply-patch` の strip について」を canonical 参照先とする。
- 自身が生成した artifact を参照する際は `output_manifests/<agent_run_id>.json` の `allowed_output_paths` に列挙されたプロジェクトルートからの相対パス（例: `workspace/plans/...`）を使うこと。`/home/<user>/...` 等の絶対パスや `workspace/` 接頭辞を持たないパスを使ってはならない。
- `orchestration_id`・`agent_run_id`・`node_key`・`step`・`write_roots` 等の orchestration メタデータは `capabilities/<agent_run_id>.json` を canonical source とする。`orchestration_meta.json` は `read_manifest_read_guard` でブロックされる。
- `skill_name` と `skill_ref` が未指定の場合は fail で停止すること。launch prompt で指定された `skill_ref` の 1 ファイルのみを読み、**自 phase 以外の SKILL.md を追加 Read してはならない**（phase ↔ skill 対応は本ファイル末尾参照）。
- 入力不足時は推測補完せず fail で停止すること。
- `workflow_mode=dev` の場合、verify 系判定で `issue_severity=major|critical` を検出した時点で fail 停止すること。
- `workflow_mode=dev` で fail した場合、`failure_analysis.json` 生成に必要な根拠（失敗理由、関連 output_refs、主要ログ要約）を返答へ含めること。
- `Plan` の場合、直下依存 `node` の `direct dependency plan readiness` を満たさない限り開始してはならない。
- `Plan` の `plan_meta.json` 更新時は `attempt_count` と `verification_status` と `last_fail_reason` と `debug_mode` と `context_isolated` を必須記録し、`context_isolated=false` の場合は `constraint_reason` を必須記録すること。
- `Generate` / `Build` / `Execute` / `Judge` の場合、直下依存 `node` の `direct dependency execution readiness` を満たさない限り開始してはならない。
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
plan_ref: <plan_ref>
pipeline_ref: <pipeline_ref>
dependency_ref: <dependency_ref>
skill_name: <skill_name>
skill_ref: <skill_ref>
skill_must_read_refs: <skill_must_read_refs>
issue_severity: <issue_severity>
repair_strategy: <repair_strategy>
repair_target_agent_run_id: <repair_target_agent_run_id>
repair_reason: <repair_reason>

**Step 0（最初の Bash で必ず実行）** — 1 行コマンド:
    export TMPDIR=$(jq -er '.allowed_tmp_root' "workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json")

以降の `cat > ...`、`mkdir -p ...`、heredoc は **すべて** `$TMPDIR/` 配下で行う。canonical path（`workspace/pipelines/...`、`workspace/plans/...`、`lineage.json` 等）への直接書き込みは、`Edit`/`Write` tool で `allowed_file_tool_paths` に登録済みのものに限る。それ以外は `guarded-apply-patch` を必須とし、Bash heredoc で書くと `enforce_guarded_apply_patch` でブロックされる。

必須要件:
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
- 一時ファイルが必要な場合は `/tmp`・`/dev/shm` を直接指定せず、`$TMPDIR` 環境変数を展開した path のみを使用すること（例: `"${TMPDIR}/work.json"` または `$(mktemp -p "${TMPDIR}")`）。**起動直後の最初の Bash ステップ**で `output_manifests/<agent_run_id>.json` の `allowed_tmp_root` を読み `export TMPDIR=<allowed_tmp_root>` を実行すること（詳細手順は本ファイル末尾「`$TMPDIR` の設定主体と起動手順」参照）。`$TMPDIR` 未設定のまま `/tmp/`・`/dev/shm/` をハードコードすると `output_manifest_write_guard` でブロックされる。
- `gates/<agent_run_id>/` 配下の内部 gate ファイル（`apply_patch_writes.json` 等）は、自身の `agent_run_id` に対応するものであっても直接読んではならない。gate 実行結果の取得は **`run-gate` / `guarded-apply-patch` の stderr** を canonical 経路とし、`2>"${TMPDIR}/last_gate_stderr.txt"` で保存して参照すること（例: `python3 tools/orchestration_runtime.py run-gate --gate ... 2>"${TMPDIR}/last_gate_stderr.txt"`）。失敗時の `violations[]` は stderr に JSON 形式で出力される。要約は `agent.summary.txt` / `step_result.json` を参照すること。他 agent の内部 artifact（`capabilities/`・`output_manifests/`・`read_manifests/`・`access_logs/`・`agents/<other_agent_run_id>/`・`dialogs/` 配下で自身の `agent_run_id` に対応しないファイル）も同様に直接読んではならない。cross-agent read は `rule_source_violation` を発火し phase を fail させる。
- `python3 -c "..."` や `python3 - <<'EOF'` でファイルへの書き込み（`open(path, 'w'/'a'/'x')`・`Path.write_text`・`shutil.copy*` 等）を行ってはならない。`forbid_python_inline_write` でブロックされる。`.json`/`.txt` は `guarded-apply-patch`、その他は `Edit`/`Write` tool を使うこと。
- JSON ファイルの内容確認は `Read` tool を使うこと。`python3 -c "import json; ..."` は `forbid_python_inline_write` でブロックされる。Python 処理がどうしても必要な場合のみ `${TMPDIR}/x.py` に script を書き `python3 ${TMPDIR}/x.py` で実行する。
- UUID 生成は `cat /proc/sys/kernel/random/uuid` を使うこと。`python3 -c 'import uuid; print(uuid.uuid4())'` は `-S` 等の flag を含めて全て `forbid_python_inline_write` でブロックされる。
- 自身の launch prompt 本文は Agent tool 起動時の入力で渡されている。`launches/<agent_run_id>.prompt.txt` を `Read` で再読する必要はなく、`read_manifest_read_guard` でブロックされる。
- `tools/`・`tests/`・validator script・hook 実装への `Read`/`grep`/`sed`/`cat` は `forbid_tools_direct_read` でブロックされる。要件と判定規則は `docs/`・`spec/`・`skill_must_read_refs` のみから解釈すること。`guarded-apply-patch` の内部動作（strip 判定等）は `docs/ORCHESTRATION.md` の「Patch 適用契約」と本ファイル末尾「`guarded-apply-patch` の strip について」を canonical 参照先とする。
- 自身が生成した artifact を参照する際は `output_manifests/<agent_run_id>.json` の `allowed_output_paths` に列挙されたプロジェクトルートからの相対パス（例: `workspace/plans/...`）を使うこと。`/home/<user>/...` 等の絶対パスや `workspace/` 接頭辞を持たないパスを使ってはならない。
- `orchestration_id`・`agent_run_id`・`node_key`・`step`・`write_roots` 等の orchestration メタデータは `capabilities/<agent_run_id>.json` を canonical source とする。`orchestration_meta.json` は `read_manifest_read_guard` でブロックされる。
- `skill_name` と `skill_ref` が未指定の場合は fail で停止すること。launch prompt で指定された `skill_ref` の 1 ファイルのみを読み、**自 phase 以外の SKILL.md を追加 Read してはならない**（phase ↔ skill 対応は本ファイル末尾参照）。
- 入力不足時は推測補完せず fail で停止すること。
- `workflow_mode=dev` の場合、verify 系判定で `issue_severity=major|critical` を検出した時点で fail 停止すること。
- `workflow_mode=dev` で fail した場合、`failure_analysis.json` 生成に必要な根拠（失敗理由、関連 output_refs、主要ログ要約）を返答へ含めること。
- `Plan` の substep は、直下依存 `node` の `direct dependency plan readiness` を満たさない限り開始してはならない。
- `Plan` の `plan_meta.json` 更新時は `attempt_count` と `verification_status` と `last_fail_reason` と `debug_mode` と `context_isolated` を必須記録し、`context_isolated=false` の場合は `constraint_reason` を必須記録すること。
- `Generate` / `Build` / `Execute` / `Judge` の substep は、直下依存 `node` の `direct dependency execution readiness` を満たさない限り開始してはならない。
- 直下依存 `node` が未完了でも、依存先 code を対象 `node` の `src/` へ内包して代替してはならない。
- **MCP 副次出力の `mcp_command_log.jsonl` を必ず `allowed_output_paths` に含めること**。canonical placement は phase ごとに以下:
  - Generate substep: `<pipeline_ref>/generate/<generation_id>/src/mcp_command_log.jsonl` (run_linter)
  - Build step (in-phase, CMake/Meson out-of-source): `<pipeline_ref>/build/<build_id>/mcp_command_log.jsonl` (compile_project, project_dir=<build_id>/)
  - Build step (cross-phase, Make in-source for Fortran/C-family): `<pipeline_ref>/generate/<generation_id>/src/mcp_command_log.jsonl` (compile_project, project_dir=<gen>/src/)。launch request の `generation_id` で bind され、`record-launch` が `generate_meta.json` の `verification_status=pass` を検証する
  - Execute step (in-phase): `<pipeline_ref>/execute/<execution_id>/<node_safe>/mcp_command_log.jsonl` (run_program 等)
  - Execute step (cross-phase quality_check): `<pipeline_ref>/generate/<generation_id>/src/mcp_command_log.jsonl` (`skills/workflow-execute/SKILL.md` L20 — `toolchain.build_system=make` + Fortran/C-family では `run_quality_checks` を `project_dir=generate/<generation_id>/src/` で実行するため、log は generate ツリーに副次出力される)。Execute step の launch request に `generation_id` を含めると runtime が cross-phase canonical placement を auto-inject し、phase contract と write_roots check を bypass する。`generation_id` は `<pipeline_ref>/generate/<generation_id>/generate_meta.json` の存在で検証され、未知の generation_id (実際の generate 実行に対応しない値) を渡すと `record-launch` が `ValueError` で reject する (任意 caller による cross-phase write authorization injection 防止)。

  さらに、`validate_pipeline_semantics.py` は MCP tool 実行証跡として trust する全 `command_log_ref` を canonical placement のみに制限する:
  - `lint_command_ref.run_linter[].command_log_ref`: `<gen_dir>/src/mcp_command_log.jsonl`
  - `source_command_ref.<run_program-key>.command_log_ref`: `<execute node_dir>/mcp_command_log.jsonl` (sibling of trial_meta)
  - `source_command_ref.run_quality_checks.command_log_ref`: `<pipeline_ref>/generate/<source_generation_id>/src/mcp_command_log.jsonl` — `trial_meta.source_generation_id` で **単一の gen_id にのみ bind** される。同 pipeline の sibling/older generation の canonical placement は受理しない。

  非 canonical path への placement (例: `<execute>/raw/forged.jsonl`) は post_generate / post_execute gate が reject する (forge MCP execution evidence の防止)。

  さらに `_validate_trial_meta` は `source_command_ref` の各 entry が指す log record に **recognized MCP `tool_name`** (`run_program` / `run_quality_checks`) が含まれることを必須とする。`compile_project` は build phase の道具で execute trial_meta では受理しない。`tool_name` 欠落 / 未知の値の forge record は reject される (tool-specific validator が silent skip する経路を遮断)。

  Execute step の cross-phase canonical 配置は launch request の `generation_id` フィールドにのみ bind される。`allowed_output_paths` に `<pipeline_ref>/generate/<other_gen>/src/...` (request の `generation_id` と異なる generation) を含めると phase contract が reject する。Trial_meta 側でも `source_generation_id` を必須記録とし、execute と generate の bind を確定させる。

  **単一 namespace 強制:** generate / build / execute step の `allowed_output_paths` は単一の `<gen_id>` / `<build_id>` / `<exec_id>` のみを target としなければならない。同 pipeline 配下に複数 id を混在 listing すると `record-launch` が `ValueError` で reject する (sibling/older run の audit log への write authority 付与を防止)。Generate / Execute step では追加で request の `generation_id` / `execution_id` と listed paths の id が一致することを要求する (mismatch は `does not match request ...id` で reject)。

  **Quality_check stale-generation 対策:** `trial_meta.source_generation_id` が指す `generate_meta.json` は `verification_status=pass` でなければならない。失敗 / 古い generation を quality_check evidence として参照すると post_execute validator が reject する。さらに **`record-launch` も** `verification_status` を check し、failed generation 配下の MCP audit log に対する write authority 付与を launch 時点で reject する (failed gen tree の provenance contamination 防止)。

  **Build lineage bind (specific build):** Execute step の launch request は `source_build_id` を必須記録とし、`<pipeline>/build/<source_build_id>/build_meta.json` の `source_generation_id` と request の `generation_id` が一致しなければならない。`source_build_id` 欠落、build_meta.json 不在、`source_generation_id` 未記録、または値の mismatch は record_launch が reject する。これにより mixed-build forge (build A の binary を実行しながら build B の quality_check evidence を流用) を防止。Build step は `build_meta.json` に `source_generation_id` を必須記録 (`skills/workflow-build/SKILL.md` 参照)。

  **Cross-phase auto-inject の Make-only ゲート:** `<pipeline>/generate/<generation_id>/src/mcp_command_log.jsonl` への cross-phase write authority は `toolchain.build_system=make` (Fortran/C-family in-source build) のときのみ auto-inject される。CMake/Meson/Ninja 等の out-of-source toolchain では cross-phase は注入されず、build/execute の log は in-phase canonical (`<build_id>/mcp_command_log.jsonl` または `<exec_id>/<node_safe>/mcp_command_log.jsonl`) のみ許可される。`record-launch` は `impl.resolved.yaml` の `toolchain.build_system` を読んで自動判定する。

  **`ok=true` requirement for execute evidence:** post_execute validator は `run_program` / `run_quality_checks` record の `ok=true` を必須要求する。`ok=false` または `ok` 欠落の record は失敗実行とみなし、tool-execution evidence として認めない (lint validator と同じポリシー)。

  **Role binding for source_command_ref:** Execute trial_meta の `source_command_ref` 各 entry は `tool_name` フィールド (= `run_program` または `run_quality_checks`) を宣言し、log record の `tool_name` と一致しなければならない。`compile_project` は build phase 専用で execute trial_meta では受理しない。trial_meta は最低 1 つ `tool_name='run_program'` entry を含むことが必須 (実プログラム実行証跡)。role mismatch (例: run_program slot に compile_project record) は forge とみなし reject される。

  **Run_program log canonical placement (MCP gate enforcement):** `validate_mcp_build_tool_invocation` (MCP server pre-call gate) は `tool_name=run_program` かつ `step=execute` の呼び出しで log placement を canonical (`<pipeline_ref>/execute/<execution_id>/<node_safe>/mcp_command_log.jsonl`) のみに強制する。`project_dir` を execute node_dir に設定するか、`command_log_path` 引数で canonical absolute/relative path を明示すること。非 canonical 配置は MCP 呼び出し時点で `RuntimeError` で reject され、後の post_execute validator まで遅延しない。

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

```bash
# --patch-file を使用することで:
#   (a) patch text をファイル経由で渡し argv の ARG_MAX 制限を真に回避する
#   (b) 存在確認と apply の race を retry で吸収する
# patch ファイルの書き込み先は $TMPDIR（= workspace/tmp/<agent_run_id>/）で
# output_manifest_write_guard の許可範囲に含まれる。
python3 - <<'APPLY'
import os, json, subprocess, pathlib

target = "workspace/plans/<node_key_safe>/<plan_id>/derived_contract.json"
orchestration_id = os.environ["METDSL_ORCHESTRATION_ID"]
agent_run_id = "<agent_run_id>"
capability_token = "<capability_token>"
patch_file = pathlib.Path(os.environ["TMPDIR"]) / "guarded_patch_input.txt"

new_data = {"spec_id": "<spec_id>", "key": "value"}
new_content = json.dumps(new_data, indent=2, ensure_ascii=False) + "\n"
new_lines = new_content.splitlines(keepends=True)

def build_patch(file_exists):
    """file_exists=True → update/replace 形式, False → /dev/null create 形式"""
    if file_exists:
        with open(target) as f:
            old_lines = f.readlines()
        if old_lines:
            hdr = f"--- a/{target}\n+++ b/{target}\n@@ -1,{len(old_lines)} +1,{len(new_lines)} @@\n"
            bdy = "".join("-" + l for l in old_lines) + "".join("+" + l for l in new_lines)
        else:
            # 0 バイト既存ファイル: コンテンツなし → 全行追加 update ハンク
            hdr = f"--- a/{target}\n+++ b/{target}\n@@ -0,0 +1,{len(new_lines)} @@\n"
            bdy = "".join("+" + l for l in new_lines)
    else:
        # ファイル不在: /dev/null create hunk
        hdr = f"--- /dev/null\n+++ b/{target}\n@@ -0,0 +1,{len(new_lines)} @@\n"
        bdy = "".join("+" + l for l in new_lines)
    return hdr + bdy

def apply(patch_text):
    patch_file.write_text(patch_text)
    subprocess.run([
        "python3", "tools/orchestration_runtime.py", "guarded-apply-patch",
        "--repo-root", ".",
        "--orchestration-id", orchestration_id,
        "--actor-role", "substep",
        "--agent-run-id", agent_run_id,
        "--paths-json", json.dumps([target]),
        "--patch-file", str(patch_file),
        "--capability-token", capability_token,
    ], check=True)

# 存在確認→apply。失敗した場合は逆の形式で 1 回リトライして race を吸収する。
first_exists = os.path.exists(target)
try:
    apply(build_patch(first_exists))
except subprocess.CalledProcessError:
    apply(build_patch(not first_exists))
APPLY
```

**禁止パターン（NG — hook がブロックする）:**

```bash
# NG: workspace/ prefix なしのパス
echo "$CONTENT" > plans/derived_contract.json

# NG: python3 -c によるインラインファイル書き込み (intent_detected=write)
python3 -c "import json; open('workspace/plans/.../derived_contract.json','w').write(json.dumps({}))"

# NG: python3 -c による JSON 読み取り (intent_detected=json_read) — Read tool または jq を使う
python3 -c "import json; print(json.load(open('workspace/orchestrations/<oid>/output_manifests/<id>.json'))['allowed_tmp_root'])"

# NG: python3 -c による UUID 生成 (intent_detected=uuid) — cat /proc/sys/kernel/random/uuid を使う
python3 -c "import uuid; print(uuid.uuid4())"

# NG: heredoc リダイレクト（直接ファイル指定）
cat <<EOF > workspace/plans/.../derived_contract.json
{"key": "value"}
EOF

# NG: $TMPDIR 配下であっても、.json/.txt 出力 path への heredoc redirect は禁止
# hook は file_path を解釈できず '\"' をパスと誤検知してブロックする
TMPFILE=$(mktemp "${TMPDIR}/work.json.XXXXXX")
cat > "${TMPFILE}" << 'EOF'
{"key": "value"}
EOF
# → patch text を変数に組み立てて guarded-apply-patch --patch-file に渡すこと（line 124-180 参照）
```

**重要:** `.json` / `.txt` の出力は上記の `guarded-apply-patch` 手順（line 124-180）**以外の手段をすべて禁止する**。patch text を変数に組み立てること自体は許可されるが、最終書き込みは必ず `guarded-apply-patch --patch-file` 経由で行うこと。

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
| plan | generate | workflow-plan-generate | skills/workflow-plan-generate/SKILL.md |
| plan | verify | workflow-plan-verify | skills/workflow-plan-verify/SKILL.md |
| generate | generate | workflow-generate-generate | skills/workflow-generate-generate/SKILL.md |
| generate | verify | workflow-generate-verify | skills/workflow-generate-verify/SKILL.md |
| tune | generate | workflow-tune-generate | skills/workflow-tune-generate/SKILL.md |
| tune | verify | workflow-tune-verify | skills/workflow-tune-verify/SKILL.md |
| build | — | workflow-build | skills/workflow-build/SKILL.md |
| execute | — | workflow-execute | skills/workflow-execute/SKILL.md |
| judge | — | workflow-judge | skills/workflow-judge/SKILL.md |
| promote | — | workflow-promote | skills/workflow-promote/SKILL.md |

**ネガティブ制約:** 自 phase 以外の SKILL.md を Read してはならない（例: generate substep が `skills/workflow-plan-verify/SKILL.md` を読む行為は `rule_source_violation` を発火する）。launch prompt の `skill_ref` で渡された 1 ファイルだけを読むこと。

---

#### `$TMPDIR` の設定主体と起動手順

`record-launch` は `workspace/tmp/<agent_run_id>/` を作成するが、`$TMPDIR` 環境変数の `export` は **agent が自身で行う**責務である。

**起動直後の最初の Bash ステップで必ず実行すること:**

```bash
# output_manifest から allowed_tmp_root を読み取り TMPDIR を確定する
set -e
_TMPROOT=$(jq -er '.allowed_tmp_root // empty' "workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json")
export TMPDIR="${_TMPROOT}"
```

- `python3 -c "import json; ..."` は `forbid_python_inline_write` (intent_detected=`json_read`) でブロックされるため使用しない。`jq` は workflow 環境の必須ツールとして利用可能（`docs/RUNBOOK.md` の required tools 参照）。
- `-e` flag と `// empty` filter により、file 不在 / `allowed_tmp_root` field 欠損 / null 値の場合に `jq` が exit 1 を返し、`set -e` 下で fail-fast する。`TMPDIR=null` のような壊れた状態で後続を続行しないこと。

- `<orchestration_id>` と `<agent_run_id>` は launch prompt の対応フィールドで置換すること。
- 以降のすべての一時ファイルは `${TMPDIR}/` 配下に置くこと。
- `/tmp/`・`/dev/shm/`・`$(mktemp)` 無引数（`$TMPDIR` 未設定時は `/tmp` へフォールバック）は禁止。`$(mktemp -p "${TMPDIR}")` は OK。
