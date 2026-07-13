# Zero-base reference architecture and adoption decision

Status: **proposed** (reference architecture). Recorded 2026-07-12 and amended 2026-07-13 from a first-principles redesign study against the stated final goal. Parts of this architecture are already realized by the current implementation and are identified in place (R2 verdicts, R3-core test kinds, R5 exemplar injection, R1/M3c harness structure with the A6 amendment); the Z-series migration items are not implemented. This document defines the target architecture the implementation converges to, compares it with the current implementation, and records the adoption decision (evolve in place / reimplement). It extends, and does not replace, `workflow_scaling_redesign.md` (R1-R6); overlaps are cross-referenced.

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

Given a spec node $S$ (controlled spec, verification set, dependency declarations) and a target profile $P$ (language, hardware, toolchain, parallelization policy), produce a certified artifact $A(S, P)$ — `CodegenBundle`, assembled source, built binary, execution evidence, verdict — such that every test in the verification set passes on $P$, with full provenance, at the scale of premises 1-3.

The framework is therefore a **build system whose compiler is non-deterministic**. Every design decision follows from that framing:

- Because the compiler is non-deterministic, certification (execution + deterministic verdict) is the ground truth, and every LLM output is untrusted data until validated.
- Because it is a build system, identity, caching, incremental rebuild, parallelism, and resume are substrate concerns solved once by content addressing, not per-feature bookkeeping.

## Reference architecture

### A1. Certification as content-addressed derivation

Every artifact is the output of a **derivation**: a description of (inputs, transformation). Because the transformation is non-deterministic (premise 4), a single identity is insufficient; three identities are kept distinct:

- **Derivation key** — a content hash over the derivation's contract inputs (table below). It identifies the *request* ("this work under these contracts"), not any particular output.
- **Attempt id** — one per execution of a derivation. The event log records every attempt (inputs as resolved, model, usage, outcome), including failed attempts.
- **Output hash** — a content hash of a produced artifact. Certified outputs are stored under it; distinct attempts of the same derivation may produce distinct output hashes.

Cache policy: only a **certified success** satisfies a derivation-key lookup. Failed attempts are recorded in the event log but are never cache hits, so re-evaluation retries them. Exactly two paths re-execute a derivation whose key already holds a certified output, both recorded in the event log: **forced re-derivation** (an operator-initiated or policy-initiated rerun of the same key, e.g. to obtain a better-performing output after the exemplar corpus improves) and **revocation** (a certified output later found defective — by a post-hoc finding or a corrected comparand — is marked ineligible for lookup; it remains addressable by output hash, and the next DAG evaluation re-runs the derivation if no eligible output remains). Multiple certified outputs under one key arise only from these paths. A versioned selection policy chooses among eligible certifications using the declared correctness and performance objectives; recency is only the final tie-breaker. Older outputs remain addressable by output hash for provenance and regression baselines.

The key's input set is defined **per derivation type**; there is no single all-artifact key:

| derivation | contract inputs hashed into the key |
|---|---|
| spec-compile | spec bodies (`controlled_spec.md`, `tests.md`, `deps.yaml`), semantic-IR output hashes of the dependency closure, transformation version |
| codegen | ordered semantic-IR output hashes of the optimization unit, target profile $P$, harness capability ABI output hash for $P$, dependency interface output hashes, transformation version |
| assemble | ordered semantic-IR output hashes of the optimization unit, `CodegenBundle` output hash, harness output hash for $P$, transformation version |
| build | assembled-source output hash, toolchain identity from $P$, transformation version |
| execute | binary output hash, case sets from the optimization-unit IRs, resolved execution-environment identity (hardware, runtime/driver/library versions as resolved from $P$), parallel execution policy and runtime parameters, sandbox/run policy version, execution-backend version, transformation version |
| verdict | evidence output hash, predicate sets from the optimization-unit IRs, regression-baseline output hashes, `cross_target` counterpart evidence output hashes, comparand selection policy version, transformation version |

