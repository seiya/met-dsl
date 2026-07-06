# Requirements and format of the Controlled Spec (canonical source)

## Purpose
The `Controlled Spec` is the project's **sole physics-specification canonical source**, and must simultaneously satisfy the following.
- It can be read and understood by a domain researcher.
- It can be **deterministically** converted into the subroutine groups (`model`) that implement the computation task defined by each `spec`, and the `runner` responsible for input/output, execution, and judgment coordination.

## Role separation (most important)
This project divides the specification into the following 2 layers.

1. `Controlled Spec` (this document)
- The `Controlled Spec` has a `spec_kind` and is classified into the 4 kinds `problem` / `component` / `profile` / `infrastructure`.
- `problem` defines the equation system to be integrated and the runtime input contract, and references the dependent `component` and adopted `profile`.
- `component` defines the input/output contract of a reusable physics operation and the published `operation`.
- `profile` defines the selection rules and parameter constraints for a `component`.
- `infrastructure` (R1 harness) defines the shared runner plumbing (argv/case parsing, case-loop driver, JSON/snapshot/perf emission) as a certified node per `(language, hardware)` target, carrying no physics.

2. `tests` (`tests.md`)
- It describes the input conditions used in verification (initial conditions, execution conditions, case expansion) and the judgment thresholds.
- `tests` applies to all of `problem` / `component` / `profile` / `infrastructure`.
- The description discipline uses `TESTS.md` as the canonical source and is defined natural-language-first.

Note:
- The execution module is not test-only. In production execution of scientific computation, the user can design the runtime input according to their purpose.
- `tests` is the document that defines, among that runtime input, the "default profile used for verification".
- Regardless of language, the output separates `model` (physics computation) and `runner` (execution / judgment coordination), with a structure in which the `runner` calls the `model`.

Boundary rules:
- Do not write the internal implementation procedure of a `component` in a `problem spec`. Define it by reference to `component_id` and `profile_id`.
- Do not write case-specific settings (`nx` sweep, `t_end`, the `case_id` group, etc.) in a `component spec`.
- Do not write the new introduction of an equation definition or conserved-quantity update expression in a `profile spec`.
- Do not write the discretization-scheme definition itself in `tests`.

## Basic policy (natural-language-first)
- The main vehicle of description is **natural language**.
- Limit structured blocks (`YAML` / `JSON` / tables) to **places that would be ambiguous with natural language alone**.
- For the `Markdown` math notation, use `$...$` for inline and `$$...$$` for block, and do not use `\(...\)` or `\[...\]`.
- Forbid completion of omissions by the `LLM` or a converter. Treat **a shortage as an error**.
- Fix the physics algorithm (A) in the `Controlled Spec`, and handle the execution algorithm (B) in the `impl_defaults` section of `spec.ir.yaml`.

## Description format (fixed template)
Place **0. Meta information** at the top. The subsequent sections are fixed per `spec_kind`.

0. **Meta information (fixed at the top)**
- Required statement: state `spec_id`, `spec_version`, `status`, `spec_kind`, `domain`, and `family` at the top of the document.

### Required sections of a `problem spec`
1. **Problem definition**
- Required statement: state the target equations, conservative / non-conservative form, target variables, and physical assumptions in prose.

2. **Definition of variables and coordinates**
- Required statement: for each variable, state "name, meaning, placement, unit" in prose.
- Required statement: state the coordinate system and dimension in prose.

3. **Type definition of domain and boundary conditions**
- Required statement: state the domain type and the boundary-condition algorithm in prose.
- Required statement: state the items that are variable as runtime inputs (`runtime inputs`).
- Required statement: state that at verification time `tests` provides a partial profile of the runtime input.

4. **Dependent `component` and adopted `profile`**
- Required statement: state the referenced `component_id` and `profile_id`, the application order, and the compatibility constraints.

5. **Integration algorithm**
- Required statement: state the `component` call order, data passing, and time-update order.

6. **Model parameters and the runtime input contract**
- Required statement: state the fixed / variable physical constants, units, and default values.
- Required statement: enumerate the runtime input contract (initial conditions, end time, step rules, etc.).

7. **Prohibitions**
- Required statement: state the unsupported features, the prohibition of implicit completion, and the handling of undefined parameters in prose.

8. **Traceability**
- Required statement: state `spec_version`, the referenced literature / basis, and the correspondence rule for the determined values that fall into the `case` section of `spec.ir.yaml`, in prose.

9. **tests reference**
- Required statement: state the reference path of the corresponding `tests.md` and the `test_profile_version`.

10. **AD preparation information**
- Required statement: state the value of `ad_readiness.enabled` and, when `true`, the required information, in prose.

