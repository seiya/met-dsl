# Document authoring style

When **editing or creating documents** in this repository (markdown specs, runbooks, design notes), follow the rules below. This is doc-authoring guidance; it is not required reading for code-generating workflow agents.

## Purpose
- Write documents as "finished specifications", not as "discussion logs".
- Keep documents in a state where a reader can interpret the decisions and requirements by reading them standalone.

## Style rules
- Use concise, declarative prose.
- State the subject and responsibility explicitly; avoid ambiguous omissions.
- Do not use colloquialisms, metaphors, or expressions of opinion.
- Prioritize describing specifications, requirements, constraints, and decision criteria.

## Terminology rules
- Use the English notation defined in `docs/GLOSSARY.md` for terms, artifact names, role names, phase names, and classification names.
- When adding a new term, first define it in `docs/GLOSSARY.md` in English, then use it across documents.
- Keep terms consistent with the canonical notation in `docs/GLOSSARY.md`; do not introduce ad-hoc synonyms.

## Markdown math notation rules
- Use `$...$` for inline math.
- Use `$$...$$` for block math.
- Do not use `\(...\)` or `\[...\]`.

## Forbidden expressions
- Expressions that reveal the discussion process: "in conclusion", "the reason is", "after discussion", "first", "next", "trial and error", and similar.
- Expressions that reveal AI dialogue: "brainstormed with AI", "the AI thought", "asked the LLM", and similar.
- Colloquial or slang expressions.

## Recommended headings
- `Purpose`
- `Scope`
- `Requirements`
- `Design Policy`
- `Operations Rules`
- `Decision Criteria`

## Writing guidelines
- Write "what is required", not "why it ended up this way".
- For branching decisions, state the conditions and the selection rule explicitly.
- Do not leave undefined items unresolved; state explicitly that an item is undefined and how it is handled (forbidden / error).
- Define abbreviations at first use, and keep terms consistent with the existing documents (`docs/GLOSSARY.md`).

## Change checklist
- No discussion-log-style expressions remain.
- Each section is a self-contained, complete statement that reads standalone.
- Requirements, constraints, input/output, and decision conditions are concrete.
- Terms, artifact names, role names, phase names, and classification names match the English notation in `docs/GLOSSARY.md`.