The transformation version of an LLM stage is its prompt contract version + gate/validator version + model policy (A7); the transformation version of a deterministic stage is the version identity of the implementing tool and its policy inputs (renderer + templates for assemble, build-runtime server for build and execute, verdict evaluator for verdict). Evidence is environment-specific (perf in particular), so a change of execution environment invalidates the execute derivation and, through the evidence output hash, its dependent verdict. Downstream references to the output of a non-deterministic stage are always the selected certified **output hash**, never the upstream derivation key: one derivation key can hold multiple certified outputs (cache policy above), so a key-based reference would leave the dependent key unchanged when the selected output changes. The spec-compile key contains no target profile, so the semantic IR is compiled once per spec and shared across all targets (A5); target dependence enters at codegen.

Inputs divide into two classes, and the classification rule is explicit:

- **Contract inputs** define what a valid output is; every one of them is hashed into the key (the table above). Comparands consumed by the verdict — the regression baseline, the `cross_target` counterpart evidence — are contract inputs: updating a baseline or a counterpart invalidates the dependent verdict derivation.
- **Advisory inputs** influence which valid output an attempt produces without changing what "valid" means; the selected exemplar (A7) is the canonical case. Advisory inputs are recorded per attempt in the event log for full provenance but are excluded from the key: output validity is established by certification, and keying on the exemplar would let every newly certified node invalidate its siblings' certified derivations (the corpus grows monotonically), cascading recertification with no correctness gain.

Consequences, obtained structurally rather than by dedicated mechanisms:

- **Incremental recertification**: an unchanged derivation key is a cache hit; a one-line spec change invalidates exactly its dependent closure. Version-granularity freshness checks, content-free version bumps, and staleness comparison logic are unnecessary (subsumes R6-lite and implements R6 proper).
- **Resume**: re-running a partially failed workflow is re-evaluating the DAG; certified derivations are cache hits and failed attempts are retried. User-facing phase checkpoint, reopen, supersede, tombstone, backfill, and overwrite-orphan machinery is unnecessary. The runtime still requires durable attempt records, atomic output publication, concurrency leases, duplicate-work suppression, and crash recovery for in-flight derivations.
- **Parallelism**: independent derivations execute concurrently by construction; no sequential-execution invariant is needed, only a concurrency limit.
- **Identity**: derivation key + attempt id + output hash replace hand-minted `ir_id` / `pipeline_id` / `source_id` / `binary_id` / `run_id` families and their format regexes, uniqueness rules, and index files.

Storage is a content-addressed store plus an append-only event log (one record per derivation attempt: inputs as resolved, model, usage, outcome). Human-readable views are generated from the log, not maintained as parallel canonical files.

Invalidation is content-exact, not semantically minimal: any hashed input change, including a non-semantic edit to an unnormalized spec body, changes the derivation key and invalidates its graph dependents. Canonicalization may remove explicitly defined representation-only differences, but the cache does not infer semantic equivalence from arbitrary text changes.

### A2. LLM stages as pure functions

Every LLM stage is a host-mediated transformation: the host assembles the authorized context, the model returns a complete typed artifact, and the host validates and writes it. Pure-function status means that the model has no authority outside the host-mediated input/output channel; it does not require every stage to fit in one request.

- The model has **no filesystem access, no shell, no gate invocations, and no write path**. Context (spec bodies, IR, dependency interfaces, exemplar, findings) is inlined by the host in a fixed, byte-stable order by default; artifacts come back as typed fields.
- A stage that exceeds the closed-context limit may issue bounded read-only requests for named authorized artifacts. The host records each request and response as part of the attempt input. The model still receives no path traversal, repository search, shell, or write authority.
- Code generation returns one complete `CodegenBundle`, not algorithm-step fragments for the host to concatenate. The LLM owns every source file inside the optimization unit, including private helper procedures, internal modules, data structures, and target-specific execution algorithms. The host owns only contract-boundary assembly.
- Repair is a continuation: deterministic gate findings are appended to the same conversation as a findings turn, preserving the producer's context (the warm-repair pattern) without any session-transcript coupling to a CLI harness.
- Separate personas (generate vs verify, execute vs judge) are separate conversations, preserved from the current design.
- Backends are API clients behind one gateway interface. Backend preflight (multi-agent capability probes, hooks-feature probes, CLI liveness checks) is unnecessary because there is no agent harness to probe.

