# 全体ワークフロー: Spec→Plan→Generate→Execute→Judge→Tune
この文書はプロジェクト全体の流れを単独で把握できるようにまとめる。
用語は `GLOSSARY.md` を参照。

## 0. 仕様作成（人間）
- Controlled Spec（文章＋構造化ブロック）で物理アルゴリズム（A）を決定する。
- physical_tests（構造化定義）で実験条件と判定条件を決定する。
- 実行アルゴリズム（B）はここでは固定しない（将来探索する）。

成果物:
- `spec/<domain>/<component>/<spec_id>/controlled_spec.md`（Controlled Spec）
- `spec/<domain>/<component>/<spec_id>/physical_tests/<suite_id>.yaml`（テスト入力・判定条件）

## 1. Plan 生成（決定的）
### 1-1) 物理 Plan（case.resolved.yaml）
- Controlled Spec から物理アルゴリズム（A）を読み、physical_tests から入力条件と sweep/refinement を展開する。
- この層は「物理結果を保証するために決定的」である必要がある。

### 1-2) 実装 Plan（impl.resolved.yaml）
- target や環境に応じて、実行アルゴリズム（B）を決める。
- この時点で `target.backend` と `toolchain.language`（例: Fortran/C++）を固定する。
- compiler 種別は任意（再現性が必要な運用でのみ固定）とする。
- Phase 1 では固定値でもよい。
- Phase 2 以降は tuner が複数候補を生成し探索する。

## 2. 生成（LLM）
- LLM は交換可能。
- 入力: case.resolved（A 固定）と impl.resolved（B の指定、または候補集合）
- 出力: 実装コード（model + runner）と付随ドキュメント
- 言語に依らず、**モデル本体（物理計算）** と **テスト runner（入出力・判定連携）** を分離して生成する。
- runner はモデルを `call` / `use` / `import` で呼び出し、物理更新ロジックを重複実装しない。

## 3. 実行（runner / simulate）
- 入力: case.resolved.yaml と（任意で）impl.resolved.yaml
- runner が model を呼び出し、diagnostics.json と perf.json を出力する。
- perf.json の仕様は `PERFORMANCE_DIAGNOSTICS.md`

## 4. 判定（runner）
- 物理判定: diagnostics を用いて checks/thresholds を評価（詳細は `PHYSICAL_VALIDATION.md`）
- 性能判定（任意）: perf を用いて performance regression を評価
- 物理 fail 時は性能評価をスキップする。

## 5. チューニング（tuner: Phase 2+）
- 同一 case.resolved に対し複数 impl.resolved を生成し、物理合格を満たす範囲で性能目的関数を最大化する。
- 詳細は `TUNING_WORKFLOW.md`

## 6. 成果物配置規約（Plan / Generate / Build / Execute）
### 6-1) ルート構造
ワークフロー成果物の保存先は `workspace/` を正本とし、次の構造を必須とする。

```text
workspace/
  plans/
    <plan_id>/
      case.resolved.yaml
      impl.resolved.yaml
      plan_meta.json
  pipelines/
    <pipeline_id>/
      lineage.json
      generate/
        <generation_id>/
          src/
          generate_meta.json
      build/
        <build_id>/
          bin/
          build_meta.json
      execute/
        <execution_id>/
          diagnostics.json
          perf.json
          verdict.json
          summary.json
          trial_meta.json
          stdout.log
          stderr.log
  index/
    plan_index.json
    pipeline_index.json
```

### 6-2) ID と不変条件
- `plan_id`: `case.resolved.yaml` と `impl.resolved.yaml` の組を一意に識別する ID とする。推奨形式は `<spec_id>_<case_hash12>_<impl_hash12>` とする。
- `pipeline_id`: 1 回の Generate→Build→Execute 系列を一意に識別する ID とする。推奨形式は `<plan_id>_<utc_ts>_<seq3>` とする。
- `generation_id` / `build_id` / `execution_id`: 各段階の試行単位 ID とする。
- `plan_id` 配下の `resolved` ファイルは immutable とする。更新ではなく新しい `plan_id` を発行する。
- `pipeline_id` 配下は append-only とし、既存 `execution_id` の上書きを禁止する。

### 6-3) 起点モード
- `spec` 起点モード: `spec` から Plan を作成し、新しい `plan_id` を発行してから `pipeline` を開始する。
- `resolved` 起点モード: 既存 `plan_id` を指定し、Generate 以降のみを実行する。
- `lineage.json` は、`spec_ref` と `plan_ref` と各段階 `id` を必須記録する。

### 6-4) 再実行規則
- 同一 `plan_id` で Generate を複数回実行してよい。各試行は別 `generation_id` とする。
- 同一 `generation_id` で Build を複数回実行してよい。各試行は別 `build_id` とする。
- 同一 `build_id` で Execute を複数回実行してよい。各試行は別 `execution_id` とする。
- Judge の入力は常に `execution_id` 配下成果物とし、他 `execution_id` との混在を禁止する。

### 6-5) 参照規則
- `pipeline` から `plan` を参照するときは `plan_id` を使う。相対ファイルパス直参照を禁止する。
- `execution` の再現は `lineage.json` と `trial_meta.json` のみで可能でなければならない。
- `index/plan_index.json` と `index/pipeline_index.json` は探索専用とし、判定ロジックの正本に使ってはならない。


補足:
- impl の仕様: `IMPL_PLAN_SPEC.md`
- 自動チューニング運用: `TUNING_WORKFLOW.md`
