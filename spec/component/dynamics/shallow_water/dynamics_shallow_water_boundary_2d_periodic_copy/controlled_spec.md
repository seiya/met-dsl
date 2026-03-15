# Controlled Spec: 2 次元 周期境界 写像（component spec）

## 0. メタ情報
- `spec_id`: `dynamics_shallow_water_boundary_2d_periodic_copy`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. 責務と適用範囲
本 `component` は 2 次元 周期境界 ghost 写像のみを担当する。

## 2. input/output contract
入力は `U(i,j)`、`nx`、`ny`、`ng` とする。出力は 周期写像後の `U` とする。

## 3. 演算定義
公開 `operation` は `dynamics_shallow_water_boundary_2d_periodic_copy__apply` とする。`x` と `y` 方向の周期写像を順に適用する。

## 4. 失敗条件と制約
`nx<2`、`ny<2`、`ng<1` を入力不正としてエラーとする。

## 5. 公開 API と互換性
公開 `operation_id` は `dynamics_shallow_water_boundary_2d_periodic_copy__apply` のみとする。

## 6. 禁止事項
非周期境界への自動フォールバックを禁止する。

## 7. トレーサビリティ
`component_catalog.yaml` と `case.resolved.yaml` に採用結果を必須記録とする。

## 8. AD 準備情報
`ad_readiness.enabled` は `true` とする。離散演算として 周期インデックス wrap を明示する。

## 9. tests 参照
対応する `tests.md` を同一ディレクトリに配置し、`test_profile_version` を `0.1.0` とする。
