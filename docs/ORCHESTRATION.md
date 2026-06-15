# Workflow Orchestration

この文書は、`workflow` 全体を統括する `orchestration agent` と、phase / substep の独立エージェント実行規約を定義する。`Spec -> Compile -> Generate -> Build -> Validate` の 5 phase 構成を前提とする。

## 関連文書

- CLI 全 subcommand reference: [`docs/CLI_REFERENCE.md`](CLI_REFERENCE.md)
- workspace artifact 配置: [`docs/WORKSPACE_LAYOUT.md`](WORKSPACE_LAYOUT.md)
- workflow 起動契約: [`skills/workflow-orchestration/SKILL.md`](../skills/workflow-orchestration/SKILL.md) と [`skills/workflow-orchestration/references/startup_contract.md`](../skills/workflow-orchestration/references/startup_contract.md)
- launch 要求テンプレート: [`skills/workflow-orchestration/references/launch_prompts.md`](../skills/workflow-orchestration/references/launch_prompts.md)

## 目的
- workflow 実行を階層化し、phase responsibilities と監査責務を分離する。
- 各 `step` / 各 `substep` を独立エージェントとして実行し、実行経路を追跡可能にする。

## 適用範囲
- `Compile` / `Generate` / `Build` / `Validate`
- `node workflow` 単位の phase 実行と、phase 内 `substep`（`generate` / `verify` / `execute` / `judge`）の実行

## term rules
- `phase` は `docs/workflow/WORKFLOW_CORE.md` と `docs/workflow/phases/` 配下の契約文書で定義する workflow の論理単位を指す。
- `step` は 1 つの phase に対応するオーケストレーション上の実行単位を指す。
- `substep` は `step` を分解した下位実行単位を指す。
- `stage` は `generated_by_stage` などの既存フィールド名としてのみ使用する。

## phase / substep 種別

| phase | step 種別 | substep |
|-------|-----------|---------|
| Compile | substep を持つ | `generate` / `verify` |
| Generate | substep を持つ | `generate` / `verify` |
| Build | 単一 step | - |
| Validate | substep を持つ | `execute` / `judge` |

## 要件

### preflight と起動制御
- workflow 実行は、必ず 1 つの `orchestration agent` を最初に起動して開始する。
- workflow 開始前に、`step agent` と `substep agent` を独立起動できる execution platform の preflight を必須実行しなければならない。preflight は `multi_agent` 機能と子 `agent` 起動可否を検証対象に含め、`pass` でない場合は workflow を開始してはならない。
- `backend=codex` の preflight は、`feature_states.codex_hooks=true` と `checks.codex_hooks_enabled.pass=true` と `checks.codex_home_writable.pass=true` を同時に満たさなければならない。
- preflight は `sandbox_runtime=bwrap` と `sandbox_enforced=true` を必須条件に含めなければならない。`checks.sandbox_bwrap_available.pass=true` または `checks.sandbox_bwrap_userns.pass=true` または `checks.sandbox_bwrap_exec.pass=true` の少なくとも 1 つを満たさない場合、workflow を開始してはならない。
- native hook 実行時の `codex_hooks` feature 判定は `orchestration_id` ごとに最初の 1 回だけ実行し、結果を `workspace/orchestrations/<orchestration_id>/hooks/codex_feature_check.json` へキャッシュしなければならない。
- `preflight.json` の手動編集による `pass` 化を禁止する。
- 子 `agent` 起動直前に execution platform の live probe で `multi_agent` と子 `agent` 起動可否を再検査しなければならない。`fail` の場合は `record-launch` と子 `agent` 起動を禁止し、workflow を `fail` へ遷移させる。
- 各 phase の着手前に `workflow-launch-check` を実行し、required child `agent` 種別判定、execution platform 可否、session policy 可否、dependency readiness を同時に検査しなければならない。

### phase 種別と agent 種別
- 各 phase の着手前に、対象 phase が `step agent` 必須か `substep agent` 必須かを phase 種別で明示判定しなければならない。`Compile` / `Generate` / `Validate` は `substep agent` 必須、`Build` は `step agent` 必須とする。
- phase 着手前判定で子 `agent` 必須と確定した場合、親 `agent` は `spawn_agent` 完了前に phase artifact 生成、MCP 実行、検証目的の仮実装を開始してはならない。
- `workspace/ir/` と `workspace/pipelines/` の phase artifact root は、`record-launch` と capability token と `phase_state=child_running` の 3 条件を満たした child `agent` だけが実体化できる。`orchestration agent` による直接生成を禁止する。
- child `agent` 起動前に root path 予約が必要な場合は、`workspace/orchestrations/<orchestration_id>/reservations/<node_key_safe>/<step>.json` の reservation artifact のみを生成し、`workspace/ir/` と `workspace/pipelines/` の実ディレクトリを作成してはならない。
- `orchestration agent` は workflow 全体の進行制御のみを担当し、phase 本体の artifact（例: `spec.ir.yaml`、`diagnostics.json`）を直接生成してはならない。
- workflow 実行の代替として、複数 phase の進行と artifact generation を一括自動化する script を新規生成または実行してはならない。
- `Build` step は MCP `compile_project` を呼び出す determinstic な処理で、LLM 推論を要しない。`step agent` は MCP 呼び出しと結果記録に責務を限定する。
- `Compile` / `Generate` / `Validate` の各 substep は `orchestration agent` が `spawn_agent` で直接起動する。

