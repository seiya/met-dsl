# Tests: 1 次元線形移流拡散（検証入力・判定条件）

## 0. メタ情報
- `status`: `draft`
- `test_profile_id`: `advdiff1d_linear_baseline`
- `test_profile_version`: `0.1.1`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `advdiff1d_linear`
- `spec_ref.spec_version`: `0.3.0`
- `spec_ref.controlled_spec_path`: `spec/problem/transport/advection_diffusion/advdiff1d_linear/controlled_spec.md`

## 1. 目的
本スイートは、1 次元線形移流拡散方程式の離散実装について、精度・保存性・平行移動同値性・CFL ガードを検証する。判定対象は `L0` から `L3` とし、期待失敗（`xfail`）を含む。

## 2. 入力既定化
### 2-1. 入力インスタンス
- 領域長 `L` は `1.0` とする。
- 初期条件は $u(x,0)=\sin(2\pi x/L)+0.5\sin(4\pi x/L)$ とする。
- 理論解は次式で定義する。  
  $$
  u(x,t)=\exp(-\nu k_1^2 t)\sin(k_1(x-a t))+0.5\exp(-\nu k_2^2 t)\sin(k_2(x-a t))
  $$
- 理論解で用いる記号は $k_1=2\pi/L$、$k_2=4\pi/L$ とする。

### 2-2. 実行制御の既定値
- $t_{start}=0.0$、$t_{end}=0.5$ とする。
- `dt` は次の手順で決定する。
  1. $\text{dt\_raw} = \min(\text{cfl\_adv}\cdot dx/a,\ \text{cfl\_dif}\cdot dx^2/\nu)$
  2. $\text{n\_step} = \lceil t_{end}/\text{dt\_raw}\rceil$
  3. $dt = t_{end}/\text{n\_step}$
- $\text{cfl\_adv}=0.6$、$\text{cfl\_dif}=0.25$ とする。
- 停止条件は $n = \text{n\_step}$ とする。
- 出力時刻は $0.0, 0.1, 0.2, 0.3, 0.4, 0.5$ とする。

## 3. ケース展開規則
### 3-1. family 定義
`family` ごとの sweep と固定値を次に定義する。

| family_id | purpose | sweep | fixed |
| --- | --- | --- | --- |
| `advdiff1d_ref` | refinement_for_accuracy | $nx \in \{64,128,256\}$ | $\text{shift\_fraction}=0.0,\ \text{dt\_scale}=1.0$ |
| `advdiff1d_sym` | translation_symmetry | $\text{nx}=128,\ \text{shift\_fraction}=0.25$ | $\text{dt\_scale}=1.0$ |
| `advdiff1d_guard` | expected_failure_for_cfl | $\text{nx}=64,\ \text{dt\_scale}=1.2$ | $\text{shift\_fraction}=0.0$ |

### 3-2. `case_id` 生成規則
- テンプレートは `advdiff1d_{family}_nx{nx:03d}_shift{shift_pct:03d}_dts{dts_pct:03d}` とする。
- `shift_pct` は $\text{round}(100\cdot\text{shift\_fraction})$ で決定する。
- `dts_pct` は $\text{round}(100\cdot\text{dt\_scale})$ で決定する。
- 展開順序は `family`, `nx`, `shift_fraction`, `dt_scale` の順で固定する。

### 3-3. 明示上書きケース
- `case_id` は `advdiff1d_ref_nx128_shift000_dts100_tend200` を追加する。
- `base_case_id` は `advdiff1d_ref_nx128_shift000_dts100` を参照し、`t_end` は $2.0$ のみ上書きする。

## 4. 診断成果物と契約
### 4-1. 成果物
- 診断出力ファイルは `diagnostics.json` とする。
- 判定出力ファイルは `verdict.json` とする。

### 4-2. 必須診断項目
`diagnostics.json` は次のフィールドを必須とする。
- `cfl.combined_max`
- `cfl.combined_time_series`
- `conserved.mass.initial`
- `conserved.mass.final`
- `errors.analytic.l2_rel_tend`
- `errors.mode_gain`
- `errors.mode_phase_rad`
- `errors.symmetry_l2_rel`

