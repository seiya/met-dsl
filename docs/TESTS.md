# Requirements and format of tests (canonical source)

## Purpose
`tests.md` is the canonical source for the verification input and judgment conditions of a `spec`. It is commonly used for all `spec_kind` of `problem` / `component` / `profile` / `infrastructure`.
The evaluation result of `tests.md` is mapped to the relevant `node`'s `self_verdict` in `verdict.json`, and the aggregated judgment including dependencies is handled in `aggregate_verdict.json`.

## Scope
- `spec/problem/<domain>/<family>/<spec_id>/tests.md`
- `spec/component/<domain>/<family>/<spec_id>/tests.md`
- `spec/profile/<domain>/<family>/<spec_id>/tests.md`
- `spec/infrastructure/<domain>/<family>/<spec_id>/tests.md`

## Requirements
1. The canonical source format is `Markdown`.
2. State `test_profile_id`, `test_profile_version`, `status`, and `spec_ref` at the top of the document as required.
3. `spec_ref` requires `spec_kind`, `spec_id`, `spec_version`, and `controlled_spec_path`.
4. Each `spec` defines at least 1 `L0` test.
5. Define `L1` / `L2` / `L3` according to the verification purpose. They are not forbidden by `spec_kind`.
6. No fixed lower bound on the number of tests is set. Sufficiency is judged by the requirement-coverage rule.
7. Treat an undefined item as an error without completion.
8. The judgment condition must be evaluable per `node_key`. It must not implicitly reference the state of a dependency `node`.

## Coverage rules per `spec_kind`
- `problem`
  - Define execution control, case expansion, judgment expressions, and pass/fail aggregation rules.
  - When there is a non-applicable item in the validity judgment, define `N/A` and `reason_na`.

- `component`
  - For each published `operation`, define at least one normal case and one guard case (`fail` or `xfail`) each.
  - Add `L1`-and-above accuracy / conservation / equivalence tests as needed.

- `profile`
  - Define the judgment of the selection-establishment condition, exclusion condition, and fallback-prohibition condition.
  - Define guard-case tests for input outside the compatibility range.

- `infrastructure` (R1 harness)
  - For each published harness operation, define at least one normal case and one guard case (`fail` / `xfail`) each — e.g. numeric round-trip (negative / min / max), boolean-literal emission, case fan-out → per-case snapshot naming, a missing-`--cases` guard (`xfail`), and per-test index completeness.
  - The harness's own runner (a self-test driver) exercises these; the existing `post_execute` gate group provides additional oracles.

## Description format
0. Meta information
1. Test purpose
2. Input-defaulting rules
3. Execution-control rules
4. Case-expansion rules
5. Diagnostics contract
6. Test definitions
7. Pass/fail aggregation rules
8. Traceability

When there is an unnecessary section depending on the `spec_kind`, state `N/A` and the reason rather than omitting it.

## Operations Rules
- When a `Controlled Spec` change affects the judgment conditions, update `tests.md` in the same change.
- For `xfail`, define `xfail_condition` and `pass_when` simultaneously.
- When changing a threshold, state the affected `test_id`.
- Judge pass/fail including dependencies not in `tests.md` but in `dependency.resolved.yaml` and `aggregate_verdict.json`.

## Decision Criteria
- The test input and pass/fail judgment can be restored from the document alone.
- The correspondence between `spec_ref` and `controlled_spec.md` is unique.
- An `L0` test exists.
- The requirement-coverage rule is satisfied.
- The judgment result can be reproduced as a per-`node_key` `self_verdict`.
