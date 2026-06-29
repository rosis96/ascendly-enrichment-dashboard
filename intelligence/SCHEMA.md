# Intelligence Workspace — Configuration Schema (v1, LOCKED)

This document is the **contract**. An Intelligence Workspace is a single portable
JSON object. The engine that runs it is generic: it does not know what any
pipeline or module "means" — it loads the config, gathers context for a lead,
runs the configured **pipelines in order** (each pipeline runs its modules
respecting dependencies and conditions), and emits structured results.
Everything a client needs — strategy, knowledge, analysis, decisions, enrichment,
campaign assets, export — lives inside this one object so a workspace can be
duplicated, exported, imported, and version-controlled with no code change.

Nothing here is industry- or use-case-specific. A workspace can have 5 analysis
modules or 15, a knowledge layer or none, and the engine treats them identically.

> **Pipelines are plug-ins, not special cases.** `pipelines` is an *ordered list*.
> Each entry names a `kind` that the engine resolves through a **pipeline-kind
> registry**. Shipping a new capability (knowledge, CRM history, social signals,
> buying-committee analysis, …) means registering a new kind and dropping it into
> the array — the core architecture never changes.

---

## 0. Design rules

1. **One config = one client.** The whole behaviour of a workspace is this JSON.
   No hidden state, no per-industry branches in code.
2. **The engine is a runner, not a brain.** Meaning lives in prompts, knowledge,
   and rules inside the config — never in the executor.
3. **Every pipeline is a plug-in.** The engine has no special case for "analysis"
   vs "knowledge". Both are entries in `pipelines` whose `kind` maps to an
   executor in the registry (§4). New kinds are additive.
4. **Portable.** Export → import on another instance reproduces identical
   behaviour. Only secrets (API keys) and the lead data live outside the config;
   large knowledge files are referenced and travel as bundled resources.
5. **Additive versioning.** `schema_version` is an integer. New optional fields
   and new pipeline kinds may ship in the same version; breaking changes bump the
   version with a migration in the reader.
6. **Forward-safe & non-destructive.** Unknown keys are preserved on round-trip.
   Importing or editing a config never mutates lead data or prior results.

---

## 1. Top-level shape

```jsonc
{
  "schema_version": 1,
  "id": "acme",                 // slug, unique per instance, [a-z0-9_-]
  "name": "Acme Corp",
  "meta": {                     // optional, informational
    "created_at": "2026-06-29T00:00:00Z",
    "updated_at": "2026-06-29T00:00:00Z",
    "author": "rosis_s@ascendly.one",
    "config_version": "1.0.0",  // the CLIENT's own version of THIS workspace
    "notes": ""
  },

  "strategy":        { /* §2  the brain */ },
  "knowledge_base":  [ /* §3  durable client knowledge (docs, pricing, cases…) */ ],
  "context_sources": [ /* §4  deterministic per-lead inputs (scrape, esp, tech…) */ ],

  "pipelines": [ /* §5  ORDERED array of pipeline plug-ins; each has a `kind` */
    { "id": "analysis",   "kind": "analysis",   "modules": [ /* §7 */ ] },
    { "id": "decision",   "kind": "decision",   "modules": [ /* §8 */ ] },
    { "id": "knowledge",  "kind": "knowledge",  "modules": [ /* §9 */ ] },
    { "id": "enrichment", "kind": "enrichment", "modules": [ /* §10 */ ] },
    { "id": "assets",     "kind": "assets",     "modules": [ /* §10 */ ] }
  ],

  "export":   { /* §11 */ },
  "settings": { /* §12 */ }
}
```

Required: `schema_version`, `id`, `name`, `pipelines`. Everything else is optional
and defaults sensibly (empty strategy/knowledge, no context sources, CSV export of
standard lead fields).

The default/recommended pipeline order is
`analysis → decision → knowledge → enrichment → assets`, matching:

```
Strategy → Analysis → Decision → Knowledge → Enrichment → Assets → Export
```

