# Tests: dependency-chain base scale (L0)

## 0. Meta information
- `test_profile_id`: `demo_dep_base_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `demo_dep_base`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/demo/dep_chain/demo_dep_base/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `demo_dep_base__scale` at `L0`: the elementwise scale identity `y = 2*x` for a known field, and the input guard for an invalid length (`n <= 0`).

## 2. Input-defaulting rules
- The normal case uses `n >= 1` and finite `x`.
- The abnormal case uses `n <= 0`.

## 3. Execution-control rules
`N/A`: this `component` exposes a single elementwise `operation` and defines no time-stepping or iteration. Execution control is the responsibility of the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed single-vector inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.scale_identity` and `checks.input_guard` in `diagnostics.json`.

## 6. Test definitions
- `test_id`: `l0_scale_identity_pass`
  - `level`: `L0`
  - `operation_id`: `demo_dep_base__scale`
  - `expected_outcome`: `pass`
  - `judgment`: with input `x = [1.0, 2.0, 3.0]`, satisfy `y = [2.0, 4.0, 6.0]` within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`).
- `test_id`: `l0_invalid_length_xfail`
  - `level`: `L0`
  - `operation_id`: `demo_dep_base__scale`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `n <= 0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
