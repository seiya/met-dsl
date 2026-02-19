# 性能診断（perf.json）仕様案

目的:
- 物理妥当性テストと同じ“枠組み”で、性能測定・性能回帰検知を扱えるようにする。
- CPU/GPUや最適化変換の比較を、記録と可視化が可能な形で残す。

## 1. 収集単位
- 1ケース実行（case_id, target, impl設定）ごとに `perf.json` を1つ出力する。
- refinementやsweepの各サブケースも同様に出力する。

## 2. 最小フィールド（必須）
- `case_id`: 文字列
- `target`: cpu|gpu|...
- `walltime_sec`: 実行全体の壁時計時間（秒）
- `steps`: 実行ステップ数
- `cells_updated`: 更新したセル数の総数（nx*ny*nz*steps 等）
- `throughput_cells_per_sec`: cells_updated / walltime_sec
- `timestamp_utc`: ISO8601（任意でもよいが推奨）

## 3. 推奨フィールド（可能なら）
- `kernel_breakdown`: 主要カーネルごとの時間（秒）と比率
- `memory_bytes_read/write`: 推定でもよい
- `device`: GPU名、SM数等
- `compiler`: コンパイラ/バージョン、主要フラグ
- `impl_hash`: impl.resolved.yaml のhash
- `git_sha`: 実行したコードのコミット

## 4. 測定上の注意
- ウォームアップ（GPU）やキャッシュ効果があるので、可能なら複数回実行して統計（平均/分散）も記録する。
- ただしPhase 0では複雑にしない。まずは walltime と throughput を確実に取る。

## 5. 性能テストの位置づけ
- 性能評価は「物理テスト合格」が前提。
- L3に performance regression を追加できる。
 - 例: throughput が基準より 10% 以上低下したら fail（ただしノイズを考慮し、統計的に扱うのが望ましい）。

## 6. runnerとの連携
- runnerは `perf.json` を読み、性能チェックを `verdict.json` に追記できる。
- 物理fail時は性能評価をスキップする（意味が薄い）。
