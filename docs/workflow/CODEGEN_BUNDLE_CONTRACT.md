# CodegenBundle contract (`bundle_schema_version` 1.0.0)

> **Scope note.** Under `Z2` (`docs/design/zero_base_architecture.md`) the pure
> `Generate.generate` leaf produces exactly one `CodegenBundle`; the host validates
> it with `validate_bundle` (the post-generate gate), writes the declared files, and
> assembles the build graph. On a residual node that stays on the agentic leaf (a
> `codex` backend, or a node whose runner/Makefile are not host-rendered — the pure
> executor is the only executor since `M-F`, but such a node cannot be expressed as a
> pure producer), no bundle is produced — the agentic `Generate.generate` leaf writes
> the Fortran sources directly, as described in
> `docs/workflow/phases/phase_02_generate.md` together with `CHECKS_MODULE_CONTRACT.md`.
> This document is the canonical contract for the bundle document itself; the schema
> (`spec/schema/generate/codegen_bundle.schema.json`) and the validator module
> `tools/codegen_bundle.py` are pinned at `bundle_schema_version` 1.0.0.

## Purpose

A `CodegenBundle` is the typed output of target-specific code generation for one
`optimization unit`. It carries the complete generated source-file set, the file
roles, the externally visible `entrypoints`, the `target lowering plan`, the
`capability_requirements` against the `harness capability ABI`, and the
`state_bindings`. The host derives the build graph from the bundle's declared roles
and capabilities; the bundle never carries build or shell commands.

The contract exists so the model retains freedom over internal code organization
(multiple compilation units, private helper procedures, internal modules,
target-specific execution algorithms) while host assembly stays deterministic and
auditable, and so a semantic `node` boundary does not become a mandatory
optimization boundary.

## Scope

- Canonical: the serialized bundle document, its field grammar, its cross-field
  invariants, the optimization-unit membership rules, the capability ABI and its
  negotiation rule, and the build-graph derivation contract.
- Declarative form: `spec/schema/generate/codegen_bundle.schema.json` and
  `spec/schema/generate/harness_capabilities.schema.json` (JSON Schema draft-07).
- Canonical validators: `tools/codegen_bundle.py` — `validate_bundle` for the bundle
  document, `harness_capability_manifest_violations` for the harness capability manifest
  document. The schema is the
  declarative copy of the field grammar a draft-07 pattern can carry; the module is the
  enforcement point and additionally holds the rules a pattern cannot express — the
  reserved build filenames, the forbidden build/script extensions, the language extension
  allowlist, the capability vocabulary, and every cross-field invariant. Validation itself
  reads only the module constants, so a missing or unreadable schema file cannot fail-open
  a running gate; the unit suite enforces that the two copies agree.
- Out of scope: how a bundle is requested from a model (the pure-leaf launch prompt,
  `docs/workflow/LAUNCH_PROMPT_REFERENCE.md`), how repair turns are routed
  (`docs/ORCHESTRATION.md`), and how the build graph is turned into commands (a
  target-backend concern).

### Terminology disambiguation: two unrelated uses of "capability"

- **agent capability token** — the per-`agent_run` secret at
  `workspace/orchestrations/<orchestration_id>/capabilities/<agent_run_id>.json` that
  authorizes MCP calls. It is an authorization credential.
- **harness capability** (this document) — a declared feature of a target harness ABI
  (`sync_single_case@1`, `state_registration@1`, …). It is a code-generation contract
  term and carries no authority.

The two share only the English word. Every identifier in this contract that refers to
the second sense lives in the `harness_capabilit*` / `capability_requirements` /
`CAPABILITY_VOCABULARY` namespace; nothing here reads or issues an agent capability
token.

## Serialization

