# 性能診断（perf.json）仕様

## 目的
- 物理妥当性テストと同じ“枠組み”で、性能測定・性能回帰検知を扱えるようにする。
- CPU/GPU や最適化変換の比較を、記録と可視化が可能な形で残す。

## 1. 収集単位
- 1 ケース実行（case_id,target,impl 設定）ごとに `perf.json` を 1 つ出力する。
- refinement や sweep の各サブケースも同様に出力する。

## 2. 最小フィールド（必須）
- `case_id`: 文字列
- `target`: cpu|gpu|...
- `walltime_sec`: 実行全体の壁時計時間（秒）
- `steps`: 実行ステップ数
- `cells_updated`: 更新したセル数の総数（nx*ny*nz*steps 等）
- `throughput_cells_per_sec`: cells_updated / walltime_sec
- `parallelism`: 並列度情報（必須オブジェクト）
  - `mpi_ranks`: MPI ランク数（非 MPI 時は 1）
  - `threads_per_rank`: 1 ランクあたりスレッド数（単一スレッド時は 1）
  - `gpu_devices`: 使用 GPU デバイス数（CPU のみは 0）
  - `parallel_degree_total`: 総並列度。定義は `mpi_ranks * threads_per_rank * max(gpu_devices,1)`
- `timestamp_utc`: ISO8601（任意でもよいが推奨）

## 3. 推奨フィールド（可能なら）
- `kernel_breakdown`: 主要カーネルごとの時間（秒）と比率
- `memory_bytes_read/write`: 推定でもよい
- `device`: GPU 名、SM 数等
- `compiler`: コンパイラ/バージョン、主要フラグ
- `impl_hash`: impl.resolved.yaml の hash
- `git_sha`: 実行したコードのコミット

## 4. 測定上の注意
- ウォームアップ（GPU）やキャッシュ効果があるので、可能なら複数回実行して統計（平均/分散）も記録する。
- Phase 0 では walltime と throughput の取得を必須とし、測定項目を過度に拡張しない。

## 5. 性能テストの位置づけ
- 性能評価は「物理テスト合格」が前提。
- L3 に performance regression を追加できる。
- 例: throughput が基準より 10% 以上低下したら fail（ただしノイズを考慮し、統計的に扱うのが望ましい）。

## 6. runner との連携
- runner は `perf.json` を読み、性能チェックを `verdict.json` に追記できる。
- 物理 fail 時は性能評価をスキップする（意味が薄い）。
- `parallelism` が欠落している `perf.json` は不正入力として扱い、判定不能（error）にする。
- `perf.json` は UTF-8 の単一 `JSON object` として標準 parser で復元可能でなければならない。
- 数値 token は `RFC 8259` に従い、先頭 0 を欠落させた `.123` と `-.123` を禁止する。
- `toolchain.language=fortran` の `runner` は、`F0.d` 書式を `JSON` 数値 token へ直接埋め込んではならない。