but order is whatever the array says — a client could gather knowledge before
analysis, or run two analysis pipelines around a knowledge step. The engine just
executes the array top to bottom.

---

## 2. `strategy` — the brain (Vision §1)

Free-form **and** structured. Every field optional. Compiled into a context block
(`{{strategy}}`) injected into prompts, and individually addressable
(`strategy.icp`, `strategy.deal_size`, …). Read-only during a run.

```jsonc
"strategy": {
  "business_overview": "What the client does, in plain language.",
  "offers": [ { "name": "Managed SOC", "description": "...", "price_point": "$4k–$12k/mo", "value_prop": "..." } ],
  "positioning": "How they differ from alternatives.",
  "market": "Who they sell to at a market level.",
  "objectives": "Business objective behind this outbound.",
  "campaign_goals": "What a 'win' looks like.",
  "icp": "Free-text ideal customer definition.",
  "non_icp": "Free-text exclusion definition.",
  "buyer_personas": [ { "title": "VP Engineering", "role": "Economic buyer", "pains": "...", "goals": "...", "triggers": "..." } ],
  "sales_process": "How deals are won.",
  "deal_size": "Typical ACV / deal-size assumptions.",
  "geo": "Geographic assumptions / restrictions.",
  "constraints": "Hard constraints (compliance, exclusions, do-not-contact).",
  "notes": "Anything else.",
  "custom": { "any_key": "arbitrary client-specific facts" }
}
```

---

## 3. `knowledge_base` — durable client knowledge (Vision: Knowledge layer)

The client's stored, structured knowledge — **not** scraped per lead. This is
*data*: case studies, product docs, pricing, competitor intel, PDFs, sales decks,
FAQs, CRM notes, and prior enrichment. It persists across runs and is reused by
the Knowledge pipeline (§9) to inject rich context before enrichment.

```jsonc
"knowledge_base": [
  { "id": "case_acme_fintech", "kind": "case_study", "label": "Acme × Fintech",
    "tags": ["fintech", "soc2"], "content": "Inline text of the case study…" },

  { "id": "pricing_2026", "kind": "pricing", "label": "2026 Pricing",
    "content": "Plans: Starter $4k/mo … Enterprise custom." },

  { "id": "competitors", "kind": "competitor", "label": "Competitive landscape",
    "content": "Vs Foo: … Vs Bar: …" },

  { "id": "brochure", "kind": "pdf", "label": "Product brochure",
    "ref": "res://brochure.pdf", "extracted": "res://brochure.txt", "tags": ["overview"] },

  { "id": "crm_notes", "kind": "crm_note", "label": "Discovery notes", "content": "…" },

  { "id": "prior", "kind": "prior_enrichment", "label": "Previous enrichment",
    "ref": "lead://result" }   // reuse this lead's existing stored result (non-destructive)
]
```

### Item fields
| field | meaning |
|-------|---------|
| `id` | unique within the workspace; addressable as `knowledge_base.<id>` |
| `kind` | `case_study` \| `product_doc` \| `pricing` \| `competitor` \| `pdf` \| `deck` \| `faq` \| `website` \| `crm_note` \| `prior_enrichment` \| `custom` |
| `label` | human name |
| `content` | inline text (small items), OR |
| `ref` | pointer to a stored resource for large files (`res://…`); `extracted` holds its text form |
| `tags` | strings used by knowledge retrieval to match a lead |
| `metadata` | open object |

**Resources & portability:** big files (`ref: res://…`) live in a resource store
keyed by the ref; the config holds only metadata + refs. A workspace **export**
bundles the referenced resources alongside the JSON so import on another instance
is lossless. `lead://result` references the lead's own existing enrichment so we
*reuse*, never overwrite, data you already have.

`knowledge_base` is inert storage. Turning it into per-lead context is the job of
the Knowledge pipeline (§9).

---

## 4. `context_sources` — deterministic per-lead inputs

