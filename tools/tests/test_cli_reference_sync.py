#!/usr/bin/env python3
"""argparse ↔ docs/CLI_REFERENCE.md{,_RARE} の sync test.

CLI 引数情報の取得方針 (`CLAUDE.md` の「CLI 仕様の確認規約」節) では、頻出
subcommand (Tier-A) を `docs/CLI_REFERENCE.md` で網羅、稀少 subcommand (Tier-B)
を `docs/CLI_REFERENCE_RARE.md` で overview のみ保持する。本テストは:

1. argparse 上の subcommand 集合と Tier-A/Tier-B 分類の網羅性を確認する。
2. Tier-A subcommand では、argparse 引数集合が doc 引数表に**含まれている**ことを
   確認する (doc 側の追加記述は許容、欠落は reject)。
3. Tier-B subcommand では、RARE doc の table に登場することのみ確認する
   (詳細引数は `--help` を canonical とする方針のため diff しない)。

差分が検出された場合、Tier-A / Tier-B のどちらに記載すべきかのレビューを
強制する目的で fail する。
"""

from __future__ import annotations

import argparse
import io
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools.orchestration_runtime import main as orchestration_runtime_main


TIER_A_SUBCOMMANDS: frozenset[str] = frozenset({
    "record-launch",
    "record-child-return",
    "deactivate-child",
    "record-reply",
    "record-agent-run",
    "set-status",
    "mark-dependency-readiness",
    "write-step-result",
    "reserve-phase-root",
    "workflow-launch-check",
    "run-gate",
    "guarded-apply-patch",
})

TIER_B_SUBCOMMANDS: frozenset[str] = frozenset({
    "init",
    "preflight",
    "preflight-status",
    "record-timeout",
    "read-checkpoint",
    "verify-checkpoint-integrity",
    "check-step-completed",
    "orchestration-read",
    "repair-agent-runs",
})

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_TIER_A = REPO_ROOT / "docs" / "CLI_REFERENCE.md"
DOC_TIER_B = REPO_ROOT / "docs" / "CLI_REFERENCE_RARE.md"

ARG_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")


