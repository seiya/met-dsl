# Controlled Spec: 1D advection-diffusion default profile (profile spec)

## 0. Meta information
- `spec_id`: `dynamics_advection_diffusion_profile_1d_upwind_center2_euler1`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `profile`
- `domain`: `dynamics`
- `family`: `advection_diffusion`

## 1. Target `component` and compatibility range
The target `component` are the following.
- `dynamics_advdiff_flux_1d_upwind_center2` (`>=0.1.0 <1.0.0`)
- `dynamics_advection_diffusion_boundary_1d_periodic_copy` (`>=0.1.0 <1.0.0`)
- `dynamics_advection_diffusion_time_update_1d_euler1` (`>=0.1.0 <1.0.0`)

## 2. Selection rules
When a `problem spec` requires `family=advection_diffusion` and a 1D periodic boundary, select this `profile` by default.

## 3. Parameter constraints
The discretization constraints are the following.
- advection term: first-order upwind
- diffusion term: second-order central
- time integration: forward Euler
- boundary condition: periodic mapping

## 4. Fallback rules
When the compatibility condition of a target `component` is not satisfied, it is an error, and automatic switching to an alternative `profile` is forbidden.

## 5. Traceability
`case.resolved.yaml` requires recording `profile_id=dynamics_advection_diffusion_profile_1d_upwind_center2_euler1` and the resolved `component_id@version`.

## 6. tests reference
The corresponding `tests.md` is `spec/profile/dynamics/advection_diffusion/dynamics_advection_diffusion_profile_1d_upwind_center2_euler1/tests.md`, with `test_profile_version` of `0.1.0`.
