# Controlled Spec: 2D periodic-boundary mapping (component spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_boundary_2d_periodic_copy`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Responsibility and scope
This `component` is responsible only for the 2D periodic-boundary ghost mapping.

## 2. input/output contract
The input is `U(i,j)`, `nx`, `ny`, and `ng`. The output is `U` after the periodic mapping.

## 3. Operation definition
The published `operation` is `dynamics_shallow_water_boundary_2d_periodic_copy__apply`. It applies the periodic mapping in the `x` and `y` directions in order.

## 4. Failure conditions and constraints
Treat `nx<2`, `ny<2`, and `ng<1` as invalid input and an error.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_shallow_water_boundary_2d_periodic_copy__apply`.

## 6. Prohibitions
Forbid automatic fallback to a non-periodic boundary.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/shallow_water/dynamics_shallow_water_boundary_2d_periodic_copy/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. The periodic-index wrap is made explicit as a discrete operation.
