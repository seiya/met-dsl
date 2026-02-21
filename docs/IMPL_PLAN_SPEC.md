# Implementation Plan（impl.resolved.yaml）: 汎用性と自動チューニングを両立する設計

## 設計方針
実装 Plan は **2 層構造（Abstract Knobs + Backend Overrides）** を採用する。

- **abstract**: ハードウェア/言語に依存しにくい“意図”の表現（自動探索しやすい）
- **backend**: OpenACC/CUDA Fortran/CUDA C++ 等のバックエンド固有パラメタ（実装に落とすため）

この構造により、次を満たす。
- 将来の自動チューニングで探索空間を拡大しても表現が破綻しにくい
- 実装に必要な具体パラメタを明示できる
- backend 追加時も既存のチューニング履歴が無駄になりにくい

## 1. 何を汎用化し、何を汎用化しないか
- 汎用化する: ループ変換の“意図”（タイル、融合、並列粒度、ベクトル化、メモリレイアウトの方針、非同期/重ね合わせの方針）
- 汎用化しない: コンパイラ固有フラグ、GPU アーキ固有の詳細、具体的な pragma/attribute の書き方
- これらは `backend_overrides` に隔離する

## 2. 必須項目（impl.resolved.yaml）
`impl.resolved.yaml` は次を必須とする。

- `target.class`（cpu/gpu など）
- `target.backend`（例: `cpu_fortran_reference`,`cuda_fortran`）
- `target.architecture`（例: `x86_64`,`aarch64`,`nvidia_sm80`）
- `toolchain.language`（例: `fortran`,`cpp`,`cuda_fortran`）
- `toolchain.standard`（例: `2008`,`c++17`）
- `toolchain.build_system`（例: `make`,`cmake`,`meson`,`ninja`）
- `abstract`（言語非依存ノブ）
- `backend_overrides`（言語/バックエンド依存ノブ）
- `selected.backend_key`

ルール:
- **プログラミング言語は 1-2（実装 Plan）で必ず固定する。**
- **ターゲットアーキテクチャは 1-2（実装 Plan）で必ず固定する。**
- `toolchain.language` は Plan 生成時に固定する。ユーザーからプログラミング言語の明示指定がない場合、`target.class=cpu` では `fortran`、`target.class=gpu` では `cuda_fortran` を必ず採用する。
- `toolchain.language` の既定値からの逸脱は、ユーザーがプログラミング言語を明示指定した場合にのみ許可する。
- `target.class` が `cpu` / `gpu` 以外の場合、`toolchain.language` の既定値補完を禁止する。
- `impl.resolved.yaml` で `toolchain.language` / `toolchain.standard` / `toolchain.build_system` が未定義の場合、生成工程へ進めずエラーとする。
- `target.architecture` が未定義の場合、生成工程へ進めずエラーとする。
- `toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`toolchain.build_system` は `make` / `cmake` / `meson` / `ninja` のいずれかとする。既定値は `make` とする。

## 3. 任意項目（環境依存）
- `toolchain.compiler` / `toolchain.linker` は**任意**とする。
- compiler 種別・バージョンを固定したい場合（CI 再現性重視）のみ記載する。
- 固定しない場合は、実行環境の既定コンパイラを使う。
- 直接 `gcc` / `clang` / `gfortran` を呼び出して単発ビルドする運用を禁止し、必ず `toolchain.build_system` を介してビルドする。

## 4. 生成物の構成ルール（言語共通）
- 生成コードは言語に依らず、`model`（物理計算）と `runner`（入出力・判定連携）を分離する。
- `runner` は `model` を `call` / `use` / `import` で呼び出す。
- 物理更新ロジックを `runner` 側に重複実装してはならない。
