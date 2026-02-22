# Agent Skills Mapping

この文書は、プロジェクト内で利用する `skills` の参照規約を定義する。

## 目的
- `Codex` / `Gemini` / `Claude Code` で同一の工程定義を使う。

## 適用範囲
- ワークフロー工程 `Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote`
- 各工程で参照する `skills/<skill_name>/SKILL.md`

## 要件
- エージェントは、作業対象工程を特定してから対応 `SKILL.md` を読み込む。
- `generate -> verify -> regenerate` を持つ工程は、`generate` 用と `verify` 用の 2 つの `SKILL` を必ず分離適用する。
- `spec_kind` を問わず依存 `DAG` を実行する場合、各 `node_key` を個別 workflow として扱い、個別 `Plan` と個別 `pipeline` を発行する。
- 明示的な指定がない場合、既存 workflow 出力（過去試行の `workspace/plans` / `workspace/pipelines` 成果物）を参照してはならない。中身の閲覧を禁止し、各工程を独立実行する。
- 過去実行で生成された成果物は、ディレクトリ名に関係なく閲覧・参照・コピー・リンクを禁止する。`workspace/` 配下に存在する過去成果物も同様に扱う。
- workflow は毎回独立実行し、`workspace/plans/<node_key_safe>/<plan_id>/` と `workspace/pipelines/<node_key_safe>/<pipeline_id>/` の既定構造で、`plan_id` / `pipeline_id` / `generation_id` / `build_id` / `execution_id` を毎回新規発行する。入力は `spec` 正本と当該実行で生成した前段成果物のみに限定する。
- 検証契約は `controlled_spec.md` と `tests.md` と `deps.yaml` から導出する。追加必須項目の記述をユーザーへ要求してはならない。
- 検証契約の導出に必要な情報が不足する場合は当該工程を `fail` で停止し、推測補完を禁止する。
- `Build` / `Execute` / `quality check` は `MCP` サーバー経由で実行する。
- `SKILL` に記載された必須出力（例: `<stage>_meta.json`、`verdict.json`）を欠落させない。
- `dummy` 出力、`dummy` データ、`dummy` 計算、進行目的の人工成果物生成を禁止する。
- `Generate verify` は、`runner` の外部インタプリタ起動（`python` / `bash` / `sh` / `node`）を検出した場合に `fail` とする。
- `Generate verify` は、`model` の `no-op` 実装、固定値返却専用実装、固定 `JSON` 出力専用実装を検出した場合に `fail` とする。
- `Generate verify` は、依存 `operation` と出力指標のデータ依存を検証し、定数出力または解析式直接代入による `diagnostics` 生成を検出した場合に `fail` とする。
- 依存を持つ `node` の `Generate verify` は、`dependency.resolved.yaml` の `direct_deps` で解決された依存 `node` の公開 `operation` 呼び出しを必須検証とし、欠落時は `fail` とする。
- 依存を持つ `node` の `Generate verify` は、依存 `operation` と同等機能の再実装を検出した場合に `fail` とする。
- `toolchain.language=fortran` の `Generate verify` は、`module` 名とソースファイル名の一致、および `spec_id` 由来接頭辞の有無を必須検証とし、違反時は `fail` とする。
- `Execute` は、`execution_id/<node_key>/raw/` の実行証跡を必須出力とし、欠落時は `fail` とする。
- `Execute` は、`raw` に一次証跡のみを保存し、`diagnostics.json` の複写を `metrics_basis` として保存してはならない。
- `Judge` は、`raw` 実行証跡から判定指標を再計算して `diagnostics` と照合し、不整合時は `fail` とする。
- `Judge` の再計算入力は `raw` 一次証跡に限定し、`diagnostics.json` を再計算入力へ流用してはならない。
- `quality check` は `diagnostics.json` と `verdict.json` の比較を正本とし、`stdout` 差分のみで合否を確定してはならない。
- 異なる `node_key` の `generate/src` が共通ライブラリ明示なしで完全一致した場合、`copy_based_artifact_reuse` として `invalid` にする。
- workflow 成果物の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。
- workflow 実行開始前に `workspace/` が存在しない場合は、リポジトリルート直下へ `workspace/` を作成する。
- workflow 実行の開始前と完了前に `python3 tools/validate_workspace_root.py` を必須実行し、`fail` 時は当該 workflow を停止する。
- `dependency.resolved.yaml` の `node_key` 集合と `workspace/plans` / `workspace/pipelines` の `node` 集合一致を必須検証とする。
- `blocked` 終了 `node` でも `aggregate_verdict.json`、`summary.json`、`trial_meta.json` を必須出力し、`blocked_reason` を記録する。

## 工程と Skill 対応表
- `Plan generate`: `skills/workflow-plan-generate/SKILL.md`
- `Plan verify`: `skills/workflow-plan-verify/SKILL.md`
- `Generate generate`: `skills/workflow-generate-generate/SKILL.md`
- `Generate verify`: `skills/workflow-generate-verify/SKILL.md`
- `Build`: `skills/workflow-build/SKILL.md`
- `Execute`: `skills/workflow-execute/SKILL.md`
- `Judge`: `skills/workflow-judge/SKILL.md`
- `Tune generate`: `skills/workflow-tune-generate/SKILL.md`
- `Tune verify`: `skills/workflow-tune-verify/SKILL.md`
- `Promote`: `skills/workflow-promote/SKILL.md`

## 運用ルール
1. 1 回の作業で複数工程を扱う場合、工程ごとに対応 `SKILL` を切り替える。
2. `verify` で失敗した場合、同一工程の `generate` に戻し、再生成後に再検証する。
3. ループの状態と失敗理由は、該当工程のメタデータへ記録する。
4. `SKILL` 定義を変更した場合、この対応表を同一変更で更新する。
5. 依存 `DAG` 実行時は、`topo_level` 昇順で `node` を処理し、同一 `topo_level` の独立 `node` のみ並列実行する。
6. 同一 `topo_level` で一部 `node` が `fail` しても、独立 `node` の実行を中断しない。`topo_level` 完了後に次レベル進行可否を判定する。
7. 直下依存 `node` が `pass` または `xfail` でない場合、上位 `node` を `blocked` として終了する。
8. 直下依存に起因して `blocked` で終了する `node` は、`self_verdict=not_evaluated` を明示し、停止理由を `trial_meta.json` に記録する。
9. 工程入力不足で開始条件を満たせない場合、当該工程を `fail` で停止する。推測補完で進めない。
10. `spec_kind` を問わない workflow 実行で、リポジトリ管理外パス（例: `/tmp`）の補助スクリプトを workflow 実行経路に使用してはならない。

## 判定基準
- 対象工程で使用した `SKILL` パスを説明できる。
- 生成成果物と判定成果物が、対応 `SKILL` の契約と一致する。
- エージェント間で同一入力に対する工程選択が一致する。
