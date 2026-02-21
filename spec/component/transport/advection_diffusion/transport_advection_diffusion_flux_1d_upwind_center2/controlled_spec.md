# Controlled Spec: 1 次元 移流拡散 フラックス（component spec）

## 0. メタ情報
- `spec_id`: `transport_advection_diffusion_flux_1d_upwind_center2`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `transport`
- `family`: `advection_diffusion`

## 1. 責務と適用範囲
本 `component` は 1 次元 移流拡散 問題の界面フラックスを計算する責務を持つ。状態更新そのものは扱わない。

## 2. 入出力契約
入力は `u(i)`、`a`、`nu`、`dx`、`dt` とする。出力は `flux_adv(i+1/2)`、`flux_dif(i+1/2)` とする。`u` は セル中心値を前提とする。

## 3. 演算定義
公開 `operation` は `transport_advection_diffusion_flux_1d_upwind_center2__compute_flux` とする。移流フラックスは 一次 風上、拡散フラックスは 二次 中心で定義する。
$$
F^{adv}_{i+1/2}=a\,u_i\quad(a>0)
$$
$$
F^{dif}_{i+1/2}=-\nu\frac{u_{i+1}-u_i}{dx}
$$

## 4. 失敗条件と制約
`a<=0`、`dx<=0`、`dt<=0` を入力不正としてエラーとする。

## 5. 公開 API と互換性
公開 `operation_id` は `transport_advection_diffusion_flux_1d_upwind_center2__compute_flux` のみとする。`major` 互換破壊時は `spec_id` を分離する。

## 6. 禁止事項
自動で離散化次数を変更してはならない。未定義入力の暗黙補完を禁止する。

## 7. トレーサビリティ
`component_catalog.yaml` には本 `operation_id` を必須登録とする。`case.resolved.yaml` には採用 `component_id@version` を必須記録とする。

## 8. AD 準備情報
`ad_readiness.enabled` は `true` とする。非微分演算は含まない。

## 9. tests 参照
対応する `tests.md` を同一ディレクトリに配置し、`test_profile_version` を `0.1.0` とする。
