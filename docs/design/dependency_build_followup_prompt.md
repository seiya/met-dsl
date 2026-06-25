# Starter prompt — dependency-build (Model B) implementation + deferred hardening

> **STATUS (superseded — read first):** the Model B implementation described below has
> **shipped** (conductor authors `src/Makefile` host-side for every make∧fortran node — leaf
> OR dependency; `_build_inproc` stages each closure `<dep>_model.f90` into `$(OBJDIR)`;
> authorization integration + contract docs done; unit tests green). A verification 2-node
> spec (`spec/component/demo/dep_chain/{demo_dep_base,demo_dep_top}`) is also authored. The
> ONLY outstanding item is the **billed E2E run**
> (`run_workflow.py spec/component/demo/dep_chain/demo_dep_top validate --llm claude --with-deps`
> to `meta=pass` + `aggregate_verdict=pass`). The "前提（完了済み）" / main-task wording below
> predates that and is kept only for historical context — defer to the
> "Part 2 — dependency nodes" / "D1 (PRIMARY)" status in `docs/design/deterministic_followups.md`.

Paste the block below into a fresh session (in `/home/seiya/work/met-dsl`) to resume this
work. It is self-contained; the canonical record is the "Known limitations & deferred work"
section of `docs/design/deterministic_followups.md`.

---

```
met-dsl リポジトリ（/home/seiya/work/met-dsl）で、決定論 Makefile / transport・resume 修正の
「積み残し」を対応してください。今回の主題は **依存ノードのビルド（Model B）の実装と検証**です。
背景と既存アンカーは docs/design/deterministic_followups.md の
「Known limitations & deferred work (recorded 2026-06-25)」節が正典です。まず読んでください。

## 前提（完了済み・触らない）
- leaf ノードの Makefile は conductor が決定論オーサリング済み（_write_makefile / _conductor_authors_makefile）。
- バイナリ名は <spec_id>_runner に固定済み（BIN override）。
- transport 失敗→clean fail_closed＋orphan tombstone（add_superseded_run_ids）済み。
- A/C1/C2/B1/B2 フォローアップ済み。テストは `python3 -m pytest tools/tests/ -q -p no:randomly` で全 green。

## 主タスク D1：依存ノードのビルド（推奨 Model B＝OBJDIR への一時 staging）
現状、依存ノードのビルドは未実装かつ契約矛盾（phase_02 §41 はソース取り込み禁止だが staging 機構が無い）。
未配線の足場は既にある：Conductor._dependency_closure、_write_makefile の非 leaf 分岐
（deepest-first の $(OBJDIR)/<dep>_model.o ルール＋DEP_OBJS）、_build_inproc の staging TODO コメント。
実装すること：
1. _build_inproc（および必要なら _execute_inproc）で、依存クロージャの各 <dep>_model.f90 を
   各依存の lineage.json→source_id から解決し、ビルド用 tmp（$(OBJDIR)）へ staging（canonical src/ には触れない）。
2. _write_makefile の非 leaf 分岐を run_phase に配線（現在 leaf 限定ゲート）。
   付随して build_launch_request / phase_required_outputs / orchestration_runtime の
   makefile 認可（_conductor_authors_makefile / _resolved_makefile_host_authored）を依存ノードにも整合。
3. 契約の整合：phase_02_generate.md §41 のカルブアウト（一時 OBJDIR staging は canonical-tree コピーではない）と
   phase_03 の dependency_violation を明文化。doc サイズ ceiling（tools/tests/test_orchestration_runtime.py
   ChildContextDocSizeTests）を超えないこと。
4. **検証**：実依存 spec が repo に無いため E2E 不能。**最小の依存 spec（component が別 component を use する 2 ノード）を
   作成**し、`python3 tools/run_workflow.py <spec_ref> validate --llm claude --with-deps` で
   meta=pass + aggregate_verdict=pass を真値として確認（課金・長時間、ユーザ確認の上で）。
   spec を作れない場合は synthetic IR の統合テストで closure 解決・staging パス・生成 Makefile 構造を担保。

## 副タスク（latent、機会があれば）
- L1: 生成 Makefile の `$(OBJDIR) $(BINDIR):` ルールが OBJDIR==BINDIR=="." 時に
  `target '.' given more than once` 警告。2 ルールに分割すれば解消（無害なので任意）。
- L2: C1 scalar ゲートは per-snapshot time index=scalar 前提。ベクトル時間 spec 対応時に carve-out。
- L3: C2 escalation 閾値 2 のチューニング。
- L5: judge セッション制限後の自動 resume/スケジューリング（現状は手動 --resume）。
- T1: 実 conductor→runtime CLI→completion を通す end-to-end fault-injection 統合テストの追加。

## 規約
- compile/run/quality は MCP server 経由（AGENTS.md）。直接 gcc 等は禁止。
- main 直コミット禁止。コミット/プッシュはユーザが明示した時のみ。
- 変更後は pytest 全 green を維持し、サブエージェント or /codex:review で指摘が無くなるまで回す。
- 完了後 docs/design/deterministic_followups.md の該当節を done に更新。
```
