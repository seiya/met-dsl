# Controlled Spec: 2 次元 shallow water 既定プロファイル（profile spec）

## 0. メタ情報
- `spec_id`: `dynamics_shallow_water_profile_2d_rusanov_p0_ssprk2`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `profile`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. 対象 `component` と互換範囲
対象 `component` は次とする。
- `dynamics_shallow_water_flux_2d_rusanov_p0`（`>=0.1.0 <1.0.0`）
- `dynamics_shallow_water_boundary_2d_periodic_copy`（`>=0.1.0 <1.0.0`）
- `dynamics_shallow_water_time_update_2d_ssprk2`（`>=0.1.0 <1.0.0`）

## 2. 選択規則
`problem spec` が `family=shallow_water` かつ 周期境界 を要求する場合、本 `profile` を既定で選択する。

## 3. パラメタ拘束
離散化拘束は次とする。
- 界面フラックス: Rusanov
- 再構築: `p0`
- 時間積分: `SSPRK2`
- 境界条件: 周期写像

## 4. フォールバック規則
対象 `component` の互換条件を満たさない場合はエラーとし、代替 `profile` への自動切替を禁止する。

## 5. トレーサビリティ
`case.resolved.yaml` には `profile_id=dynamics_shallow_water_profile_2d_rusanov_p0_ssprk2` と解決された `component_id@version` を必須記録とする。

## 6. tests 参照
対応する `tests.md` を同一ディレクトリに配置し、`test_profile_version` を `0.1.0` とする。