Gatherers that populate `context.<id>` **before** the pipelines run. They don't
call the LLM (except `industry_classify`).

```jsonc
"context_sources": [
  { "id": "scrape", "type": "web_scrape", "config": { "max_pages": 3, "max_chars": 9000 } },
  { "id": "title_gate", "type": "title_filter" },
  { "id": "esp", "type": "esp_detect" },
  { "id": "tech", "type": "tech_detect" },
  { "id": "industry", "type": "industry_classify", "config": { "mode": "deep" } }
]
```

| type | output (`context.<id>.*`) | notes |
|------|---------------------------|-------|
| `web_scrape` | `content`, `urls` | homepage (+priority pages if `max_pages>1`); download size-capped |
| `title_filter` | `status` (`pass`\|`rejected`) | string match on lead title |
| `esp_detect` | `label`, `mx` | MX/DNS, no cost |
| `tech_detect` | `stack[]`, `signals` | from HTML/headers/DNS, no cost |
| `industry_classify` | `label` | reuses existing classifier; `mode`: `fast`\|`deep` |

---

## 5. `pipelines` — the plug-in model (Vision: Pipelines)

`pipelines` is an **ordered array**. Each entry:

```jsonc
{
  "id": "knowledge",        // unique; becomes the namespace for its modules
  "kind": "knowledge",      // resolved via the pipeline-kind registry (§6)
  "enabled": true,          // default true
  "run_if": "always",       // optional gate for the WHOLE pipeline (§13)
  "modules": [ /* shape depends on kind; all share the common module contract §7 */ ]
  // some kinds carry extra pipeline-level keys (e.g. decision has none; export-like kinds carry config)
}
```

- The engine runs pipelines in array order.
- A pipeline's `id` is the namespace for everything it produces:
  modules in the `analysis` pipeline are read as `analysis.<module_id>.*`; a
  second analysis pipeline with `id: "deep_analysis"` exposes
  `deep_analysis.<module_id>.*`. (You can have more than one pipeline of the same
  kind.)
- `run_if` on a pipeline lets you skip an entire stage for a lead (e.g. skip
  Knowledge + Enrichment when `decision.outcome == 'Reject'`).

---

## 6. Pipeline-kind registry

Each `kind` maps to an executor that knows how to run that pipeline's modules.
**v1 registered kinds:**

| kind | module type it runs | produces |
|------|---------------------|----------|
| `analysis` | `llm_analysis` (§7) | structured analysis + scores |
| `decision` | `decision` rules (§8) | decision labels |
| `knowledge` | `knowledge` modules (§9) | injected knowledge context |
| `enrichment` | `llm_text` (§10) | data fields |
| `assets` | `llm_text` (§10) | campaign copy |

**Reserved kinds (additive, not in v1 surface but namespaced now):**
`crm_history`, `social_signals`, `buying_committee`, `web_enrich`, `compute`.

Adding a kind = implement its executor + register it. No change to the schema's
core shape, the addressing model, or any other pipeline. That is the entire point
of the array-of-plug-ins design.

---

## 7. Module contract (shared)

Modules in `analysis`, `knowledge`, `enrichment`, `assets` share one shape; only
type-specific fields differ. `decision` modules (§8) are the one structural
variant.

```jsonc
{
  "id": "icp_fit",                 // unique within its pipeline
  "label": "ICP Fit Analysis",
  "type": "llm_analysis",          // matches the pipeline kind's module type
  "enabled": true,

  "prompt": "….{{strategy}}…. WEBSITE:\n{{context.scrape.content}}",
  "depends_on": ["scrape"],        // ids that must run first (sources or modules)
  "run_if": "always",              // condition (§13); default "always"

  "output": {                      // fields this module promises to return
    "fit_score": { "type": "number", "range": [0, 10] },
    "verdict":   { "type": "enum", "values": ["ICP", "Non-ICP", "Unclear"] },
    "reasons":   { "type": "string" }
  },

  "model": null,                   // override settings.models.<kind>
  "export": { "include": false }
}
```

