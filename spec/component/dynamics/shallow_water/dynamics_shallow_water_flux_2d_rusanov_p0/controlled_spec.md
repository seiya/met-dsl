# Controlled Spec: 2 次元 shallow water Rusanov フラックス（component spec）

## 0. メタ情報
- `spec_id`: `dynamics_shallow_water_flux_2d_rusanov_p0`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. 責務と適用範囲
本 `component` は shallow water 方程式の界面フラックス計算を担当する。再構築は 一次 `p0` に固定する。

## 2. 入出力契約
入力は `U=[h,hu,hv]^T` の左右状態と上下状態、重力加速度 `g` とする。出力は `F*`、`G*` とする。

## 3. 演算定義
公開 `operation` は `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux` とする。Rusanov フラックスを
$$
F^{*}(U_L,U_R)=\frac{1}{2}\left(F(U_L)+F(U_R)\right)-\frac{1}{2}a_x\left(U_R-U_L\right)
$$
$$
G^{*}(U_B,U_T)=\frac{1}{2}\left(G(U_B)+G(U_T)\right)-\frac{1}{2}a_y\left(U_T-U_B\right)
$$
で定義し、波速は
$$
a_x=\max(|u_L|+c_L,|u_R|+c_R),\quad a_y=\max(|v_B|+c_B,|v_T|+c_T),\quad c=\sqrt{gh}
$$
とする。

## 4. 失敗条件と制約
`h<=0` を入力不正としてエラーとする。

## 5. 公開 API と互換性
公開 `operation_id` は `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux` のみとする。

## 6. 禁止事項
再構築次数の自動切替と limiter の暗黙適用を禁止する。

## 7. トレーサビリティ
`component_catalog.yaml` と `case.resolved.yaml` に採用結果を必須記録とする。

## 8. AD 準備情報
`ad_readiness.enabled` は `true` とする。非微分演算として `max` と `abs` を明示する。

## 9. tests 参照
対応する `tests.md` を同一ディレクトリに配置し、`test_profile_version` を `0.1.0` とする。
