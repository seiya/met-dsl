# Phase 1: Compile

## 概要
自然言語仕様 (`controlled_spec.md` / `tests.md` / `deps.yaml`) を **単一構造 IR (`spec.ir.yaml`)** に統合する phase。core workflow で唯一 `controlled_spec.md` を直接読む phase であり、以降の `Generate` / `Build` / `Validate` は `spec.ir.yaml` を canonical source とする。

## I/O 契約
- execution input: `controlled_spec.md`、`tests.md`、`deps.yaml`、`spec/registry/spec_catalog.yaml`
- verification input: `controlled_spec.md`、`tests.md`、`deps.yaml`、`spec/registry/spec_catalog.yaml`、生成された `spec.ir.yaml`
- 出力: `workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml`、`ir_meta.json`

## substep 構成
- `Compile.generate`: `spec.ir.yaml` を生成する LLM substep。
- `Compile.verify`: 構造 invariant の self-check を行う独立 LLM substep（`Compile.generate` と context isolated）。

## `spec.ir.yaml` schema

`spec.ir.yaml` は YAML mapping artifact とし、以下のトップレベルキーを必須保持する。

```yaml
schema_version: "1.0"

meta:
  node_key: "<spec_kind>/<spec_id>@<spec_version>"
  spec_kind: "<problem|component|profile>"
  spec_id: "<spec_id>"
  spec_version: "<semver>"
  source_refs:
    controlled_spec: "spec/<spec_kind>/<domain>/<family>/<spec_id>/controlled_spec.md"
    tests: "spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md"
    deps: "spec/<spec_kind>/<domain>/<family>/<spec_id>/deps.yaml"

case:
  # 実行時入力の決定値（sweep 展開済み）
  test_case_set:
    - case_id: "<case_id>"
      inputs:
        grid: {...}
        time: {...}
        initial: {...}
        boundary: {...}
        profile_selection: {...}
        test_profile_id: "<test_profile_id>"
        test_profile_version: "<version>"
  sweeps: {...}        # 任意。テスト profile が sweep を定義する場合
  refinements: {...}   # 任意。grid refinement を定義する場合

algorithm:
  algorithm_id: "<id>"
  execution_mode: "<sequence|conditional|iterative|columnwise>"
  steps:
    - step_id: "<step_id>"
      step_kind: "<boundary_apply|reconstruct|flux_compute|source_term|time_integrate|column_process|pointwise_process|iterative_solve|filter|reduction|diagnostic>"
      operation_ref: "<operation_id>"
      inputs: ["<var1>", "<var2>"]
      outputs: ["<var3>"]
  ordering: [...]              # step_id の列、または before/after dependency object 配列
  control_condition: ...       # 文字列、文字列配列、object のいずれか
  iteration_contract: {...}    # object。execution_mode=iterative の場合は空 object 禁止
  update_semantics: {...}
  temporaries: [...]           # 文字列配列、または name + shape_expr object 配列
  derived_field_rules: [...]
  invariants: ["<inv1>", ...]  # 非空文字列配列
  splitting_policy:
    kind: "<kind>"

impl_defaults:
  # core workflow ではこの値を固定して使用する。
  # Tune 任意フローはこのセクションのうち knob レイヤのみを variant として上書きできる。
  # （fixed / knob の境界は本ファイル末尾の「impl_defaults の fixed / knob 境界」節を参照）
  target:
    class: "<cpu|gpu>"
    backend: "<backend>"
    architecture: "<architecture>"
  toolchain:
    language: "<fortran|c|cpp|cuda_fortran|cuda_c|mixed|python>"
    standard: "<standard>"
    build_system: "<make|cmake|setuptools|...>"
  selected:
    backend_key: "<key>"
  abstract: {...}              # 並列化、レイアウト、融合、タイル等の意図 (knob 領域)
  backend_overrides: {...}     # backend ごとの上書き (knob 領域)

io_contract:
  # IO 契約と検証契約を統合保持: inputs / outputs / semantic_dependency / raw_requirements / test_evidence_requirements
  inputs:
    - name: "<name>"
      shape_expr: "<scalar | [d1,d2,...] | (d1,d2,...)>"
  outputs:
    - name: "<name>"
      shape_expr: "<...>"
      evidence_ref: "raw/state_snapshots | raw/diagnostics | ..."
      raw_variables: ["<name1>", ...]  # evidence_ref=raw/state_snapshots の場合は非空配列必須
  raw_requirements:
    required_evidence:
      - artifact: "<state_snapshots|metrics_basis|execution_trace|...>"
        required: true|false
        min_samples: <int>
        schema:               # artifact=state_snapshots 時に必須
          variables:
            - name: "<name>"
              shape_expr: "<...>"
          time_variable: "<name>"
          time_shape_expr: "<...>"
  test_evidence_requirements:
    - test_id: "<test_id>"
      required_raw_variables: ["<var1>", ...]
  semantic_dependency:
    required_sources: ["<var1>", "<var2>", ...]   # 非空文字列配列

dependency:
  # 旧 dependency.resolved.yaml 相当
  node_key: "<spec_kind>/<spec_id>@<spec_version>"
  direct_deps:
    - node_key: "<spec_kind>/<spec_id>@<spec_version>"
      kind: "<component|profile|problem>"
      operations: ["<operation_id>", ...]
  transitive_deps:
    - node_key: "<...>"
      via: ["<intermediate_node_key>", ...]
  all_nodes:
    - node_key: "<...>"
      topo_level: <int>
```