Rules: `id` is the namespace; `depends_on` controls ordering (cycles are config
errors); a skipped module yields `null` (downstream conditions treat null as
false); `output` is validated/coerced (numbers clamped to `range`, enums snapped,
missing → null).

---

## 8. `decision` modules — Decision Engine (Vision §4)

Deterministic. Ordered rules over everything prior pipelines produced; sets
decision keys. No LLM.

```jsonc
{
  "id": "primary", "type": "decision",
  "outputs": ["outcome", "priority", "flags"],
  "rules": [
    { "if": "context.title_gate.status == 'rejected'",  "set": { "outcome": "Reject" } },
    { "if": "analysis.icp_fit.verdict == 'Non-ICP'",    "set": { "outcome": "Reject" } },
    { "if": "analysis.icp_fit.fit_score >= 8 and analysis.buyer_fit.score >= 7",
                                                        "set": { "outcome": "Accept", "priority": "High" } },
    { "if": "analysis.outbound_suitability.score < 4",  "set": { "outcome": "Skip Personalization" } },
    { "if": "contains(analysis.competitor.names, 'Acme')", "append": { "flags": "competitor_customer" } }
  ],
  "default": { "outcome": "Needs Review" }
}
```

**Semantics (locked):** rules top-to-bottom; `set` applies a key only if unset
(first-match-wins per key); `append` always pushes onto a list key; unset
`outputs` fall back to `default`. The **first** decision pipeline's
`outcome`/`priority`/`flags` are aliased as `decision.outcome` / `decision.priority`
/ `decision.flags`. Decision values are free strings the client defines (`Accept`,
`Reject`, `Needs Review`, `High Priority`, `Enterprise`, `Skip Personalization`,
`Generate Outreach`, `Send To Manual Review`, …); the engine gives them no special
meaning — gating happens via `run_if`.

---

## 9. `knowledge` modules — Knowledge Engine (Vision: Knowledge layer)

Turn the inert `knowledge_base` (§3) into per-lead context. Each module selects
and/or condenses knowledge relevant to the current lead and exposes it under
`knowledge.<id>.*` for enrichment/assets to use. Because this pipeline typically
sits *after* Decision, you only spend effort assembling knowledge for leads worth
contacting.

```jsonc
"modules": [
  { "id": "relevant_cases", "type": "knowledge", "op": "retrieve",
    "from": { "kind": "case_study" },
    "match": "by_tags",                         // match item.tags against the lead
    "match_on": ["context.industry.label", "lead.country"],
    "limit": 2,
    "output": { "items": { "type": "list" } } },// -> knowledge.relevant_cases.items

  { "id": "pricing", "type": "knowledge", "op": "static",
    "from": { "id": "pricing_2026" },
    "output": { "text": { "type": "string" } } },

  { "id": "context_pack", "type": "knowledge", "op": "summarize",
    "depends_on": ["relevant_cases", "pricing"],
    "prompt": "Condense the most relevant proof + pricing for this lead:\n{{knowledge.relevant_cases.items}}\n{{knowledge.pricing.text}}",
    "max_words": 150,
    "output": { "text": { "type": "string" } } } // -> knowledge.context_pack.text
]
```

### Knowledge module ops (v1)
| op | what it does | LLM? |
|----|--------------|------|
| `static` | inject a specific `knowledge_base` item verbatim | no |
| `retrieve` | select items by filter/tags (`match`, `match_on`, `limit`) | no |
| `summarize` | condense selected knowledge into a tight context block | yes |

Reserved op (additive): `semantic` (embedding retrieval) — same module shape,
adds a vector index behind the scenes. Knowledge outputs are referenced in any
later prompt, e.g. an enrichment module's prompt can include
`{{knowledge.context_pack.text}}` so every variable is grounded in real client
proof, not just the homepage.

---

## 10. `enrichment` & `assets` modules — (Vision §3 & §5)