A bundle is a single JSON document. `bundle_schema_version` is required and is
compared by major version: `CODEGEN_BUNDLE_SCHEMA_VERSION = "1.0.0"`, and a document
whose major version differs from the module's is rejected without further inspection.
Every object in the document is closed (`additionalProperties: false`) with **one declared
exception**: the value objects of `target_lowering_plan` (`precision`, `data_layout`,
`parallelization`, `decomposition`, `communication`, `accelerator_mapping`), whose interiors
are the `A5` target-backend extension point and are not constrained in v1. Everywhere else
an unknown key is a violation.

**Version compatibility is backward only, by design.** The major-version comparison lets a
*later* validator read an *earlier* same-major document (a `1.3` validator accepts a `1.0`
bundle). It does **not** make an earlier validator forward-compatible with a later minor: a
minor release is additive, so a `1.1` document may carry a field a `1.0` validator does not
know, and closed-object validation rejects that field. This is intentional — the closure is a
load-bearing security property (a command cannot ride in on an unknown key) and is never
relaxed for forward compatibility. A producer that emits a new minor's fields therefore
requires a validator of that minor; it does not silently pass an older one. The declarative
schema's `bundle_schema_version` pattern pins the supported major (`^1\.[0-9]+\.[0-9]+$`), so a
schema-only consumer (structured generation) rejects an incompatible major at the schema
boundary rather than admitting it to fail later at `validate_bundle`.

That closure is one of the three facts the no-arbitrary-command guarantee rests on (see
"Design Policy: the command prohibition is structural"), and the exception does not weaken
it: no consumer executes a lowering-plan value, and the derived build graph has no command
slot. A `Z2` target backend that synthesizes flags from a plan interior treats that interior
as untrusted model-authored input, exactly as it treats `files[].content`.

```json
{
  "bundle_schema_version": "1.0.0",
  "optimization_unit": {"members": ["problem/adv1d@0.1.0"]},
  "files": [{"logical_path": "adv1d_model.f90", "role": "model",
             "language": "fortran", "member_node_key": "problem/adv1d@0.1.0",
             "content": "module adv1d_model\n..."}],
  "entrypoints": [{"symbol": "adv1d__apply", "kind": "operation",
                   "node_key": "problem/adv1d@0.1.0", "defined_in": "adv1d_model.f90"}],
  "target_lowering_plan": {"precision": {"real_kind": "real64"},
                           "state_residency": "host"},
  "capability_requirements": ["sync_single_case@1"],
  "state_bindings": [{"node_key": "problem/adv1d@0.1.0", "state_variable": "q",
                      "storage_symbol": "adv1d_checks__get_r1",
                      "capture": "checks_getter", "capability": null}]
}
```

`files[].content` is inline. A detached-content variant (a content hash plus a
side-channel payload) is not part of v1; if `Z2` requires one it is added as a minor
version bump. Per the compatibility rule above, a `1.0` validator does not read a bundle
that uses the new detached field — the producer and validator move to that minor together. A
`1.0` producer keeps emitting inline `content`, which every same-or-later-major validator
still accepts.

## Optimization unit

`optimization_unit.members` is an ordered list of `node_key`
(`<spec_kind>/<spec_id>@<spec_version>`), of length at least 1, without duplicates. A
single-member unit is the default and is the only shape the pure `Generate.generate`
producer emits on a live workflow; assembly rejects a multi-node unit
(`bundle_shape_unsupported`). A multi-node unit is exercised by the validator unit
suite only.

The ordered member list **is** the unit's identity. No derived `unit_id` is stored:
the order is itself a contract input to code generation (a fused unit's member order
fixes the generated execution order), so a separate identifier would only be a second
copy of the same fact.

A multi-member unit preserves each member's external semantic interface and its
verification predicates. It permits internal fusion, shared intermediate values, and a
common data layout across members. Consequently every member must remain
independently addressable in the generated code: each member requires at least one
`model`-role file and at least one `operation` entrypoint, defined in a file belonging to that
same member (an entrypoint attributed to another member's file would satisfy the count while
leaving the member unaddressable). Operation **cardinality is by node kind**: a `problem` node
publishes **exactly one** operation (its single integration update path — the contract carries
no operation selector, so two would leave the host unable to pick it); a `component` or
`infrastructure` node publishes an API of one or more operations (the harness ABI is many); and
a `profile` publishes **exactly zero** operations (it is consumed through its selection result,
not a call — `phase_02_generate.md`), so an `operation` entrypoint on a profile member is an
invented callable interface and is rejected.

