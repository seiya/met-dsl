# Controlled Spec: 2D shallow water default profile (profile spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_profile_2d_rusanov_p0_ssprk2`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `profile`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Target `component` and compatibility range
The target `component` are the following.
- `dynamics_shallow_water_flux_2d_rusanov_p0` (`>=0.1.0 <1.0.0`)
- `dynamics_shallow_water_boundary_2d_periodic_copy` (`>=0.1.0 <1.0.0`)
- `dynamics_shallow_water_time_update_2d_ssprk2` (`>=0.1.0 <1.0.0`)

## 2. Selection rules
When a `problem spec` requires `family=shallow_water` and a periodic boundary, select this `profile` by default.

## 3. Parameter constraints
The discretization constraints are the following.
- interface flux: Rusanov
- reconstruction: `p0`
- time integration: `SSPRK2`
- boundary condition: periodic mapping

## 4. Fallback rules
When the compatibility condition of a target `component` is not satisfied, it is an error, and automatic switching to an alternative `profile` is forbidden.

## 5. Traceability
`case.resolved.yaml` requires recording `profile_id=dynamics_shallow_water_profile_2d_rusanov_p0_ssprk2` and the resolved `component_id@version`.

## 6. tests reference
Place the corresponding `tests.md` in the same directory, with `test_profile_version` of `0.1.0`.
