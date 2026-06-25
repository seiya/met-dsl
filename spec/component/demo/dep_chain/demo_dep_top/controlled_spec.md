# Controlled Spec: dependency-chain top shift-scaled (component spec)

## 0. Meta information
- `spec_id`: `demo_dep_top`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `demo`
- `family`: `dep_chain`

## 1. Responsibility and scope
This `component` is the dependent node of a minimal two-node dependency chain used to exercise the deterministic dependency build (Model B). It composes the published scale `operation` of `demo_dep_base` with a fixed shift. It must not re-implement the scale itself.

## 2. input/output contract
The input is `x(i)` for `i = 1..n` with `n >= 1`. The output is `z(i)` for `i = 1..n`. `x` and `z` are cell-centered values of the same length `n`.

## 3. Operation definition
The published `operation` is `demo_dep_top__shift_scaled`. It calls the dependency `operation` `demo_dep_base__scale` to obtain `y = 2*x`, then adds the fixed shift `1`.
$$
z_i = \big(\,\texttt{demo\_dep\_base\_\_scale}(x)\,\big)_i + 1 = 2\,x_i + 1
$$

## 4. Failure conditions and constraints
Treat `n <= 0` as invalid input and an error. The shift is the fixed constant `1`; it must not be made configurable. The scale must be obtained by calling `demo_dep_base__scale`, not re-derived in this `component`.

## 5. Public API and compatibility
The only published `operation_id` is `demo_dep_top__shift_scaled`. On a `major` compatibility break, separate the `spec_id`.

## 6. Prohibitions
The shift constant must not be changed automatically. A function equivalent to `demo_dep_base__scale` must not be re-implemented here. Forbid implicit completion of undefined input.

## 7. Traceability
This `operation_id` requires registration in `component_catalog.yaml`. `case.resolved.yaml` requires recording the adopted `component_id@version` for both this `component` and `demo_dep_base`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/demo/dep_chain/demo_dep_top/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. It includes no non-differentiable operations.
