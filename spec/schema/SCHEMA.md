# spec/schema/

## Purpose
`spec/schema/` is the canonical-source location that holds the field-notation rules of workflow artifacts (`algorithm.resolved.yaml`, `derived_contract.json`, etc.) as **declarative JSON Schema**.

A schema here is consumed in one of two ways, and each schema states which in its `x-canonical-validator` target:
- **Runtime driving source.** The validator loads the schema and reads the regex / enum / type information from it at judgment time (`tools/validate_pipeline_semantics.py` and `ir/shape_expr.schema.json`). The grammar is changed by editing the schema alone.
- **Declarative copy.** The validator module holds the same facts as constants and enforces them without file I/O, so a missing or unreadable schema cannot fail-open a running gate (`tools/codegen_bundle.py` and `generate/codegen_bundle.schema.json`). The unit suite enforces that the two copies agree, so the grammar is changed by editing both.

## Scope
- the per-field notation rules of workflow artifacts (e.g. `shape_expr`)
- enumerated values, regex patterns, format constraints
- the whole-document shape of an artifact **that no phase produces yet** (e.g. `generate/codegen_bundle.schema.json`, whose producer arrives with `Z2`). For such an artifact the schema is the declarative copy of the field grammar; the rules a draft-07 pattern cannot carry (closed vocabularies, cross-field invariants) live in the canonical validator module named by `x-canonical-validator`, and the prose contract in the document named by `x-canonical-doc`. State in `x-forbidden-examples-note` which forbidden examples the schema rejects on its own and which the validator rejects.

The structural rules of an artifact that a phase already produces continue to use `docs/workflow/phases/phase_*.md` as the canonical source, and the schema in this directory bears the declarative expression of a part of them.

## Placement rules
- 1 schema = 1 file in the form `spec/schema/<phase>/<field>.schema.json` (e.g. `spec/schema/ir/shape_expr.schema.json`). A whole-document schema uses the document name in place of the field name (e.g. `spec/schema/generate/codegen_bundle.schema.json`).
- Use JSON Schema draft-07. It has no dependency on the `jsonschema` library, and the validator interprets `pattern` with the standard `re` module.
- Anchor a whole-string `pattern` with `^` … `(?![\s\S])`, not `^` … `$`. Under Python `re` (used by the canonical validator via `fullmatch` and by the `jsonschema` library via `re.search`), `$` also matches just before a trailing newline, so `^…$` would let a schema-only consumer accept a value such as `"1.0.0\n"` that the canonical validator rejects; and `\A` / `\Z` fix Python but are invalid in ECMA-262 (Ajv), where `\A` degrades to a literal `A`. The negative lookahead `(?![\s\S])` means "true end of string" and is valid and identical in both engines.
- State "when and where the rule is applied" in the `description` field.
- Note the validator-side entry point in `x-canonical-validator` (extension).
- Note the corresponding document in `x-canonical-doc` (extension).
- Enumerate concrete examples that should be rejected in `x-forbidden-examples` (extension) (for agent learning).

## The contract the validator receives from the schema
The loader (`_load_shape_expr_patterns_cached`) of `tools/validate_pipeline_semantics.py` reads the `oneOf` of `shape_expr.schema.json` and **treats the schema completely as the driving source** as follows:

- Each `oneOf` branch requires a `pattern` (string regex). The validator decides the classification in **2 stages**:
  1. **explicit metadata (recommended)**: note `x-shape-form` (extension) in the branch as one of `"scalar"` / `"list"` / `"tuple"`. The validator trusts this declaration and classifies grammar-independently. This is the recommended path and works explicitly even for a schema whose grammar is unusual (e.g. allowing only identifiers without integer literals).
  2. **probe fallback**: for a branch without `x-shape-form`, the validator tries the canonical probe matrix:
     - scalar probes: `"scalar"`, `"Scalar"`, `"SCALAR"` (case-insensitive scalar literal)
     - list/tuple probes: `[1]`, `[a]`, `[A]`, `[Nx]`, `[1,2]`, `[a,b]`, `[Nx,Ny]`, and the equivalent paren forms
     - if the branch's regex fullmatches any scalar probe → scalar form
     - if the branch's regex fullmatches any list probe → list/tuple form
  3. A branch that cannot be classified by either path (all probes fail and `x-shape-form` is unspecified) is a **malformed schema** and a `RuntimeError`. The error message guides "set `x-shape-form` to resolve the ambiguity".
- For a value matching a list-form branch, the validator strips the outer `[...]` / `(...)`, splits on `,`, and extracts the dim tokens. **Because the dim-token syntax itself is fully governed by the schema's regex**, there is no hard-coded dim-token restriction on the validator side. Any dim-token grammar the schema allows is accepted at runtime.
- `_shape_matches_expr` looks at the split dim tokens and: a numeric literal (`isdigit()`) requires an exact match with the actual value, and any other token (identifier, symbol, etc.) only requires that the same notation appearing multiple times binds to the same actual value (case-sensitive). This is a grammar-non-specific runtime classification and does not restrict what the schema allows as a dim token.

In other words, the schema can declare the grammar of shape_expr that the validator accepts as the **single source of truth**. The grammar can be expanded/contracted by just updating the schema, with no need to edit the validator code (if you want to introduce a new structural form — e.g. a brace form — it is outside the coverage of the validator's structure-classification probes `"[1]" / "(1)"`, so the loader needs to be extended).

## Reference rules
- Cross-reference this schema as the canonical source from documents such as `docs/workflow/phases/phase_01_plan.md`.
- Also reference it from SKILL documents such as `skills/workflow-plan-generate/SKILL.md`.
- The only legitimate reference targets when an agent derives a rule are `docs/` / `spec/` / `skill_must_read_refs` (the validator code is under `tools/`, so referencing it is forbidden). `spec/schema/` is under `spec/`, so it can be referenced.

## Current schema
- `ir/shape_expr.schema.json` — the notation rules for `temporaries[].shape_expr` etc. Limited to the 3 forms `scalar` / `[d1,...]` / `(d1,...)`.
- `generate/codegen_bundle.schema.json` — the whole-document grammar of a `CodegenBundle` (`files[]`, `entrypoints[]`, `target lowering plan`, `capability_requirements`, `state_bindings[]`). Every object is closed; the file-role and `logical_path` rules deny the bundle any build or shell authority. Canonical validator: `tools/codegen_bundle.py:validate_bundle`. Canonical document: `docs/workflow/CODEGEN_BUNDLE_CONTRACT.md`. No phase produces a bundle yet — the producer arrives with `Z2`.
- `generate/harness_capabilities.schema.json` — the shape of a harness capability manifest (the `harness capability ABI` a `CodegenBundle` negotiates against). Canonical validator: `tools/codegen_bundle.py:harness_capability_manifest_violations`. The manifests themselves are tool-side data in `tools/codegen_bundle.py` until the harness spec is next re-specified on content grounds (`Z6`).
