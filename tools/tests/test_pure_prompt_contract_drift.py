"""Drift guard for the pure prompt contract (TODO Item 5).

The pure leaf is doc-blind: its behavioral contract is whatever the launch templates + the fixed
ABI constants say, and a change to any of them is a behavior change that MUST be observable through
`PURE_PROMPT_CONTRACT_VERSION` (so `bundle_meta.json` / the launch record stamp the new contract).
Nothing else enforces that coupling — a one-character template edit with no version bump, or a bump
with a stale template, both ship silently.

This test pins a sha256 over the COUPLED contract tuple against the CURRENT version, keeping every
historical pin. The assertions catch three drift directions:
  * an edit to any pinned surface WITHOUT bumping the version -> digest mismatch;
  * a version bump whose PINNED entry was not recomputed -> KeyError / digest mismatch;
  * an EMPTY version bump, or a silent REVERT to a prior contract (a new version whose contract
    tuple equals an earlier version's, so its digest matches) -> duplicate-digest failure. This
    enforces the no-empty-version-bump policy and reverse-drift detection: a genuine, non-reverting
    bump changes the contract tuple and therefore the digest, so every pinned version's digest must
    be unique. History runs from `pure-6` (the pre-guard baseline, seeded below) forward; pins are
    frozen literals (NOT recomputed from the current tuple, which has since moved) and exist only to
    reject a later version that duplicates one of them.

The pin set is deliberately NARROW (a churn magnet if widened): the three template files, the
fixed `PURE_SYSTEM_PROMPT` (the `--system-prompt` string, a documented version-bump trigger in
`pure_leaf.py`), the cold-repair static-paragraph prefix list, and the checks-ABI constants the
templates distill verbatim (`CHECKS_PUBLIC_NAMES` and the two character widths). Every member is a
STABLE, behavior-defining input (not a churny one); do NOT grow it beyond that bar.

Every pinned member is either a production constant IMPORTED from its authority
(`CHECKS_PUBLIC_NAMES`, the status width, the prefixes, `PURE_SYSTEM_PROMPT`) or the template file
bytes themselves — never a test-local COPY of a production value, which would drift silently from its
source and pin nothing. In particular the checks-status vocabulary (`'pass'`/`'fail'`/`'na  '`) is
NOT pinned as a separate literal: it has no production enum constant (it lives only as prose in the
templates), and the template prose is already covered by hashing the template bytes above, so a copy
here would be redundant and self-referential.

DELIBERATELY OUT OF SCOPE — host-side acceptance-gate / backstop IMPLEMENTATIONS (e.g.
`validate_pipeline_semantics._validate_diagnostics_contract_output`, the post_execute backstop for
the diagnostics contract that the producer prompt's clause (A) is written against).
`PURE_PROMPT_CONTRACT_VERSION` tracks the pure leaf's INPUT contract (the prompt templates,
`PURE_SYSTEM_PROMPT`, the transport request shape — see `pure_leaf.py`), NOT host-side checks that
run AFTER the leaf returns. Hashing such a gate's source into this tuple would (a) force a spurious
version bump on every transparent gate change — a refactor, a comment, or a false-positive FIX —
making the very churn magnet this pin set is scoped to avoid, and (b) miscategorize a host-side
change as a leaf-input-contract change. The gate's behavior is instead guarded by its own
behavioral tests, and a gate change is captured for A/B comparability by the run's recorded repo
revision (`preflight` / `orchestration_meta.json#invocation`), not by this version. The prose the
gate and prompt share IS pinned — as the template bytes.
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

import tools.codegen_bundle as cb
import tools.orchestration_runtime as ort
import tools.runner_renderer as rr
from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION, PURE_SYSTEM_PROMPT

_TEMPLATE_FILES = (
    "pure_generate_generate.txt",
    "pure_generate_verify.txt",
    "pure_bundle_repair.txt",
)

# sha256 of the canonical serialization of the coupled tuple, keyed by contract version. When an
# INTENTIONAL contract change bumps `PURE_PROMPT_CONTRACT_VERSION`, KEEP the existing entries and
# ADD the new (version -> digest) entry with the value this test prints on failure. A bump with no
# matching entry, an edit with no bump, or a new version whose digest duplicates an earlier one (an
# empty bump OR a silent revert to a prior contract) all fail here. Never delete or edit a
# historical entry — they backstop those checks.
#
# `pure-6` is the pre-guard BASELINE: it predates this guard (the guard was introduced at pure-7), so
# it never had a live pin; its digest is the pure-6 template bytes (origin/main) hashed under the
# pure-6/pure-7 tuple schema. Only pure_generate_generate.txt differs between pure-6 and pure-7;
# every other pinned member is identical across the two.
#
# NOTE — the contract-tuple SCHEMA changed at pure-8: `check_id_width` was dropped from
# `_contract_tuple` when the runner-driven per-id checks ABI removed the pinned check-id width
# (`runner_renderer.CHECK_ID_WIDTH` no longer exists). The pure-6 / pure-7 digests below are FROZEN
# literals computed under the OLD (wider) schema; they are NOT recomputed from the current tuple and
# serve ONLY the uniqueness / no-empty-bump check (`test_no_empty_version_bump`). Because they were
# hashed under a schema the current `_digest()` no longer produces, they can never collide with a
# current-schema digest — so they do NOT detect a future REVERT of the template to the pre-pure-8
# (pure-6/pure-7) contract. Revert detection holds only among versions sharing the CURRENT schema
# (pure-8 onward); the empty-bump guard across all pinned versions is unaffected. Only the CURRENT
# version's pin (pure-11, below) is a live equality target for `_digest()`.
PINNED: dict[str, str] = {
    "pure-6": "b614072bcaad7ffe61f48d54256305b89982457d2ef6c3b5126e09598e5e7067",
    "pure-7": "14c7db85579eeb5f0dd21af2a7321edfcc9bcd647bcb735f511e0d3f80aa2eda",
    "pure-8": "1b1a9575930504226c6d6acebf7cf3ee4b64247e4146f978ee84bbe505b1e4c2",
    "pure-9": "273f38bdbf82569ed5f7ebb7a4ce9896c6b386297f1e25ccbd74923b4f38c70a",
    "pure-10": "ba2da518724e26df35bae96bd69a462f96bfb9509b8785b6355ebf51e7e8cc4b",
    "pure-11": "cec79c570b3de442677ab90d18f2064bf32e8e113d9200b862dbf6a89254b8f8",
}


def _contract_tuple() -> dict[str, object]:
    tpl_dir = Path(ort.__file__).resolve().parent / "prompt_templates"
    return {
        "templates": {
            name: (tpl_dir / name).read_text(encoding="utf-8") for name in _TEMPLATE_FILES
        },
        "system_prompt": PURE_SYSTEM_PROMPT,
        "repair_static_prefixes": list(ort.PURE_REPAIR_STATIC_PARAGRAPH_PREFIXES),
        "checks_public_names": list(rr.CHECKS_PUBLIC_NAMES),
        "check_status_width": rr.CHECK_STATUS_WIDTH,
    }


def _digest() -> str:
    payload = json.dumps(_contract_tuple(), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PurePromptContractDriftTests(unittest.TestCase):
    def test_current_version_is_pinned_and_matches(self) -> None:
        computed = _digest()
        resolution = (
            f"\n\nRecomputed digest: {computed}\n"
            "Resolve ONE of two ways:\n"
            f"  (1) INTENTIONAL contract change: bump PURE_PROMPT_CONTRACT_VERSION (tools/pure_leaf.py), "
            "KEEP the existing PINNED entries, and add PINNED['<new-version>'] = '" + computed
            + "' here (its digest must differ from every existing pin — else it is an empty bump).\n"
            "  (2) UNINTENTIONAL drift: revert the edit to the pinned surface "
            "(the three pure_*.txt templates, PURE_REPAIR_STATIC_PARAGRAPH_PREFIXES, or the "
            "runner_renderer checks-ABI constants)."
        )
        self.assertIn(
            PURE_PROMPT_CONTRACT_VERSION, PINNED,
            f"PURE_PROMPT_CONTRACT_VERSION={PURE_PROMPT_CONTRACT_VERSION!r} has no PINNED entry."
            + resolution,
        )
        self.assertEqual(
            computed, PINNED[PURE_PROMPT_CONTRACT_VERSION],
            f"pure prompt contract digest for {PURE_PROMPT_CONTRACT_VERSION!r} does not match the pin."
            + resolution,
        )

    def test_no_empty_version_bump(self) -> None:
        # Every pinned version's digest must be UNIQUE. A genuine contract bump changes the tuple and
        # therefore the digest; a new version whose digest equals an earlier one is an EMPTY version
        # bump (unchanged contract), which the no-empty-version-bump policy rejects. Without this,
        # bumping the version literal and copying the failure message's recomputed (unchanged) digest
        # into a new PINNED entry would pass — the reverse-drift hole.
        by_digest: dict[str, list[str]] = {}
        for version, digest in PINNED.items():
            by_digest.setdefault(digest, []).append(version)
        collisions = {d: vs for d, vs in by_digest.items() if len(vs) > 1}
        self.assertEqual(
            collisions, {},
            "empty version bump detected — these versions share an identical contract digest, so "
            f"their contract tuple is unchanged: {collisions}. A version bump must change the "
            "contract (templates / PURE_SYSTEM_PROMPT / coupled ABI constants); do not add a new "
            "version whose digest duplicates an earlier one.",
        )


class TemplateGateParityTests(unittest.TestCase):
    """The `pure_generate_generate.txt` sentences S1-S3 distil constants that live in
    `codegen_bundle.py`. If a constant moves and the prompt does not, the leaf is told to
    emit a value the gate no longer accepts — the exact E2E#7 failure mode. These assert the
    template's distilled surface still agrees with the gate's live constants.

    The pin members are IMPORTED production constants and the template file bytes only
    (per the drift-guard scoping above); this class adds no test-local copy of a gate value.
    """

    @staticmethod
    def _generate_template_bytes() -> str:
        tpl_dir = Path(ort.__file__).resolve().parent / "prompt_templates"
        return (tpl_dir / "pure_generate_generate.txt").read_text(encoding="utf-8")

    def test_template_names_every_state_residency(self) -> None:
        template = self._generate_template_bytes()
        for residency in cb.STATE_RESIDENCIES:
            self.assertIn(
                residency, template,
                f"state_residency {residency!r} (cb.STATE_RESIDENCIES) is not named in "
                "pure_generate_generate.txt — S2 has drifted from the gate enum.")

    def test_template_capability_tokens_are_all_manifest_provided(self) -> None:
        # Every `<name>@<version>` token the prompt shows as an example must be one the
        # harness manifests actually provide; otherwise the prompt points the leaf at a
        # capability the gate rejects as unavailable.
        template = self._generate_template_bytes()
        provided = set().union(*cb.HARNESS_CAPABILITY_MANIFESTS.values())
        tokens = set(re.findall(r"[a-z][a-z0-9_]*@[0-9]+", template))
        self.assertEqual(
            tokens - provided, set(),
            "pure_generate_generate.txt names capability tokens the harness manifests do not "
            f"provide: {sorted(tokens - provided)} (provided: {sorted(provided)}).")


if __name__ == "__main__":
    unittest.main()