### `shape_expr` 許容形式
`spec/schema/plan/shape_expr.schema.json` を canonical source とする。`scalar` (case-insensitive) / `[d1, d2, ...]` / `(d1, d2, ...)` の 3 形式に限る。`vector(N)` / `matrix(M,N)` / `tensor` 等の関数呼び出し記法は禁止し、`Compile fail` とする。

### `algorithm.steps[].inputs` と `algorithm.steps[].outputs`
非空文字列の list（例: `["U_L", "U_R"]`）とし、object 形式（`[{name: ..., source: ...}]`）は禁止する。

### `algorithm.execution_mode`
`sequence` / `conditional` / `iterative` / `columnwise` のみを許可する。

### `algorithm.steps[].step_kind`
`boundary_apply` / `reconstruct` / `flux_compute` / `source_term` / `time_integrate` / `column_process` / `pointwise_process` / `iterative_solve` / `filter` / `reduction` / `diagnostic` のみを許可する。

## `ir_meta.json` 必須 key
- `attempt_count`、`verification_status`、`last_fail_reason`、`debug_mode`、`context_isolated`
- `context_isolated=false` の場合、`constraint_reason` を必須とする。

## `ir_id` フォーマット
- 形式: `<slug>_<YYYYMMDD>_<seq3>`
- `slug` は `spec_id` 由来の短い可読 token。ハイフン区切り英数字とする。
- 正規表現: `^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$`

## substep 詳細

### 1-1. Compile.generate substep
- `Controlled Spec` の物理アルゴリズム（A）を読み、`tests.md` から入力条件と `sweep` / `refinement` を決定的に展開して `spec.ir.yaml` の `case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency` 5 セクションを生成する。
- `deps.yaml` と `spec/registry/spec_catalog.yaml` から依存解決を行い、`dependency` セクションに `direct_deps` / `transitive_deps` / `all_nodes` を保持する。
- `impl_defaults` の既定値は `IMPL_PLAN_SPEC.md`（既存）の規則に従う。Tune 任意フローでの variant 探索を考慮し、`abstract` / `backend_overrides` の knob 集合は IR に表現する。
- 生成過程で `controlled_spec.md` の意図が schema に収まらない場合、schema を拡張するのではなく `Compile fail` として `last_fail_reason` に「IR schema insufficiency」を記録し停止する。schema 拡張は別途人手で `spec.ir.yaml` schema 設計を更新してから retry する。