Both run `llm_text`. Identical shape; enrichment runs before assets so assets may
reference enrichment output. Now also able to reference `knowledge.*`.

```jsonc
{
  "id": "email_p1", "label": "Email Personalization", "type": "llm_text",
  "key": "email_p1",                  // export/merge key; defaults to id
  "prompt": "….{{strategy}}…. Proof: {{knowledge.context_pack.text}} Angle: {{analysis.messaging.opportunities}}",
  "format": "plain",                  // plain | markdown | html | json
  "min_words": 20, "max_words": 60,
  "run_if": "decision.outcome in ['Accept', 'High Priority']",
  "depends_on": ["context_pack", "messaging"],
  "output": { "text": { "type": "string" } },
  "export": { "include": true, "column": "Email_Personalization" }
}
```

`llm_text` always returns `text` (or, with `format: "json"`, the JSON declared in
`output`). `min_words`/`max_words` are enforced like the current writer's hard
range. Addressable as `enrichment.<key>` / `assets.<key>`.

---

## 11. `export` — Export Layer (Vision §6)

Operates on the finished per-lead document. Each destination maps internal paths
to output fields and may filter rows with `only_where`.

```jsonc
"export": {
  "default_format": "csv",
  "csv": {
    "only_where": "decision.outcome != 'Reject'",
    "fields": [
      { "source": "lead.first_name",            "column": "First Name" },
      { "source": "lead.company",               "column": "Company" },
      { "source": "lead.email",                 "column": "Email" },
      { "source": "context.industry.label",     "column": "Industry" },
      { "source": "decision.outcome",           "column": "Decision" },
      { "source": "analysis.icp_fit.fit_score", "column": "ICP_Score" },
      { "source": "enrichment.email_p1",        "column": "Email_Personalization" }
    ]
  },
  "instantly": { "enabled": false, "campaign_id": null,
    "only_where": "decision.outcome in ['Accept','High Priority']",
    "field_map": { "personalization": "enrichment.email_p1", "first_line": "assets.li_opener" } },
  "hubspot": { "enabled": false,
    "field_map": { "lead_score": "analysis.icp_fit.fit_score", "lifecyclestage": "decision.outcome" } }
}
```

Omitted ⇒ default CSV of standard lead fields plus any module field with
`export.include: true`. (Export is the output layer, distinct from the per-lead
pipelines; a future `export` *pipeline kind* could push mid-run, but v1 keeps it
here.)

---

## 12. `settings`

```jsonc
"settings": {
  "models": { "analysis": "gpt-4o-mini", "knowledge": "gpt-4o-mini",
              "enrichment": "gpt-4.1", "assets": "gpt-4.1", "classify": "gpt-4o-mini" },
  "scrape": { "max_pages": 3, "max_chars": 9000 },
  "concurrency": { "hint": 150 },
  "currency": "USD"
}
```

`decision` needs no model. `module.model` overrides the per-kind default.

---

## 13. Addressing, templating & conditions (LOCKED)

### Namespaces usable in `prompt`, `run_if`, rule `if`, `export.source`
| prefix | meaning |
|--------|---------|
| `strategy.*` | strategy fields (§2) |
| `knowledge_base.<id>` | a raw stored knowledge item (§3) |
| `lead.*` | raw lead fields + any custom import field |
| `context.<id>.*` | context source outputs (§4) |
| `<pipeline_id>.<module_id>.*` | any pipeline's module outputs — e.g. `analysis.icp_fit.fit_score`, `knowledge.context_pack.text` |
| `decision.outcome`/`priority`/`flags` | aliases for the first decision pipeline |

A missing path resolves to `null` (falsy in conditions, empty in templates).

### Templating
`{{path}}` interpolates (objects/arrays → compact JSON; strings verbatim).
`{{strategy}}` renders the whole strategy as a readable block. `\{` emits a
literal brace.