## Files

`files[]` is non-empty. Each entry declares `logical_path`, `role`, `language`,
`member_node_key`, `content` (non-empty), and `modules`, and may declare `compile_after`.

### `modules`

`modules` is the non-empty list of Fortran modules the file defines. A module name is unique
across the whole bundle (one `.mod` per module; compared case-insensitively). This list is
what ties an `entrypoints[].module` or a `state_bindings[].module` to the file that owns it —
and, through the file's `member_node_key`, to a member: without it, an attribution that names
the member's own file in `defined_in` could still route the rendered `use <module>, only:
<symbol>` to another member's export. The host reads `modules` to build the `use`-graph; it
never parses the source.

### Roles

`role` is one of the closed set:

| role | meaning | may hold an entrypoint |
|---|---|---|
| `model` | the physics kernel and the published operation of a member | yes (`operation`) |
| `checks` | the member's checks module (`CHECKS_MODULE_CONTRACT.md`) | yes (`checks_interface`) |
| `helper` | a private procedure set the generated code calls internally | no |
| `internal_module` | an internal module (shared types, parameters, work arrays) | no |

There is **no runner or glue role**: contract-boundary glue is host-rendered
(`tools/runner_renderer.py`) and can never be bundle content. There is **no build or
script role**: this is the backbone of the no-arbitrary-command rule.

`member_node_key` is either one of the unit's members or `null`. `null` means the file
is shared by the whole unit, and only `helper` / `internal_module` may be shared — a
`model` or `checks` file belongs to exactly one member.

**Privacy invariant.** A `helper` or `internal_module` file cannot be the `defined_in`
of any entrypoint. This *is* the definition of "private" in this contract: privacy is
declared by role, not inferred from a Fortran `private` statement.

### `logical_path`

Every `logical_path` is relative to the assembled source root, normalized, and unique
within the bundle. The rules (canonical implementation:
`tools/codegen_bundle.py:logical_path_violations`):

- POSIX separators only; a backslash is rejected.
- Already normalized: `posixpath.normpath(p) == p`. This rejects `./x.f90`, `a/../b.f90`,
  `a//b.f90`, a trailing `/`, and the empty string.
- Relative and confined: no leading `/`, and no `.` / `..` segment anywhere.
- Each segment matches `[A-Za-z0-9_][A-Za-z0-9_.-]*`.
- The extension is the one allowed for the file's `language`
  (`LANGUAGE_EXTENSION_ALLOWLIST`; `fortran` → `.f90`).
- The basename is not a reserved build filename (`Makefile`, `makefile`, `GNUmakefile`,
  `CMakeLists.txt`, `configure`) and the extension is not a build/script extension
  (`.sh`, `.bash`, `.mk`, `.cmake`, `.py`).
- Paths are unique **after case folding**, so a bundle cannot depend on a
  case-sensitive filesystem to keep `A.f90` and `a.f90` apart.
