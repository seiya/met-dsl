# Tests: 2D shallow water Rusanov flux (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_flux_2d_rusanov_p0_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_flux_2d_rusanov_p0`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_flux_2d_rusanov_p0/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux` at `L0`: the consistency of the numerical flux when the left/right states are equal, the non-negativity of the Rusanov wave speeds, and the input guard for a dry state (`h<=0`).

## 2. Input-defaulting rules
- The normal case uses left/right and bottom/top states satisfying `h>0`.
- The abnormal case uses a state including `h<=0`.

## 3. Execution-control rules
`N/A`: this `component` exposes a single pointwise `operation` and defines no time-stepping or iteration. Execution control is the responsibility of the time-update `component` and the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed single-interface states and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.equal_state_consistency`, `checks.wave_speed_nonnegative`, and `checks.input_guard` in `diagnostics.json`.

## 6. Test definitions
- `test_id`: `l0_equal_state_consistency_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with `U_L=U_R`, the dissipation term is analytically zero, so satisfy `$\max_i |F^{*}_i - F(U_L)_i| \le 10^{-12}$` component-wise (and the same for `G*` with `U_B=U_T`).
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

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
