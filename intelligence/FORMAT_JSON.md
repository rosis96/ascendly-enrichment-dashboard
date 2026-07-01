# Format JSON — the exact structure that fills the Formats editor

Paste this in **Formats → "Paste Format JSON"**. Every key below lands in a
specific box in the variable editor. Get the keys right and the editor fills
itself — variable name, guidance, template, word range, and each placeholder's
description / word range / examples.

---

## Editor field → JSON key (the whole map)

| Box in the Formats editor | JSON key |
|---|---|
| Variable name (top input) | `label` |
| **How to write it — rules & guidance** | `guidance` |
| **Format** (the `{{...}}` sentence) | `template` |
| **Whole-variable word range** (left → right) | `min_words` → `max_words` |
| Placeholder heading `{{token}}` | each item's `token` |
| Placeholder **"How to write this placeholder"** | placeholder `description` |
| Placeholder **words** (left → right) | placeholder `min_words` → `max_words` |
| Placeholder **Examples** (one per line) | placeholder `examples` (array) |

Notes:
- Every `{{token}}` used in `template` should have a matching entry in `placeholders`.
- A token that is a **lead field** (e.g. `{{company_name}}`, `{{first_name}}`) is
  filled automatically from the lead — you can leave it out of `placeholders`.
- `label` is the only required key. `name` is optional (auto-made from `label`).
  Leave `template`/`placeholders` out for free-form variables (like a first line).

---

## One variable — canonical shape

```json
{
  "label": "Value Proposition",
  "guidance": "Write it like this... (this whole text lands in 'How to write it — rules & guidance').",
  "template": "We specialize in {{revenue_result}} for {{company_category}}, by {{mechanism}}.",
  "min_words": 42,
  "max_words": 80,
  "placeholders": [
    {
      "token": "revenue_result",
      "description": "What goes in this slot and how to write it (lands in 'How to write this placeholder').",
      "min_words": 3,
      "max_words": 8,
      "examples": ["qualified buyer visibility", "enterprise opportunity growth"]
    }
  ]
}
```

---

## The full Format JSON (wrapper)

Wrap your variables with the global settings. Paste this whole object:

```json
{
  "temperature": 0.75,
  "max_tokens": 2400,
  "global_output_rules": [
    "Use only facts on the prospect website.",
    "Pitch the CLIENT's offer, never the prospect's own service.",
    "No fabricated numbers. No em dash. No emojis."
  ],
  "variables": [
    { "label": "...", "...": "..." }
  ]
}
```

- `global_output_rules` = rules that apply to **every** variable (short strings).
- `variables` = the array of variable objects (each shaped as above).

---

## Full worked example — the Value Proposition from your screenshot

This pastes in and fills every box exactly as shown:

```json
{
  "temperature": 0.75,
  "max_tokens": 2400,
  "global_output_rules": [
    "Use only facts on the prospect website.",
    "Pitch RevCadence's outbound growth, never the prospect's own service.",
    "No fabricated numbers. No em dash. No emojis. No ALL CAPS."
  ],
  "variables": [
    {
      "label": "Value Proposition",
      "min_words": 42,
      "max_words": 80,
      "guidance": "Write RevCadence's value proposition using the exact winning cold email structure. Sentence 1 pitches RevCadence. Sentence 2 references the prospect. Sentence 3 returns to RevCadence's commercial outcome. While writing the Value Proposition, always remember that the prospect website describes the prospect's business, while RevCadence sells outbound growth. Every placeholder must clearly belong to one side.",
      "template": "We specialize in {{revenue_result}} for {{company_category}}, by {{revcadence_mechanism}}. And, I have seen how {{company_name}} {{personalized_observation}}. And, from our experience, {{growth_strategy}} can {{results_clause}}.",
      "placeholders": [
        {
          "token": "revenue_result",
          "description": "The specific commercial outcome RevCadence creates for this prospect category. This must describe RevCadence's result, not the prospect's service.",
          "min_words": 3,
          "max_words": 8,
          "examples": [
            "qualified buyer visibility",
            "enterprise opportunity growth",
            "executive pipeline growth",
            "qualified conversation growth"
          ]
        },
        {
          "token": "company_category",
          "description": "The category the prospect would use for itself.",
          "min_words": 2,
          "max_words": 6,
          "examples": ["industrial manufacturers", "cybersecurity consultancies", "ERP implementation partners"]
        },
        {
          "token": "revcadence_mechanism",
          "description": "Simple explanation, after 'by', of how RevCadence creates the outcome. Business-focused, not technical.",
          "min_words": 8,
          "max_words": 18,
          "examples": ["running targeted outbound and follow-up around their ideal buyers"]
        },
        {
          "token": "personalized_observation",
          "description": "A grounded observation about what the prospect actually does, from their website. Never invented.",
          "min_words": 8,
          "max_words": 22,
          "examples": ["helps manufacturers modernize finance and operations through ERP"]
        },
        {
          "token": "growth_strategy",
          "description": "The specific lever RevCadence applies.",
          "min_words": 2,
          "max_words": 7,
          "examples": ["structured follow-up", "pipeline recovery", "targeted buyer outreach"]
        },
        {
          "token": "results_clause",
          "description": "Two or three commercial outcomes joined naturally.",
          "min_words": 6,
          "max_words": 16,
          "examples": ["increase qualified conversations & reduce stalled deals"]
        }
      ]
    }
  ]
}
```

`{{company_name}}` is not in `placeholders` on purpose — it's a lead field and
gets filled automatically from the prospect record.

---

## Adding more variables

Just add more objects to the `variables` array. A free-form variable (no fill-in
slots), like a first line, needs only `label`, `guidance`, `min_words`,
`max_words`, and optional `examples` — no `template`, no `placeholders`:

```json
{
  "label": "Personalized First Line",
  "min_words": 8,
  "max_words": 28,
  "guidance": "One specific observation from the prospect's website. No pitch, no question.",
  "examples": ["Saw Tulsa Tube Bending runs custom CNC bending for energy and aerospace clients."]
}
```

Paste the whole object in **Formats → Paste Format JSON → Save**. The editor
fills in, and enrichment uses these rules immediately.
