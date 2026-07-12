# Zero-base reference architecture and adoption decision

Status: **proposed** (reference architecture). Recorded 2026-07-12 from a first-principles redesign study against the stated final goal. Parts of this architecture are already realized by the current implementation and are identified in place (R2 verdicts, R3-core test kinds, R5 exemplar injection, R1/M3c harness structure with the A6 amendment); the Z-series migration items are not implemented. This document defines the target architecture the implementation converges to, compares it with the current implementation, and records the adoption decision (evolve in place / reimplement). It extends, and does not replace, `workflow_scaling_redesign.md` (R1-R6); overlaps are cross-referenced.

## Purpose

Define the architecture this framework would have if designed today from first principles, without carrying over the existing implementation, under the operator premises below, and use it as (a) the yardstick for judging whether the current implementation is structurally sound, and (b) the target state for subsequent migration items.

## Premises (operator-stated)

Identical to `workflow_scaling_redesign.md`:

1. Specs scale to a full atmospheric (weather) model: hundreds to thousands of certified nodes.
2. Humans author only `controlled_spec.md` / `tests.md` / `deps.yaml`. No human-written implementation code and no human reference implementations.
3. Per-hardware code variants: the same spec produces different code in the language optimal for each hardware target; the (language, hardware) target set is open-ended.

Additional standing constraints:

4. LLM output is non-reproducible; final quality is guaranteed at the exit by execution-based judgment (per `SPEC.md` invariant principles). The exact scope of this guarantee — certification against the declared evidence contract, with named accepted residuals — is defined in A4.
5. Certification runs are billed; cost per certified artifact (wall clock, tokens, operator attention) is a first-class design objective.

## Problem statement

Given a spec node $S$ (controlled spec, verification set, dependency declarations) and a target profile $P$ (language, hardware, toolchain, parallelization policy), produce a certified artifact $A(S, P)$ — kernel source, built binary, execution evidence, verdict — such that every test in the verification set passes on $P$, with full provenance, at the scale of premises 1-3.

The framework is therefore a **build system whose compiler is non-deterministic**. Every design decision follows from that framing:

- Because the compiler is non-deterministic, certification (execution + deterministic verdict) is the ground truth, and every LLM output is untrusted data until validated.
- Because it is a build system, identity, caching, incremental rebuild, parallelism, and resume are substrate concerns solved once by content addressing, not per-feature bookkeeping.

## Reference architecture

### A1. Certification as content-addressed derivation

Every artifact is the output of a **derivation**: a description of (inputs, transformation). Because the transformation is non-deterministic (premise 4), a single identity is insufficient; three identities are kept distinct:

- **Derivation key** — a content hash over the derivation's contract inputs (table below). It identifies the *request* ("this work under these contracts"), not any particular output.
- **Attempt id** — one per execution of a derivation. The event log records every attempt (inputs as resolved, model, usage, outcome), including failed attempts.
- **Output hash** — a content hash of a produced artifact. Certified outputs are stored under it; distinct attempts of the same derivation may produce distinct output hashes.

Cache policy: only a **certified success** satisfies a derivation-key lookup. Failed attempts are recorded in the event log but are never cache hits, so re-evaluation retries them. Exactly two paths re-execute a derivation whose key already holds a certified output, both recorded in the event log: **forced re-derivation** (an operator-initiated rerun of the same key, e.g. to obtain a better-performing output) and **revocation** (a certified output later found defective — by a post-hoc finding or a corrected comparand — is marked ineligible for lookup; it remains addressable by output hash, and the next DAG evaluation re-runs the derivation if no eligible output remains). Multiple certified outputs under one key arise only from these paths; selection picks the newest eligible certification, and older outputs remain addressable by their output hash (provenance, regression baselines).

The key's input set is defined **per derivation type**; there is no single all-artifact key:

| derivation | contract inputs hashed into the key |
|---|---|
| spec-compile | spec bodies (`controlled_spec.md`, `tests.md`, `deps.yaml`), semantic-IR output hashes of the dependency closure, transformation version |
| codegen | semantic-IR output hash, target profile $P$, harness output hash for $P$ (interface + plumbing as consumed), dependency interface output hashes, transformation version |
| assemble | semantic-IR output hash, codegen output hash (kernel + checks), harness output hash for $P$, transformation version |
| build | assembled-source output hash, toolchain identity from $P$, transformation version |
| execute | binary output hash, case set from the IR, resolved execution-environment identity (hardware, runtime/driver/library versions as resolved from $P$), parallel execution policy and runtime parameters, sandbox/run policy version, execution-backend version, transformation version |
| verdict | evidence output hash, predicate set from the IR, regression-baseline output hash, `cross_target` counterpart evidence output hashes, comparand selection policy version, transformation version |

