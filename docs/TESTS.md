# tests（canonical source）の要件と書式

## 目的
`tests.md` は `spec` の verification input と判定条件の canonical source である。`problem` / `component` / `profile` の全 `spec_kind` で共通利用する。
`tests.md` の評価結果は当該 `node` の `self_verdict` として `verdict.json` へ写像し、依存込みの集約判定は `aggregate_verdict.json` で扱う。

## 適用範囲
- `spec/problem/<domain>/<family>/<spec_id>/tests.md`
- `spec/component/<domain>/<family>/<spec_id>/tests.md`
- `spec/profile/<domain>/<family>/<spec_id>/tests.md`

## 要件
1. canonical source 形式は `Markdown` とする。
2. 文書先頭に `test_profile_id`、`test_profile_version`、`status`、`spec_ref` を必須記載する。
3. `spec_ref` は `spec_kind`、`spec_id`、`spec_version`、`controlled_spec_path` を必須とする。
4. 各 `spec` は `L0` テストを少なくとも 1 件定義する。
5. `L1` / `L2` / `L3` は検証目的に応じて定義する。`spec_kind` で禁止しない。
6. テスト件数の固定下限は設けない。要件網羅規則で十分性を判定する。
7. 未定義項目は補完せずエラーとする。
8. 判定条件は `node_key` 単位で評価できなければならない。依存 `node` の状態を暗黙参照してはならない。

## `spec_kind` 別の網羅規則
- `problem`
  - 実行制御、ケース展開、判定式、合否集約規則を定義する。
  - 妥当性判定で非適用項目がある場合は `N/A` と `reason_na` を定義する。

- `component`
  - 公開 `operation` ごとに、正常系とガード系（`fail` または `xfail`）を少なくとも 1 件ずつ定義する。
  - 必要に応じて `L1` 以上の精度・保存性・同値性テストを追加する。

- `profile`
  - 選択成立条件、排他条件、フォールバック禁止条件の判定を定義する。
  - 互換範囲外入力に対するガード系テストを定義する。

## 記述フォーマット
0. メタ情報
1. テスト目的
2. 入力既定化規則
3. 実行制御規則
4. ケース展開規則
5. 診断契約
6. テスト定義
7. 合否集約規則
8. トレーサビリティ

`spec_kind` により不要節がある場合は、省略ではなく `N/A` と理由を記載する。

## 運用ルール
- `Controlled Spec` 変更で判定条件に影響する場合、同一変更で `tests.md` を更新する。
- `xfail` は `xfail_condition` と `pass_when` を同時定義する。
- 閾値変更時は影響 `test_id` を明示する。
- 依存込みの合否は `tests.md` ではなく `dependency.resolved.yaml` と `aggregate_verdict.json` で判定する。

## 判定基準
- 文書単独でテスト入力と合否判定を復元できる。
- `spec_ref` と `controlled_spec.md` の対応が一意である。
- `L0` テストが存在する。
- 要件網羅規則を満たす。
- 判定結果を `node_key` 単位の `self_verdict` として再現できる。
