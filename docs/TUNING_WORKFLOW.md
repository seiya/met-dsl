# 自動チューニング運用ワークフロー（任意フロー）

## 位置づけ
`Tune` は core workflow（`Spec → Compile → Generate → Build → Validate`）から分離した **任意フロー** として扱う。core workflow は `spec.ir.yaml` の `impl_defaults` を固定値として使用するが、`Tune` は `spec.ir.yaml` を不変前提として `impl_defaults` の variant を探索する。

## 基本方針
tuner は generator から分離し、次のループを標準運用とする。

1. 構造 IR 固定（`spec.ir.yaml`、core workflow で合格済み）
2. 実装裁量 variant 探索（`tuning.spec` で `impl_defaults` の **knob レイヤのみ**上書きを定義）
3. 生成/ビルド/実行/評価の自動化（core workflow と同等の Generate / Build / Validate を variant ごとに実行）
4. 物理合格を制約とした性能目的関数の最適化
5. 最良候補の固定と回帰監視への移行

## `impl_defaults` の上書き可能境界（必読）
`Tune` が `tuning.spec` で上書き可能な範囲は **`impl_defaults` の knob レイヤに限定**する。canonical な fixed / knob 境界は `docs/workflow/phases/phase_01_compile.md` の「impl_defaults の fixed / knob 境界」節を参照する。要点:

| 越境禁止 (fixed) | 上書き可能 (knob) |
|---|---|
| `target.class` / `target.backend` / `target.architecture` | `abstract.*`（並列化粒度・レイアウト・融合・タイル等の意図） |
| `toolchain.language` / `toolchain.standard` / `toolchain.build_system` | `backend_overrides.<key>.*`（スレッド数・block size・ベクトル幅等の backend 固有値） |
| `selected.backend_key` | |

`tuning.spec` が fixed sub-key を上書きする entry を含む場合、`Tune` 起動時に **fail_closed で停止する**ものとし、`Tune` 内部で variant 生成してはならない。これにより、Tune が `spec.ir.yaml` の構造を破壊しないことを保証する。

新しいハードウェア/コンパイラ向けに fixed レイヤを変更する場合は、core workflow から `Compile` をやり直して新しい `ir_id` を発行する。これは Tune の責務ではない。

設計要点:
- 物理保証（A 固定）と性能探索（B 探索）を明確に分離できる（IR レベルでの分離）
- 生成モデルを交換しても tuner のロジックが変わらない
- 失敗時の原因局所化がしやすい（物理 fail vs 性能ノイズ vs 実装未対応）

## 1. ループの構成（実務的な最小形）
### Inputs
- `spec.ir.yaml`（不変、core workflow で確定済み）
- `tuning.spec`（探索範囲 search_space を定義する Tune 専用入力）
- コードテンプレ（実装パターン群）

### Per-trial Outputs
- variant 用の `spec.ir.yaml`（`impl_defaults` を `tuning.spec` で上書きしたコピー）
- `<stage>_meta.json`（`LLM` 利用ステージ内検証の結果）
- `diagnostics.json`（物理）
- `perf.json`（性能）
- `verdict.json`（物理合否 + 可能なら性能判定）
- `trial_meta.json`（ビルドログ、環境、乱数、git sha）

### Loop
- 候補生成（LLM 支援/BO/ ルール）
- （必要なら）generator でコード差分生成
- `LLM` を利用する候補生成・コード生成は `SPEC.md` の「LLM の扱い」を適用する
- 標準運用は `debug_mode=false` とし、失敗試行 artifact を保存しない。調査時のみ `debug_mode=true` を許可する
- 推奨: **コードの構造はテンプレで固定し、impl ノブで分岐**。 LLM は新しい実装パターン追加時のみ使う。
- ビルド（ターゲット別）
- quick physics gate（L0-L2 のサブセット）
- perf 測定（複数回・統計）
- モデル更新（次候補を提案）

## 2. 候補生成方式
段階戦略を選択する。

### Stage A: ルールベース（最初に必須）
- 安全なノブのみ（tile,fuse,vectorize,layout）
- 探索点数を絞る（例: 20-50）
- 目的: “明確に速くなる”領域を見つける

### Stage B: ベイズ最適化（推奨）
- 離散ノブでも扱える BO（TPE 等）を想定
- ノイズを考慮し、同一点を再測定する

### Stage C: LLM 支援（必要時のみ）
- 新しい実装パターンの追加（例: fused kernel を新設、async halo を追加）
- “ノブの追加”を提案し、search_space を拡張する
- 注意: LLM は探索の主役ではなく、探索空間の設計支援に使う

## 3. 物理合格ゲート
- 物理 fail の候補は性能評価しない（評価コスト削減）
- 許容は物理的妥当性一致（bitwise 不要）
- quick→full の 2 段階を選択
- quick: 小さい nx/ 短い t_end（ノイズと時間のバランス）
- full: 本番に近いケース

## 4. 性能測定の扱い
- `perf.json` は最小でも `walltime_sec`、`throughput_cells_per_sec`、`parallelism` を必須
- GPU はウォームアップを入れ、複数回測定して平均/分散を保存
- baseline と比較する performance regression を L3 に追加可能

## 5. キャッシュと再利用
- `case_hash` と `impl_hash` で結果をキャッシュし、同一試行を再実行しない
- ビルド artifact も hash で再利用する（可能なら）

## 6. いつ"決め打ち"するか
- チューニングで得た best impl を `spec.ir.yaml.impl_defaults` の上書き variant として固定する。
- 固定後は回帰（物理 + 性能）へ移行する。
- 新アーキ/新コンパイラのときだけ再チューニングする
- 採用 variant は任意フロー `Promote` で `releases/` へ昇格する。
