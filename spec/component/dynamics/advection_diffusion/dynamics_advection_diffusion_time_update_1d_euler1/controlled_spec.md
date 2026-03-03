# Controlled Spec: 1 次元 前進 Euler 更新（component spec）

## 0. メタ情報
- `spec_id`: `dynamics_advection_diffusion_time_update_1d_euler1`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `advection_diffusion`

## 1. 責務と適用範囲
本 `component` は 1 次元 移流拡散 問題の時間更新を実行する責務を持つ。

## 2. 入出力契約
入力は `u^n(i)`、`a`、`nu`、`dx`、`dt`、境界適用済みの近傍セル値とする。出力は `u^{n+1}(i)` とする。

## 3. 演算定義
公開 `operation` は `dynamics_advection_diffusion_time_update_1d_euler1__advance` とする。更新式は
$$
u_i^{n+1}
= u_i^n
- C\left(u_i^n-u_{i-1}^n\right)
+ D\left(u_{i+1}^n-2u_i^n+u_{i-1}^n\right)
$$
$$
C=a\frac{\Delta t}{\Delta x},\quad D=\nu\frac{\Delta t}{\Delta x^2}
$$
とする。

## 4. 失敗条件と制約
`dx<=0`、`dt<=0` を入力不正としてエラーとする。

## 5. 公開 API と互換性
公開 `operation_id` は `dynamics_advection_diffusion_time_update_1d_euler1__advance` のみとする。

## 6. 禁止事項
時間積分法の自動切替を禁止する。

## 7. トレーサビリティ
`component_catalog.yaml` と `case.resolved.yaml` に採用結果を必須記録とする。

## 8. AD 準備情報
`ad_readiness.enabled` は `true` とする。非微分演算として `ceil`（`dt` 規則に用いる場合）を明示する。

## 9. tests 参照
対応する `tests.md` を同一ディレクトリに配置し、`test_profile_version` を `0.1.0` とする。
