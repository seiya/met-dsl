# CLI Reference (稀少 subcommand overview)

## このドキュメントの位置づけ

`tools/orchestration_runtime.py` のうち **使用頻度が低い** 稀少 subcommand の overview を置く。詳細な引数仕様は `python3 tools/orchestration_runtime.py <sub> --help` を canonical source とする。

頻出 subcommand (Tier-A) の詳細仕様は [docs/CLI_REFERENCE.md](CLI_REFERENCE.md) を参照する。tool / subcommand 別の情報取得方針は `CLAUDE.md` の「CLI 仕様の確認規約」節を canonical source とする。

関連 canonical source:
- 頻出 subcommand 詳細: [docs/CLI_REFERENCE.md](CLI_REFERENCE.md)
- workflow 全体の起動契約: `skills/workflow-orchestration/SKILL.md` および `skills/workflow-orchestration/references/startup_contract.md`
- 例外復旧手順: [docs/RUNBOOK.md](RUNBOOK.md)

## 共通規約

- `--repo-root` / `--orchestration-id` は (ほぼ) 全 subcommand で **required**。
- ISO 8601 timestamp は UTC (`Z` suffix) を canonical とする。
- 詳細な引数 (required / optional / default 値) は `<sub> --help` で確認する。

## 稀少 subcommand 一覧

| subcommand | 用途 | 主な caller / 状況 |
|---|---|---|
| `init` | orchestration 開始 / `orchestration_meta.json` 生成 | 通常は `tools/run_workflow.py` 経由で起動。直接呼び出すのは例外運用のみ |
| `preflight` | execution platform 起動可否 probe / `preflight.json` 生成 | `tools/run_workflow.py` が内部で呼ぶ。手動呼び出しは禁止 (`AGENTS.md` 参照) |
| `preflight-status` | 既存 `preflight.json` を読み返す | 起動後の状態確認 |
| `record-timeout` | `Agent` tool の API stream idle timeout 等の canonical 復旧経路 | child agent が wedge した例外復旧フロー。`--force-reason` は marker check bypass の最終手段 |
| `read-checkpoint` | `workspace/orchestrations/<orch>/orchestration_checkpoint.json` を取得 | `resume_enabled=true` の orchestration で resume 判定時 |
| `verify-checkpoint-integrity` | checkpoint に記録された artifact hash と現状を照合 | resume 開始時の整合性確認。`stale` 検出時はその step を skip してはならない |
| `check-step-completed` | `resume_enabled=true` で対象 step の完了状態を確認 | canonical な skip 判定経路。`step_result.json` の直接参照で skip 判断してはならない |
| `orchestration-read` | manifest 外 path の gate-mediated read | 通常は `run-gate --gate orchestration_read --args-json '{"read_path": "..."}'` 経由で呼ぶ |

## 引数取得経路

各 subcommand の required / optional 引数および返値 schema は次のコマンドで確認する。

```bash
python3 tools/orchestration_runtime.py <subcommand> --help
```

argparse の出力には description / 全引数の help 文字列が含まれ、本 doc を補完する形で詳細を提供する。`--help` 呼び出し自体は `forbid_tools_direct_read` の対象外であり、`tools/hooks/common.py` の `cli_help_invocation_observed` audit ポリシーで使用頻度が記録される (block されない)。

## 例外復旧フローへの link

- `record-timeout` の `--force-reason` 使用条件: `docs/RUNBOOK.md#substep-timeout-recovery`
- `verify-checkpoint-integrity` で `stale` 検出時の対応: `docs/RUNBOOK.md` 該当節
- `check-step-completed` を含む resume フロー全体: `skills/workflow-orchestration/SKILL.md` 運用ルール 19
