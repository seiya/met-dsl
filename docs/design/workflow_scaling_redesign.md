# Workflow scaling redesign roadmap

Status: **proposed** (no item below is implemented). Recorded 2026-07-06 from a workflow review against the measured cost baseline. This document is the canonical record of the redesign direction; per-item detailed designs are authored separately when an item is picked up.

## Purpose

Define the design direction for scaling the spec-to-code workflow from the current demonstration specs to the final target, under the premises in the next section. The goals are (a) generated code that satisfies the spec intent, and (b) minimum wall-clock time and token cost per certified node.

## Premises (operator-stated, 2026-07-06)

These premises override any implicit assumption in the current implementation:

1. **The existing specs are examples only.** Future specs add increasingly complex logic; the final target is a full atmospheric (weather) model. The certified node graph will grow to hundreds or thousands of nodes.
2. **No human-written implementation code.** Humans author only `controlled_spec.md` / `tests.md` / `deps.yaml`. Humans do not write Fortran (or any target-language) code, and do not provide reference implementations.
3. **Per-hardware code variants.** The workflow generates different code, in the programming language optimal for each hardware target, from the same spec. The set of (language, hardware) targets is open-ended.

## Measured cost baseline (2026-07)

Reference runs: `orch_20260702T105419Z_c5b81af9` (62.2 min, pass) and `orch_20260703T065033Z_b1847b13` (43.2 min, pass). Analysis method: `skills/workflow-timing-audit/SKILL.md`.

- 96-97% of node wall time is LLM leaf execution; the deterministic in-process substeps (G1-G7 of `deterministic_followups.md`) total 3-4%.
- 89-90% of LLM output tokens are extended thinking; wall time ≈ output tokens / ~100 tok/s.
- `generate.generate` is the dominant leaf (up to 1379 s per run; retries multiply it — one run executed it 4 times).
- Every cold leaf force-reads ~35-50 KB of contract documents; document bodies are passed as paths, not inlined, so no prompt prefix is shared across leaves.
- Generated source is ~90% runner boilerplate by line count (measured example: model 66 lines, runner 644-650 lines). The majority of `post_generate` gate rules, retry failure classes, and SKILL/contract document text constrain the runner plumbing, not the physics.

## Diagnosis

The deterministic-gate hoist (G1-G7) has exhausted its lever: the remaining cost is the LLM thinking itself. That thinking is dominated by per-node regeneration of cross-node-identical plumbing (runner argv parsing, case loop, JSON/snapshot/perf emission) under a large per-language trap-rule contract. Under premise 3, per-language trap-rule documents multiply per target; under premise 1, per-node fixed costs (cold document reads, sequential execution, judge nondeterminism) multiply per node. The construction "LLM writes the plumbing, gates and documents correct it" is the cost driver and does not scale.

## Design direction

Target construction: **the workflow self-produces its plumbing, verification oracles, and exemplars; the LLM authors only the semantic content (physics kernel and per-test check logic).**

### R1. Harness as a generated infrastructure node

Define the runner plumbing (argv/case-set parsing, case loop driver, JSON/snapshot/perf/metrics_basis emission) as one **infrastructure spec node per (language, hardware) target** (e.g. `harness__fortran__cpu`). The workflow itself generates, validates, and certifies each harness node once; humans author only its spec (consistent with premise 2). Every physics node depends on the harness node of its target through the existing dependency machinery (published operations, `<dependency_facts>` injection, `--with-deps`, certified-source selection).

Consequences:
- The physics-node generation scope shrinks to the model kernel plus a per-test checks callback; glue code between kernel and harness is host-rendered from the IR.
- Per-language plumbing rules (JSON numeric descriptors, snapshot naming, perf field set, make-test conventions) are stated once in the harness node's spec and removed from the physics-node SKILL/contract documents; the corresponding `post_generate` gate rules and retry failure classes become structurally impossible and are deleted.
- Adding a (language, hardware) target = authoring and certifying one harness spec node.

This generalizes the established host-authored `src/Makefile` decision (correct-by-construction) from "authored by conductor code" to "produced as a certified workflow artifact".

### R2. Deterministic per-test verdict