The transformation version of an LLM stage is its prompt contract version + gate/validator version + model policy (A7); the transformation version of a deterministic stage is the version identity of the implementing tool and its policy inputs (renderer + templates for assemble, build-runtime server for build and execute, verdict evaluator for verdict). Evidence is environment-specific (perf in particular), so a change of execution environment invalidates the execute derivation and, through the evidence output hash, its dependent verdict. Downstream references to the output of a non-deterministic stage are always the selected certified **output hash**, never the upstream derivation key: one derivation key can hold multiple certified outputs (cache policy above), so a key-based reference would leave the dependent key unchanged when the selected output changes. The spec-compile key contains no target profile, so the semantic IR is compiled once per spec and shared across all targets (A5); target dependence enters at codegen.

Inputs divide into two classes, and the classification rule is explicit:

- **Contract inputs** define what a valid output is; every one of them is hashed into the key (the table above). Comparands consumed by the verdict — the regression baseline, the `cross_target` counterpart evidence — are contract inputs: updating a baseline or a counterpart invalidates the dependent verdict derivation.
- **Advisory inputs** influence which valid output an attempt produces without changing what "valid" means; the selected exemplar (A7) is the canonical case. Advisory inputs are recorded per attempt in the event log for full provenance but are excluded from the key: output validity is established by certification, and keying on the exemplar would let every newly certified node invalidate its siblings' certified derivations (the corpus grows monotonically), cascading recertification with no correctness gain.

Consequences, obtained structurally rather than by dedicated mechanisms:

- **Incremental recertification**: an unchanged derivation key is a cache hit; a one-line spec change invalidates exactly its dependent closure. Version-granularity freshness checks, content-free version bumps, and staleness comparison logic are unnecessary (subsumes R6-lite and implements R6 proper).
- **Resume**: re-running a partially failed workflow is re-evaluating the DAG; certified derivations are cache hits and failed attempts are retried. Checkpoint files, reopen/supersede/tombstone/backfill machinery, and overwrite-orphan handling are unnecessary.
- **Parallelism**: independent derivations execute concurrently by construction; no sequential-execution invariant is needed, only a concurrency limit.
- **Identity**: derivation key + attempt id + output hash replace hand-minted `ir_id` / `pipeline_id` / `source_id` / `binary_id` / `run_id` families and their format regexes, uniqueness rules, and index files.

Storage is a content-addressed store plus an append-only event log (one record per derivation attempt: inputs as resolved, model, usage, outcome). Human-readable views are generated from the log, not maintained as parallel canonical files.

### A2. LLM stages as pure functions

Every LLM stage is a host-mediated call: the host assembles the complete context, the model returns the complete artifact as structured output, the host validates and writes it.

- The model has **no filesystem access, no shell, no gate invocations, and no write path**. Context (spec bodies, IR, dependency interfaces, exemplar, findings) is inlined by the host in a fixed, byte-stable order; artifacts come back as typed fields.
- Repair is a continuation: deterministic gate findings are appended to the same conversation as a findings turn, preserving the producer's context (the warm-repair pattern) without any session-transcript coupling to a CLI harness.
- Separate personas (generate vs verify, execute vs judge) are separate conversations, preserved from the current design.
- Backends are API clients behind one gateway interface. Backend preflight (multi-agent capability probes, hooks-feature probes, CLI liveness checks) is unnecessary because there is no agent harness to probe.

The prompt-prefix-caching item of R6 is the default behavior here: the fixed context prefix is shared across nodes by construction, and no leaf spends turns on document reads.

### A3. Trust model: unrepresentable over policed

The current threat model (a leaf forging evidence, writing outside scope, deriving rules from validator code, tampering with audit logs) exists because the untrusted actor holds a shell inside the repository. Under A2 the untrusted actor holds nothing:

- It cannot write: the host writes all artifacts.
- It cannot forge the evidence channel: `diagnostics.json` / `perf.json` / snapshots are produced only by host execution of the built binary under the host-rendered runner.
- It cannot read out of scope: it reads only what the host inlined.
- Audit is total by construction: every prompt and response is recorded at the gateway.

