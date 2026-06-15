# Tests: 2D shallow water Rusanov flux (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_flux_2d_rusanov_p0_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_flux_2d_rusanov_p0`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_flux_2d_rusanov_p0/controlled_spec.md`

## 1. Tested `operation`
- `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`

## 2. Input-defaulting rules
- The normal case uses left/right and bottom/top states satisfying `h>0`.
- The abnormal case uses a state including `h<=0`.

## 3. Diagnostics contract
- Require outputting `checks.equal_state_consistency`, `checks.wave_speed_nonnegative`, and `checks.input_guard` in `diagnostics.json`.

## 4. Test definitions
- `test_id`: `l0_equal_state_consistency_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with `U_L=U_R`, satisfy `F*=F(U_L)`.
- `test_id`: `l0_wave_speed_nonnegative_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: the computed wave speeds `a_x`,`a_y` are non-negative.
- `test_id`: `l0_invalid_dry_state_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<=0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 5. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 6. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