- No two paths derive the same object name, compared case-folded (see "Build-graph
  derivation"): `a/b.f90` and `a__b.f90` both flatten to `a__b.o`, and a colliding pair
  would compile as one object and silently drop the other from the link.

### `compile_after`

`compile_after` is an optional array of other files' `logical_path`s that must compile
before this file — the case where a file `use`s a module another bundle file defines.
Role precedence (see "Build-graph derivation") orders files of *different* roles; two files
of the *same* role (two `internal_module`s, for instance) are ordered by `compile_after`.
Each entry must resolve to another `files[]` entry, must not name the file itself, and the
whole edge set must be acyclic. An edge may order files **within** a role or **agree with**
role precedence; it must never **reverse** it — a `model` file cannot declare
`compile_after` on a `checks` file, because `ROLE_BUILD_PRECEDENCE` already orders the model
first and the checks module `use`s it. The host never infers these edges by parsing the
source; the bundle declares them, and `derive_build_graph` topologically sorts by them.

### Language

`language` is `fortran` in v1. A new language is added by extending the schema enum and
`LANGUAGE_EXTENSION_ALLOWLIST` together.

## Entrypoints

`entrypoints[]` is non-empty. Each entry declares `symbol` (an identifier), `module` (the
Fortran module that publishes `symbol`), `kind` (`operation` or `checks_interface`),
`node_key` (a unit member), and `defined_in` (the `logical_path` of a file in this bundle).
An identifier — `symbol`, `module`, and in `state_bindings` (`state_variable`,
`storage_symbol`, `module`) — is a Fortran identifier of at most `FORTRAN_IDENTIFIER_MAX`
(63) characters, the f2008/f2018 limit; a longer name is rejected here rather than deferred to
the `Generate.syntax` compiler gate.

`module` exists so the host renders the boundary glue `use <module>, only: <symbol>`
mechanically. A file may define several modules or name them freely, and the host never
parses the generated source to discover which module publishes a symbol — the bundle
declares it.

- `kind: operation` must be defined in a `model`-role file; `kind: checks_interface`
  in a `checks`-role file.
- `defined_in` must resolve to an existing `files[]` entry.
- **Attribution invariant**: the file named by `defined_in` belongs to the entrypoint's
  own `node_key` (`files[].member_node_key == entrypoints[].node_key`), **and** `module` is
  one that `defined_in` declares in its `modules`. Both together stop an entrypoint that names
  the member's own file in `defined_in` while `module`/`symbol` route the rendered `use` to
  another member's export.
- **Symbol uniqueness**: a symbol is published at most once **per module**, compared
  case-insensitively (Fortran is case-insensitive in both). A symbol is module-qualified, so
  each member's checks module legitimately exports the same fixed ABI name (`case_run`,
  `get_r1`) — `a_checks::case_run` and `b_checks::case_run` are distinct procedures. Only the
  same name in the same module is an unlinkable duplicate.
- **Coverage invariant**: every unit member owns at least one `model` file and an
  `operation` entrypoint count set by its kind (see "Optimization unit"): a `problem` member
  **exactly one**, a `component` / `infrastructure` member **at least one**, a `profile` member
  **exactly zero**. A member may have any number of `checks_interface` entrypoints — the checks
  surface is a fixed ABI.

`entrypoints` plus `state_bindings` are the structural anchors that replace
signature-shape heuristics: the published update path of a node is a declared field,
not something recovered by counting `intent(out)` dummy arguments
(`docs/design/deterministic_followups.md`, "Problem state-array usage").

## Target lowering plan

`target_lowering_plan` is a **closed key envelope with open values**: the set of keys is
fixed by this contract, and what a key's object contains is not constrained in v1 (an
`A5` extension point — a target backend defines the interior when it needs one).

- Required: `precision` (object), `state_residency` (one of `host`, `device`,
  `distributed`).
