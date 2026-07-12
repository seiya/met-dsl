# Tests: 1D linear advection-diffusion (verification input / judgment conditions)

## 0. Meta information
- `status`: `draft`
- `test_profile_id`: `advdiff1d_linear_baseline`
- `test_profile_version`: `0.2.0`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `advdiff1d_linear`
- `spec_ref.spec_version`: `0.3.0`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/advection_diffusion/advdiff1d_linear/controlled_spec.md`

## 1. Test purpose
This suite verifies, for the discrete implementation of the 1D linear advection-diffusion equation, accuracy, conservation, translation equivariance, and the CFL guard. The judgment targets are `L0` to `L3`, and include an expected failure (`xfail`).

## 2. Input-defaulting rules
### 2-1. Input instance
- The domain length `L` is `1.0`.
- The initial condition is parameterized by `shift_fraction`, and is defined by the following.
  $$
  u(x,0)=\sin\left(2\pi\left(\frac{x}{L}-\text{shift\_fraction}\right)\right)+0.5\sin\left(4\pi\left(\frac{x}{L}-\text{shift\_fraction}\right)\right)
  $$
- The default of `shift_fraction` is `0.0`, and the case value is fixed by the 4-1 table.
- The theoretical solution is defined by the following.
  $$
  u(x,t)=\exp(-\nu k_1^2 t)\sin(k_1(x-a t-\text{shift\_fraction}\cdot L))+0.5\exp(-\nu k_2^2 t)\sin(k_2(x-a t-\text{shift\_fraction}\cdot L))
  $$
- The symbols used in the theoretical solution are $k_1=2\pi/L$ and $k_2=4\pi/L$.
- The initial condition of a case with $\text{shift\_fraction}=s$ is the translation of the $s=0$ initial condition by $+s\cdot L$; this is the property the translation-equivariance judgment of 6-3 evaluates.

## 3. Execution-control rules
- $t_{start}=0.0$ and $t_{end}=0.5$.
- `dt` is decided by the following procedure.
  1. $\text{dt\_raw} = \text{dt\_scale}\cdot\min(\text{cfl\_adv}\cdot dx/a,\ \text{cfl\_dif}\cdot dx^2/\nu)$
  2. $\text{n\_step} = \lceil t_{end}/\text{dt\_raw}\rceil$
  3. $dt = t_{end}/\text{n\_step}$
- $\text{cfl\_adv}=0.6$ and $\text{cfl\_dif}=0.25$.
- The default of `dt_scale` is `1.0`, and the case value is fixed by the 4-1 table. `dt_scale` enters the procedure only at step 1; it does not scale $dt$ or $\text{n\_step}$ directly, so the integration still lands exactly on $t_{end}$.
- The stop condition is $n = \text{n\_step}$.
- The output times are $t_{start} + j\cdot (t_{end}-t_{start})/5$ for $j = 0,1,2,3,4,5$, so the last output time is $t_{end}$. For a case that does not override $t_{end}$ this is $0.0, 0.1, 0.2, 0.3, 0.4, 0.5$.
- The per-case state snapshot is emitted at $t_{end}$, which is the evaluation time of every metric of 5-4.

## 4. Case-expansion rules
### 4-1. family definition
The sweep and fixed values per `family` are defined below.

| family_id | purpose | sweep | fixed |
| --- | --- | --- | --- |
| `advdiff1d_ref` | refinement_for_accuracy | $nx \in \{64,128,256\}$ | $\text{shift\_fraction}=0.0,\ \text{dt\_scale}=1.0$ |
| `advdiff1d_sym` | translation_symmetry | $\text{nx}=128,\ \text{shift\_fraction}=0.25$ | $\text{dt\_scale}=1.0$ |
| `advdiff1d_guard` | expected_failure_for_cfl | $\text{nx}=64,\ \text{dt\_scale}=1.2$ | $\text{shift\_fraction}=0.0$ |

### 4-2. `case_id` generation rule
- The template is `{family_id}_nx{nx:03d}_shift{shift_pct:03d}_dts{dts_pct:03d}`.
- `family_id` is the `family_id` column of the 4-1 table, substituted verbatim; it already carries the `advdiff1d_` prefix, and no further prefix is prepended.
- `shift_pct` is decided by $\text{round}(100\cdot\text{shift\_fraction})$.
- `dts_pct` is decided by $\text{round}(100\cdot\text{dt\_scale})$.
- The expansion order is fixed in the order `family_id`, `nx`, `shift_fraction`, `dt_scale`.

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
- `conserved.mass.initial`
- `conserved.mass.final`
- `conserved.mass.abs_initial`
- `metrics.mass_drift_rel`
- `errors.analytic.l2_rel_tend`
- `errors.mode_gain`
- `errors.mode_phase_rad`
- `errors.symmetry_l2_rel`
- `convergence.nx64_to_nx128.l2_order`
- `convergence.nx128_to_nx256.l2_order`

### 5-3. `N/A` rule
- When a diagnostic item is incomputable, make the output value `null` and require `reason_na`.
- `errors.symmetry_l2_rel` is `N/A` for anything other than a translation-pair evaluation, and is emitted on the `shifted` case of the pair.
- The threshold-definition source is this file, and a per-test threshold override is permitted.

### 5-4. Definition of the metrics
Every judged metric name below is bound to the `diagnostics.json` field of 5-2 that carries its value. A metric that is a derived quantity is emitted by the runner under its own field address, already reduced to a scalar; the definition below fixes how that value is computed. No correspondence other than the ones fixed here is permitted.

`cfl_combined_max` is emitted as the field `cfl.combined_max`. Its value is $\max_n (C+2D)$ over the integration steps of the case, where $C=a\,dt/dx$ and $D=\nu\,dt/dx^2$.

The relative mass-drift value is defined by the following.
$$
\mathrm{mass\_drift\_rel}=
\frac{|M_{end}-M_0|}{\max(M^{abs}_0,\ 1e{-14})},
\quad
M=\sum_i u_i\,dx,
\quad
M^{abs}_0=\sum_i |u_i(0)|\,dx
$$
Here $M_0$ is the field `conserved.mass.initial`, $M_{end}$ is `conserved.mass.final`, and $M^{abs}_0$ is `conserved.mass.abs_initial`. The signed initial mass of the initial condition in 2-1 is zero, so the normalizer is $M^{abs}_0$ and not $|M_0|$. The reduced value is emitted as the field `metrics.mass_drift_rel`.

The theoretical-comparison error is defined by the following.
$$
\mathrm{l2\_rel\_error\_to\_analytic\_tend}=
\frac{\|u_{num}(t_{end})-u_{exact}(t_{end})\|_2}{\|u_{exact}(t_{end})\|_2}
$$
Here $u_{exact}$ is the theoretical solution of 2-1, and the value is emitted as the field `errors.analytic.l2_rel_tend`.

The translation-equivariance error is defined by the following.
$$
\mathrm{symmetry\_l2\_rel}=
\frac{\|u_{shifted}(t_{end})-\mathrm{shift}(u_{ref}(t_{end}),+\delta_s)\|_2}{\|u_{ref}(t_{end})\|_2}
$$
Here $u_{ref}$ is the numerical solution of the `reference` case of the pair, $u_{shifted}$ that of the `shifted` case, and $\delta_s=\text{shift\_fraction}\cdot L$ is the shift distance of the `shifted` case. $\delta_s$ is a distance and is not the grid spacing $dx$. The value is emitted as the field `errors.symmetry_l2_rel`.

`convergence_order` is a cross-case reduction over `errors.analytic.l2_rel_tend`, using $p=\log(e_{coarse}/e_{fine})/\log(2)$, and is accumulated only over the target cases of the test that judges it. It is emitted as a per-case field of the finer case of each pair: `advdiff1d_ref_nx128_shift000_dts100` carries `convergence.nx64_to_nx128.l2_order`, and `advdiff1d_ref_nx256_shift000_dts100` carries `convergence.nx128_to_nx256.l2_order`. The cases preceding the one that completes a reduction omit that field.

`mode_gain_error` is emitted as the field `errors.mode_gain`, and `mode_phase_error_rad` as the field `errors.mode_phase_rad`. The $G_{num}, G_{ref}$ used in `mode_gain_error` and `mode_phase_error_rad` are defined as the 1-step amplification rate determined from each case's `dt` and `nx`.

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
  - The CFL judgment is applied. The evaluation expression is `cfl_combined_max`, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-12}$.
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
  - The CFL judgment is applied. The evaluation expression is `cfl_combined_max`, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 3.0e{-12}$.
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
  - The CFL judgment is applied. The evaluation expression is `cfl_combined_max`, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is $\le 1.0e{-12}$.
  - The symmetry judgment is applied. The evaluation expression is `symmetry_l2_rel`, and the threshold is $\le 1.0e{-12}$.
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
  - The CFL judgment is applied. The evaluation expression is `cfl_combined_max`, and the threshold is $\le 1.0$.
  - The mass-conservation judgment is applied. The evaluation expression is `mass_drift_rel`, and the threshold is `informational_only`.
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