### capability / write_root
- child `agent` に許可する phase artifact の変更は、capability token が許可した `write_root` 配下に限定しなければならない。
  - `Compile.generate` / `Compile.verify`: `workspace/ir/<node_key_safe>/<ir_id>/`
  - `Generate.generate` / `Generate.verify`: `workspace/pipelines/<node_key_safe>/<pipeline_id>/source/<source_id>/`
  - `Build`: `workspace/pipelines/<node_key_safe>/<pipeline_id>/binary/<binary_id>/`
  - `Validate.execute` / `Validate.judge`: `workspace/pipelines/<node_key_safe>/<pipeline_id>/runs/<run_id>/<node_key_safe>/`
- `ir_ref` / `pipeline_ref` 配下の変更は、`.json` / `.txt` 出力については `guarded-apply-patch` を通過した canonical path、それ以外の extension については `output_manifests/<agent_run_id>.json` の `allowed_file_tool_paths` に列挙された path への `Edit` / `Write` 直接書き込みに限定する。
- `record-launch` は child `agent_run_id` ごとに `workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json` を生成し、`allowed_output_paths` と `allowed_file_tool_paths` と `allowed_tmp_root`（`workspace/tmp/<agent_run_id>`）を確定しなければならない。
- **Make build の必須 file pin auto-inject**: `Generate` step かつ `spec.ir.yaml.impl_defaults.toolchain.build_system=make` のとき、`record-launch` は in-source `Makefile` (`<pipeline_ref>/source/<source_id>/src/Makefile`) を `allowed_output_paths` へ自動注入し、`allowed_file_tool_paths` へ流す。bare な `src/` directory entry だけでは source 拡張子 (`.f90`/`.c`) は `guarded-apply-patch` で書けても拡張子なしの `Makefile` は directory-allowlist の source-extension 集合から意図的に除外 (`tools/hooks/common.py`) されるため全経路で書けず、child が mid-run で推測回避 fail-stop する。orchestration agent は通常 `allowed_file_tool_paths` を省略 (auto-derive) すればよい。
- **launch-time provisioning 検証**: `record-launch` は、Make build Generate launch で必須 `Makefile` pin が確定後の `allowed_file_tool_paths` に欠落している場合 (caller が明示 `allowed_file_tool_paths` を渡して pin を漏らした等)、**child 起動前に `ValueError` で fail-fast** する。artifact を汚染する mid-run fail-stop を、安価で recoverable な launch-time error に変換する。
- `record-launch` は child `agent_run_id` ごとに `workspace/orchestrations/<orchestration_id>/read_manifests/<agent_run_id>.json` を生成し、`allowed_read_roots` と `denied_read_roots` を確定しなければならない。
- `record-launch` は child `agent_run_id` ごとに `workspace/orchestrations/<orchestration_id>/sandbox_profiles/<agent_run_id>.json` を生成し、`bwrap` 実行に必要な `read_roots` と `write_roots` と runtime bind 構成を確定しなければならない。child 起動は当該 profile を用いた `bwrap` 実行のみを許可する。

### file write 経路
- `step agent` / `substep agent` は phase artifact を変更する場合、出力 path の extension で書き込み経路を分岐しなければならない。`.json` / `.txt` の出力は `apply_patch_writes` gate を通過した `guarded-apply-patch` を canonical invocation とし、`.yaml` / `.yml` / `.md` / source code 等の上記以外の extension は `output_manifests/<agent_run_id>.json.allowed_file_tool_paths` に列挙された path への `Edit` / `Write` 直接書き込みを canonical invocation とする。
- `spec.ir.yaml` は `.yaml` 形式なので `Edit` / `Write` で書き込む。
- 通常 `apply_patch` の直接実行、shell redirection、`tee`、`sed -i`、`perl -0pi`、`python` / `sh` / `bash` による file write は禁止する。
- `guarded-apply-patch` の疎通確認は dry-run または no-op patch で実施しなければならない。
- shell による file write は、対象 path が phase artifact かどうかを問わず、child `agent` 起動要求で明示した canonical invocation に含まれない限り禁止しなければならない。

### LLM コンテキスト
- `step agent` と `substep agent` は、同一 `LLM` コンテキストを共有してはならない。各 `agent_run_id` は固有の `context_id` を持ち、`context_isolated=true` を必須記録とする。
- `Compile.generate` と `Compile.verify` は独立コンテキストで起動する。`Generate.generate` と `Generate.verify`、`Validate.execute` と `Validate.judge` も同様。