- Optional: `data_layout`, `parallelization`, `decomposition`, `communication`,
  `accelerator_mapping` (each an object), and `fusion` (an array; each element's
  `members` must be a subset of the unit's members).
- An unknown top-level key is rejected.

**Coupling invariant.** `state_residency` other than `host` requires the corresponding
**residency capability** in `capability_requirements`: `device` requires an
`async_device_resident@N` token, `distributed` requires a `distributed_state@N` token. A
lowering plan cannot claim a residency the target has not been asked to provide. This is
in addition to — not instead of — the execution-model requirement below: a `distributed`
bundle declares `distributed_state@N` **and** exactly one execution-model token, because
`distributed_state` is a residency capability and states nothing about how cases are
driven.

## Harness capability ABI

`HARNESS_CAPABILITY_ABI_VERSION = 1`. A capability token is
`^[a-z][a-z0-9_]*@[0-9]+$`: a name from the closed `CAPABILITY_VOCABULARY` and an
integer version. The version is part of the token, not a range.

| capability | meaning |
|---|---|
| `sync_single_case` | synchronous one-case-at-a-time execution; the current `harness_fortran_cpu` ABI |
| `async_device_resident` | device-resident state with asynchronous capture (reserved) |
| `distributed_state` | distributed state across ranks (reserved) |
| `batched_cases` | the harness drives several cases per invocation (reserved) |
| `full_state_capture` | the harness captures full snapshots itself (reserved, `A4`/`Z6`) |
| `trusted_reductions` | the harness computes certified reductions over state (reserved, `A4`/`Z6`) |
| `state_registration` | generated code registers state storage the harness reads (reserved, `Z6`) |

`capability_requirements` is a duplicate-free list of tokens. A name outside the
vocabulary is a violation (fail-closed: an unrecognized capability is never treated as
"probably available"). Exactly one **execution-model** capability
(`sync_single_case`, `async_device_resident`, `batched_cases`) is required, so every
bundle states how it expects to be driven.

### Provided capabilities and negotiation

`HARNESS_CAPABILITY_MANIFESTS` maps a harness `node_key` to the capability set it
provides. It is tool-side data, not a field of the harness `controlled_spec.md`: adding
it to the spec would edit a certified artifact and force recertification for no change
in generated behavior. When the harness spec is next re-specified on content grounds
(`Z6`), the manifest moves into a `§capabilities` section of the spec and this table
becomes its projection.

```
"infrastructure/harness_fortran_cpu@0.4.0": {"sync_single_case@1"}
```

`sync_single_case@1` is defined as exactly the canonical interface block of
`harness_fortran_cpu@0.4.0` §5.1 (13 operations, 5 published types, `dp = real64`,
`case_id_len = 64`). The mechanical enforcer of that definition remains
`tools/runner_renderer.py:assert_harness_pin`, which compares §5.1 against the certified
harness IR's `public_api.signatures` and the generated harness source; this contract
adds a name for the ABI, not a second checker of it.

### The manifest document

The manifest set has a document form, `spec/schema/generate/harness_capabilities.schema.json`
(canonical validator: `tools/codegen_bundle.py:harness_capability_manifest_violations`;
`harness_capability_manifest_document()` renders the tool-side table into it). It is the
shape a spec-side `capabilities` section takes when the manifest moves into the harness spec
at `Z6`:

- `harness_capability_abi_version` — the ABI generation, pinned to
  `HARNESS_CAPABILITY_ABI_VERSION` (1). It is a pin, not a floor: a later-generation manifest
  is rejected rather than read under this generation's semantics, mirroring the terminal
  `bundle_schema_version` major mismatch on the bundle side.
- `manifests[]` — one entry per harness, each `{node_key, provides}`. `node_key` is an
  `infrastructure` node_key (only an `infrastructure` node provides capabilities) and appears
  at most once. `provides` is a non-empty, duplicate-free list of tokens whose names are in
  `CAPABILITY_VOCABULARY`.
- Every object is closed, as in the bundle document.

Negotiation is two pure functions and is fail-closed:

- `harness_provided_capabilities(node_key)` returns the declared set, or `None` for a
  harness with no manifest entry. `None` means "nothing is provided": an undeclared
  harness satisfies no requirement.
- `unsatisfied_capability_requirements(required, provided)` returns the required tokens
  that are not provided, matching on the **exact `name@version` token**. Version ordering
  is not assumed to imply compatibility: a bundle requiring `sync_single_case@2` is not
  satisfied by a harness providing `sync_single_case@1`. Compatibility is declared by
  adding a token to a manifest, never inferred. A `required` value that is not a **non-empty
  token collection** — `None`, a bare string, a number, a mapping (whose iteration yields
  keys), or an empty collection — is reported as unsatisfiable, never as "nothing required";
  a mapping supplied as `provided` provides nothing. The failure direction of a negotiation
  gate is always closed.

Assembly fails closed when any required capability is unsatisfied.

## State bindings

`state_bindings[]` records how each member's primary state is reached. It may be empty
in v1. Each entry declares `node_key` (a unit member), `state_variable`, `storage_symbol`,
`module`, `capture`, and `capability`. `(node_key, state_variable)` is the identity
of a member's primary state and is **unique** across the array: a second binding for the same
pair would leave the mapping ambiguous (two consumers could register or read different storage
for one declared state). The same `state_variable` name on two distinct members is allowed —
the identity is the pair, not the name.

`module` is the Fortran module that publishes `storage_symbol`, so the host renders
`use <module>, only: <storage_symbol>` mechanically (as for an entrypoint). It must be a
module a `checks`-role file **owned by the binding's own `node_key`** declares — for **either
capture**. Otherwise a binding for member A could name member B's checks module and
capture/register B's storage as A's state, producing incorrect verification evidence with no
compile or link failure. The exporting module is declared, never inferred from the source.

- `capture: checks_getter` — the value is read through the generated checks module's
  snapshot getters. This is the current (`M3c`) mechanism, and it takes
  `capability: null` because no harness capability is involved. Here `storage_symbol` is a
  rank getter (`get_r1`) that dispatches on the variable name, so several same-rank variables
  legitimately share one `storage_symbol`; they are disambiguated by `state_variable` at the
  call.
- `capture: harness_registration` — the generated code registers storage the harness
  reads directly. It requires a `state_registration@N` token in `capability`, and the
  same token must appear in `capability_requirements`. This is the shape `Z6` adopts;
  the schema already admits it, so `Z6` is additive (add `state_registration@1` to the
  harness manifest and switch the default `capture`), with no schema change. Here
  `storage_symbol` is the actual registered storage, so `(module, storage_symbol)` is
  **unique** across `harness_registration` bindings — two states registering one storage
  would silently capture the same evidence for both.

The coupling holds in **both directions, per token**: each `state_registration@N` token in
`capability_requirements` requires a `harness_registration` binding whose `capability` is
that same token. A declared `state_registration@2` is not licensed by a binding that uses
`state_registration@1`; otherwise the bundle negotiates an ABI wider than the code it ships
actually uses.

Agreement between `state_bindings[].state_variable` and the IR's
`algorithm.state_variables` is checked at assembly time (`Z2`), where the IR is in
scope. This contract validates the bundle in isolation and therefore does not check it.

## Build-graph derivation

`derive_build_graph(doc, *, dependency_closure, toolchain, host_glue_sources)` returns
pure data under exactly three keys:

- `compile_units[]`, each `{source, object, prerequisite_objects}`. `source` is prefixed
  by origin: `staged:` (a dependency source the host stages), `bundle:` (a bundle file),
  `glue:` (a host-rendered file).
- `link.objects`, the ordered link line.
- `toolchain`, the caller's `impl_defaults.toolchain` **projected onto a fixed declarative
  allowlist and value-validated** (`TOOLCHAIN_ECHO_KEYS`: `language`, `standard`,
  `build_system`, `compiler`, `linker`, `backend`) for the target backend. The IR's toolchain
  object is not closed, so the graph projects rather than echoes verbatim: only the
  declarative key set keeps the "no command and no flag string" guarantee structural.
  `compiler` and `linker` are **executable selectors** a backend runs as a program, so
  freedom from shell metacharacters is not enough (a bare `sh`, an absolute `/tmp/payload`, or
  a traversal `a/../b` is still runnable). They are carried only when they name a **recognized
  compiler/linker driver for the bundle's language** (`COMPILER_SELECTOR_FAMILIES_BY_LANGUAGE`)
  — a bare program name with no path separator (the backend resolves it on a trusted PATH),
  optionally version-suffixed and prefixed by a **target triple that begins with a known CPU
  architecture** (`COMPILER_TARGET_TRIPLE_ARCHES`), so `gfortran`,
  `x86_64-linux-gnu-gfortran-12`, `frt`, and `frtpx` are kept for a Fortran bundle while a
  driver for the wrong language (`gcc`, `g++`, `clang`) is dropped — the backend pins it as `FC`
  and it would deterministically fail on `.f90` — and an arbitrary prefix that merely ends in a
  family name (`payload-gfortran`, `sh-gfortran`) is dropped too. An unrecognized selector is
  dropped and the backend uses its default (`gfortran`); a new compiler, architecture, or
  language adds its driver family set. The other declarative fields are carried only as single
  tokens without whitespace or shell metacharacters. This keeps the graph free of any runnable
  command even though its input is an open, LLM-authored IR object.

There is **no command string anywhere in the returned graph**. Command synthesis belongs
to the target backend (`Z2`); a graph that could carry a command would reintroduce the
build authority the file-role rules exist to deny.

Ordering is derived from roles and the bundle's declared `compile_after` edges — never from
`use`-statement analysis of the generated Fortran:

1. the dependency closure, deepest first, as **`node_key`s** (`_dependency_closure_nodes`
   semantics) — a bare `spec_id` (the shape `_dependency_closure` returns) is rejected, since it
   would derive an empty spec_id and emit a corrupt `staged:_model.f90` — **minus any
   dependency that is itself a member of this optimization unit**. A member's implementation is
   generated inside the bundle (its own `model` file), so staging
   it from the closure would collide on `<spec_id>_model.o` or link two implementations of the
   same member. The caller may pass the whole closure; `derive_build_graph` excludes the
   members by **exact `node_key`** (not bare `spec_id`), so a distinct dependency that only
   shares a `spec_id` with a member — a different kind or version — is kept, and the
   `<spec_id>_model.o` basename collision it then forms is surfaced loudly rather than
   silently omitting an implementation. The staged deps compile before the bundle; a staged
   dependency that **depends on** an absorbed member would `use` a module the bundle now
   provides but compile before it — an unbuildable `bundle → staged → bundle` straddle.
   Detecting it needs actual dependency **edges**, not the flat closure's order (that order is a
   global topological sort, and two independent branches can appear as `(member, staged)` with
   no dependency between them). When the caller supplies `dependency_edges` (from the
   dependency-graph sidecar) a proven straddle fails closed; absorb the dependent into the unit,
   or leave the member unfused. When `dependency_edges` is **not** supplied the straddle check
   is skipped (fail-open) — deliberately: it is a build-ordering **refinement**, not a security
   boundary, so a false reject of an independent-branch closure is worse than a skipped check.
   The safety property it protects (a buildable order) is not the closure's to guarantee; the
   bundle's own acyclicity is enforced by `compile_after` (the cycle check) regardless;
