# Controlled Spec: 2 次元 shallow water equation（物理・アルゴリズム定義）

## 0. メタ情報
- `spec_id`: `shallow_water2d`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`

## 1. 問題定義
対象は 2 次元 shallow water equation の保存形
$$
\frac{\partial h}{\partial t}
+ \frac{\partial (hu)}{\partial x}
+ \frac{\partial (hv)}{\partial y}
= 0
$$
$$
\frac{\partial (hu)}{\partial t}
+ \frac{\partial }{\partial x}\left(hu^2 + \frac{1}{2} g h^2\right)
+ \frac{\partial (huv)}{\partial y}
= 0
$$
$$
\frac{\partial (hv)}{\partial t}
+ \frac{\partial (huv)}{\partial x}
+ \frac{\partial }{\partial y}\left(hv^2 + \frac{1}{2} g h^2\right)
= 0
$$
である。未知変数は水深 `h` と運動量 `hu`,`hv` とする。底面地形は平坦（`z_b=0`）で固定し、外力項（Coriolis、摩擦、降水・蒸発、地形勾配項）は持たない。

## 2. 変数と座標の定義
座標系は 2 次元 Cartesian 座標とし、座標名は `x`,`y`、単位は `m` とする。状態変数は次とする。
- `h`: 水深、セル中心配置、単位 `m`
- `hu`: `x` 方向運動量、セル中心配置、単位 `m2/s`
- `hv`: `y` 方向運動量、セル中心配置、単位 `m2/s`

導出変数は `u=hu/h`、`v=hv/h`、`c=\sqrt{gh}` とする。`h<=0` は物理的に不正であり許可しない。

## 3. 領域・格子・境界条件の型定義
領域は直交周期領域 $[0,L_x)\times[0,L_y)$ とする。格子は一様セル中心有限体積格子を使い、格子幅は $dx=L_x/nx$、$dy=L_y/ny$ とする。`L_x`,`L_y`,`nx`,`ny` は実行時入力（runtime inputs）であり、検証では `physical_tests` が既定プロファイルを与える。

境界条件アルゴリズムは全境界で周期境界とし、ghost 幅は各方向 1 とする。境界写像は `x` 方向で $(i=-1)\mapsto(i=nx-1)$、$(i=nx)\mapsto(i=0)$、`y` 方向で $(j=-1)\mapsto(j=ny-1)$、$(j=ny)\mapsto(j=0)$ とする。

## 4. 物理アルゴリズム（A）
空間離散化は有限体積法とし、界面フラックスは local Lax-Friedrichs（Rusanov flux）で固定する。再構築は一次（piecewise constant）で固定し、limiter を使用しない。時間積分は 2 段 `SSPRK2` で固定する。

保存変数ベクトルを $U=[h,hu,hv]^T$ とし、更新式を
$$
U_{i,j}^{n+1}
= U_{i,j}^{n}
- \frac{\Delta t}{\Delta x}\left(F_{i+1/2,j}^{*}-F_{i-1/2,j}^{*}\right)
- \frac{\Delta t}{\Delta y}\left(G_{i,j+1/2}^{*}-G_{i,j-1/2}^{*}\right)
$$
で定義する。物理フラックスは
$$
F(U)=
\begin{bmatrix}
hu\\
\frac{(hu)^2}{h}+\frac{1}{2}gh^2\\
\frac{hu\,hv}{h}
\end{bmatrix},
\quad
G(U)=
\begin{bmatrix}
hv\\
\frac{hu\,hv}{h}\\
\frac{(hv)^2}{h}+\frac{1}{2}gh^2
\end{bmatrix}
$$
とする。Rusanov flux は
$$
F^{*}(U_L,U_R)=\frac{1}{2}\left(F(U_L)+F(U_R)\right)-\frac{1}{2}a_x\left(U_R-U_L\right)
$$
$$
a_x=\max\left(|u_L|+c_L,\ |u_R|+c_R\right),\quad c=\sqrt{gh}
$$
$$
G^{*}(U_B,U_T)=\frac{1}{2}\left(G(U_B)+G(U_T)\right)-\frac{1}{2}a_y\left(U_T-U_B\right)
$$
$$
a_y=\max\left(|v_B|+c_B,\ |v_T|+c_T\right)
$$
で定義する。

安定指標は
$$
\mathrm{cfl}=\Delta t\cdot\max_{i,j}\left(\frac{|u_{i,j}|+c_{i,j}}{\Delta x}+\frac{|v_{i,j}|+c_{i,j}}{\Delta y}\right)
$$
とする。閾値と運用ルールは検証プロファイルごとに定義する。

## 5. モデルパラメタと実行時入力契約
この Spec で固定する物理定数は重力加速度 `g=9.81 m/s2` とする。底面高度は `z_b=0` で固定する。

実行時入力（runtime inputs）は次とする。
- `L_x`, `L_y`, `nx`, `ny`（領域サイズと格子分割）
- `initial_condition`（`h`,`hu`,`hv` の初期場）
- `t_start`, `t_end`
- `dt_rule`（`dt` 直接指定または `cfl` 由来規則）
- `output_schedule`（保存時刻）

入力契約として、初期状態で全セル `h>0` を必須とする。検出時は必ずエラー終了する。未定義パラメタの暗黙補完を禁止する。

この入力契約を満たす限り、ユーザーは科学目的に応じて実行時入力を設計できる。`physical_tests` は妥当性検証で使用する既定入力プロファイルを提供する。

## 6. 禁止事項
非周期境界の使用を禁止する。底面地形の導入を禁止する。外力項（Coriolis、摩擦、降水・蒸発、地形勾配）の追加を禁止する。実行時の離散化自動切替（空間離散化・時間積分法の切替）を禁止する。`h` のクリップや limiter/filter の追加を禁止する。未定義パラメタ検出時は必ずエラー終了し、暗黙の既定値を適用してはならない。

## 7. トレーサビリティ
参照根拠は LeVeque（2002）と Toro（2009）を一次資料とする。`case.resolved.yaml` には、少なくとも `physics.equation`, `physics.discretization`, `physics.boundary`, `inputs_contract` が本 Spec から一意に復元できる形で写像されなければならない。

実行メタデータの必須項目は `spec_id`, `spec_version`, `case_id`, `case_hash`, `impl_hash`, `git_sha` とする。

妥当性検証で使用する既定の入力・判定条件は `spec/dynamics/shallow_water/shallow_water2d/physical_tests.md` を参照する。

## 8. AD 準備情報
`ad_readiness.enabled` は `true` とする。状態更新は $U_{next}=F(U_{now},params,forcing)$ の形で表現できることを要件とする。演算子は「`x` 方向フラックス」「`y` 方向フラックス」「周期境界処理」「時間更新」に分離する。

非滑らか・離散演算として `abs`、`max`、周期インデックス wrap、`ceil`（`dt` 規則でステップ数決定に使用される場合）を明示する。`ceil` は制御フロー整数演算として扱い、勾配対象外とする。識別可能パラメタは `g`（`m/s2`）であり、この Spec では固定値である。