The prompt-prefix-caching item of R6 is the default behavior here: the fixed context prefix is shared across nodes by construction, and no leaf spends turns on document reads. A bounded read-only request reduces prefix-cache efficiency for that attempt and is used only when the closed context is insufficient.

### A3. Trust model: unrepresentable over policed

The current threat model (a leaf forging evidence, writing outside scope, deriving rules from validator code, tampering with audit logs) exists because the untrusted actor holds a shell inside the repository. Under A2 the untrusted actor holds nothing:

- It cannot write: the host writes all artifacts.
- It cannot forge the evidence channel: `diagnostics.json` / `perf.json` / snapshots are produced only by host execution of the built binary under the host-rendered runner.
- It cannot read out of scope: it reads only what the host inlined.
- Audit is total by construction: every prompt and response is recorded at the gateway.

Host execution authenticates the evidence **channel**, not the evidence **content**: check statuses and metric values are computed by generated code, and the kernel and its checks are co-generated by the same untrusted stage (stage 2). A defective kernel paired with checks that report the defective behavior as passing therefore clears stages 4-6 unaided. This gap is closed on the verification side, not the enforcement side: A4 defines the evidence trust boundary (a harness-owned, target-aware primary-state channel that bypasses generated checks, and per-test corroboration before secondary evidence can carry a verdict).

Sandboxing (`bwrap`) is retained for exactly one purpose: executing generated binaries and build commands. The authoring-side enforcement complex — capability tokens, write/read/output manifests, PreToolUse hooks, write-scope baselines and FS-diff containment, access-log observability, launch-prompt marker validation, forbidden-write-idiom rules — has no counterpart because the behaviors it polices are unrepresentable.

### A4. The verification contract is the product

Humans cannot author expected outputs for complex physics at scale (premises 1-2); the verification set is therefore the framework's core intellectual property and is machine-evaluable end to end.

- `tests.md` states intent in natural language; Compile formalizes every test into predicates over declared evidence (the R2 predicate DSL). The verdict is computed deterministically from evidence + predicates. No LLM holds pass/fail authority.
- Test kinds are those of R3: `case` (L0 fixed cases), `property` (conservation, positivity, symmetry, boundedness), `mms`, `convergence`, `cross_target` (variants of the same spec on different targets validate each other, replacing the absent human reference), `regression` (against the previously certified derivation of the same node).
- The LLM judge is an **advisory semantic reviewer**: it flags spec-intent divergence the predicates cannot see and routes repairs, but cannot flip a verdict in either direction.
- **Evidence trust boundary — primary state**: the host evaluates predicates over **primary state** (per-case snapshots of the declared state variables) for every test kind expressible on it (`property`, `mms`, `convergence` over snapshot norms, `cross_target` / `regression` comparisons). Primary state is defined by its capture path, stated exactly: the generated kernel makes one decision — the **binding**, registering its state storage with the certified harness through the selected harness capability ABI at initialization — and everything downstream of the binding (every read, trusted reduction, and serialization of snapshot values) is owned by the harness or its certified target backend (A6). Generated kernel or checks code cannot compute, filter, or rewrite a captured value, and checks code has no role in snapshot capture. The capture contract fixes the evidence cut: for a synchronous target, the harness copies registered storage into harness-owned buffers immediately after `case_run` returns; for an asynchronous target, the certified backend captures the registered device state at the capability-defined case-completion event. Both paths establish the captured value **before any generated check/metric callback for that case is invoked**, and snapshot values are never re-read after a callback has run. Read-only mapping of the registered storage during the callback window, process isolation of callbacks, or target-owned immutable device buffers are acceptable stronger alternatives. The trust property is therefore not "no generated code in the path" but "generated code contributes exactly the binding and nothing downstream of it". The binding's defect class — registering decoy storage disconnected from the integrated state — is accepted: decoy storage must itself satisfy every host-evaluated predicate on every case, a strictly harder defect to produce by error than checks that report pass. Acceptable alternatives where direct state registration is not expressible on a target are a certified target-owned capture adapter or a host-verifiable ABI binding each captured value to kernel storage; generated snapshot getters co-certified with kernel + checks remain secondary evidence and do not satisfy the primary-state boundary by themselves.
- **Evidence trust boundary — secondary evidence**: generated check/metric code is **secondary evidence**, admissible only for quantities not expressible over primary state. Corroboration is required **per test**, not per node: every `test_id` whose verdict rests on secondary evidence must be corroborated on the same claimed quantity by a host-evaluated predicate over primary state or by a `cross_target` / `regression` comparison of that quantity. A predicate set in which a test passes on uncorroborated secondary evidence alone is rejected at the spec-compile gate. `cross_target` and `regression` reduce rather than eliminate correlated defects — a regression baseline can carry a defect present at first certification, and `cross_target` variants share the spec, IR, and generation policy — so a host-evaluated predicate over primary state is the preferred corroborant wherever one is expressible.
- **Guarantee scope**: certification asserts exactly that every test in the verification set passes over the declared evidence contract on $P$; it is not unconditional correctness. The accepted residuals of this section — decoy-state registration, correlated `cross_target` / `regression` defects — are named exceptions to premise 4's exit guarantee, recorded as accepted residuals rather than covered by it.

