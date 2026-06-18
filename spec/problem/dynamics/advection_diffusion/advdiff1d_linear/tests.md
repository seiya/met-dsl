# Tests: 1D linear advection-diffusion (verification input / judgment conditions)

## 0. Meta information
- `status`: `draft`
- `test_profile_id`: `advdiff1d_linear_baseline`
- `test_profile_version`: `0.1.1`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `advdiff1d_linear`
- `spec_ref.spec_version`: `0.3.0`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/advection_diffusion/advdiff1d_linear/controlled_spec.md`

## 1. Test purpose
This suite verifies, for the discrete implementation of the 1D linear advection-diffusion equation, accuracy, conservation, translation equivariance, and the CFL guard. The judgment targets are `L0` to `L3`, and include an expected failure (`xfail`).

## 2. Input-defaulting rules
### 2-1. Input instance
- The domain length `L` is `1.0`.
- The initial condition is $u(x,0)=\sin(2\pi x/L)+0.5\sin(4\pi x/L)$.
- The theoretical solution is defined by the following.
  $$
  u(x,t)=\exp(-\nu k_1^2 t)\sin(k_1(x-a t))+0.5\exp(-\nu k_2^2 t)\sin(k_2(x-a t))
  $$
- The symbols used in the theoretical solution are $k_1=2\pi/L$ and $k_2=4\pi/L$.

## 3. Execution-control rules
- $t_{start}=0.0$ and $t_{end}=0.5$.
- `dt` is decided by the following procedure.
  1. $\text{dt\_raw} = \min(\text{cfl\_adv}\cdot dx/a,\ \text{cfl\_dif}\cdot dx^2/\nu)$
  2. $\text{n\_step} = \lceil t_{end}/\text{dt\_raw}\rceil$
  3. $dt = t_{end}/\text{n\_step}$
- $\text{cfl\_adv}=0.6$ and $\text{cfl\_dif}=0.25$.
- The stop condition is $n = \text{n\_step}$.
- The output times are $0.0, 0.1, 0.2, 0.3, 0.4, 0.5$.

## 4. Case-expansion rules
### 4-1. family definition
The sweep and fixed values per `family` are defined below.

| family_id | purpose | sweep | fixed |
| --- | --- | --- | --- |
| `advdiff1d_ref` | refinement_for_accuracy | $nx \in \{64,128,256\}$ | $\text{shift\_fraction}=0.0,\ \text{dt\_scale}=1.0$ |
| `advdiff1d_sym` | translation_symmetry | $\text{nx}=128,\ \text{shift\_fraction}=0.25$ | $\text{dt\_scale}=1.0$ |
| `advdiff1d_guard` | expected_failure_for_cfl | $\text{nx}=64,\ \text{dt\_scale}=1.2$ | $\text{shift\_fraction}=0.0$ |

### 4-2. `case_id` generation rule
- The template is `advdiff1d_{family}_nx{nx:03d}_shift{shift_pct:03d}_dts{dts_pct:03d}`.
- `shift_pct` is decided by $\text{round}(100\cdot\text{shift\_fraction})$.
- `dts_pct` is decided by $\text{round}(100\cdot\text{dt\_scale})$.
- The expansion order is fixed in the order `family`, `nx`, `shift_fraction`, `dt_scale`.

### 4-3. Explicit-override case
- Add the `case_id` `advdiff1d_ref_nx128_shift000_dts100_tend200`.
- The `base_case_id` references `advdiff1d_ref_nx128_shift000_dts100`, and overrides only `t_end` to $2.0$.

## 5. Diagnostics contract
### 5-1. Artifacts
- The diagnostic output file is `diagnostics.json`.
- The judgment output file is `verdict.json`.

### 5-2. Required diagnostic items
`diagnostics.json` requires the following fields.
- `cfl.combined_max`
- `cfl.combined_time_series`
- `conserved.mass.initial`
- `conserved.mass.final`
- `errors.analytic.l2_rel_tend`
- `errors.mode_gain`
- `errors.mode_phase_rad`
- `errors.symmetry_l2_rel`

### 5-3. `N/A` rule
- When a diagnostic item is incomputable, make the output value `null` and require `reason_na`.
- The threshold-definition source is this file, and a per-test threshold override is permitted.

### 5-4. Definition of the mode metric
The $G_{num}, G_{ref}$ used in `mode_gain_error` and `mode_phase_error_rad` are defined as the 1-step amplification rate determined from each case's `dt` and `nx`.

Let the discrete mode number be $m \in \{1,2\}$, $\theta_m=2\pi m/nx$, and $k_m=2\pi m/L$.
The dimensionless numbers are $C=a\,dt/dx$ and $D=\nu\,dt/dx^2$.
Then the 1-step amplification rate is defined by the following.
$$
G_{num}(m)=1-C\left(1-e^{-i\theta_m}\right)+D\left(e^{i\theta_m}-2+e^{-i\theta_m}\right)
$$
$$
G_{ref}(m)=\exp\left(\left(-\nu k_m^2-iak_m\right)dt\right)
$$

`mode_gain_error` is defined by the following.
$$
\max_{m\in\{1,2\}}\frac{\left||G_{num}(m)|-|G_{ref}(m)|\right|}{|G_{ref}(m)|}
$$

`mode_phase_error_rad` is defined by the following.
$$
\max_{m\in\{1,2\}} \left| \mathrm{wrapToPi}\left(arg(G_{num}(m)) - arg(G_{ref}(m))\right) \right|
$$
Here `wrapToPi` is the operation that normalizes the phase difference to $[-\pi,\pi]$.

The Fourier-coefficient ratio of `u(t_{end})`, or a cumulative comparison using $G^{n_{step}}$, must not be used as the evaluation expression of these 2 metrics.

### 5-5. Default thresholds
- $\text{cfl\_combined\_max} \le 1.0$
- $\text{mass\_drift\_rel} \le 1.0e{-12}$
- `l2_rel_error_to_analytic_tend` is $\le 2.0e{-1}$ for $\text{nx}=64$, $\le 1.1e{-1}$ for $\text{nx}=128$, and $\le 6.0e{-2}$ for $\text{nx}=256$.
- $\text{convergence\_order} \ge 0.50$
- $\text{mode\_gain\_error} \le 5.0e{-3}$
- $\text{mode\_phase\_error\_rad} \le 5.0e{-3}$
- $\text{symmetry\_l2\_rel} \le 1.0e{-12}$

## 6. Test definitions
### 6-1. `l1_refinement_against_analytic`
- `level`: `L1`
- `objective`: confirm that the error decreases with refinement and the agreement with the theoretical solution is within tolerance.
- target cases:
  - `advdiff1d_ref_nx064_shift000_dts100`
  - `advdiff1d_ref_nx128_shift000_dts100`
  - `advdiff1d_ref_nx256_shift000_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The CFL judgment is applied. The evaluation expression is $\max_t(C + 2D)$, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is $\frac{|M_{end} - M_0|}{\max(\sum_i |u_i(0)|\cdot dx,\ 1e{-14})}$, and the threshold is $\le 1.0e{-12}$.
  - The symmetry judgment is not applied. The non-application basis is "because the translated pair case is not run in this test".
  - The theoretical-comparison judgment is applied.
    - `l2_rel_error_to_analytic_tend` applies a per-case threshold.
      - `advdiff1d_ref_nx064_shift000_dts100` is $\le 2.0e{-1}$
      - `advdiff1d_ref_nx128_shift000_dts100` is $\le 1.1e{-1}$
      - `advdiff1d_ref_nx256_shift000_dts100` is $\le 6.0e{-2}$
    - `convergence_order` uses $p=\log(e_{coarse}/e_{fine})/\log(2)$, and requires $\ge 0.50$ for both `nx64_to_nx128` and `nx128_to_nx256`.
    - `mode_gain_error` uses the 1-step amplification-rate evaluation expression defined in 5-4, and requires $\le 5.0e{-3}$.
    - `mode_phase_error_rad` uses the 1-step phase-error evaluation expression defined in 5-4, and requires $\le 5.0e{-3}$.

