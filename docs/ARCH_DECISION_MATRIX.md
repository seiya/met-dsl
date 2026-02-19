# ターゲットアーキテクチャ決定表

この文書は、CPU参照実装（ゴールデン）とGPUターゲットを意思決定するための比較表である。
参照実装や同値性の定義は `docs/GLOSSARY.md` を参照。

## 結論
- Phase 0-1参照実装: **Python + NumPy**
- Phase 3 GPU入口: **Fortran + OpenACC（nvfortran）**
- 性能が必要な箇所のみ CUDA Fortran 等へ段階的に最適化

## CPU参照実装（ゴールデン）候補
- Python+NumPy: 選択（契約確立と検証を最短化）
- Fortran/C++: 互換実装として追加し、同値性回帰で監視

## GPU候補
- OpenACC（Fortran）: 選択（入口）
- CUDA Fortran: 部分選択（ホットスポット）
- Kokkos/SYCL: 将来HW最適化が必要になった段階で検討