### A5. Semantic IR, target lowering, and the target matrix

Generation inputs and decisions are split at design time into:

- **Semantic layer** (per spec version): cases, algorithm structure, io contract, test predicates, dependency edges. Compiled once per spec; target-independent (its derivation key contains no target profile; A1).
- **Target profile** (per $P$): language, hardware, toolchain, parallelization/memory-layout policy.
- **Target lowering plan** (per codegen output): the execution-algorithm decisions that realize one or more semantic IRs on $P$, including precision, data layout, decomposition, fusion, accelerator mapping, communication, and state residency. It is emitted as a structured member of the `CodegenBundle` and validated against the semantic IR and target profile.

Certification identity remains per semantic node and target, but code generation operates on an **optimization unit** containing one or more semantic nodes. A single-node unit is the default. A multi-node unit is permitted when it preserves each member's external semantic interface and verification predicates while enabling internal fusion, shared intermediate values, or a common data layout. The optimization-unit membership and order are contract inputs to codegen. This prevents the certification-node boundary from becoming a mandatory compiler optimization boundary.

Codegen, build, execute, and verdict run per (optimization unit, $P$), with per-node verdicts retained for every member; certified artifacts, exemplar retrieval, and harness selection are keyed by their semantic-node membership and $P$. This is R4 as a founding assumption rather than a migration.

### A6. Harness as certified infrastructure

Per target profile, one selected versioned `infrastructure` node supplies runner plumbing (case parsing, case loop, evidence emission) through a **harness capability ABI**. Physics codegen produces a `CodegenBundle`; glue is host-rendered only where the binding is a mechanical projection of the IR, bundle entry points, and selected harness capabilities. A data conversion, memory transfer, decomposition, or semantic mapping that affects execution algorithms remains LLM-authored in the bundle and is not synthesized by the host.

The minimum ABI covers a synchronous single-case path. Target backends may expose versioned capabilities for asynchronous or device-resident execution, distributed state, batched cases, full-state capture, and trusted reductions. Codegen declares the capabilities it requires, and assemble fails closed when the selected harness cannot provide them. This prevents a fixed CPU-style ABI from imposing synchronization, host transfer, or kernel-boundary constraints on accelerator targets.

