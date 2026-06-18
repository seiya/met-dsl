# Controlled Spec: 1D periodic-boundary mapping (component spec)

## 0. Meta information
- `spec_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `advection_diffusion`

## 1. Responsibility and scope
This `component` is responsible only for the periodic-boundary ghost mapping of a 1D array.

## 2. input/output contract
The input is `u(-ng:nx-1+ng)`, `nx`, and `ng`. The output is `u` after the periodic mapping.

## 3. Operation definition
The published `operation` is `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`. When `ng=1`, apply
$$
u_{-1}=u_{nx-1},\quad u_{nx}=u_0
$$

## 4. Failure conditions and constraints
Treat `nx<2` and `ng<1` as invalid input and an error.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`. On a `major` compatibility break, separate the `spec_id`.

## 6. Prohibitions
Forbid automatic fallback to a non-periodic boundary.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_boundary_1d_periodic_copy/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. The periodic-index wrap is made explicit as a discrete operation.