### 1-2. Compile.verify substep
`Compile.verify` は **構造 invariant** のみを self-check する。意味的な正しさは `Validate` の実行結果に委ねる。

self-check の必須 invariant 集合（**最小集合**として確定）:

#### V1. case 被覆性
- `case.test_case_set[].case_id` が `tests.md` で要求される全 `test_id` の要求 case を被覆していること。
- `tests.md` の `sweep` / `refinement` 指示が `case.sweeps` / `case.refinements` に反映されていること。

#### V2. algorithm 完全性
- `algorithm.steps[]` の各 `step.outputs` 集合の和集合が、`algorithm.update_semantics` で更新対象とした状態変数を被覆していること。
- `algorithm.ordering` が `algorithm.steps[].step_id` に対する有効な順序関係であること（循環なし、未定義 step_id 参照なし）。
- `algorithm.iteration_contract` が `algorithm.execution_mode=iterative` の場合に空 object でないこと。

#### V3. io_contract 整合
- `io_contract.outputs[]` の各 `name` が `algorithm.steps[]` のいずれかの `outputs` に出現するか、`algorithm.derived_field_rules` で導出されること。
- `io_contract.outputs[].evidence_ref=raw/state_snapshots` の場合、`raw_variables` が非空配列であり、各要素が `io_contract.raw_requirements.required_evidence[].schema.variables[].name` または `time_variable` を参照すること。
- `io_contract.test_evidence_requirements[].required_raw_variables` が `tests.md` の全 `test_id` を被覆すること。
- `io_contract.semantic_dependency.required_sources` が非空文字列配列であること。

#### V4. dependency 整合
- `dependency.direct_deps[]` と `dependency.transitive_deps[]` の和集合の閉包が `dependency.all_nodes` と一致すること（node_key 重複・欠落なし）。
- `deps.yaml` と `spec/registry/spec_catalog.yaml` から再構成した `expected_node_set` と `dependency.all_nodes` の node_key 集合が一致すること。
- 各 `direct_deps[].operations` が依存先 `node` の公開 `operation_id` 集合に含まれること（依存先 IR が既存の場合は照合、未生成の場合は `spec_catalog.yaml` から取得）。

#### V5. impl_defaults 整合
- `impl_defaults.toolchain.language` と `impl_defaults.toolchain.build_system` の組み合わせが `IMPL_PLAN_SPEC.md` の既定値規則に整合すること。
- `impl_defaults.target.class` と `impl_defaults.target.backend` の組み合わせが `impl_defaults.selected.backend_key` で識別可能であること。

#### 検証ツール
- `Compile` 完了前に `python3 tools/check_artifact_syntax.py --format yaml --expect-top object spec.ir.yaml` を用いて syntax 妥当性を検査し、`fail` 時は `Compile fail` としなければならない。`ir_meta.json` も同 tool で検査する。
- `Compile.verify` 完了前に `python3 tools/validate_pipeline_semantics.py --stage compile --ir-ref workspace/ir/<node_key_safe>/<ir_id>/` を実行し、`exit code 0` を必須としなければならない。`fail` 時は `ir_meta.json` の `verification_status=pass` を付与してはならない。

## 失敗時挙動
- 入力 (`controlled_spec.md` / `tests.md` / `deps.yaml`) が不足する場合は `Compile fail` とし、推測補完を禁止する。
- self-check invariant のいずれかが `fail` した場合は `Compile fail` とし、`ir_meta.json.last_fail_reason` へ違反 invariant ID（V1〜V5）と詳細を記録する。
- repair_strategy は `reuse` を既定とし、構造的に大幅な再構成が必要な場合のみ `restart` を選択する。

