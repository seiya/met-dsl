# ロードマップ

この文書は、何をどの順でやれば全体が前に進むかを単独で示す。
Artifacts/L0-L3は `docs/GLOSSARY.md` を参照。

## 直近（1-2週間）
1. Phase 0完走（CPU）
 - 期待失敗（cfl_guard）をPASS扱いにする
 - L0→L3が安定PASS
2. 契約固定
 - diagnostics/verdict/summary互換性確認

## 次（数週間）
3. Controlled Spec 1枚運用
 - Spec→cases骨格生成の最小機能
4. 浅水に向けた診断セット設計

## 中期
5. Fortran互換実装 + 同値性回帰
6. CPU CI導入

## 長期
7. OpenACCでGPU入口 → 同値性回帰
8. 自動縮約・自動テスト追加
