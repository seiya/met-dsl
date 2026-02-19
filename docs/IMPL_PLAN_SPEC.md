# Implementation Plan（impl.resolved.yaml）: 汎用性と自動チューニングを両立する設計

## 結論
**2層構造（Abstract Knobs + Backend Overrides）** を選択する。

- **abstract**: ハードウェア/言語に依存しにくい“意図”の表現（AIが探索しやすい）
- **backend**: OpenACC/CUDA Fortran/CUDA C++ 等のバックエンド固有パラメタ（実装に落とすため）

この方式により、
- 将来の自動チューニングで探索空間を拡大しても表現が破綻しにくい
- ただし現場の実装に必要な具体パラメタも書ける
- backend追加時も既存のチューニング履歴が無駄になりにくい

## 1. 何を汎用化し、何を汎用化しないか
- 汎用化する: ループ変換の“意図”（タイル、融合、並列粒度、ベクトル化、メモリレイアウトの方針、非同期/重ね合わせの方針）
- 汎用化しない: コンパイラ固有フラグ、GPUアーキ固有の詳細、具体的なpragma/attributeの書き方
 - これらは backend に隔離する

## 2. 最小スキーマ
```yaml
impl_version: 0.1
target: cpu|gpu
objective:
 metric: throughput_cells_per_sec # perf.jsonで計算できるもの
 mode: maximize
constraints:
 max_walltime_sec: 60
 require_physics_pass: true
search_space: # tunerが探索する“可変パラメタ集合”
 abstract:
 tile_i: [32, 64, 128, 256]
 fuse_flux_update: [true, false]
 layout: [SoA, AoS]
 vectorize: [true, false]
 unroll: [1, 2, 4]
 parallel_grain: [cell, tile]
 backend:
 openacc:
 num_gangs: [null, 64, 128]
 vector_length: [null, 32, 64, 128]
 async: [0, 1, 2]
seed: 0
selected: # 1つの試行では、ここが決定される（impl.resolved）
 abstract:
 tile_i: 128
 fuse_flux_update: true
 layout: SoA
 vectorize: true
 unroll: 2
 parallel_grain: tile
 backend:
 openacc:
 num_gangs: 128
 vector_length: 64
 async: 1
provenance:
 generator: llm
 generator_model: any
 notes: ""
```

運用上の区別:
- `search_space`: 探索範囲の宣言（tunerが読む）
- `selected`: 1回の試行で選ばれた設定（simulateが読む）

## 3. 実装への落とし方
- `selected.abstract` は、コード側で“実装パターン”にマッピングする（例: fuse=trueなら fused kernel を呼ぶ、tile_iでループ境界を変える）。
- `selected.backend` は、pragma/launch parameters/compile flagsに反映する。
- もし実装が抽象ノブをまだサポートしない場合は、未サポートはエラーで止める（暗黙の無視は禁止）。

## 4. 互換性と将来拡張
- backendは namespace で追加する（例: `cuda_fortran`, `cuda_cpp`, `hip`, `kokkos`）
- abstractノブは最小に始め、実装と測定が回るものから増やす。
- 追加の際は、`impl_version` を上げ、互換ルールを docs に明記する。

## 5. なぜこの方式がAIチューニングに向くか
- AI/BOが扱いやすい“離散ノブ集合”として表現できる
- backend固有のノブも同じ枠で探索できる
- 実装ターゲットを変えても abstract の履歴が流用できる