## Validate からの retry 受け入れ
`Validate` の `judge` が `attribution=ir` で `confidence>=medium` の finding を出した場合、`orchestration agent` は `Compile` へ retry を再投入する（routing 規則の canonical source は `docs/workflow/phases/phase_04_validate.md` の判定テーブル）。`Compile` 側の受け入れ契約:

- 再投入 `Compile` は `launches/<agent_run_id>.request.json#repair_reason` に Validate の finding 情報（`description`、`evidence_refs[]`、`finding_id`）が引用されていることを前提とする。引用が無い場合は `Compile fail` で停止する。
- `ir_meta.json.last_fail_reason` に `validate_feedback:<finding_id>` を記録し、`ir_meta.json.attempt_count` を加算する。
- 再投入 `Compile` は **finding が指す `spec.ir.yaml` の section（例: `algorithm.steps[].ordering`、`io_contract.outputs[].shape_expr`）に限定** して修正することを既定とし、全 section の刷新は finding が `confidence=high` かつ `description` で構造再構成を要請している場合のみ許可する。
- 修正後の `Compile.verify` は self-check invariant に加え、`validate_feedback` で指摘された path に対する修正が反映されていることを `ir_meta.json.repair_target_sections[]` に明示する。

## `impl_defaults` の fixed / knob 境界
`impl_defaults` セクションは **core workflow が固定として扱う fixed レイヤ**と、**Tune 任意フローが variant 上書きできる knob レイヤ**に明確に分離する:

| sub-key | レイヤ | 扱い |
|---|---|---|
| `target.class` | **fixed** | 物理ハードウェアカテゴリ (`cpu` / `gpu`)。Tune 越境禁止 |
| `target.backend` | **fixed** | バックエンド種別 (`openmp` / `cuda` / `mpi` 等)。Tune 越境禁止 |
| `target.architecture` | **fixed** | 具体アーキ (`x86_64` / `sm80` 等)。Tune 越境禁止 |
| `toolchain.language` | **fixed** | コンパイラ前提を決めるため不変 |
| `toolchain.standard` | **fixed** | 言語規格 (`f2008` / `c11` 等) |
| `toolchain.build_system` | **fixed** | ビルドツール (`make` / `cmake` 等) |
| `selected.backend_key` | **fixed** | `target` と整合する識別子 |
| `abstract` | **knob** | 並列化粒度 / レイアウト / 融合 / タイル等の意図。Tune の主要探索領域 |
| `backend_overrides.<key>` | **knob** | backend 固有の上書き値（スレッド数、block size、ベクトル幅等） |

core workflow の各 phase は **fixed レイヤを不変前提として扱い、knob レイヤの値は IR の既定値を尊重して読み取り専用とする**。`Tune` 任意フローのみが `tuning.spec` で knob レイヤの上書き候補を指定し、variant pipeline を生成できる。

`Compile.verify` は invariant V1〜V5 に加え、次を確認する:
- V6: `impl_defaults` の全 fixed sub-key が値を持つこと（欠落禁止）。
- V7: `abstract` および `backend_overrides` の各 leaf 値が「default 値」として確定していること（`null` / `<TBD>` 等の plug-hole 禁止）。

詳細な knob schema と Tune の variant 制約は `docs/TUNING_WORKFLOW.md` を canonical source とする。

## 設計トレードオフ
- IR は **構造 invariant のみ self-check** することで、検証契約の肥大化を避ける。意味的正しさは `Validate` 実行結果に委ねる方針（「ハイブリッド検証」原則）。
- 実装裁量を `impl_defaults` として IR 内に保持することで、Tune 任意フローが variant 探索する base を提供しつつ、core workflow は固定値で進行できる。
- `impl_defaults` 内を fixed / knob レイヤに分離することで、core workflow が「Tune が変える可能性のある領域」と「絶対に変えない領域」を機械的に区別でき、Tune の variant 生成が IR の構造を破壊しないことを保証できる。