Host execution authenticates the evidence **channel**, not the evidence **content**: check statuses and metric values are computed by generated code, and the kernel and its checks are co-generated by the same untrusted stage (stage 2). A defective kernel paired with checks that report the defective behavior as passing therefore clears stages 4-6 unaided. This gap is closed on the verification side, not the enforcement side: A4 defines the evidence trust boundary (a host-owned primary-state channel that bypasses generated code, and per-test corroboration before secondary evidence can carry a verdict).

Sandboxing (`bwrap`) is retained for exactly one purpose: executing generated binaries and build commands. The authoring-side enforcement complex — capability tokens, write/read/output manifests, PreToolUse hooks, write-scope baselines and FS-diff containment, access-log observability, launch-prompt marker validation, forbidden-write-idiom rules — has no counterpart because the behaviors it polices are unrepresentable.

### A4. The verification contract is the product

Humans cannot author expected outputs for complex physics at scale (premises 1-2); the verification set is therefore the framework's core intellectual property and is machine-evaluable end to end.

- `tests.md` states intent in natural language; Compile formalizes every test into predicates over declared evidence (the R2 predicate DSL). The verdict is computed deterministically from evidence + predicates. No LLM holds pass/fail authority.
- Test kinds are those of R3: `case` (L0 fixed cases), `property` (conservation, positivity, symmetry, boundedness), `mms`, `convergence`, `cross_target` (variants of the same spec on different targets validate each other, replacing the absent human reference), `regression` (against the previously certified derivation of the same node).
- The LLM judge is an **advisory semantic reviewer**: it flags spec-intent divergence the predicates cannot see and routes repairs, but cannot flip a verdict in either direction.
- **Evidence trust boundary — primary state**: the host evaluates predicates over **primary state** (per-case snapshots of the declared state variables) for every test kind expressible on it (`property`, `mms`, `convergence` over snapshot norms, `cross_target` / `regression` comparisons). Primary state is defined by its capture path, stated exactly: the generated kernel makes one decision — the **binding**, registering its state storage with the certified harness through a fixed registration ABI at initialization — and everything downstream of the binding (every read and every serialization of snapshot values) is owned by the harness (a separately certified infrastructure node, A6). Generated kernel or checks code cannot compute, filter, or rewrite a captured value, and checks code has no role in snapshot capture. That property holds only with a capture contract, which the registration ABI fixes: per case, the harness copies the registered storage's values into harness-owned buffers immediately after `case_run` returns and **before any generated check/metric callback for that case is invoked**, and snapshot values are never re-read after a callback has run — so a callback that writes to registered storage cannot alter captured evidence. Read-only mapping of the registered storage during the callback window, or process isolation of the callbacks, are acceptable stronger alternatives where the target supports them. The trust property is therefore not "no generated code in the path" but "generated code contributes exactly the binding and nothing downstream of it". The binding's defect class — registering decoy storage disconnected from the integrated state — is accepted: decoy storage must itself satisfy every host-evaluated predicate on every case, a strictly harder defect to produce by error than checks that report pass. Acceptable alternatives where the registration ABI is not expressible on a target: snapshot getters generated and certified as a unit separate from kernel + checks, or a host-verifiable ABI binding each snapshot value to kernel storage.
- **Evidence trust boundary — secondary evidence**: generated check/metric code is **secondary evidence**, admissible only for quantities not expressible over primary state. Corroboration is required **per test**, not per node: every `test_id` whose verdict rests on secondary evidence must be corroborated on the same claimed quantity by a host-evaluated predicate over primary state or by a `cross_target` / `regression` comparison of that quantity. A predicate set in which a test passes on uncorroborated secondary evidence alone is rejected at the spec-compile gate. `cross_target` and `regression` reduce rather than eliminate correlated defects — a regression baseline can carry a defect present at first certification, and `cross_target` variants share the spec, IR, and generation policy — so a host-evaluated predicate over primary state is the preferred corroborant wherever one is expressible.
- **Guarantee scope**: certification asserts exactly that every test in the verification set passes over the declared evidence contract on $P$; it is not unconditional correctness. The accepted residuals of this section — decoy-state registration, correlated `cross_target` / `regression` defects — are named exceptions to premise 4's exit guarantee, recorded as accepted residuals rather than covered by it.

### A5. Two-layer IR and the target matrix

The IR is split at design time (not retrofitted) into:

- **Semantic layer** (per spec version): cases, algorithm structure, io contract, test predicates, dependency edges. Compiled once per spec; target-independent (its derivation key contains no target profile; A1).
- **Target profile** (per $P$): language, hardware, toolchain, parallelization/memory-layout policy.