This is R1/M3c, adopted with one evidence amendment: under the A4 evidence trust boundary, snapshot capture moves from generated checks getters (the current M3c path routes snapshot values through the checks module's `get_scalar` / `get_rN` callbacks) to a harness-owned state channel (registration ABI or an equivalent target capability, A4).

### A7. Corpus feedback

Three corpora grow with every certified node and feed back into generation:

- **Exemplar corpus** (R5, adopted unchanged): certified (family, spec_kind, language, target) siblings injected as prior art. An advisory input under A1: recorded per attempt in the event log, excluded from derivation keys.
- **Failure taxonomy as data**: failure classes, attribution rules, and routing decisions are versioned structured data consumed by the router and injected into repair turns — not prose distributed across contract documents.
- **Prompt contracts as versioned inputs**: the per-stage instruction text is a versioned artifact participating in the derivation key, so a contract change is an observable, cache-invalidating event with measurable effect on pass rates.

### A8. Runtime substrate

One orchestrator process evaluates the derivation DAG: schedules ready derivations (respecting a concurrency limit), invokes LLM stages through the gateway, runs deterministic stages in-process, executes builds/runs under `bwrap` via the MCP build-runtime server, appends to the event log, and stores outputs in the content-addressed store. Failure routing is a deterministic classifier over gate findings, build logs, and verdicts, with a single escalation reviewer for unclassifiable failures.

### Pipeline walkthrough (per optimization unit and target)

| stage | actor | input | output |
|-------|-------|-------|--------|
| 1 spec-compile | LLM (pure fn) + deterministic gates + independent LLM cross-check | spec bodies, catalog, dependency IRs | semantic IR incl. test predicates |
| 2 codegen | LLM (host-mediated) + lint/syntax/static gates | optimization-unit semantic IRs, target profile, dependency interfaces, exemplar, harness capability ABI | `CodegenBundle` (source files + entry-point bindings + target lowering plan + capability requirements + state bindings) |
| 3 assemble | deterministic | IRs, harness, `CodegenBundle` | full source tree and build graph (contract-boundary glue host-rendered) |
| 4 build | deterministic (sandboxed) | source tree, toolchain | binary |
| 5 execute | deterministic (sandboxed) | binary, cases, execution environment | evidence (diagnostics, perf, snapshots, metrics basis) |
| 6 verdict | deterministic | evidence, predicates, comparands (regression baseline, `cross_target` counterpart) | per-test + aggregate verdict |
| 7 review | LLM (advisory) | spec, IR, source, evidence excerpts | semantic review, repair routing |

Gate findings at stages 1-2 append a findings turn to the producing conversation (warm repair). Verdict failures at stage 6 route by the deterministic attribution table (codegen / spec-compile / spec). Stage 7 findings route identically but cannot override a stage-6 pass/fail.

## Comparison with the current implementation

### Where the two designs agree

The macro structure is identical: controlled NL spec + machine-evaluable tests as canonical source; a single structured IR; separated model/runner; deterministic build and execution; exit-based certification; generate/verify persona separation; dependency DAG of typed spec nodes; harness as a certified infrastructure node; exemplar injection; deterministic verdicts. The current implementation reached this by iterative hoisting (G1-G8, R1/M3a-M3d, R2, R3-core, R5, R6-lite); the zero-base design derives it directly. The macro structure of the current design is the same structure the zero-base derivation produces.

The current M3c path is a bounded feasibility proof of contract-boundary assembly: the LLM authors the physics model and checks module, while the conductor renders the runner and Makefile from the IR and certified harness interface. It does not prove that a fixed two-file codegen output or a synchronous CPU-style harness ABI is sufficient for every target; Z0 and the A5/A6 extensions define the required generalization.

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
| verification | R2 + R3-core landed; judge advisory; snapshot values via generated checks getters | R2 + R3-core adopted; adds the A4 evidence trust boundary (harness-owned target-aware state channel, per-test corroboration) |
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

- **Z0. Codegen bundle and optimization-boundary contract.** Define and validate `CodegenBundle` before changing the Generate executor. The bundle contains `files[]` (`logical_path`, `role`, `language`, `content`), external `entrypoints`, a structured `target_lowering_plan`, `capability_requirements`, and `state_bindings`. It permits multiple compilation units, private helper procedures, internal modules, and target-specific execution algorithms. Every `logical_path` is normalized, relative, confined to the assembled source root, and unique within the bundle. The bundle forbids arbitrary shell commands and build scripts; the target backend derives the build graph from file roles and declared capabilities. Host rendering is limited to mechanical contract-boundary glue. Define optimization-unit membership and the versioned harness capability ABI in the same contract so semantic-node boundaries do not become mandatory optimization boundaries.
- **Z1. Pure-function stage pilot — `compile.generate`.** Host inlines spec bodies + catalog + dependency IRs; the model returns `spec.ir.yaml` + `ir_meta.json` as structured output; the host writes them and runs the existing `Compile.static` gates; findings append as repair turns. Smallest surface (bounded inputs/outputs, no MCP use), measurable against the current baseline.
- **Z2. Host-mediated `generate.generate` + `generate.verify`.** The host assembles the optimization-unit IRs, dependency facts, exemplar, target profile, and harness capability ABI. `generate.generate` returns one complete `CodegenBundle`; it does not return algorithm-step fragments for conductor concatenation. The host writes the declared files, deterministically assembles boundary glue and the build graph, and runs the existing lint/syntax/static gates. `generate.verify` reviews the bundle and assembled-source contract in an independent conversation. Closed-context execution is the default; bounded named-artifact reads are permitted only when the context-size policy requires them. This is the dominant-cost leaf and is expected to capture most of the prefix-caching benefit while retaining internal code-organization and hardware-optimization freedom.
- **Z3. Pure-function `validate.judge` (advisory reviewer).** Host inlines evidence excerpts under an explicit excerpting policy; R2 already removed verdict authority. Retires the judge-side gate wiring.
- **Z4. Enforcement retirement per migrated stage.** For each stage moved in Z1-Z3, delete its capability/manifest/hook/preflight surface and the leaf-facing contract text; `bwrap` remains solely on build/execute. Gateway prompt/response retention, response-schema validation, truncation classification, and audit records replace the retired authoring-side controls. Each deletion lands with the migration that makes it dead, never ahead of it. Any agentic fallback is an operator-selected diagnostic path for an unclassifiable failure, not an automatic normal-path retry.
- **Z5. Derivation-keyed store.** Introduce content-hash derivation keys alongside the existing ids (dual-keyed), move cache/skip/freshness decisions onto them (replacing R6-lite comparison logic), then retire user-facing checkpoint-based resume in favor of DAG re-evaluation. Preserve internal durable attempt state, atomic publication, leases, duplicate-work suppression, and crash recovery. Enable DAG-parallel execution as the final step. Implement the full A1 identity model: derivation key over per-type contract inputs, attempt ids in the event log, output hashes for certified artifacts, certified-success-only cache eligibility, forced re-derivation, revocation, and versioned eligible-output selection.
- **Z6. Evidence trust boundary (A4).** Two sub-items. (1) Harness-owned primary-state channel: add state registration or an equivalent trusted-capture capability to the harness ABI and move snapshot capture off the generated checks getters (`get_scalar` / `get_rN`). The host or a certified target backend computes primary-state-expressible quantities from full snapshots or trusted reductions. Accelerator backends may retain state on device and capture asynchronously; the contract must bind captured values to the same kernel and state used by the certified execution. (2) Per-test corroboration: classify leaf-computed check/metric values as secondary evidence and enforce, at the spec-compile gate, that every `test_id` resting on secondary evidence has a same-quantity corroborant. Closes the correlated kernel/checks defect gap, which the current implementation shares (its runner acquires snapshot values through the generated checks module).

### Migration effectiveness and tradeoffs

| item | expected effect | effectiveness conditions | disadvantages and residuals |
|---|---|---|---|
| Z0 | Preserves LLM control over internal code structure while making host assembly deterministic and auditable. Establishes the extension point for new languages and hardware. | The schema permits multi-file output, private helpers, target lowering, capability negotiation, and multi-node optimization units without arbitrary build authority. | Adds a new versioned contract and validators. An underspecified bundle or ABI converts source flexibility into repeated assembly failures. |
| Z1 | Validates gateway structured output, host writes, prefix caching, and repair turns on the smallest LLM-authoring surface. | IR semantic error rate and downstream Generate success remain equal to or better than the CLI baseline. | Compile is not the dominant cost, so direct wall-time and token reduction may be limited. Schema-valid but semantically incorrect IR remains an LLM risk. |
| Z2 | Removes the dominant leaf's document reads, repository exploration, tool turns, and authoring-side enforcement overhead. Produces the largest expected per-node wall-time and token reduction. | The `CodegenBundle` is the output boundary; the conductor does not concatenate logic fragments or impose a fixed file count. Repair findings preserve producer context. | Large bundles can hit output limits. Fixed ABI, file-shape, or optimization-unit restrictions can reduce accelerator performance even when correctness passes. |
| Z3 | Reduces evidence-reading and judge-side orchestration cost with low certification risk because the judge is advisory. | The excerpt policy is versioned, complete for routing, and supports bounded named-artifact reads for exceptional cases. | Incomplete excerpts can reduce semantic finding and repair-routing quality even though they cannot change the deterministic verdict. |
| Z4 | Deletes enforcement and contract complexity that exists only because an authoring leaf holds repository tools. Reduces maintenance burden and prompt size. | Gateway logging, schema validation, truncation handling, and stage-specific migration coverage are operational before deletion. | Provides limited direct runtime savings. Premature deletion weakens auditability or removes the only diagnostic path for novel failures. |
| Z5 | Makes recertification proportional to content-invalidated graph closure and enables concurrency, producing the largest scale benefit across hundreds or thousands of nodes. | Contract-input classification, selection policy, atomic publication, revocation, and crash recovery are correct under concurrent execution. | Content invalidation is not semantic equivalence; harmless text changes can still invalidate a closure. Store migration and concurrency correctness are high-complexity work. |
| Z6 | Prevents generated checks from being the sole authority for values produced by the co-generated kernel and strengthens the certification claim. | Capture is bound to integrated kernel state, occurs before generated callbacks can mutate it, and has a target-aware trusted implementation. | Full-state copies and synchronization can distort GPU or distributed performance. Trusted reductions reduce transfer cost but certify only the quantities they compute. |

Sequencing with the R-series: Z0 precedes Z2 and defines the R4 target-matrix generation boundary. Z1 validates the gateway before the dominant Generate migration. Z6's capability ABI is designed with Z0 even when its implementation lands independently, because retrofitting state ownership after accelerator harnesses exist is costly. Z1-Z3 supersede the R6 prompt-prefix-caching item; Z5 supersedes R6 incremental-recertification proper; R6 per-persona model tiering becomes a gateway parameter after Z1-Z3.

### Risks

- **Context-size ceiling.** Inlined context (spec + IR + dependency facts + exemplar + evidence excerpts) can exceed practical prompt budgets for large nodes. Mitigation: explicit per-stage excerpting policies; as a fallback, a host-mediated read-only fetch tool (model requests a named artifact, host serves it) — which preserves A3 because there is still no write path.
- **Loss of leaf self-directed exploration.** An agentic leaf can inspect unexpected state mid-task; a pure function cannot. Mitigation: the deterministic gates + repair-turn loop already carry that role today (the leaf-autonomy surface has been shrinking since G1); unclassifiable failures retain the escalation reviewer.
- **Structured-output fidelity.** Large source files and multi-file bundles as structured output risk truncation, omission, and fencing artifacts. Mitigation: per-file and aggregate size accounting, schema-level file manifests, retry-on-schema-violation at the gateway, and the existing syntax gate as backstop.
- **Optimization-boundary restriction.** A fixed per-node, two-file, synchronous ABI can block helper extraction, loop fusion, common data layout, device residency, and communication overlap. Mitigation: Z0 `CodegenBundle`, A5 optimization units, target lowering plans, and A6 versioned harness capabilities. The host never concatenates algorithm-step fragments.
- **Multi-node optimization blast radius.** A fused optimization unit couples rebuild, execution, and performance behavior across member nodes. Mitigation: single-node units remain the default; a multi-node unit records ordered membership as a derivation input, preserves each public semantic interface, and emits per-node verdicts.
- **Migration-period duplication.** Dual leaf models (CLI + gateway) coexist during Z1-Z3. Mitigation: per-stage cutover with no long-lived dual path per stage; the conductor's stage interface (launch → result → route) is unchanged, only the executor behind it.
- **Correlated kernel/checks defects.** Kernel and checks are co-generated; a kernel defect mirrored in the checks passes stages 4-6. This risk exists identically in the current implementation, where snapshot values are acquired through generated checks getters. Mitigation: the A4 evidence trust boundary — the harness-owned, target-aware primary-state channel and per-test corroboration of secondary evidence (Z6). Accepted residual: decoy kernel state (A4).
- **Certification instrumentation distortion.** Full snapshots or callback synchronization can change accelerator timing and execution order. Mitigation: bind correctness evidence and performance evidence to the same `CodegenBundle`, assembled-source, and binary output hashes; separate the measurement policies in the execute derivation key; and use certified target-owned asynchronous capture or trusted reductions when full host copies are impractical.

## Decision Criteria

- Z0 is accepted only when schema and assembly tests cover multi-file source, a private helper procedure, target capability negotiation, deterministic build-graph derivation, forbidden arbitrary commands, and a multi-node optimization-unit manifest. Z2 does not begin before this contract is pinned.
- Each implementation-bearing Z-item is adopted only with billed A/B E2E comparison against the current executor, reaching `aggregate_verdict=pass` at equal-or-better certified-outcome accuracy. The comparison records first-attempt gate pass rate, repair count, input/output tokens, wall time, and source performance.
- Z2 coverage includes a small single kernel, a dependency-bearing multi-module node, and a target-optimized node. When an accelerator or distributed backend is available, coverage includes its device-residency or communication capability before that backend adopts Z2. Passing only the current synchronous Fortran CPU path does not certify the general `CodegenBundle` boundary.
- A cost reduction is rejected when the generated artifact exceeds the performance-regression tolerance declared by the target profile's versioned performance policy under the same execution environment, even when correctness predicates pass. A target profile without such a tolerance cannot support a performance-preservation adoption claim. Correctness, certified-source performance, workflow wall time, and token cost are separate decision dimensions.
- Cost targets: after Z1-Z3, ≤10 min wall and ≤100k output tokens per certified node (tightening the R1+R2+R5 target of ≤15 min / ≤150k); after Z5, dependency-closure re-certification cost proportional to the invalidated closure only.
- Enforcement retirement (Z4) is complete for a stage only when the deleted surface has no remaining consumer in `tools/` and the unit suite passes without the corresponding fixtures.
- Z5 is complete only when concurrent duplicate requests publish at most one selected eligible output without losing either attempt record, a crash between artifact write and publication is recoverable, revocation removes an output from lookup without deleting it, and a performance-selection-policy change can select a different certified output while downstream keys follow the selected output hash.
- Z6 is complete only when, in addition to the billed E2E pass, adversarial fixtures fail certification: (a) a checks module that reports pass against a defective kernel; (b) snapshot values falsified through the retired getter path — the path must be unreachable from the rendered runner; (c) a registration binding decoy storage that diverges from the integrated state on at least one case — the host-evaluated predicates must fail; (d) a check/metric callback that writes to registered storage after `case_run` — the captured snapshot must be unaffected. A billed E2E pass with the legacy getter path still live does not qualify.
- An accelerator implementation of Z6 additionally demonstrates that trusted capture is bound to the certified `CodegenBundle`, assembled-source, and binary output hashes and that correctness capture overhead is excluded from, or separately identified in, the performance measurement contract.

## References

- `workflow_scaling_redesign.md` (R1-R6)
- `deterministic_followups.md` (G-series, M3, R2/R3-core/R5/R6-lite records)
- `docs/ORCHESTRATION.md`, `docs/workflow/WORKFLOW_CORE.md` (current contracts)
- `docs/SPEC.md` (invariant principles retained unchanged)