2. the bundle files by `ROLE_BUILD_PRECEDENCE = (internal_module, helper, model, checks)`,
   tie-broken by unit-member order and then by `logical_path` lexical order. A unit-shared
   file (`member_node_key: null`) precedes every member-specific file of the same role: it
   is what they may `use`. This base order is then refined by a **stable topological sort**
   over `compile_after`, so a file compiles after every bundle file it declares a dependency
   on — the case (two files of one role) that role precedence alone cannot order. The base
   order is the sort's deterministic tie-break;
3. the host glue last.

`prerequisite_objects` is the conservative total order (each unit depends on every unit
before it), which is the same safe convention the current deterministic Makefile uses, and
which — combined with the `compile_after` topological order — guarantees every declared
dependency is already built.

An object name is derived from its source path: the extension becomes `.o` and any `/` is
flattened to `__` (`core/util.f90` → `core__util.o`), so a flat `<name>.f90` keeps the
`<name>.o` the current Makefile uses. Derivation **fails closed** when two sources of any
origin derive the same object name. Within the bundle that is already a validation
violation; across origins only assembly can see it, and it is the case that matters: a
bundle file at the host-rendered runner's path would otherwise overwrite the glue object
and so capture the contract boundary the "no runner/glue role" rule denies it.

Derivation **also fails closed on a Fortran module-name collision** across origins: a bundle
file may declare a `modules` name equal to a staged dependency's derived `<spec_id>_model`
module even when the object names differ, and two definitions of one module overwrite the
dependency's `.mod`. `validate_bundle` enforces module uniqueness only within the bundle; the
closure's module names are a host input only assembly holds. (A unit member is excluded from
the staged closure, so the bundle's own `<spec_id>_model` module never false-collides.)

