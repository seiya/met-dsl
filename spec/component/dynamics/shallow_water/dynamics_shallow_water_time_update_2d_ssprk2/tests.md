# Tests: 2D `SSPRK2` update (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_time_update_2d_ssprk2_l0`
- `test_profile_version`: `0.3.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_time_update_2d_ssprk2`
- `spec_ref.spec_version`: `0.3.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_time_update_2d_ssprk2/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_shallow_water_time_update_2d_ssprk2__advance` at `L0`: the zero-`RHS` invariance, the 2-stage weight consistency, the frozen-`RHS` exactness, the `z_b`-invariance, and the input guard for an invalid time step (`dt<=0`).

## 2. Input-defaulting rules
- The normal case uses `dt>0`, `dx>0`, `dy>0`, and includes both `S_b=0` and `S_b!=0` in the evaluation target.
- `L_flux` and `S_b` are supplied as fixed fields; the runner does not recompute them.
- The `z_b`-invariance case holds `U^n`, `L_flux`, and `S_b` fixed and evaluates the update for two distinct `z_b` fields, comparing the two `U^{n+1}` results.
- The abnormal case uses `dt<=0`.

## 3. Execution-control rules
`N/A`: this `component` advances a single step for a given `dt` and does not control the time loop or its cadence. Execution control is the responsibility of the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.zero_rhs_invariance`, `checks.stage_weight_consistency`, `checks.input_guard`, `checks.zb_invariance`, and `checks.frozen_rhs_exactness` in `diagnostics.json`.

## 6. Test definitions
- `test_id`: `l0_zero_rhs_invariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `judgment`: with `L_flux=0` and `S_b=0`, satisfy `U^{n+1}=U^n` within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`).
- `test_id`: `l0_stage_weight_consistency_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `judgment`: the weights of the 2-stage composition are applied as `1/2,1/2`.
- `test_id`: `l0_frozen_rhs_exactness_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `judgment`: with fixed nonzero `L_flux` and `S_b`, satisfy `U^{n+1} = U^n + dt*(L_flux + S_b)` within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`; frozen-field closed form; both stages use the identical RHS).
- `test_id`: `l0_zb_invariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `judgment`: with `U^n`, `L_flux`, and `S_b` held fixed, the update evaluated for two distinct `z_b` fields yields the identical `U^{n+1}` (`z_b` is inert at L0).
- `test_id`: `l0_invalid_dt_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt<=0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
