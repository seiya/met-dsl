#!/usr/bin/env python3
"""Sync test for argparse ↔ docs/CLI_REFERENCE.md{,_RARE}.

In the CLI argument information-acquisition policy (the "Information-acquisition
policy" section of `docs/CLI_REFERENCE.md`), the frequent subcommands (Tier-A) are covered in
`docs/CLI_REFERENCE.md`, and the rare subcommands (Tier-B) are kept as an
overview only in `docs/CLI_REFERENCE_RARE.md`. This test:

1. Confirms the completeness of the argparse subcommand set and the Tier-A/Tier-B classification.
2. For a Tier-A subcommand, confirms that the argparse argument set is **included** in the
   doc argument table (additional descriptions on the doc side are allowed, an omission is rejected).
3. For a Tier-B subcommand, confirms only that it appears in the RARE doc's table
   (the detailed arguments are not diffed because the policy is `--help` as canonical).

When a diff is detected, it fails for the purpose of forcing a review of whether it
should be documented in Tier-A or Tier-B.
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
    "finalize-child",
    "set-status",
    "mark-dependency-readiness",
    "write-step-result",
    "reserve-phase-root",
    "workflow-launch-check",
    "run-gate",
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
    "repair-step-result-executor",
    "reopen-phase",
    "dismiss-violation",
})

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_TIER_A = REPO_ROOT / "docs" / "CLI_REFERENCE.md"
DOC_TIER_B = REPO_ROOT / "docs" / "CLI_REFERENCE_RARE.md"

ARG_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")


def _argparse_args_for(subcommand: str) -> set[str]:
    """Invoke `<sub> --help` and extract the argument flag set.

    `-h` / `--help` are excluded (the universal flag argparse auto-adds).
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
    """Extract `--xxx` arguments from the `## <subcommand>` section in the doc.

    The section runs until the next `## ` heading or EOF. The notation assumes a
    markdown table `| `--name` | yes | ... |`.
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
    """Enumerate the argparse subparser registrations by intercepting them with a monkey-patch.

    Because a regex parse of the `--help` output becomes fragile against changes
    in the argparse output format, capture the `add_subparsers().add_parser(name, ...)`
    calls to obtain the subcommand set.
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
    """Confirm the sync of argparse and the doc."""

    def test_tier_classification_covers_all_argparse_subcommands(self) -> None:
        """Every argparse subcommand is classified as either Tier-A or Tier-B."""
        argparse_subs = _enumerate_argparse_subcommands()
        self.assertTrue(
            argparse_subs,
            "could not extract the argparse subcommand set (possible parser format change)",
        )
        classified = TIER_A_SUBCOMMANDS | TIER_B_SUBCOMMANDS
        unclassified = argparse_subs - classified
        stale = classified - argparse_subs
        self.assertFalse(
            unclassified,
            f"unclassified subcommand: {sorted(unclassified)}. "
            f"Add it to docs/CLI_REFERENCE.md (Tier-A) or docs/CLI_REFERENCE_RARE.md (Tier-B) "
            f"and also reflect it in this test's TIER_A_SUBCOMMANDS / TIER_B_SUBCOMMANDS.",
        )
        self.assertFalse(
            stale,
            f"subcommand removed on the argparse side: {sorted(stale)}. "
            f"Remove it from this test's TIER_A_SUBCOMMANDS / TIER_B_SUBCOMMANDS and the doc.",
        )

    def test_tier_a_doc_covers_all_argparse_flags(self) -> None:
        """The argparse arguments of a Tier-A subcommand appear in the doc argument table without omission.

        Additional descriptions on the doc side (derived fields, JSON payload, etc.) are allowed.
        A flag present on the argparse side but absent in the doc is failed as an omission.
        """
        # Cross-cutting flags documented once in the "Common conventions" section
        # rather than repeated in every per-subcommand section. `--verbose`
        # toggles off the default terse stdout projection and applies uniformly
        # to all bookkeeping subcommands (see CLI_REFERENCE.md Common conventions).
        global_doc_flags = {"--verbose"}
        missing: dict[str, set[str]] = {}
        for sub in sorted(TIER_A_SUBCOMMANDS):
            cli_flags = _argparse_args_for(sub)
            doc_flags = _doc_section_args(DOC_TIER_A, sub)
            absent = cli_flags - doc_flags - global_doc_flags
            if absent:
                missing[sub] = absent
        self.assertFalse(
            missing,
            "argparse arguments missing from the Tier-A doc: "
            + ", ".join(f"{sub}: {sorted(flags)}" for sub, flags in missing.items())
            + ". Add them to the relevant section of docs/CLI_REFERENCE.md.",
        )

    def test_tier_b_doc_lists_all_rare_subcommands(self) -> None:
        """A Tier-B subcommand appears in the overview table of the RARE doc.

        Because the policy is `--help` as canonical for the detailed arguments, only
        whether the name appears in the doc table is checked.
        """
        text = DOC_TIER_B.read_text(encoding="utf-8")
        missing = [sub for sub in sorted(TIER_B_SUBCOMMANDS) if f"`{sub}`" not in text]
        self.assertFalse(
            missing,
            f"Tier-B subcommand not listed in docs/CLI_REFERENCE_RARE.md: {missing}. "
            f"Add it to the overview table.",
        )

    def test_tier_b_subcommands_absent_from_tier_a_doc(self) -> None:
        """A Tier-B subcommand has no section in the Tier-A doc (maintaining compression)."""
        text = DOC_TIER_A.read_text(encoding="utf-8")
        present = [
            sub
            for sub in sorted(TIER_B_SUBCOMMANDS)
            if re.search(rf"^## {re.escape(sub)}\s*$", text, re.MULTILINE)
        ]
        self.assertFalse(
            present,
            f"treated as Tier-B but a section remains in docs/CLI_REFERENCE.md: {present}. "
            f"Remove the section and keep only the overview in the Tier-B doc.",
        )


if __name__ == "__main__":
    unittest.main()
