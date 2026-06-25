# Controlled Spec: dependency-chain base scale (component spec)

## 0. Meta information
- `spec_id`: `demo_dep_base`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `demo`
- `family`: `dep_chain`

## 1. Responsibility and scope
This `component` is the leaf of a minimal two-node dependency chain used to exercise the deterministic dependency build (Model B). It is responsible for an elementwise scale of a 1D field. It has no dependency `component` of its own.

## 2. input/output contract
The input is `x(i)` for `i = 1..n` with `n >= 1`. The output is `y(i)` for `i = 1..n`. `x` and `y` are cell-centered values of the same length `n`.

## 3. Operation definition
The published `operation` is `demo_dep_base__scale`. It multiplies each element by the fixed factor `2`.
$$
y_i = 2\,x_i
$$

## 4. Failure conditions and constraints
Treat `n <= 0` as invalid input and an error. The scale factor is the fixed constant `2`; it must not be made configurable.

## 5. Public API and compatibility
The only published `operation_id` is `demo_dep_base__scale`. On a `major` compatibility break, separate the `spec_id`.

## 6. Prohibitions
The scale factor must not be changed automatically. Forbid implicit completion of undefined input.

## 7. Traceability
This `operation_id` requires registration in `component_catalog.yaml`. `case.resolved.yaml` requires recording the adopted `component_id@version`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/demo/dep_chain/demo_dep_base/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. It includes no non-differentiable operations.