**Determinism contract.** Permuting the input order of `files[]` does not change the
result: `json.dumps(graph, sort_keys=True)` is byte-identical. This is what lets the
graph become a derivation input in `Z5`.

**Parity.** For a bundle of the `M3c` shape (one member, `model` + `checks`, a
dependency closure, host-rendered runner glue), the derived object order equals the
object order of the IR-shaped Makefile the conductor renders via `_write_makefile`
(dependency objects → model → checks → runner). `_write_makefile` remains the live
Makefile author for Model B dependency closures and for non-`M3c` agentic leaves, so it
is not dead code. That equality is what the parity test pins: it compares
`derive_build_graph(...)["link"]["objects"]` against the object list parsed out of the
`_write_makefile`-authored Makefile. Under `Z2` a pure `M3c` node renders its Makefile
from this derived graph (`_render_pure_makefile_from_graph`) while `_write_makefile` is
unchanged, so the two renders are **not** byte-identical
(they differ in header comments and in whether object paths are carried by
`MODEL_SRC` / `MODEL_OBJ` variables or inlined per compile unit). What both must agree
on is the derived build graph — the object set and its order — plus the overridable
`FC` / `OBJDIR` / `BINDIR` / `BIN` / `SPEC` / `CASES` surface and the `test` / `clean`
targets that `Build` and `Validate.execute` drive.