### 6-2. `l2_mass_conservation_long_run`
- `level`: `L2`
- `objective`: confirm discrete mass conservation in long-time integration.
- target cases:
  - `advdiff1d_ref_nx128_shift000_dts100_tend200`
- `expected_outcome`: `pass`
- judgment conditions:
  - The CFL judgment is applied. The evaluation expression is $\max_t(C + 2D)$, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is $\frac{|M_{end} - M_0|}{\max(\sum_i |u_i(0)|\cdot dx,\ 1e{-14})}$, and the threshold is $\le 3.0e{-12}$.
  - The symmetry judgment is not applied. The non-application basis is "because this test aims at the long-time conservation evaluation of a single initial condition".
  - The theoretical-comparison judgment is applied. It requires $\text{l2\_rel\_error\_to\_analytic\_tend} \le 2.3e{-1}$.

### 6-3. `l3_translation_equivariance`
- `level`: `L3`
- `objective`: confirm that the numerical solution translates by the same amount in response to the translation of the initial condition.
- pair cases:
  - `reference` is `advdiff1d_ref_nx128_shift000_dts100`
  - `shifted` is `advdiff1d_sym_nx128_shift025_dts100`
- `expected_outcome`: `pass`
- judgment conditions:
  - The CFL judgment is applied. The evaluation expression is $\max_t(C + 2D)$, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is $\frac{|M_{end} - M_0|}{\max(\sum_i |u_i(0)|\cdot dx,\ 1e{-14})}$, and the threshold is $\le 1.0e{-12}$.
  - The symmetry judgment is applied. The evaluation expression is $\|u_{shifted}(t_{end}) - shift(u_{ref}(t_{end}), +0.25L)\|_2 / \|u_{ref}(t_{end})\|_2$, and the threshold is $\le 1.0e{-12}$.
  - The theoretical-comparison judgment is applied. It requires $\text{l2\_rel\_error\_to\_analytic\_tend} \le 1.1e{-1}$.

