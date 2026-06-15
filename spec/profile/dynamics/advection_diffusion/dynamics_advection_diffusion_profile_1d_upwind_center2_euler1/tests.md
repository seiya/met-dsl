# Tests: 1D advection-diffusion default profile

## 0. Meta information
- `test_profile_id`: `dynamics_advection_diffusion_profile_1d_upwind_center2_euler1_validation`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `profile`
- `spec_ref.spec_id`: `dynamics_advection_diffusion_profile_1d_upwind_center2_euler1`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/profile/dynamics/advection_diffusion/dynamics_advection_diffusion_profile_1d_upwind_center2_euler1/controlled_spec.md`

## 1. Test purpose
This suite verifies the default-profile selection rule and the compatibility guard for the `advection_diffusion` problem.

## 2. Input-defaulting rules
- The normal case takes `problem.family=advection_diffusion`, `dimension=1d`, `boundary=periodic` as input.
- The normal-case `component` versions use the following.
  - `dynamics_advection_diffusion_flux_1d_upwind_center2@0.1.0`
  - `dynamics_advection_diffusion_boundary_1d_periodic_copy@0.1.0`
  - `dynamics_advection_diffusion_time_update_1d_euler1@0.1.0`
- The abnormal case uses `dynamics_advection_diffusion_flux_1d_upwind_center2@1.0.0` as an out-of-compatibility-range version.

## 3. Execution-control rules
This suite targets only the judgment of the `profile`-selection logic. The execution control of the time integration is `N/A`. The reason is "because this suite verifies only profile resolution".

## 4. Case-expansion rules
- `case_id=profile_select_default`
- `case_id=profile_guard_incompatible_version`
- `case_id=profile_guard_nonperiodic_boundary`

## 5. Diagnostics contract
`diagnostics.json` requires the following.
- `checks.profile_selected`
- `checks.component_compatibility`
- `checks.boundary_requirement`

## 6. Test definitions
- `test_id`: `l0_select_default_profile_pass`
  - `level`: `L0`
  - `expected_outcome`: `pass`
  - `target_case`: `profile_select_default`
  - `judgment`: `profile_id=dynamics_advection_diffusion_profile_1d_upwind_center2_euler1` is selected, and `checks.profile_selected=true` is satisfied.

- `test_id`: `l0_guard_incompatible_component_version_xfail`
  - `level`: `L0`
  - `expected_outcome`: `xfail`
  - `target_case`: `profile_guard_incompatible_version`
  - `xfail_condition`: a target `component` version does not satisfy `>=0.1.0 <1.0.0`.
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'component_compatibility'`

- `test_id`: `l0_guard_nonperiodic_boundary_xfail`
  - `level`: `L0`
  - `expected_outcome`: `xfail`
  - `target_case`: `profile_guard_nonperiodic_boundary`
  - `xfail_condition`: `boundary != periodic`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'boundary_requirement'`

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
Record `test_profile_id`, `test_profile_version`, and `spec_ref` in `trial_meta.json`.