### Design Policy: the command prohibition is structural

**Scope of the guarantee.** The prohibition is about the **host-side assembly** of a bundle:
nothing a bundle declares can inject a command into the build the host derives and runs. It is
**not** a claim that the compiled program is harmless — generated Fortran can call
`execute_command_line`, `system`, or open files, exactly as any leaf-authored source can today.
That runtime behavior is contained by the **separate execution sandbox** the workflow already
applies to Build/Validate (`bwrap`), not by this contract. Z0 does not change the runtime trust
model; it fixes the *authoring/assembly* boundary.

Within that scope the prohibition rests on three structural facts and nothing else: the
document is closed outside the declared `target_lowering_plan` extension point (no *named*
field carries a command), the role and path rules reject build and script files, and the
build-graph type has no command slot — so nothing a bundle declares reaches a shell **during
assembly**. `files[].content` is **not** scanned for shell-looking strings, and neither is a
lowering-plan interior. A Fortran source legitimately contains string literals (error messages,
format strings, file names) and legitimate calls, so a content scan is a false-positive source
that adds no guarantee the three structural rules give and cannot soundly bound runtime
behavior anyway. What a target backend does with model-authored text — `files[].content`, a
lowering-plan interior — is the backend's own input-handling contract: it compiles the source
and never passes such text to a shell unquoted during assembly.

## Decision Criteria

- A bundle is valid when `validate_bundle(doc)` returns an empty list. It reports schema
  (presence / type / enum / closed-key) violations first and evaluates the cross-field
  invariants only on a structurally sound document, so an invariant check never has to
  defend against a missing or mistyped field.
- A bundle whose `bundle_schema_version` major differs from
  `CODEGEN_BUNDLE_SCHEMA_VERSION` is rejected, and no other check is reported for it.
- Assembly of a valid bundle fails closed when `unsatisfied_capability_requirements` is
  non-empty for the target harness.
- Violation clauses are prefix-free (the `tools/meta_contracts.py` idiom): the caller
  supplies the artifact path or field prefix in its own reporting idiom.