### 6-4. `l0_cfl_guard_xfail`
- `level`: `L0`
- `objective`: confirm that a CFL-violation case can be detected (expected failure).
- target cases:
  - `advdiff1d_guard_nx064_shift000_dts120`
- `expected_outcome`: `xfail`
- `xfail_condition`: `cfl.combined_max > 1.0`
- `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'cfl'`
- judgment conditions:
  - The CFL judgment is applied. The evaluation expression is $\max_t(C + 2D)$, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is $\frac{|M_{end} - M_0|}{\max(\sum_i |u_i(0)|\cdot dx,\ 1e{-14})}$, and the threshold is `informational_only`.
  - The symmetry judgment is not applied. The non-application basis is "because the purpose of the guard test is only the detection of a stability-condition violation".
  - The theoretical-comparison judgment is not applied. The non-application basis is "because under an unstable condition, the cannot-continue-execution judgment is prioritized over the theoretical-agreement judgment".

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: pass when all applicable checks pass.
- `per_test.xfail_rule`: xfail when `expected_outcome == xfail` and `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: pass when all `test_id` satisfy `pass_rule` or `xfail_rule`.

## 8. Traceability
### 8-1. Output requirements
- `verdict.json` requires each check's `status`, `metric_value`, `threshold`, and `reason_na` when `applicable=false`.
- `summary.json` requires the counts of `pass`, `fail`, `xfail`, and `skipped`.

### 8-2. Traceability records
- The `test_profile_id` and `test_profile_version` of this document must be recorded in `case.resolved.yaml` and `trial_meta.json`.
- The judgment conditions of this document must be mappable to the evaluation basis of `verdict.json`.