### 4-3. `N/A` 規則
- 診断項目が計算不能な場合は、出力値を `null` とし、`reason_na` を必須とする。
- 閾値の定義源は本ファイルとし、テスト単位の閾値上書きを許可する。

### 4-4. mode 指標の定義
`mode_gain_error` と `mode_phase_error_rad` で用いる $G_{num}, G_{ref}$ は、各ケースの `dt` と `nx` から定まる 1 step 増幅率として定義する。

離散モード番号を $m \in \{1,2\}$ とし、$\theta_m=2\pi m/nx$、$k_m=2\pi m/L$ とする。  
無次元数は $C=a\,dt/dx$、$D=\nu\,dt/dx^2$ とする。  
このとき、1 step 増幅率は次で定義する。
$$
G_{num}(m)=1-C\left(1-e^{-i\theta_m}\right)+D\left(e^{i\theta_m}-2+e^{-i\theta_m}\right)
$$
$$
G_{ref}(m)=\exp\left(\left(-\nu k_m^2-iak_m\right)dt\right)
$$

`mode_gain_error` は次で定義する。
$$
\max_{m\in\{1,2\}}\frac{\left||G_{num}(m)|-|G_{ref}(m)|\right|}{|G_{ref}(m)|}
$$

`mode_phase_error_rad` は次で定義する。
$$
\max_{m\in\{1,2\}} \left| \mathrm{wrapToPi}\left(arg(G_{num}(m)) - arg(G_{ref}(m))\right) \right|
$$
ここで `wrapToPi` は位相差を $[-\pi,\pi]$ へ正規化する演算とする。

`u(t_{end})` のフーリエ係数比、または $G^{n_{step}}$ を用いた累積比較をこの 2 指標の評価式として使用してはならない。

## 5. 既定閾値
- $\text{cfl\_combined\_max} \le 1.0$
- $\text{mass\_drift\_rel} \le 1.0e{-12}$
- `l2_rel_error_to_analytic_tend` は $\text{nx}=64$ で $\le 2.0e{-1}$、$\text{nx}=128$ で $\le 1.1e{-1}$、$\text{nx}=256$ で $\le 6.0e{-2}$ とする。
- $\text{convergence\_order} \ge 0.50$
- $\text{mode\_gain\_error} \le 5.0e{-3}$
- $\text{mode\_phase\_error\_rad} \le 5.0e{-3}$
- $\text{symmetry\_l2\_rel} \le 1.0e{-12}$

## 6. テスト定義
### 6-1. `l1_refinement_against_analytic`
- `level`: `L1`
- `objective`: refinement で誤差が低下し、理論解との一致が許容内であることを確認する。
- 対象ケース:
  - `advdiff1d_ref_nx064_shift000_dts100`
  - `advdiff1d_ref_nx128_shift000_dts100`
  - `advdiff1d_ref_nx256_shift000_dts100`
- `expected_outcome`: `pass`
- 判定条件:
  - CFL 判定は適用する。評価式は $\max_t(C + 2D)$、閾値は $\le 1.0$ とする。
  - 質量保存判定は適用する。評価式は $\frac{|M_{end} - M_0|}{\max(\sum_i |u_i(0)|\cdot dx,\ 1e{-14})}$、閾値は $\le 1.0e{-12}$ とする。
  - 対称性判定は適用しない。理由は「平行移動したペアケースをこのテストでは実行しないため」とする。
  - 理論比較判定は適用する。
    - `l2_rel_error_to_analytic_tend` はケース別閾値を適用する。
      - `advdiff1d_ref_nx064_shift000_dts100` は $\le 2.0e{-1}$
      - `advdiff1d_ref_nx128_shift000_dts100` は $\le 1.1e{-1}$
      - `advdiff1d_ref_nx256_shift000_dts100` は $\le 6.0e{-2}$
    - `convergence_order` は $p=\log(e_{coarse}/e_{fine})/\log(2)$ を用い、`nx64_to_nx128` と `nx128_to_nx256` の双方で $\ge 0.50$ を要求する。
    - `mode_gain_error` は 4-4 で定義した 1 step 増幅率の評価式を用い、$\le 5.0e{-3}$ を要求する。
    - `mode_phase_error_rad` は 4-4 で定義した 1 step 位相誤差の評価式を用い、$\le 5.0e{-3}$ を要求する。

