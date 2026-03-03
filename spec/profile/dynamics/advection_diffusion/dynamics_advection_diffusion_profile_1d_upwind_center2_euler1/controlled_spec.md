# Controlled Spec: 1 次元 移流拡散 既定プロファイル（profile spec）

## 0. メタ情報
- `spec_id`: `dynamics_advection_diffusion_profile_1d_upwind_center2_euler1`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `profile`
- `domain`: `dynamics`
- `family`: `advection_diffusion`

## 1. 対象 `component` と互換範囲
対象 `component` は次とする。
- `dynamics_advection_diffusion_flux_1d_upwind_center2`（`>=0.1.0 <1.0.0`）
- `dynamics_advection_diffusion_boundary_1d_periodic_copy`（`>=0.1.0 <1.0.0`）
- `dynamics_advection_diffusion_time_update_1d_euler1`（`>=0.1.0 <1.0.0`）

## 2. 選択規則
`problem spec` が `family=advection_diffusion` かつ 1 次元 周期境界 を要求する場合、本 `profile` を既定で選択する。

## 3. パラメタ拘束
離散化拘束は次とする。
- 移流項: 一次 風上
- 拡散項: 二次 中心
- 時間積分: 前進 Euler
- 境界条件: 周期写像

## 4. フォールバック規則
対象 `component` の互換条件を満たさない場合はエラーとし、代替 `profile` への自動切替を禁止する。

## 5. トレーサビリティ
`case.resolved.yaml` には `profile_id=dynamics_advection_diffusion_profile_1d_upwind_center2_euler1` と解決された `component_id@version` を必須記録とする。

## 6. tests 参照
対応する `tests.md` を同一ディレクトリに配置し、`test_profile_version` を `0.1.0` とする。