### Condition grammar (`run_if`, rule `if`) — safe, no `eval`
```
expr      := or
or        := and ( "or" and )*
and       := not ( "and" not )*
not       := "not" not | comparison
comparison:= value ( ("==" | "!=" | ">" | ">=" | "<" | "<=" | "in" | "not in") value )?
value     := path | number | string | "true" | "false" | "null" | list | call
list      := "[" ( value ("," value)* )? "]"
call      := IDENT "(" ( value ("," value)* )? ")"
```
Functions (v1): `len`, `lower`, `upper`, `contains`, `exists`, `coalesce`.
`always`/`true` mean "run".

---

## 14. Execution order

```
Lead
  ↓  context_sources              (deterministic; parallel where independent)
  ↓  pipelines[0..n] in array order:
       analysis    (llm_analysis; parallel where independent)
       decision    (deterministic rules)
       knowledge   (retrieve / static / summarize from knowledge_base)   ← the new layer
       enrichment  (llm_text; gated by run_if on decision)
       assets      (llm_text; may read enrichment + knowledge)
  ↓  export                       (map + filter the finished lead doc)
```

Stages are whatever the `pipelines` array contains, in that order. A module that
fails or is skipped yields `null` and never aborts the lead. Per-lead results are
stored as one structured document keyed by `<pipeline_id>.<module_id>` — exactly
what the Testing Environment renders.

---

## 15. Testing Environment (Vision §7)

A single-company run produces a complete, inspectable document:

```jsonc
{
  "lead": { … },
  "context":   { "scrape": {…}, "esp": {…}, "title_gate": {…}, "industry": {…} },
  "analysis":  { "icp_fit": { "fit_score": 8, "verdict": "ICP", "reasons": "…", "_ms": 1200, "_cost": 0.0004 } },
  "decision":  { "primary": { "outcome": "Accept", "priority": "High" } },
  "knowledge": { "context_pack": { "text": "…", "_used": ["case_acme_fintech","pricing_2026"] } },
  "enrichment":{ "email_p1": { "text": "…", "_words": 41 } },
  "assets":    { "li_opener": { "text": "…" } },
  "export":    { "csv_row": { "First Name": "…", "Decision": "Accept", … } },
  "_trace":    [ { "pipeline": "analysis", "id": "icp_fit", "ms": 1200, "cost": 0.0004, "skipped": false } ]
}
```

Every module records `_ms` and `_cost`; skipped modules record `skipped: true`
with the failing condition; knowledge modules record `_used` (which
`knowledge_base` items fed the output). This is the debugging surface.

---

## 16. Versioning, portability & parallel rollout

- **Unit of portability** = this whole JSON **+ bundled resources** for any
  `ref: res://…` knowledge files. `duplicate` = deep copy with a new `id`.
  `export` = JSON + resources (+ optional checksum manifest). `import` = validate
  against `workspace.schema.json`, store resources, store config.
- **Non-destructive:** import/edit never touches lead data or prior results;
  `prior_enrichment` knowledge *reads* existing results via `lead://result`.
- **Parity rollout:** `examples/ascendly_current.json` reproduces today's engine
  (scrape → title gate → strict ICP → writer) purely as config. The new executor
  runs it in **parallel** with the live pipeline; we diff a sample until outputs
  match, then migrate workspaces one at a time. The 700k production path is never
  touched until a workspace is verified.

---

## 17. Why this is future-proof

Adding "Knowledge" required **no new top-level concept** beyond a storage list and
a pipeline kind — it slotted into the same array, the same module contract, the
same addressing model. The next capabilities do the same:

- **CRM history** → `kind: "crm_history"` pipeline reading a `crm_note` /
  external source, exposing `crm.*`.
- **Social signals** → `kind: "social_signals"` exposing `social.*`.
- **Buying-committee analysis** → `kind: "buying_committee"` exposing `committee.*`.

Each is a plug-in dropped into `pipelines`, referenced by later prompts/conditions
through the universal `<pipeline_id>.<module_id>.*` namespace. The core never
changes.
