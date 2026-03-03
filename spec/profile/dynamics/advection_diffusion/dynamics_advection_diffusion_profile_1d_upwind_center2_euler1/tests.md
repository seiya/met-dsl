# Tests: 1 次元 移流拡散 既定プロファイル

## 0. メタ情報
- `test_profile_id`: `dynamics_advection_diffusion_profile_1d_upwind_center2_euler1_validation`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `profile`
- `spec_ref.spec_id`: `dynamics_advection_diffusion_profile_1d_upwind_center2_euler1`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/profile/dynamics/advection_diffusion/dynamics_advection_diffusion_profile_1d_upwind_center2_euler1/controlled_spec.md`

## 1. テスト目的
本スイートは、`advection_diffusion` 問題に対する既定プロファイル選択規則と互換性ガードを検証する。

## 2. 入力既定化規則
- 正常系は `problem.family=advection_diffusion`、`dimension=1d`、`boundary=periodic` を入力とする。
- 正常系の `component` 版は次を使用する。
  - `dynamics_advection_diffusion_flux_1d_upwind_center2@0.1.0`
  - `dynamics_advection_diffusion_boundary_1d_periodic_copy@0.1.0`
  - `dynamics_advection_diffusion_time_update_1d_euler1@0.1.0`
- 異常系は互換範囲外版として `dynamics_advection_diffusion_flux_1d_upwind_center2@1.0.0` を使用する。

## 3. 実行制御規則
本スイートは `profile` 選択ロジックの判定のみを対象とする。時刻積分の実行制御は `N/A` とする。理由は「本スイートがプロファイル解決のみを検証対象とするため」とする。

## 4. ケース展開規則
- `case_id=profile_select_default`
- `case_id=profile_guard_incompatible_version`
- `case_id=profile_guard_nonperiodic_boundary`

## 5. 診断契約
`diagnostics.json` は次を必須とする。
- `checks.profile_selected`
- `checks.component_compatibility`
- `checks.boundary_requirement`

## 6. テスト定義
- `test_id`: `l0_select_default_profile_pass`
  - `level`: `L0`
  - `expected_outcome`: `pass`
  - `target_case`: `profile_select_default`
  - `判定`: `profile_id=dynamics_advection_diffusion_profile_1d_upwind_center2_euler1` が選択され、`checks.profile_selected=true` を満たす。

- `test_id`: `l0_guard_incompatible_component_version_xfail`
  - `level`: `L0`
  - `expected_outcome`: `xfail`
  - `target_case`: `profile_guard_incompatible_version`
  - `xfail_condition`: 対象 `component` 版が `>=0.1.0 <1.0.0` を満たさない。
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'component_compatibility'`

- `test_id`: `l0_guard_nonperiodic_boundary_xfail`
  - `level`: `L0`
  - `expected_outcome`: `xfail`
  - `target_case`: `profile_guard_nonperiodic_boundary`
  - `xfail_condition`: `boundary != periodic`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'boundary_requirement'`

## 7. 合否集約規則
- `per_test.pass_rule`: 判定式を満たす場合に `pass` とする。
- `per_test.xfail_rule`: `xfail_condition` が真で `pass_when` を満たす場合に `xfail` とする。
- `suite.pass_rule`: 全 `test_id` が `pass` または `xfail` の場合に `pass` とする。

## 8. トレーサビリティ
`test_profile_id`、`test_profile_version`、`spec_ref` を `trial_meta.json` に記録する。