Formalize each test's pass rule (`tests.md` `pass_when` / `judgment`) into a machine-evaluable predicate in `spec.ir.yaml` during Compile. `validate.execute` computes `verdict.json#per_test` deterministically from `diagnostics.json` and the predicates at the end of its in-process run (implementation note: moved from the originally-proposed `validate.post_judge` to `execute` so a predicate failure short-circuits the judge spawn entirely — see `deterministic_followups.md` G8); the judge leaf authors `semantic_review.json` only. This removes the judge-nondeterminism failure class (`xfail_verdict_contract_gap`) and the per-test schema-fabrication class, and is a precondition for scale: probabilistic judge variance multiplied by thousands of nodes is not acceptable. Continues the G6/G7 trajectory.

### R3. Oracle-free verification stack

Under premises 1-2, expected-output tests authored by humans do not scale to complex physics. Extend the `tests.md` / IR test contract with test kinds whose oracles are machine-derived:

1. `property` — conservation (mass/energy/momentum), positivity, symmetry, boundedness invariants stated in the spec and checked deterministically.
2. `mms` — method of manufactured solutions; the manufactured solution and forcing terms are produced during Compile (LLM or symbolic tooling), giving discretization-order verification without a human oracle.
3. `convergence` — grid-refinement error-rate checks against the scheme's declared order.
4. `cross_target` — differential testing between the variants of the same spec on different (language, hardware) targets; each variant is the reference for the others within stated tolerances. This structurally replaces the absent human reference implementation once R4 exists.
5. `regression` — numerical comparison against the previously certified version of the same node on spec revision.

Verdict evaluation of all kinds flows through R2. This extension must land **before** the complex-spec influx: retrofitting the test contract across a large node corpus is expensive.

### R4. Hardware-neutral IR and target matrix

Split the IR into a hardware-neutral semantic layer (`case` / `algorithm` / `io_contract` / `dependency`) and a target profile (language, hardware, parallelization policy; currently mixed in via `impl_defaults.toolchain`). Compile runs once per spec; Generate/Build/Validate run per target. Pipelines, certified artifacts, exemplar retrieval (R5), and harness nodes (R1) are all keyed by `node_key × target`. `validate.execute` needs a target-aware execution dispatch (the MCP `run_program` backend selects the execution environment per target).

### R5. Self-grown exemplar corpus

Premise 2 forbids human-prepared references, not machine-certified precedent. The conductor injects, per `(family, spec_kind, language, target)`, a previously certified model+checks source as a rendered exemplar block in the `generate.generate` launch prompt (same mechanism as `<dependency_facts>`; certified-source selector already exists). Exemplars trade cheap, cacheable input tokens for expensive thinking tokens and raise first-attempt pass rate; the corpus grows with every certified node (self-bootstrapping).

### R6. Scaling substrate

Required for premise 1 node counts; each item is independent:

- **Incremental recertification** — content-hash-based invalidation over (spec, IR, dependency closure, harness version, target profile); unchanged nodes are never re-run (generalization of the existing ready-skip). A one-component change re-runs only its dependency-affected closure.
- **DAG-parallel execution** — revise the sequential-execution invariant (`WORKFLOW_CORE.md`) to permit concurrent execution of same-topo-level independent nodes; the workspace-global baseline contamination constraint requires per-node isolation of shared state.
- **Prompt prefix caching** — inline the contract-document bodies into the launch prompt in a fixed byte-stable order (host-rendered) instead of path-list must-reads, so leaves share a cacheable prefix and spend no turns on document reads. Effective after R1 shrinks the documents.
- **Per-persona model tiering** — verification-side leaves (verify / judge) run on a smaller model or lower effort than `generate.generate`; the deterministic backstops (build, execute, R2, R3) bound the accuracy risk. Adoption requires an A/B measurement showing no certified-outcome regression (correctness-reducing cost cuts are rejected).

## Sequencing

1. R2 and the R1 harness-node spec design first, on the current small node set. The harness boundary API (kernel/checks vs plumbing) is specified language-neutrally in anticipation of R4.
2. R3 test-contract extension before complex specs are added.
3. R4 when the second (language, hardware) target is introduced; enables `cross_target` at the same time.
4. R6 items land in parallel with 1-3.

## Decision Criteria

- A proposal item is adopted only with a billed E2E demonstration on a real node reaching `aggregate_verdict=pass`.
- Cost claims are verified with the timing-audit method (`skills/workflow-timing-audit/SKILL.md`); the per-node targets after R1+R2+R5 are ≤15 min wall and ≤150k output tokens (baseline: 43-62 min, 360-540k).
- Any change that lowers certified-outcome accuracy is rejected regardless of cost savings.