### Required sections of a `component spec`
1. **Responsibility and scope**
- Required statement: state the operation responsibility the `component` bears, the out-of-scope responsibilities, and the premise of the input state.

2. **input/output contract**
- Required statement: state the input variables, output variables, array placement, units, dimensions, and boundary handling.

3. **Operation definition**
- Required statement: state, per published `operation`, the equations, discretization, and selection rules.

4. **Failure conditions and constraints**
- Required statement: state the judgment conditions for invalid input, the error-termination conditions, and the handling of out-of-tolerance.

5. **Public API and compatibility**
- Required statement: state the `operation_id` list, the `major` / `minor` update rules, and the backward-compatibility policy.

6. **Prohibitions**
- Required statement: state the rules that forbid automatic switching, implicit completion, and silent ignoring of out-of-spec input.

7. **Traceability**
- Required statement: state the correspondence rule for the determined values that fall into `component_catalog.yaml` and the `case` section of `spec.ir.yaml`.

8. **tests reference**
- Required statement: state the reference path of the corresponding `tests.md` and the `test_profile_version`.

9. **AD preparation information**
- Required statement: state the non-differentiable operations, gradient-excluded operations, and branching rules.

### Required sections of a `profile spec`
1. **Target `component` and compatibility range**
- Required statement: state the target `component_id`, the target `operation_id`, and the applicable `major` range.

2. **Selection rules**
- Required statement: state the application conditions, priority, and exclusion conditions.

3. **Parameter constraints**
- Required statement: state the default values, allowed ranges, units, and derivation rules.

4. **Fallback rules**
- Required statement: state the alternative selection when a condition is not met, the prohibition conditions, and the error conditions.

5. **Traceability**
- Required statement: state the correspondence rule for the keys and values fixed into the `case` section of `spec.ir.yaml`.

6. **tests reference**
- Required statement: state the reference path of the corresponding `tests.md` and the `test_profile_version`.

### Required sections of an `infrastructure spec` (R1 harness)
1. **Responsibility and scope**
- Required statement: state the runner plumbing this harness provides (argv/`--cases` parsing, the case-loop driver, JSON/snapshot/perf/`metrics_basis` emission) and the `(language, hardware)` target it serves; state explicitly that it carries no physics.

2. **Published harness API contract**
- Required statement: state the `<spec_id>__*` operations a physics-node runner calls (e.g. case-set parse, per-case snapshot/JSON writers, rank-N array emitters, perf/diagnostics/metrics_basis writers), with each operation's argument roles.

3. **Runner output contract produced**
- Required statement: state the `diagnostics.json` / `perf.json` / `raw/*` (snapshot / `metrics_basis.json`) shapes the harness emits, and the numeric/boolean descriptor rules (per `docs/workflow/RUNNER_OUTPUT_CONTRACT.md`).

4. **Failure conditions and constraints**
- Required statement: state the guard behaviors (e.g. missing `--cases` aborts) and the invariants the harness enforces.

5. **Prohibitions**
- Required statement: state what the harness must NOT do (embed physics, write `verdict.json` / `aggregate_verdict.json` / `summary.json` / `trial_meta.json`, hard-code case-specific values).

6. **Traceability**
- Required statement: state the correspondence rule for the keys and values fixed into `spec.ir.yaml`.

7. **tests reference**
- Required statement: state the reference path of the corresponding `tests.md` and the `test_profile_version`.

## Usage criteria for structured blocks
### When it may be used (only when truly necessary)
- When, like a parameter list, **there are many items and they are easy to mix up**
- When, like an input/output schema, **strict match is needed for machine processing**
- When natural language would become verbose and reduce readability

### When it must not be used
- Explanatory text such as policy explanations, background explanations, and selection rationale
- A place that shows only one or two values
- A place that can be uniquely defined in prose

## Ambiguity-elimination rules
- Forbid subjective expressions such as "appropriate" or "sufficiently small".
- When there are multiple possible options, state the **selection rule** (priority or fixed value).
- Describe with units, thresholds, equations, and procedures.
- Do not fill an undefined item with an "implicit default"; send it back as a `Spec` deficiency.

## Review checklist
- The meta information (`spec_id`, `spec_version`, `status`, `spec_kind`, `domain`, `family`) is at the top of the document.
- All required sections corresponding to the `spec_kind` exist.
- The physics / algorithm definition and the test input conditions are not mixed.
- For a `problem spec`, the dependent `component` and adopted `profile` are stated.
- For a `component spec`, the published `operation` and failure conditions are stated.
- For a `profile spec`, the application conditions and exclusion conditions are stated.
- For each `spec`, the `tests.md` reference is stated and can be reconciled with `spec_ref`.
- There are no undefined parameters, missing units, or missing thresholds.