### 6-2. `l2_mass_conservation_long_run`
- `level`: `L2`
- `objective`: 長時間積分での離散質量保存を確認する。
- 対象ケース:
  - `advdiff1d_ref_nx128_shift000_dts100_tend200`
- `expected_outcome`: `pass`
- 判定条件:
  - CFL 判定は適用する。評価式は $\max_t(C + 2D)$、閾値は $\le 1.0$ とする。
  - 質量保存判定は適用する。評価式は $\frac{|M_{end} - M_0|}{\max(\sum_i |u_i(0)|\cdot dx,\ 1e{-14})}$、閾値は $\le 3.0e{-12}$ とする。
  - 対称性判定は適用しない。理由は「本テストは単一初期条件の長時間保存性評価が目的のため」とする。
  - 理論比較判定は適用する。$\text{l2\_rel\_error\_to\_analytic\_tend} \le 2.3e{-1}$ を要求する。

### 6-3. `l3_translation_equivariance`
- `level`: `L3`
- `objective`: 初期条件の平行移動に対して数値解も同じだけ平行移動することを確認する。
- ペアケース:
  - `reference` は `advdiff1d_ref_nx128_shift000_dts100`
  - `shifted` は `advdiff1d_sym_nx128_shift025_dts100`
- `expected_outcome`: `pass`
- 判定条件:
  - CFL 判定は適用する。評価式は $\max_t(C + 2D)$、閾値は $\le 1.0$ とする。
  - 質量保存判定は適用する。評価式は $\frac{|M_{end} - M_0|}{\max(\sum_i |u_i(0)|\cdot dx,\ 1e{-14})}$、閾値は $\le 1.0e{-12}$ とする。
  - 対称性判定は適用する。評価式は $\|u_{shifted}(t_{end}) - shift(u_{ref}(t_{end}), +0.25L)\|_2 / \|u_{ref}(t_{end})\|_2$、閾値は $\le 1.0e{-12}$ とする。
  - 理論比較判定は適用する。$\text{l2\_rel\_error\_to\_analytic\_tend} \le 1.1e{-1}$ を要求する。

### 6-4. `l0_cfl_guard_xfail`
- `level`: `L0`
- `objective`: CFL 違反ケースを検出できることを確認する（期待失敗）。
- 対象ケース:
  - `advdiff1d_guard_nx064_shift000_dts120`
- `expected_outcome`: `xfail`
- `xfail_condition`: `cfl.combined_max > 1.0`
- `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'cfl'`
- 判定条件:
  - CFL 判定は適用する。評価式は $\max_t(C + 2D)$、閾値は $\le 1.0$ とする。
  - 質量保存判定は適用する。評価式は $\frac{|M_{end} - M_0|}{\max(\sum_i |u_i(0)|\cdot dx,\ 1e{-14})}$ とし、閾値は `informational_only` とする。
  - 対称性判定は適用しない。理由は「ガードテストの目的は安定条件違反検知のみ」とする。
  - 理論比較判定は適用しない。理由は「不安定条件では理論一致判定より先に実行継続不可判定を優先する」とする。

## 7. verdict 集約規則
- `per_test.pass_rule`: 適用対象の check がすべて pass の場合に pass とする。
- `per_test.xfail_rule`: `expected_outcome == xfail` かつ `xfail_condition` が真で、`pass_when` を満たす場合に xfail とする。
- `suite.pass_rule`: 全 `test_id` が `pass_rule` または `xfail_rule` を満たす場合に pass とする。

## 8. 出力要件
- `verdict.json` は各 check の `status`、`metric_value`、`threshold`、`applicable=false` の場合の `reason_na` を必須とする。
- `summary.json` は `pass`、`fail`、`xfail`、`skipped` の件数を必須とする。

## 9. トレーサビリティ
- 本文書の `test_profile_id` と `test_profile_version` は `case.resolved.yaml` と `trial_meta.json` に記録しなければならない。
- 本文書の判定条件は `verdict.json` の評価根拠へ写像できなければならない。