### agent_run 記録
- `orchestration agent` は `substep` を持つ phase で必要な `substep` 群を起動し、完了判定を行った後に `step_result.json` を確定しなければならない。
- `orchestration agent` は `deps.yaml` と `spec_catalog.yaml` から再構成した依存関係と `spec.ir.yaml` の `dependency` セクションに基づいて `step agent` または `substep agent` の起動可否を判定しなければならない。
- すべての `agent` 実行は `agent_run_id` を持ち、入力参照・出力参照・親子関係を記録しなければならない。
- `agent_runs.jsonl` の各行は `started_at` と `status` を必須記録とし、`status` が終端状態（`pass` / `fail` / `blocked` / `timeout` / `cancel`）の場合は `finished_at` を必須記録とする。
- `fail_closed` は `orchestration_meta.status` の終端状態としてのみ使用する。
- `step` / `substep` ロールの `agent_runs.jsonl` は `parent_agent_run_id` と `agent_backend` と `agent_model` と `context_id` と `context_isolated` と `agent_session_id` と `launch_request_ref` と `launch_response_ref` と `launch_prompt_ref` と `launch_reply_ref` と `agent_result_ref` と `agent_summary_ref` を必須記録とする。
- `substep agent` の `parent_agent_run_id` は、当該 `substep` を起動した `orchestration agent_run_id` を指す。
- `spawn_agent` の応答で得た子 `agent` 識別子は `agent_session_id` として記録しなければならない。
- `record-launch` は child 起動直後に `workspace/orchestrations/<orchestration_id>/session_run_index.json` を更新し、`agent_run_id` と `agent_session_id` と `context_id` と `agent_role` と `status` を記録しなければならない。
- `agent_graph.json` の `edge` は、`orchestration -> step` または `orchestration -> substep` を canonical source とする。
- `agent_runs.jsonl` と `agent_graph.json` は、実行中イベントを逐次追記して生成しなければならない。後生成を禁止する。
- `orchestration agent` 自身の `agent_runs.jsonl` 行は **起動直後に 1 回だけ** `agent_role=orchestration`、`status=running` で append する。`orchestration agent` 自身が `record-agent-run` でこの行を更新する経路は持たない（二重 invoke は `ValueError: duplicate agent_run_id` で reject される）。終端時には `set-status` が runtime 権限でこの行を in-place に terminal status へ書き換え `finished_at` を付与し（append ではなく rewrite のため duplicate guard に抵触しない）、resume（terminal reset）時は逆に `running` へ戻す。これは agent_runs ベースの audit / `validate_workspace_root` が orchestration 行を恒久 `running` と誤認するのを防ぐためであり、orchestration 全体の canonical な terminal 状態は引き続き `set-status` 経由で `orchestration_meta.json` 側に表現する。詳細は [docs/CLI_REFERENCE.md#record-agent-run](CLI_REFERENCE.md#record-agent-run) と [docs/CLI_REFERENCE.md#set-status](CLI_REFERENCE.md#set-status) を canonical source とする。
- `record-launch` は、`spawn_agent` 成功直後の request/response 保存専用処理としなければならない。
- `record-launch` は launch response に `sandbox_runtime=bwrap` と `sandbox_enforced=true` と `sandbox_profile_ref` を記録しなければならない。

### child agent 起動要求
- `orchestration agent` は、子 `agent` 起動時に `docs/workflow/WORKFLOW_CORE.md` と対象 `step` に対応する `docs/workflow/phases/phase_*.md` を canonical source として、対象 `step` または `substep` の `execution input` と `verification input` と `expected output` を明示しなければならない。
- `orchestration agent` は、子 `agent` 起動要求に要求定義と判定規則の canonical source が `docs/` と `spec/` と当該試行 artifact であることを明示しなければならない。`tools/` 配下の実装を読んで rule を抽出する指示または黙示を禁止する。
- 子 `agent` の validator invocation は `run-gate` を原則とし、`python3 tools/orchestration_runtime.py run-gate --gate <gate_name> --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '<json>'` を canonical invocation とする。
- validator script の直接実行は例外運用としてのみ許可する。許可対象は `validate_workspace_root.py` と `check_artifact_syntax.py` に限定する。
- 子 `agent` が `apply_patch` を実行する場合、`python3 tools/orchestration_runtime.py guarded-apply-patch --repo-root <repo_root> --orchestration-id <orchestration_id> --actor-role <step|substep> --agent-run-id <agent_run_id> --paths-json '["..."]' --patch-text '<patch_text>' --capability-token <capability_token>` を canonical invocation とする。`guarded-apply-patch` の使用対象は `.json` / `.txt` 出力に限定する。
- `record-agent-run` は、child `agent` が申告した `output_refs` と `apply_patch_writes` gate 記録、および `output_manifests/<agent_run_id>.json.allowed_file_tool_paths` に加えて、baseline との差分で実変更 path を検査しなければならない。実変更 path が capability token の `write_root` 配下にない、または gate 許可 path と `allowed_file_tool_paths` のいずれにも含まれない場合、`unauthorized write` として reject する。
- **runtime placeholder 復元 (recoverability)**: 終端検査の baseline diff の前に、`record-agent-run` は `created_file_pin_stubs` に記録された runtime-owned placeholder (例 `lineage.json`) のうち、gate 経由で書換えられておらず (= `gate_changed_paths` 非被覆) 現在 absent なものを 0-byte で復元する。これにより、collateral に削除された runtime placeholder が `unauthorized write` として判定され、復元手段を持たない orchestration agent が恒久 `fail_closed` に陥る deadlock を防ぐ。`status=fail`/`blocked`/`timeout` の terminal record でも適用し、失敗 run を `agent_runs.jsonl` に記録可能とする (clean restart を可能にする)。`record-agent-run` は runtime 権限で動作するため、orchestration agent 自身には禁止される canonical-path write がここでは許容される。
- `orchestration agent` は、子 `agent` 起動要求本文を `skills/workflow-orchestration/references/launch_prompts.md` の対応テンプレートから生成しなければならない。テンプレートを使わない自由形式 prompt を禁止する。
- `ir_ref` と `pipeline_ref` と `dependency_ref` は、子 `agent` 起動前に canonical path を確定しなければならない。placeholder を起動要求へ記録してはならない。

### repair / retry
- `orchestration agent` は、子 `agent` の返却結果を評価して `issue_severity`（`minor` / `major` / `critical`）を判定しなければならない。
- `orchestration agent` は、`issue_severity` と契約逸脱範囲に基づいて再投入要否を判定し、再投入が必要な場合は `repair_strategy`（`reuse` / `restart`）を選択しなければならない。
- `orchestration agent` は、phase artifact の repair を自身で直接実施してはならない。repair が必要な場合は、対象 `step` または `substep` の child `agent` へ再委譲しなければならない。
- `repair_strategy=reuse` は、対象 `step` または `substep` の input contract と expected output を変更せず、局所修正で収束可能な場合にのみ選択してよい。
- `repair_strategy=restart` は、契約再解釈、設計再構成、広範囲再生成のいずれかが必要な場合に選択する。
- 再投入時は `repair_strategy` を問わず、新規 `agent_run_id` と新規 `context_id` を発行する。
- `repair_strategy=reuse` の場合、`agent_session_id` は再利用してよい。`repair_strategy=restart` の場合、`agent_session_id` は新規発行しなければならない。
- 再投入時の `launches/<agent_run_id>.request.json` は、`issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` を必須記録とする。
- `repair_strategy=reuse` で `apply_patch_writes` gate 証跡は `repair_target_agent_run_id` から継承する。新規 `guarded-apply-patch` 呼び出しは不要とし、`record-agent-run` は `gates/<repair_target>/apply_patch_writes.json` を canonical な証跡として参照する。継承の事実は `<orch_root>/agents/<agent_run_id>/audit/gate_inheritance.json` に記録され audit 経路を構成する。`repair_strategy=restart` の場合は継承しない（契約再解釈のため新規証跡を必須とする）。

### 再投入時の baseline diff 契約
- `record-agent-run` reject 後の retry における child baseline diff は、毎回 live workspace を walk して baseline と比較する (`_compute_changed_paths_against_baseline`)。これにより deactivate 後の任意の filesystem 改変も terminal write validation の対象に残る。
- retry の brick-cascade 防止のため、`<orch_root>/agent_runs_invalid.jsonl` (および lock sidecar) のみを runtime-owned 単体 file として live diff から narrow に除外する。これは前回の `record-agent-run` 失敗が次回 retry の diff を contaminate する原因 file であり、`record-agent-run` 自身が writer であることが確定している。`<orch_root>/audit/`・`<orch_root>/violations/`・`<orch_root>/failure_analysis.json` 等の他 control-plane path は blanket exempt **しない** — child の hook-bypass write が terminal validation に backstop として残る (`tools/orchestration_runtime.py::_should_ignore_runtime_snapshot_path` を canonical source とする)。
- `<orch_root>/launches/...`・`<orch_root>/violations/...`・`<orch_root>/agents/...` 等の per-arid runtime-managed prefix も diff から除外する (元来 runtime-only directories)。`workspace/tmp/<parent_arid>/` 配下の parent scratch は `_validate_actual_write_paths` の `parent_tmp_root` exclusion で除外する。
- `deactivate-child` は `<orch_root>/agents/<agent_run_id>/deactivate_snapshot.json` に child-authored path 集合を audit 用に保存する。本 snapshot は人手 audit と将来の debug 用途のみで、`record-agent-run` の validation 経路では参照しない (live diff を必ず通す)。

- 失敗 phase からのフィードバック方向は phase ごとに固定する:
  - `Compile` 失敗 → Compile 内 retry のみ（上流は人手 Spec のため自動 retry なし）。
  - `Generate` 失敗 → Generate 内 retry。`source_meta.json` の verify 失敗が `attribution=ir` と判定された場合は `Compile` まで戻す。
  - `Build` 失敗 → Generate に戻す。Build 自体は決定的処理ゆえ LLM を介さず、`build_log` に記録された `compile_error` / `link_error` / `make_error` のいずれかを `repair_reason` として Generate へ転送する（詳細は `docs/workflow/phases/phase_03_build.md` の retry trigger 節）。
  - `Validate` 失敗 → `judge` の `semantic_review.json#findings[*].attribution` と `verdict.json#failure_class` の組合せで Generate / Compile / Spec のどこに戻すかを deterministic に決定する（canonical 判定テーブルは `docs/workflow/phases/phase_04_validate.md` の「失敗時 retry の判定基準」節）。

## 設計方針
- 単一責務: 1 つの `agent` は 1 つの責務のみを持つ。
- 階層委譲: `orchestration agent -> step agent` と `orchestration agent -> substep agent` の 2 系統で制御する。
- 契約駆動: 子 `agent` 起動時は input contract と output contract を固定し、契約外の読み書きを禁止する。
- 追跡可能性: すべての起動・終了イベントを時系列で保存し、再実行時に同一判断を再現可能にする。

## オーケストレーション指示契約
### 共通必須項目
- `orchestration agent` は、子 `agent` への起動要求に `orchestration_id` と `agent_run_id` と `parent_agent_run_id` と `node_key` と `step` と `substep`（存在する場合）と `ir_ref` と `pipeline_ref` と `dependency_ref` を必須記録しなければならない。
- 子 `agent` への起動要求本文は `skills/workflow-orchestration/references/launch_prompts.md` の対応テンプレートを基底とし、テンプレート内プレースホルダーを対象 `agent_run` の実値で置換して生成する。
- 子 `agent` への起動要求には、`execution input` と `verification input` と `expected output` と `write_root` と `read_roots` を必須記録する。
- `execution input` は当該 `agent` が artifact を生成するために直接参照してよい入力に限定する。
- `verification input` は当該 `agent` が pass/fail 判定、整合確認、依存確認にのみ使用してよい入力として明示する。
- `expected output` はファイル名、保存先、更新責務を含めて明示する。
- 親 `agent` は入力不足時に推測補完を指示してはならない。不足入力がある場合は `fail-fast` 停止を指示する。
- 子 `agent` への起動要求には `skill_name` と `skill_ref` と `skill_must_read_refs` を必須記録する。

### `ir_ref` / `pipeline_ref` / `dependency_ref` の規約
- `ir_ref` は `workspace/ir/<node_key_safe>/<ir_id>` のみとし、追加のパスセグメントを付けてはならない。`<ir_id>` は canonical な `<slug>_<YYYYMMDD>_<seq3>` 形式とする（正規表現 `^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$`、canonical source: `docs/workflow/WORKFLOW_CORE.md` と `tools/orchestration_runtime.py` の `_SLUG_DATE_SEQ3_PATTERN`）。`<node_key_safe>` は `<ir_id>` の親 directory として配置し、`<ir_id>` 自体に prefix として付与してはならない（`node_key_safe` は `__` / `_` を含むため slug 正規表現に違反する）。
- `pipeline_ref` は `workspace/pipelines/<node_key_safe>/<pipeline_id>` のみとし、追加のパスセグメント（`source/` や `source_meta.json` を含む）を付けてはならない。
- `dependency_ref` は phase ごとに canonical path を固定する。`Compile` は `spec/.../deps.yaml`、`Generate` 以降は `workspace/...` の phase root（`ir_ref` または `pipeline_ref`）を記録し、`spec` 直参照を禁止する。
- `Generate verify` の起動要求では、`source_id` を必須記録しなければならない。
- `step agent` / `substep agent` が `pass` で終了するとき、`output_refs` の各パスは、対応する起動要求に記録された `ir_ref` または `pipeline_ref` ディレクトリ配下に含まれなければならない。

### `Compile` 起動要求
- `Compile.generate` の `skill_must_read_refs` には `controlled_spec.md` と `tests.md` と `deps.yaml` と `spec/registry/spec_catalog.yaml` を含める。
- `Compile.verify` の `skill_must_read_refs` には `Compile.generate` が生成した `spec.ir.yaml` と `controlled_spec.md` と `tests.md` と `deps.yaml` を含める。
- `Compile.verify` は `spec.ir.yaml` の構造 invariant 検証（全 case が algorithm.steps に被覆 / 依存解決の閉包整合 / output 契約と algorithm 出力の整合）を必須責務とする。加えて `impl_defaults.toolchain.language` / `impl_defaults.toolchain.standard` / `impl_defaults.toolchain.build_system` / `impl_defaults.target.architecture` が未定義の場合は `fail` とする (canonical source: `docs/IMPL_PLAN_SPEC.md` "必須項目")。

### `Generate` 起動要求
- `Generate.generate` の `skill_must_read_refs` には `spec.ir.yaml` を含める。`controlled_spec.md` を直接読んではならない。
- `Generate.verify` の `skill_must_read_refs` には `spec.ir.yaml` と `pipeline_ref` を基準とする相対パスとして `lineage.json` と `source/<source_id>/source_meta.json` を含める。

### `Validate` 起動要求
- `Validate.execute` の `skill_must_read_refs` には `spec.ir.yaml` と `pipeline_ref/binary/<binary_id>/binary_meta.json` を含める。
- `Validate.judge` の `skill_must_read_refs` には `spec.ir.yaml` と `tests.md` と同一 `run_id` 配下の `raw/` / `diagnostics.json` / `perf.json` / `quality_check.json` / `trial_meta.json` と `pipeline_ref/source/<source_id>/` を含める。judge の launch request は `source_id` / `source_binary_id` を必須にしない (runtime は `run_id` のみ enforce する); 代わりに judge は同一 `run_id` 配下の `trial_meta.json` を読み、`trial_meta.json.source_source_id` を `<source_id>` として解決する (trial_meta は Validate.execute が書き込み、runtime が `binary_meta.json.source_source_id` との一致を verify 済みのため、retry で複数 source が共存する pipeline でも judge が誤った source を読む経路は無い)。
- `Validate.judge` は `raw/` から独立経路で判定指標を再計算し、`diagnostics.json` と整合確認しなければならない。`LLM` 意味検査を必須実行する。

## 運用ルール
1. workflow 開始時に `orchestration_id` を発行し、`workspace/orchestrations/<orchestration_id>/orchestration_meta.json` を作成する。
2. workflow 開始前に preflight 結果を `workspace/orchestrations/<orchestration_id>/preflight.json` へ記録し、`can_launch_step_agents=true` と `can_launch_substep_agents=true` と `sandbox_enforced=true` を同時に満たさない場合は `fail` として停止する。
3. 各 phase の着手前に phase 種別を確認し、`Compile` / `Generate` / `Validate` では `substep agent`、`Build` では `step agent` を起動対象として確定する。
4. `orchestration agent` は `step agent` または `substep agent` の起動要求ごとに `launches/<agent_run_id>.request.json` と `launches/<agent_run_id>.response.json` と `launches/<agent_run_id>.prompt.txt` と `launches/<agent_run_id>.reply.txt` を保存する。
5. `record-launch` に保存する `response.json` と `child.response.json` は、`spawn_agent` 実応答の完全保存とし、子 `agent` 識別子を欠落させてはならない。
6. 各 `step agent` と各 `substep agent` の完了時には、`agents/<agent_run_id>/dialogs/agent.result.json` と `agents/<agent_run_id>/dialogs/agent.summary.txt` を保存する。
7. `agent.summary.txt` には、少なくとも最終 `status` と失敗要因または主要成果物参照を含める。
8. `launches/<agent_run_id>.prompt.txt` は `skills/workflow-orchestration/references/launch_prompts.md` の対応テンプレートを具体化した本文とする。
9. `orchestration agent` は `deps.yaml` と `spec_catalog.yaml` と `spec.ir.yaml` の `dependency` セクションを照合し、`spec` 依存関係に基づく実行キューを確定する。
10. `orchestration agent` は起動対象ごとに `step agent` または `substep agent` を発行し、`node_key` と `step` と `ir_ref` と `pipeline_ref` と `dependency_ref` を入力として渡す。
11. `orchestration agent` は上位 `node` の `Compile` を起動する前に、直下依存 `node` ごとの `ir_ref` と `ir_meta.json.verification_status` を照合し、`direct dependency ir readiness` を満たさない場合は起動してはならない。
12. `orchestration agent` は上位 `node` の `Generate` 以降を起動する前に、直下依存 `node` ごとの `ir_ref` と `pipeline_ref` と最新 `aggregate_verdict` を照合し、`direct dependency execution readiness` を満たさない場合は起動してはならない。
13. `direct dependency ir readiness` または `direct dependency execution readiness` を満たさない場合、`orchestration agent` は当該 `node` を `blocked` または `fail` として記録する。
14. `orchestration agent` は対象 `step` の `execution input` と `verification input` と `expected output` を明示する。
15. `substep` を持つ phase では、`orchestration agent` が各 `substep agent` を逐次起動する。
16. `substep agent` は自身の artifact と対応 phase のメタデータを生成し、`agent_output_ref` を `orchestration agent` へ返却する。
17. `orchestration agent` は子 `agent` の返却結果を評価し、`issue_severity` と再投入要否を確定する。
18. 再投入が必要で `repair_strategy=reuse` の場合、`orchestration agent` は同一 `agent_session_id` の継続修正を許可してよい。新規 `agent_run_id` を発行し、`relation_type` を `reuse` として `record-launch` 記録を追加する。
19. 再投入が必要で `repair_strategy=restart` の場合、`orchestration agent` は新規 `agent_session_id` を持つ `substep agent` を再起動し、`relation_type` を `restart` として `record-launch` 記録を追加する。
20. `orchestration agent` は `substep` を持つ phase で全 `substep` の必須 artifact を検証し、`workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/step_result.json` へ `step_result.json` を出力する。この `agent_run_id` は `orchestration agent_run_id` とする。`step_result.json` の `substep_agent_run_ids` は、当該 `step` で起動して `agent_runs.jsonl` に記録された **全** `substep` の `agent_run_id` を欠落なく列挙する。
21. `step_result.json` は、再投入を実施した場合に `retry_decisions` 配列を保持し、各要素へ `issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `new_agent_run_id` と `repair_reason` を記録する。`status=pass` の `write-step-result` に記録する `retry_decisions` では、各 `new_agent_run_id` は `effective pass substep` 集合へ最終採用された `pass` run に限る。
22. `noncanonical_phase_write_attempt` を起因とする再投入では、`repair_strategy=restart` を必須とする。
23. `substep` を持つ phase の `step_result.json` における `status=pass` 判定は、`effective pass substep` 集合に対して行う。
24. `status=pass` の `step_result` では、`effective pass substep` 集合に含まれる各 run が `pass` で終端していなければならない。
25. `status=pass` の `step_result` における `required_outputs` 被覆判定は、`effective pass substep` 集合の `output_refs` のみを対象に行う。
26. `step agent` は標準 `substep` を持たない phase（`Build`）で自身の artifact を検証し、`step_result.json` を出力する。
27. `orchestration agent` は `step_result.json` を受け取り、次 `step` の起動可否を判定する。
28. `node` 実行は `deps.yaml` と `spec_catalog.yaml` と `spec.ir.yaml.dependency` から再構成した依存順で逐次実行する。明示指示がない限り並列実行を行わない。
29. `step agent` または `substep agent` が `fail` / `timeout` / `cancel` の場合、当該 `node` の当該 `step` を `fail` とし、下流 `step` 起動を禁止する。
30. `orchestration agent` は各 `agent` 実行イベントを `workspace/orchestrations/<orchestration_id>/agent_runs.jsonl` へ追記する。
31. `orchestration agent` は親子関係を `workspace/orchestrations/<orchestration_id>/agent_graph.json` へ保存し、`parent_agent_run_id` と `child_agent_run_id` と `relation_type` を必須記録とする。
32. core workflow の全 `agent` は `workspace/` 配下以外へ書き込んではならない。
33. workflow 実行時に `step` / `substep` の実処理を script で代行した場合は `fail` とし、当該試行を破棄する。
34. 再投入時は新規 `agent_run_id` を発行し、既存 `launch` 証跡や `agent_runs` 行を上書きしてはならない。
35. `preflight.json` の手動編集または後編集で `status` と `can_launch_*` を変更してはならない。
36. 子 `agent` 起動直前の live probe が `fail` の場合、`record-launch` を実行してはならない。
37. `record-launch` が実行する live probe は、`METDSL_PREFLIGHT_TTL_SECONDS` で設定した TTL（既定 30 分）以内に成功済みのプローブが存在する場合はスキップされる。`METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT=1` が明示設定されている場合は無効化される。
38. native hook の実行結果は `workspace/orchestrations/<orchestration_id>/hooks/native_hook_events.jsonl` へ追記する。
39. `tools/run_workflow.py` はワークフロー起動時に `METDSL_MISSING_ORCHESTRATION_ID_POLICY=strict` を設定し、orchestration_id なしの hook 実行を禁止する。
40. 子 `agent` 必須 phase で契約に反する近道へ逸脱しそうな場合、`orchestration agent` は当該 phase が子 `agent` 起動必須であることを明示し、正規の起動手順へ復帰する。
41. `write-step-result` が `status=pass` で完了した後、`orchestration_checkpoint.json` が `tools/orchestration_runtime.py` により自動更新される。
42. `resume_enabled=true` の orchestration において、`orchestration agent` は `check-step-completed` を各 `step` 起動前に実行し、`completed=true` かつ `integrity=ok` の場合のみ当該 `step` のスキップを許可する。
43. チェックポイントによりスキップした `step` は `agent_runs.jsonl` に `agent_role=skipped_by_checkpoint` として記録する。
44. `resume_enabled=false` の orchestration では `orchestration_checkpoint.json` を信頼して `step` をスキップしてはならない。
45. `write-step-result` の `status` が terminal (`pass` / `fail` / `blocked` / `timeout` / `cancel`) の場合、`Compile` / `Generate` / `Build` / `Validate` の `step_result.json` に `validation_stage` を必須記録する。許容値は `Compile: compile|full`、`Generate: post_generate|full`、`Build: post_build|full`、`Validate: post_execute|pre_judge|full`（runtime canonical: `tools/orchestration_runtime.py` の `STEP_REQUIRED_VALIDATION_STAGES`）。step ごとの許容値以外、または欠落時は runtime が `ValueError` で reject する。
46. `codex_feature_check.json` の `status_kind=probe_error` の結果は永続固定してはならない。`METDSL_HOOK_FEATURE_RETRY_TTL_SECONDS`（既定 30 秒）経過後に再プローブを許可する。
47. `workspace/tmp/<agent_run_id>/` は各 agent の一時作業領域として使用できる。agent は当該 literal path を直接指定する（`output_manifest_write_guard` は write 対象 path のみを判定し `$TMPDIR` env を参照しない）。`record-agent-run` は当該 `agent_run` 記録後に `workspace/tmp/<agent_run_id>/` を自動削除する。`tools/run_workflow.py` は `init` 成功後に環境変数 `TMPDIR` を `workspace/tmp/<orchestration_agent_run_id>/` に設定するが、これは subprocess inherit 用の保険であり agent 側での `export TMPDIR=...` は不要かつ禁止（Claude Code session sandbox の approval 要求で workflow が停止する）。

## 判定基準
- workflow ごとに `orchestration_id` が発行され、`orchestration_meta.json` が存在する。
- 各 `step` または各 `substep` が独立 `agent_run_id` を持つ。
- `step` と `substep` の `context_id` が重複せず、全件で `context_isolated=true` が記録される。
- `step` と `substep` の `agent_runs.jsonl` に `agent_session_id` と各種参照が記録され、参照先実体が存在する。
- `launches/<agent_run_id>.response.json` と `agents/<agent_run_id>/dialogs/child.response.json` が `spawn_agent` 実応答の同一内容を保持する。
- `agent_runs.jsonl.agent_session_id` が、対応 `launch response` の子 `agent` 識別子と一致する。
- `preflight.json` が存在し、`can_launch_step_agents=true` と `can_launch_substep_agents=true` を満たす。
- preflight で `sandbox_runtime=bwrap` と `sandbox_enforced=true` が記録されている。
- 各 phase の実行記録から、`Compile` / `Generate` / `Validate` は `substep agent`、`Build` は `step agent` を使用したことを追跡できる。
- `agent_graph.json` で `orchestration -> step` または `orchestration -> substep` の親子関係を追跡できる。
- `step_result.json` の `executor_agent_run_id` が当該ディレクトリ名と一致し、`substep_agent_run_ids` が親子関係と整合する。標準 `substep` を持たない phase（`Build`）では `substep_agent_run_ids=[]` を許可する。
- `substep` を持つ `step` では、`agent_runs.jsonl` に記録された当該 `step` の全 `substep` の `agent_run_id` が、いずれかの `step_result.json` の `substep_agent_run_ids` に含まれる。
- `step_result.json` の `required_outputs` が `docs/workflow/WORKFLOW_CORE.md` および対応する `docs/workflow/phases/phase_*.md` の phase contract と一致する。
- `step_result.json` が `retry_decisions` を保持する場合、`effective pass substep` 集合を一意に復元できる。
- `step_result.json` が terminal status の `Compile` / `Generate` / `Build` / `Validate` では、`validation_stage` が phase ごとの許容値 (項 45) に一致する。

## Patch 適用契約

`guarded-apply-patch` サブコマンドの仕様（canonical source: この節。`tools/orchestration_runtime.py` の実装を直接参照してはならない）。

### CLI インタフェース

```
python3 tools/orchestration_runtime.py guarded-apply-patch \
  --repo-root <repo_root> \
  --orchestration-id <orchestration_id> \
  --actor-role <step|substep> \
  --agent-run-id <agent_run_id> \
  --paths-json '<JSON array of changed paths>' \
  --patch-file <path_to_patch_file> \
  --capability-token <capability_token>
```

`--patch-text` による直接埋め込みも可能だが、ARG_MAX 制限を避けるため `--patch-file` 経由を推奨する。`--patch-file` の保存先は `allowed_tmp_root` (= `workspace/tmp/<agent_run_id>/`) 配下の literal path のみ許可（`$TMPDIR` env への参照は動作するが env 依存を最小化するため literal を canonical とする）。

### strip 自動判定

`--strip` という CLI 引数は存在しない。`--paths-json` で渡した `changed_paths` を oracle として `-p1` → `-p0` の順で `git apply --check` を内部試行し、すべての `changed_paths` を被覆できる最初の strip を自動選択する。

### 出力契約

- 成功時は exit code 0、失敗時は 0 以外。
- `violations[]` と失敗理由は **stderr** に JSON 形式で出力される。
- gate 結果は `workspace/orchestrations/<orch_id>/gates/<agent_run_id>/apply_patch_writes.json` に書き込まれるが、このファイルを直接 Read してはならない。

### 許可対象 extension

`.json` / `.txt` の出力のみ。`.yaml`・`.yml`・`.md`・source code は `Edit`/`Write` tool（`allowed_file_tool_paths` 経由）を使うこと。`spec.ir.yaml` は `Edit`/`Write` 経由で書き込む。

### runtime 生成 placeholder の保護

`record-launch` は file pin (例 Generate の `lineage.json`) を bwrap が file 粒度で bind できるよう 0-byte placeholder として事前生成し、`sandbox_profiles/<agent_run_id>.json` の `created_file_pin_stubs` に記録する。`guarded-apply-patch` は apply 完了後に、**`changed_paths` に含まれない `created_file_pin_stubs` の placeholder が消えていれば 0-byte で復元する** (defense-in-depth)。`changed_paths` 被覆外の path を `git apply` が削除することは strip 判定後の被覆検査で既に reject されるが、万一の out-of-band 削除でも runtime-owned placeholder を残さないことを保証し、後段 `record-agent-run` の終端検査で「runtime artifact への非 gate 変更 = `unauthorized_write`」として恒久 `fail_closed` 化するのを防ぐ。`changed_paths` で被覆された path (agent が gate 経由で意図的に書換/削除した path) は復元対象外とする。

## Capability / Manifest 契約

`record-launch` が発行する 3 つの manifest の必須フィールドと不変条件（canonical source: この節）。

### `capabilities/<agent_run_id>.json` 必須フィールド

| フィールド | 型 | 説明 |
|---|---|---|
| `agent_run_id` | string | 子 agent の UUID |
| `capability_token` | string | 32 byte hex token |
| `orchestration_id` | string | orchestration ID |
| `agent_role` | `"orchestration"\|"step"\|"substep"` | agent ロール |
| `node_key` | string | `<spec_kind>/<spec_id>@<spec_version>` |
| `step` | string | `"compile"\|"generate"\|"build"\|"validate"` |
| `write_roots` | array of strings | capability が許可する write root リスト |
| `mcp_permissions` | object | MCP 権限スコープ |
| `expires_at` | ISO8601 | capability 有効期限 |

**不変条件:** `agent_role` が `"step"` または `"substep"` の capability は `write_roots` が空配列であってはならない。

### `output_manifests/<agent_run_id>.json` 必須フィールド

| フィールド | 型 | 説明 |
|---|---|---|
| `allowed_output_paths` | array of strings | `.json`/`.txt` 出力の許可 path 集合 |
| `allowed_file_tool_paths` | array of strings | `Edit`/`Write` 直接書き込みの許可 path 集合 |
| `allowed_tmp_root` | string | 一時ファイル許可ルート（`workspace/tmp/<agent_run_id>`） |

**使用方法:** agent は `allowed_tmp_root` の literal path (`workspace/tmp/<agent_run_id>/...`) を直接指定して書き込む。`output_manifest_write_guard` は write 対象 path のみを判定し `$TMPDIR` env を参照しないため、`export TMPDIR=...` / `jq -er ...` 等の bootstrap Bash は不要かつ禁止 (Claude Code session sandbox の approval 要求で workflow が停止する。詳細は `skills/workflow-orchestration/references/startup_contract.md` の tmp area 利用契約 参照)。

### `read_manifests/<agent_run_id>.json` 必須フィールド

| フィールド | 型 | 説明 |
|---|---|---|
| `allowed_read_roots` | array of strings | read が許可されるルートパスのリスト |
| `denied_read_roots` | array of strings | 明示的に拒否されるルートパスのリスト |

`output_manifests/<agent_run_id>.json` と `read_manifests/<agent_run_id>.json` は `Read` tool で直接読み取ってよい（`run-gate` 不要）。
