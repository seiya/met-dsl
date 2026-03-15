# Controlled Spec: 2 次元 `SSPRK2` 更新（component spec）

## 0. メタ情報
- `spec_id`: `dynamics_shallow_water_time_update_2d_ssprk2`
- `spec_version`: `0.2.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. 責務と適用範囲
本 `component` は shallow water 問題の時間積分を `SSPRK2` で実行する責務を持つ。

## 2. input/output contract
入力は `U^n`、界面フラックス差分、底面地形源項 `S_b`、`dt`、`dx`、`dy` とする。出力は `U^{n+1}` とする。

## 3. 演算定義
公開 `operation` は `dynamics_shallow_water_time_update_2d_ssprk2__advance` とする。ここで $L_{flux}(U)$ を界面フラックス差分、$S_b(U,z_b)$ を底面地形源項とする。更新は
$$
U^{(1)}=U^n+\Delta t\left(L_{flux}(U^n)+S_b(U^n,z_b)\right)
$$
$$
U^{n+1}=\frac{1}{2}U^n+\frac{1}{2}\left(U^{(1)}+\Delta t\left(L_{flux}(U^{(1)})+S_b(U^{(1)},z_b)\right)\right)
$$
で定義する。

## 4. 失敗条件と制約
`dt<=0`、`dx<=0`、`dy<=0` を入力不正としてエラーとする。

## 5. 公開 API と互換性
公開 `operation_id` は `dynamics_shallow_water_time_update_2d_ssprk2__advance` のみとする。

## 6. 禁止事項
時間積分法の自動切替を禁止する。

## 7. トレーサビリティ
`component_catalog.yaml` と `case.resolved.yaml` に採用結果を必須記録とする。

## 8. AD 準備情報
`ad_readiness.enabled` は `true` とする。非微分演算として `ceil`（`dt` 規則に用いる場合）を明示する。

## 9. tests 参照
対応する `tests.md` を同一ディレクトリに配置し、`test_profile_version` を `0.2.0` とする。
