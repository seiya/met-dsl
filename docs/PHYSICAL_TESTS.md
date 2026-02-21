# physical_tests（正本）の要件と書式

## 目的
`physical_tests` は妥当性検証で使用する入力条件・ケース展開・判定条件の正本である。ドメイン研究者が単独で読んでも、何を実行し、何を合格と判定するかを解釈できることを要求する。

## 適用範囲
- `spec/<domain>/<component>/<spec_id>/physical_tests.md`
- `case.resolved.yaml` へ写像される入力条件とケース展開規則
- `verdict.json` へ写像される判定条件と合否規則

## 要件
1. `physical_tests` の正本形式は `Markdown` とする。
2. 記述は自然言語を主とし、構造化ブロック（`YAML` / `JSON` / 表）は曖昧性除去に必要な箇所に限定する。
3. `physical_tests` のファイル名は `physical_tests.md` を必須とし、`spec_id` ごとに 1 ファイルのみを許可する。
4. 文書先頭に `test_profile_id`、`test_profile_version`、`status`、`spec_ref` を明示する。
5. `spec_ref` は `spec_id`、`spec_version`、`controlled_spec_path` を必須とする。
6. ケース展開規則は sweep 軸、固定値、`case_id` 生成規則、並び順を明示する。
7. 判定条件は、適用可否、評価指標、閾値、`N/A` 条件と理由を必須とする。
8. 未定義項目は補完せずエラーとする。暗黙の既定値を禁止する。
9. 数式記法は `Markdown` の標準記法として、インラインを `$...$`、ブロックを `$$...$$` に統一する。`\(...\)` と `\[...\]` は使用しない。

## 設計方針
- `Controlled Spec` にはケース固有の検証条件を書かない。
- `physical_tests` には離散化スキーム定義や物理方程式の変更を書かない。
- `physical_tests` は入力と判定の「検証プロファイル」を定義し、ランナー実装の都合を正本化しない。
- `case.resolved.yaml` の生成は決定的でなければならない。同一 `Controlled Spec` と同一 `physical_tests` から同一 `case_id` 群を生成する。

## 記述フォーマット（固定テンプレート）
0. メタ情報
- `test_profile_id`、`test_profile_version`、`status`、`spec_ref` を記載する。

1. 検証スイートの目的
- 何を検証対象にするか、適用範囲外を何にするかを記載する。

2. 入力の既定化規則
- 実行時入力の既定値、初期条件、理論解（定義可能な場合）を記載する。

3. 実行制御規則
- `t_start`、`t_end`、`dt` 決定規則、停止条件、出力時刻規則を記載する。

4. ケース展開規則
- family 単位の sweep、固定値、追加上書きケース、`case_id` 生成規則を記載する。

5. 診断契約
- 必須診断項目、`N/A` ルール、診断未計算時の出力契約を記載する。

6. 閾値定義
- 既定閾値、ケース別上書き閾値、比較式を記載する。

7. テスト定義
- 各 `test_id` について `level`、対象ケース、期待結果（`pass` / `xfail`）、判定式を記載する。

8. 合否集約規則
- `per_test` と `suite` の pass/fail 判定規則を記載する。

9. トレーサビリティ
- `case.resolved.yaml` と `verdict.json` への写像規則、版管理項目、参照文献を記載する。

## 運用ルール
- `Controlled Spec` 更新で検証条件に影響する場合は、対応する `physical_tests` を同一変更で更新する。
- 閾値の変更時は、変更対象の `test_id` と影響範囲を文書内で明示する。
- `xfail` は失敗期待条件と `pass_when` を同時に定義し、片方のみの記述を禁止する。
- 物理判定不能の項目は `N/A` とし、`reason_na` を必須記録する。

## 判定基準
- 文書単独で、入力条件と判定条件と合否集約規則を復元できる。
- `case_id` 生成規則が一意であり、並び順規則が明示されている。
- すべての `test_id` に expected outcome と判定式がある。
- `N/A` 項目に理由があり、暗黙補完が存在しない。
- `spec_ref` と `test_profile_version` から実行時トレースを再現できる。
