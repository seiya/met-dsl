# Tests: 1D periodic-boundary mapping (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_boundary_1d_periodic_copy/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply` at `L0`: the left and right periodic-wrap mapping of the ghost cells and the input guard for an invalid grid size (`nx<2`).

## 2. Input-defaulting rules
- The normal case uses `nx>=2`, `ng=1`.
- The abnormal case uses `nx<2`.

## 3. Execution-control rules
`N/A`: this `component` applies a boundary mapping to a provided field and defines no time-stepping or iteration. Execution control is the responsibility of the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.left_wrap`, `checks.right_wrap`, and `checks.input_guard` in `diagnostics.json`.

## 6. Test definitions
- `test_id`: `l0_periodic_left_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `judgment`: after applying, satisfy `u_{-1}=u_{nx-1}`.
- `test_id`: `l0_periodic_right_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `judgment`: after applying, satisfy `u_{nx}=u_0`.
- `test_id`: `l0_invalid_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `nx<2`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