Codegen, build, execute, and verdict run per (node, $P$); certified artifacts, exemplar retrieval, and harness selection are keyed by (node, $P$). This is R4 as a founding assumption rather than a migration.

### A6. Harness as certified infrastructure

Per target profile, one `infrastructure` node supplies the runner plumbing (case parsing, case loop, evidence emission); physics codegen produces only the kernel and per-test check callbacks, and glue is host-rendered from the IR. This is R1/M3c, adopted with one amendment: under the A4 evidence trust boundary, snapshot capture moves from generated checks getters (the current M3c path routes snapshot values through the checks module's `get_scalar` / `get_rN` callbacks) to the harness-owned state channel (registration ABI, A4).

### A7. Corpus feedback

Three corpora grow with every certified node and feed back into generation:

- **Exemplar corpus** (R5, adopted unchanged): certified (family, spec_kind, language, target) siblings injected as prior art. An advisory input under A1: recorded per attempt in the event log, excluded from derivation keys.
- **Failure taxonomy as data**: failure classes, attribution rules, and routing decisions are versioned structured data consumed by the router and injected into repair turns — not prose distributed across contract documents.
- **Prompt contracts as versioned inputs**: the per-stage instruction text is a versioned artifact participating in the derivation key, so a contract change is an observable, cache-invalidating event with measurable effect on pass rates.

### A8. Runtime substrate

One orchestrator process evaluates the derivation DAG: schedules ready derivations (respecting a concurrency limit), invokes LLM stages through the gateway, runs deterministic stages in-process, executes builds/runs under `bwrap` via the MCP build-runtime server, appends to the event log, and stores outputs in the content-addressed store. Failure routing is a deterministic classifier over gate findings, build logs, and verdicts, with a single escalation reviewer for unclassifiable failures.

### Pipeline walkthrough (per node, then per target)

| stage | actor | input | output |
|-------|-------|-------|--------|
| 1 spec-compile | LLM (pure fn) + deterministic gates + independent LLM cross-check | spec bodies, catalog, dependency IRs | semantic IR incl. test predicates |
| 2 codegen | LLM (pure fn) + lint/syntax/static gates | semantic IR, target profile, dependency interfaces, exemplar, harness API | kernel + checks source |
| 3 assemble | deterministic | IR, harness, kernel + checks | full source tree (glue host-rendered) |
| 4 build | deterministic (sandboxed) | source tree, toolchain | binary |
| 5 execute | deterministic (sandboxed) | binary, cases, execution environment | evidence (diagnostics, perf, snapshots, metrics basis) |
| 6 verdict | deterministic | evidence, predicates, comparands (regression baseline, `cross_target` counterpart) | per-test + aggregate verdict |
| 7 review | LLM (advisory) | spec, IR, source, evidence excerpts | semantic review, repair routing |

Gate findings at stages 1-2 append a findings turn to the producing conversation (warm repair). Verdict failures at stage 6 route by the deterministic attribution table (codegen / spec-compile / spec). Stage 7 findings route identically but cannot override a stage-6 pass/fail.

## Comparison with the current implementation

### Where the two designs agree

The macro structure is identical: controlled NL spec + machine-evaluable tests as canonical source; a single structured IR; separated model/runner; deterministic build and execution; exit-based certification; generate/verify persona separation; dependency DAG of typed spec nodes; harness as a certified infrastructure node; exemplar injection; deterministic verdicts. The current implementation reached this by iterative hoisting (G1-G8, R1/M3a-M3d, R2, R3-core, R5, R6-lite); the zero-base design derives it directly. The macro structure of the current design is the same structure the zero-base derivation produces.

### The one structural difference

The current leaf execution model is an **agentic CLI session** (`claude -p` / `codex exec`) holding a shell inside the repository: it force-reads contract documents at cold start, invokes gates itself, and writes artifacts with file tools. Everything in the following inventory exists to make that actor safe and observable, and has no counterpart under A2/A3:

- capability tokens, output/read manifests, sandbox profiles per `agent_run_id`
- mandatory `bwrap` for authoring stages, FS-diff `write_roots` containment, write-scope baselines
- PreToolUse hook complex (write guards, read guards, inline-python bans, tool-read bans, launch-marker validation)
- backend preflight (multi-agent probe, hooks-feature cache, live re-probes, TTLs)
- leaf-facing contract documents (`AGENT_CONTRACT.md`, gate runbooks, allowed/forbidden idiom lists) and their `skill_must_read_refs` assembly
- session-transcript coupling for warm resume; launch/dialog evidence mirroring; access-log observability with its known bypass caveats

Measured consequences of the current model (2026-07 baseline): 96-97% of node wall time in LLM leaves, 89-90% of output tokens extended thinking, 35-50 KB cold force-reads per leaf with no shared prompt prefix, 43-62 min and 360-540k output tokens per certified node.

### Secondary differences

| aspect | current | zero-base |
|--------|---------|-----------|
| identity | hand-minted id families + format rules + indexes | derivation key + attempt id + output hash |
| incremental / freshness | R6-lite version-granularity readiness | hash invalidation (R6 proper), structural |
| resume / recovery | checkpoint + reopen-phase + supersede + backfill + overwrite-archiver | DAG re-evaluation over cache |
| parallelism | forbidden by invariant, revision planned | native, concurrency-limited |
| run records | ~15 JSON artifact kinds per orchestration, cross-validated by a 10.9k-line semantics validator | append-only event log + generated views |
| audit | policed (manifests, hooks, diffs) with known observational gaps | total at the gateway by construction |
| prompt economics | per-leaf cold reads, no shared prefix | host-inlined byte-stable shared prefix |
| verification | R2 + R3-core landed; judge advisory; snapshot values via generated checks getters | R2 + R3-core adopted; adds the A4 evidence trust boundary (host-owned state channel, per-test corroboration) |
| target matrix | R4 proposed | founding assumption (same content) |

### What the current implementation embodies that a reimplementation would lack

- `validate_pipeline_semantics.py` (10.9k lines) and the deterministic gates encode several hundred failure modes discovered only through billed E2E runs (render preconditions, interface pinning, lint/syntax attribution, evidence integrity, dependency-fact rank surfacing). This corpus is target-model-independent IP and transfers to any architecture.
- The verdict evaluator, predicate DSL, multi-target evidence contract, dependency-graph builder, harness spec + certification method, exemplar selector, MCP build-runtime server, and the spec/IR schemas are all A2-compatible as-is.
- ~2380 unit tests pin this behavior.

## Adoption decision

**Decision: evolve the current implementation in place toward this reference architecture. A ground-up reimplementation is rejected.**

### Rationale

1. The two designs share their macro structure; a reimplementation re-derives the same macro architecture while discarding the two assets that dominate replacement cost: the E2E-discovered failure-mode corpus (validators/gates) and the test suite that pins it. Both are directly reusable under A2.
2. The zero-base delta is concentrated in one seam — the leaf execution model — and the enforcement complex wraps that seam rather than permeating the validators. It can be replaced stage-by-stage with the suite green at every step, deleting the corresponding policing layer per migrated stage.
3. Certification is billed and slow. A parallel reimplementation has no certified corpus to validate against for months; an in-place migration validates each step with a single billed E2E against a known baseline.
4. The current trajectory (G-series hoists, M3c host-rendering, R5 injection, task cards, slim repair turns) already moves every migration in the same direction; the reference architecture names the endpoint rather than reversing course.

### Migration items (Z-series)

Each item is independently landable and gated by the standard decision criteria (billed E2E `aggregate_verdict=pass`; timing-audit cost verification; correctness-reducing changes rejected).

- **Z1. Pure-function stage pilot — `compile.generate`.** Host inlines spec bodies + catalog + dependency IRs; the model returns `spec.ir.yaml` + `ir_meta.json` as structured output; the host writes them and runs the existing `Compile.static` gates; findings append as repair turns. Smallest surface (bounded inputs/outputs, no MCP use), measurable against the current baseline.
- **Z2. Pure-function `generate.generate` + `generate.verify`.** Host-assembled context (IR, dependency facts, exemplar, harness API); returned kernel + checks written by the host; existing lint/syntax/static gates unchanged. This is the dominant-cost leaf; expected to capture most of the R6 prefix-caching benefit and delete the largest share of leaf-contract documentation.
- **Z3. Pure-function `validate.judge` (advisory reviewer).** Host inlines evidence excerpts under an explicit excerpting policy; R2 already removed verdict authority. Retires the judge-side gate wiring.
- **Z4. Enforcement retirement per migrated stage.** For each stage moved in Z1-Z3, delete its capability/manifest/hook/preflight surface and the leaf-facing contract text; `bwrap` remains solely on build/execute. Each deletion lands with the migration that makes it dead, never ahead of it.
- **Z5. Derivation-keyed store.** Introduce content-hash derivation keys alongside the existing ids (dual-keyed), move cache/skip/freshness decisions onto them (replacing R6-lite comparison logic), then retire checkpoint-based resume in favor of DAG re-evaluation. Enables DAG-parallel execution as its final step. Implements the full A1 identity model: derivation key over per-type contract inputs, attempt ids in the event log, output hashes for certified artifacts, certified-success-only cache eligibility.
- **Z6. Evidence trust boundary (A4).** Two sub-items. (1) Host-owned primary-state channel: add the state-registration ABI to the harness spec and move snapshot capture off the generated checks getters (`get_scalar` / `get_rN`) onto harness-owned reads of registered kernel storage; the host then computes primary-state-expressible compared quantities itself (extending the verdict evaluator over the snapshots). (2) Per-test corroboration: classify leaf-computed check/metric values as secondary evidence and enforce, at the spec-compile gate, that every `test_id` resting on secondary evidence has a same-quantity corroborant. Closes the correlated kernel/checks defect gap, which the current implementation shares (its runner acquires snapshot values through the generated checks module).

Sequencing with the R-series: R4 (target matrix) proceeds unchanged and is orthogonal; Z1-Z3 supersede the R6 prompt-prefix-caching item; Z5 supersedes R6 incremental-recertification proper; R6 per-persona model tiering becomes a gateway parameter after Z1-Z3. Z6 is orthogonal to the leaf-execution seam and can land independently of Z1-Z5.

### Risks

- **Context-size ceiling.** Inlined context (spec + IR + dependency facts + exemplar + evidence excerpts) can exceed practical prompt budgets for large nodes. Mitigation: explicit per-stage excerpting policies; as a fallback, a host-mediated read-only fetch tool (model requests a named artifact, host serves it) — which preserves A3 because there is still no write path.
- **Loss of leaf self-directed exploration.** An agentic leaf can inspect unexpected state mid-task; a pure function cannot. Mitigation: the deterministic gates + repair-turn loop already carry that role today (the leaf-autonomy surface has been shrinking since G1); unclassifiable failures retain the escalation reviewer.
- **Structured-output fidelity.** Large source files as structured output risk truncation/fencing artifacts. Mitigation: per-field size accounting, retry-on-schema-violation at the gateway, and the existing syntax gate as backstop.
- **Migration-period duplication.** Dual leaf models (CLI + gateway) coexist during Z1-Z3. Mitigation: per-stage cutover with no long-lived dual path per stage; the conductor's stage interface (launch → result → route) is unchanged, only the executor behind it.
- **Correlated kernel/checks defects.** Kernel and checks are co-generated; a kernel defect mirrored in the checks passes stages 4-6. This risk exists identically in the current implementation, where snapshot values are acquired through generated checks getters. Mitigation: the A4 evidence trust boundary — the host-owned primary-state channel and per-test corroboration of secondary evidence (Z6). Accepted residual: decoy kernel state (A4).

## Decision Criteria

- Each Z-item is adopted only with a billed E2E on a real node reaching `aggregate_verdict=pass` at equal-or-better certified-outcome accuracy.
- Cost targets: after Z1-Z3, ≤10 min wall and ≤100k output tokens per certified node (tightening the R1+R2+R5 target of ≤15 min / ≤150k); after Z5, dependency-closure re-certification cost proportional to the invalidated closure only.
- Enforcement retirement (Z4) is complete for a stage only when the deleted surface has no remaining consumer in `tools/` and the unit suite passes without the corresponding fixtures.
- Z6 is complete only when, in addition to the billed E2E pass, adversarial fixtures fail certification: (a) a checks module that reports pass against a defective kernel; (b) snapshot values falsified through the retired getter path — the path must be unreachable from the rendered runner; (c) a registration binding decoy storage that diverges from the integrated state on at least one case — the host-evaluated predicates must fail; (d) a check/metric callback that writes to registered storage after `case_run` — the captured snapshot must be unaffected. A billed E2E pass with the legacy getter path still live does not qualify.

## References

- `workflow_scaling_redesign.md` (R1-R6)
- `deterministic_followups.md` (G-series, M3, R2/R3-core/R5/R6-lite records)
- `docs/ORCHESTRATION.md`, `docs/workflow/WORKFLOW_CORE.md` (current contracts)
- `docs/SPEC.md` (invariant principles retained unchanged)