def _argparse_args_for(subcommand: str) -> set[str]:
    """`<sub> --help` を invoke して引数 flag 集合を抽出する。

    `-h` / `--help` は除外する (argparse が自動付与する universal flag)。
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            orchestration_runtime_main([subcommand, "--help"])
        except SystemExit:
            pass
    out = buf.getvalue()
    flags = {m.group(0) for m in ARG_FLAG_RE.finditer(out)}
    flags.discard("--help")
    return flags


def _doc_section_args(doc_path: Path, subcommand: str) -> set[str]:
    """doc 内の `## <subcommand>` section から `--xxx` 引数を抽出する。

    section は次の `## ` heading または EOF まで。表記は markdown table
    `| `--name` | yes | ... |` を想定。
    """
    text = doc_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^## {re.escape(subcommand)}\s*$(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return set()
    section = match.group(1)
    flags = {m.group(0) for m in ARG_FLAG_RE.finditer(section)}
    flags.discard("--help")
    return flags


def _enumerate_argparse_subcommands() -> set[str]:
    """argparse の subparser 登録を monkey-patch で intercept して列挙する。

    `--help` 出力の regex parse は argparse の出力形式変更で fragile になる
    ため、`add_subparsers().add_parser(name, ...)` 呼び出しを capture して
    subcommand 集合を取得する。
    """
    captured: set[str] = set()
    real_add_subparsers = argparse.ArgumentParser.add_subparsers

    def patched_add_subparsers(self: argparse.ArgumentParser, **kwargs):  # type: ignore[no-untyped-def]
        action = real_add_subparsers(self, **kwargs)
        real_add_parser = action.add_parser

        def capturing_add_parser(name: str, **parser_kwargs):  # type: ignore[no-untyped-def]
            captured.add(name)
            return real_add_parser(name, **parser_kwargs)

        action.add_parser = capturing_add_parser  # type: ignore[method-assign]
        return action

    with patch.object(argparse.ArgumentParser, "add_subparsers", patched_add_subparsers):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                orchestration_runtime_main(["--help"])
            except SystemExit:
                pass
    return captured


class CliReferenceSyncTests(unittest.TestCase):
    """argparse と doc の sync を確認する。"""

    def test_tier_classification_covers_all_argparse_subcommands(self) -> None:
        """argparse の全 subcommand が Tier-A / Tier-B のいずれかに分類されている。"""
        argparse_subs = _enumerate_argparse_subcommands()
        self.assertTrue(
            argparse_subs,
            "argparse subcommand 集合を抽出できなかった (parser 形式変更の可能性)",
        )
        classified = TIER_A_SUBCOMMANDS | TIER_B_SUBCOMMANDS
        unclassified = argparse_subs - classified
        stale = classified - argparse_subs
        self.assertFalse(
            unclassified,
            f"未分類の subcommand: {sorted(unclassified)}。"
            f"docs/CLI_REFERENCE.md (Tier-A) または docs/CLI_REFERENCE_RARE.md (Tier-B) "
            f"へ追加し、本 test の TIER_A_SUBCOMMANDS / TIER_B_SUBCOMMANDS にも反映すること。",
        )
        self.assertFalse(
            stale,
            f"argparse 側で削除された subcommand: {sorted(stale)}。"
            f"本 test の TIER_A_SUBCOMMANDS / TIER_B_SUBCOMMANDS と doc から除去すること。",
        )

    def test_tier_a_doc_covers_all_argparse_flags(self) -> None:
        """Tier-A subcommand の argparse 引数が doc 引数表に欠落なく登場する。

        doc 側の追加記述 (派生 field, JSON payload, etc) は許容する。
        argparse 側にあって doc に無い flag を欠落として fail させる。
        """
        missing: dict[str, set[str]] = {}
        for sub in sorted(TIER_A_SUBCOMMANDS):
            cli_flags = _argparse_args_for(sub)
            doc_flags = _doc_section_args(DOC_TIER_A, sub)
            absent = cli_flags - doc_flags
            if absent:
                missing[sub] = absent
        self.assertFalse(
            missing,
            "Tier-A doc に欠落している argparse 引数: "
            + ", ".join(f"{sub}: {sorted(flags)}" for sub, flags in missing.items())
            + "。docs/CLI_REFERENCE.md の該当 section に追記すること。",
        )

    def test_tier_b_doc_lists_all_rare_subcommands(self) -> None:
        """Tier-B subcommand が RARE doc の overview table に登場する。

        詳細引数は `--help` を canonical とする方針のため、doc table に名前が
        登場するかのみ check する。
        """
        text = DOC_TIER_B.read_text(encoding="utf-8")
        missing = [sub for sub in sorted(TIER_B_SUBCOMMANDS) if f"`{sub}`" not in text]
        self.assertFalse(
            missing,
            f"docs/CLI_REFERENCE_RARE.md に未掲載の Tier-B subcommand: {missing}。"
            f"overview table に追加すること。",
        )

    def test_tier_b_subcommands_absent_from_tier_a_doc(self) -> None:
        """Tier-B subcommand が Tier-A doc に section を持たない (圧縮の維持)。"""
        text = DOC_TIER_A.read_text(encoding="utf-8")
        present = [
            sub
            for sub in sorted(TIER_B_SUBCOMMANDS)
            if re.search(rf"^## {re.escape(sub)}\s*$", text, re.MULTILINE)
        ]
        self.assertFalse(
            present,
            f"Tier-B 扱いだが docs/CLI_REFERENCE.md に section が残存: {present}。"
            f"section を削除して overview のみ Tier-B doc に置くこと。",
        )


if __name__ == "__main__":
    unittest.main()
