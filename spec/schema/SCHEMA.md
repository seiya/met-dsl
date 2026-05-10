# spec/schema/

## 目的
`spec/schema/` は workflow artifact (`algorithm.resolved.yaml`, `derived_contract.json` 等) のフィールド表現規則を **declarative JSON Schema** として保持する canonical source 置き場である。`tools/validate_pipeline_semantics.py` 等の validator はここから regex / enum / 型情報を読み込んで判定に使う。

## 適用範囲
- workflow artifact のフィールド単位の表現規則 (e.g. `shape_expr`)
- 列挙値、regex pattern、形式制約

artifact 全体の構造規則は引き続き `docs/workflow/phases/phase_*.md` を canonical source とし、本 directory の schema はその一部分の declarative 表現を担う。

## 配置規則
- `spec/schema/<phase>/<field>.schema.json` の形で 1 schema = 1 ファイルとする (例: `spec/schema/plan/shape_expr.schema.json`)。
- JSON Schema draft-07 を使う。`jsonschema` ライブラリ依存は持たず、validator は標準 `re` モジュールで `pattern` を解釈する。
- `description` フィールドに「いつ・どこで適用される rule か」を明記する。
- `x-canonical-validator` (拡張) に validator 側のエントリポイントを記す。
- `x-canonical-doc` (拡張) に対応する文書を記す。
- `x-forbidden-examples` (拡張) に reject されるべき具体例を列挙する (agent 学習用)。

## validator がスキーマから受け取る契約
`tools/validate_pipeline_semantics.py` の loader (`_load_shape_expr_patterns_cached`) は、`shape_expr.schema.json` の `oneOf` を読んで以下のようにスキーマを **完全に駆動源として扱う**:

- 各 `oneOf` branch は `pattern` (string regex) を必須とする。validator は分類を **2 段階** で決定する:
  1. **explicit metadata (推奨)**: branch に `x-shape-form` (拡張) を `"scalar"` / `"list"` / `"tuple"` のいずれかで記す。validator はこの宣言を信頼し、文法非依存に分類する。これは推奨経路で、grammar が普通でない (例: integer literal を含まず identifier のみ許可する) schema でも明示的に動く。
  2. **probe fallback**: `x-shape-form` が無い branch については、validator が canonical probe matrix を試す:
     - scalar probes: `"scalar"`, `"Scalar"`, `"SCALAR"` (case-insensitive scalar literal)
     - list/tuple probes: `[1]`, `[a]`, `[A]`, `[Nx]`, `[1,2]`, `[a,b]`, `[Nx,Ny]`, および同等の paren 形
     - branch の regex が scalar probe のいずれかを fullmatch → scalar 形
     - branch の regex が list probe のいずれかを fullmatch → list/tuple 形
  3. どちらの経路でも分類できない branch (probe 全部失敗かつ `x-shape-form` 未指定) は **malformed schema** として `RuntimeError`。エラーメッセージは「`x-shape-form` を設定して曖昧さを解決すること」を案内する。
- list-form の branch にマッチした値については、validator が外側の `[...]` / `(...)` を剥がし、`,` で分割して dim token を取り出す。**dim token のシンタックス自体はスキーマの regex が完全に支配する**ため、validator 側にハードコードされた dim-token 制限は無い。スキーマが許可する任意の dim token grammar が runtime で受け入れられる。
- `_shape_matches_expr` は分割後の dim token を見て: 数字リテラル (`isdigit()`) は実値と完全一致を要求し、それ以外の token (識別子・記号など) は同一表記が複数回出現したときに同じ実値に bind することのみを要求する (case-sensitive)。これは grammar non-specific な runtime classification であり、スキーマが dim token として何を許すかを制限しない。

つまりスキーマは validator が受け入れる shape_expr の grammar を **唯一の真実 (single source of truth)** として宣言できる。スキーマ側を更新するだけで grammar を拡張・縮小でき、validator code を編集する必要は無い (新しい構造形式 — 例えば中括弧形 — を導入したい場合は、validator の構造分類 probe `"[1]" / "(1)"` が網羅していない範囲なので loader の拡張が必要)。

## 参照規則
- `docs/workflow/phases/phase_01_plan.md` 等の文書から本 schema を canonical source として相互参照する。
- `skills/workflow-plan-generate/SKILL.md` 等の SKILL 文書からも参照する。
- agent が rule を導く際の正当な参照先は `docs/` / `spec/` / `skill_must_read_refs` のみ (validator code は `tools/` 配下なので参照禁止)。`spec/schema/` は `spec/` 配下なので参照可能。

## 現在の schema
- `plan/shape_expr.schema.json` — `temporaries[].shape_expr` 等の表現規則。`scalar` / `[d1,...]` / `(d1,...)` の 3 形式に限る。
