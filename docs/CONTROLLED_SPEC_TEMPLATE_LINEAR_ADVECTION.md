# Controlled Spec テンプレ: 線形移流（1D, 周期）

このテンプレは「自然言語を最大限使いつつ、曖昧さが残らない」ように、文章と構造化ブロックで仕様を固定するための最小形である。

目的:
- Specを入力として、少なくとも cases の骨格と plan（case.resolved）を決定的に生成できるようにする。
- LLMを使う場合でも、欠落補完は許さず、欠落はエラーで止める。
- 物理アルゴリズム（精度・安定に影響）はここで固定し、実行アルゴリズム（性能のみ）は impl.resolved で扱う。

関連:
- 用語とArtifacts: `docs/GLOSSARY.md`
- impl（性能ノブ）: `docs/IMPL_PLAN_SPEC.md`

---

## 1. 問題設定（自然言語）
対象は線形移流方程式である。

- PDE: ∂t q + ∂x (u q) = 0
- 速度 u は空間・時間に依らない定数とする
- 領域は [x0, x1) の周期境界とする
- 目的は数値解 q を時刻 t_end まで進め、診断量（物理妥当性と精度）を出力する

ここで、q はセル中心に定義する。

---

## 2. 決定事項（構造化ブロック）
以下のYAMLブロックが「曖昧さを残さないための決定事項」である。

```yaml
spec_version: 0.1

pde:
  name: linear_advection
  conservative_form: true           # ∂t q + ∂x (u q) = 0

variables:
  - name: q
    role: prognostic
    location: cell_center

velocity_field:
  type: constant
  value: 1.0                        # u

domain:
  dim: 1
  x0: 0.0
  x1: 1.0
  grid: uniform
  nx: 128
  bc: periodic
  nghost: 2

numerics:
  spatial:
    scheme: donor_cell_upwind       # 一次風上（保存型フラックス）
  time:
    integrator: rk2_heun            # Heun's method（RK2）

run_control:
  time_spec:
    mode: cfl_and_t_end
    cfl: 0.5
    t_end: 0.5
  output:
    save_interval_steps: 0          # 0=最終のみ（Phase 0の最小）

diagnostics:
  required:
    - nan_inf
    - cfl_max
    - mass_conservation
    - bounds
    - tv
  analytic_solution:
    enabled: true
    method: periodic_shift          # 周期シフト（解析解）

tests:
  levels: [L0, L1, L2, L3]
  refinement:
    nx_list: [64, 128, 256]
    require_monotone_improve: true  # 誤差が単調改善することを要求
```

---

## 3. 数値定義（曖昧性排除ルール）

### 3.1 有限体積更新（保存型フラックス）
セル i の更新は以下で定義する。

- セル中心: x_i = x0 + (i+0.5) dx
- dx = (x1-x0)/nx
- 更新:
  - q_i^{n+1} = q_i^n - (dt/dx) * (F_{i+1/2} - F_{i-1/2})

### 3.2 donor-cell upwind フラックス
u が定数のとき、フラックスは

- F_{i+1/2} = u * q_up
- q_up は以下で定義する:
  - u > 0 のとき q_up = q_i
  - u < 0 のとき q_up = q_{i+1}
  - u = 0 のとき F_{i+1/2} = 0

### 3.3 時間積分（Heun, RK2）
L(q) = -(F_{i+1/2}-F_{i-1/2})/dx とすると

- q* = q^n + dt * L(q^n)
- q^{n+1} = q^n + (dt/2) * (L(q^n) + L(q*))

### 3.4 CFL と dt
- dt = cfl * dx / max(|u|)
- `mode: cfl_and_t_end` の場合:
  - dt は上式で固定
  - nsteps は t_end を超えない最大の整数（runnerまたはsimulateが決める）
  - 実際に到達した時刻 `t_actual = nsteps*dt` を diagnostics に記録するのが望ましい

### 3.5 周期境界とghost
- ghost 幅は nghost=2
- 周期境界は ghost セルを反対側の値で埋めることで実装する
- schemeが必要とする stencil に不足がある場合はエラーとする（暗黙に幅を増やさない）

---

## 4. 診断（diagnostics）定義の最小要件

### 4.1 必須診断
- `nan_inf`: NaN/Inf がないこと
- `cfl_max`: 実際の max(CFL)（dt|u|/dx）
- `mass_conservation`: 総量 M = Σ q_i dx の変化（相対/絶対）
- `bounds`: min/max(q) と、初期の範囲からの逸脱量（overshoot/undershoot）
- `tv`: 全変動 TV = Σ |q_{i+1}-q_i|（周期）

### 4.2 解析解比較（有効な場合）
- periodic_shift: q_exact(x, t) = q0(x - u t) を周期で折り返したもの
- 誤差指標（例）:
  - L1: (Σ |q - q_exact| dx)
  - L2: sqrt(Σ (q - q_exact)^2 dx)
  - Linf: max |q - q_exact|

（どのノルムを使うかは cases 側で明示し、runnerが verdict を出す）

---

## 5. Spec から Case への決定的変換（最小）

### 5.1 生成されるケースの骨格
以下は「Specを元に最低限生成できる」ケースの例である。

```yaml
case_id: advect_1d_sine_shift
level: L1
model:
  pde: linear_advection
  domain: {x0: 0.0, x1: 1.0, nx: 128, bc: periodic, nghost: 2}
  velocity: {type: constant, value: 1.0}
  numerics: {spatial: donor_cell_upwind, time: rk2_heun}
run:
  cfl: 0.5
  t_end: 0.5
initial_condition:
  type: sine
  amplitude: 1.0
  mean: 0.0
  wavenumber: 1
checks:
  analytic_error:
    enabled: true
    norm: L2
    atol: 1.0e-2
  mass_conservation:
    rtol: 1.0e-12
```

### 5.2 refinement ケース
`tests.refinement.nx_list` がある場合、同一設定で nx を変えた派生ケース群を生成する。
- 例: nx=64,128,256 の3ケース
- `require_monotone_improve=true` の場合、誤差が格子細分化で単調に減少することを要求する

---

## 6. 禁止事項（再現性と安全性のため）
- Specにないパラメタを LLM が勝手に追加すること（欠落補完は禁止）
- Specにない物理アルゴリズムを暗黙に変更すること
- 未サポートのノブ（abstract/backend）を無視して走らせること

---

## 7. 将来拡張の指針
- 高次スキーム（WENO等）を追加する場合は、フラックス定義・stencil・制約を明示する
- 多次元化では、次元分割/多次元フラックスの定義を追加する
- 物理過程では「正解が曖昧」なため、診断を性質テスト中心に設計する
