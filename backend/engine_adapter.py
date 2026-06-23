"""Adapter between the dashboard and the personalization engine.

Two modes:
  * DEMO (default): deterministic fake enrichment so the whole UI works locally
    with no OpenAI key, no scraping, and no cost.
  * REAL (ENRICH_MODE=real): lazily imports the duplicated engine/run.py and runs
    the actual pipeline. Wired but off by default until keys/docs are in place.

The title-gate keyword logic mirrors engine/run.py so the demo behaves like the
real strict gate (junior titles are rejected before any "scrape").
"""
import os
import re
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_DIR = os.path.join(BASE_DIR, "engine")
VAR_DIR = os.path.join(ENGINE_DIR, "variable_sets")
PROFILE_DIR = os.path.join(ENGINE_DIR, "client_profiles")

ALWAYS_KEYS = ["ICPReview", "ICP_reason"]

# Mirrors engine/run.py _TITLE_SENIOR_CUES (kept strict so juniors are rejected).
_TITLE_SENIOR_CUES = [
    "founder", "co-founder", "cofounder", "owner", "principal", "managing partner",
    "managing owner", "partner", "ceo", "chief executive", "president",
    "managing director", "executive director", "group managing director",
    "agency principal", "chief growth", "chief revenue", "chief business",
    "chief commercial", "chief sales", "chief marketing", "chief strategy",
    "cgo", "cro", "cbo", "cco", "cso", "cmo", "vp", "svp", "evp", "vice president",
    "director of business development", "director, business development",
    "director of new business", "director of new client acquisition",
    "director of agency growth", "director of growth", "director of revenue growth",
    "director of strategic", "director of commercial growth",
    "director of market development", "director of client acquisition",
    "director of demand generation", "director of marketing & growth",
    "director of sales & marketing", "director of commercial operations",
    "director of revenue operations", "director of go-to-market",
    "director of market expansion", "practice lead", "managing consultant",
    "practice director", "commercial director", "growth director",
    "business director", "new business director", "client development director",
    "market development director",
]


def list_variable_sets():
    out = []
    for f in sorted(os.listdir(VAR_DIR)):
        if f.endswith(".json") and not f.endswith(".bak"):
            out.append(f[:-5])
    return out


def engine_set_exists(name):
    return bool(name) and os.path.exists(os.path.join(VAR_DIR, f"{name}.json"))


def list_output_keys(variable_set):
    path = os.path.join(VAR_DIR, f"{variable_set}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("output_keys", [])
    except Exception:
        return ["ICPReview", "ICP_reason", "personalized_first_line", "company_category",
                "ideal_customers", "product_complimentary", "value_proposition"]


def output_keys_for(base):
    """Engine output keys for a resolved base set. A blank/unknown base (a blank
    workspace) yields only the always-on gate fields."""
    if engine_set_exists(base):
        return list_output_keys(base)
    return list(ALWAYS_KEYS)


def selectable_enrichments(variable_set):
    """Output variables a user can choose to include (the 'always on' gate
    fields are excluded from the picker)."""
    return [k for k in list_output_keys(variable_set) if k not in ALWAYS_KEYS]


def get_builtin_spec(variable_set, name):
    """Raw spec of a built-in variable from the engine file, or None."""
    path = os.path.join(VAR_DIR, f"{variable_set}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    for v in d.get("variables", []):
        if v.get("name") == name:
            return v
    return None


def duplicate_spec(source_spec, new_label):
    """Build a custom spec copied from a built-in or custom variable spec."""
    spec = {"name": slugify(new_label), "label": new_label, "custom": True}
    for k in ("min_words", "max_words"):
        if source_spec.get(k):
            spec[k] = source_spec[k]
    spec["purpose"] = source_spec.get("purpose") or source_spec.get("definition") or ""
    spec["template"] = source_spec.get("template", "")
    spec["placeholders"] = dict(source_spec.get("placeholders", {}))
    if source_spec.get("example_outputs"):
        spec["example_outputs"] = list(source_spec["example_outputs"])
    return spec


# Descriptive fields surfaced in the Formats view (who we're writing for).
_PROFILE_FIELDS = [
    ("service_brief", "What the client does"),
    ("main_offer", "Main offer"),
    ("what_we_are_pitching", "What we pitch"),
    ("target_outcome", "Target outcome"),
    ("icp_summary", "Who we target (ICP)"),
]


def list_profiles():
    out = []
    for f in sorted(os.listdir(PROFILE_DIR)):
        if not f.endswith(".json"):
            continue
        name = f[:-5]
        if "icp_only" in name or name.endswith("_icp"):  # skip ICP-only classifier profiles
            continue
        out.append(name)
    return out


def get_profile(name):
    path = os.path.join(PROFILE_DIR, f"{name}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return {"name": name, "fields": []}
    return {"name": d.get("client_name", name), "fields": profile_fields_from(d)}


def profile_fields_from(d):
    """List of {key,label,value} for the descriptive profile fields present in d."""
    return [{"key": key, "label": lbl, "value": d.get(key, "")}
            for key, lbl in _PROFILE_FIELDS if d.get(key)]


def profile_blank_fields():
    """All descriptive fields, empty — for filling in a new workspace profile."""
    return [{"key": key, "label": lbl, "value": ""} for key, lbl in _PROFILE_FIELDS]


def get_profile_raw(name):
    """Raw {key: value} of the descriptive fields, for cloning into a workspace."""
    path = os.path.join(PROFILE_DIR, f"{name}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return {}
    return {key: d.get(key, "") for key, _ in _PROFILE_FIELDS if d.get(key)}


def _variable_description(v):
    """Best human-readable description of how to write a variable."""
    desc = v.get("purpose") or v.get("definition") or ""
    if not desc and isinstance(v.get("complete_writing_philosophy"), dict):
        desc = " · ".join(f"{k.replace('_',' ')}: {val}"
                          for k, val in v["complete_writing_philosophy"].items())
    return desc


def _variable_notes(v):
    notes = []
    for key in ("instructions", "writing_rules", "hard_requirements"):
        val = v.get(key)
        if isinstance(val, list):
            notes.extend(str(x) for x in val if isinstance(x, str))
    rules = v.get("rules")
    if isinstance(rules, list):
        notes.extend(str(x) for x in rules if isinstance(x, str))
    elif isinstance(rules, dict):
        for vv in rules.values():
            if isinstance(vv, list):
                notes.extend(str(x) for x in vv if isinstance(x, str))
    return notes


def format_spec(variable_set):
    path = os.path.join(VAR_DIR, f"{variable_set}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return {"variable_set": variable_set, "client": "", "variables": []}
    variables = []
    for v in d.get("variables", []):
        variables.append({
            "name": v.get("name", ""),
            "min_words": v.get("min_words"),
            "max_words": v.get("max_words"),
            "always": v.get("name") in ALWAYS_KEYS,
            "description": _variable_description(v),
            "notes": _variable_notes(v)[:8],
        })
    return {
        "variable_set": variable_set,
        "client": variable_set.split("_")[0],
        "output_keys": d.get("output_keys", []),
        "variables": variables,
    }


# ------------------------- custom variable builder -------------------------

def slugify(label):
    s = re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_")
    return s or "custom_var"


def extract_tokens(template):
    """Unique {{placeholder}} tokens in order of first appearance."""
    seen = []
    for m in re.findall(r"\{\{(.*?)\}\}", template or ""):
        t = m.strip()
        if t and t not in seen:
            seen.append(t)
    return seen


def build_custom_spec(label, template, placeholders, min_words=None, max_words=None, purpose="", examples=None):
    """Turn the builder's input into a valid engine-format variable spec.

    Two styles, mixable:
      * Template style: a {{placeholder}} format + per-placeholder specs.
      * Free-form style: no template — just `purpose` (how to write it) + examples.

    placeholders: list of {token, description, min_words, max_words, examples}
    """
    spec = {"name": slugify(label), "label": str(label).strip(), "custom": True}
    if min_words:
        spec["min_words"] = int(min_words)
    if max_words:
        spec["max_words"] = int(max_words)
    if purpose:
        spec["purpose"] = purpose
    spec["template"] = template or ""

    by_token = {p.get("token", "").strip(): p for p in (placeholders or [])}
    ph = {}
    for tok in extract_tokens(template):
        p = by_token.get(tok, {})
        item = {}
        if p.get("description"):
            item["description"] = p["description"]
        if p.get("min_words"):
            item["min_words"] = int(p["min_words"])
        if p.get("max_words"):
            item["max_words"] = int(p["max_words"])
        ex = p.get("examples") or []
        if isinstance(ex, str):
            ex = [x.strip() for x in ex.splitlines() if x.strip()]
        if ex:
            item["examples"] = ex
        ph[tok] = item
    spec["placeholders"] = ph

    if isinstance(examples, str):
        examples = [x.strip() for x in examples.splitlines() if x.strip()]
    examples = [e for e in (examples or []) if e]
    if examples:
        spec["example_outputs"] = examples
    return spec


def fill_custom(spec, lead):
    """Demo-mode renderer: fill a custom template, or echo an example for a
    free-form (no-template) variable."""
    template = spec.get("template", "") or ""
    ph = spec.get("placeholders", {})
    company = lead.get("company") or "your company"

    if not template.strip():
        ex = spec.get("example_outputs") or []
        return str(ex[0]) if ex else f"[{spec.get('label', 'custom')} for {company}]"

    def repl(m):
        tok = m.group(1).strip()
        p = ph.get(tok, {})
        ex = p.get("examples") or []
        if ex:
            return str(ex[0])
        low = tok.lower()
        if "company" in low:
            return company
        if "industry" in low:
            return "agencies"
        return "[" + tok + "]"

    return re.sub(r"\{\{(.*?)\}\}", repl, template).strip()


def custom_card(spec):
    """Formats-view card shape for a custom variable spec."""
    notes = []
    for tok, p in (spec.get("placeholders") or {}).items():
        bits = [f"{{{{{tok}}}}}"]
        if p.get("description"):
            bits.append("— " + p["description"])
        if p.get("min_words") and p.get("max_words"):
            bits.append(f"({p['min_words']}-{p['max_words']} words)")
        if p.get("examples"):
            bits.append("e.g. " + "; ".join(p["examples"][:2]))
        notes.append(" ".join(bits))
    for e in (spec.get("example_outputs") or [])[:3]:
        notes.append("Example: " + str(e))
    return {
        "name": spec.get("name", ""),
        "label": spec.get("label", spec.get("name", "")),
        "min_words": spec.get("min_words"),
        "max_words": spec.get("max_words"),
        "always": False,
        "custom": True,
        "description": spec.get("purpose") or spec.get("template", ""),
        "notes": notes,
    }


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9& ]+", " ", str(s).lower())).strip()


def title_passes(title):
    t = _norm(title)
    if not t:
        return False
    return any(re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", t) for c in _TITLE_SENIOR_CUES)


def _demo_value(key, company):
    samples = {
        "personalized_first_line":
            f"The way {company} packages its client work signals real positioning, not just delivery.",
        "company_category": "marketing agencies",
        "ideal_customers": "B2B founders, growth leaders, & SaaS teams",
        "product_complimentary":
            f"{company}'s client roster is sharp. Are those the kinds of clients you want more of?",
        "value_proposition":
            (f"We specialize in client pipeline growth for {company}, by running personalized cold "
             f"outreach and capturing inbound leads before they leave your site. And, I have seen how "
             f"{company} wins work from a clear set of clients. And, from our experience, a managed "
             f"outbound and inbound booking system can improve {company}'s booked calls, closed deals "
             f"& consistent meeting flow."),
    }
    return samples.get(key, f"[{key} for {company}]")


def demo_enrich(lead, variable_set, selected=None, custom_specs=None):
    """Return (result_dict, est_cost). Deterministic, no network."""
    custom_specs = custom_specs or []
    title = lead.get("title") or ""
    company = lead.get("company") or "this company"
    website = (lead.get("website") or "").lower()
    keys = output_keys_for(variable_set)
    res = {k: "" for k in keys}
    for s in custom_specs:
        if s.get("name"):
            res[s["name"]] = ""

    # 1) Title gate first.
    if not title_passes(title):
        for k in list(res.keys()):
            res[k] = "N/A"
        res["ICPReview"] = "Non-ICP"
        res["ICP_reason"] = "Title not a decision-maker"
        res["_title_gate"] = "rejected"
        res["_status"] = "ok"
        return res, 0.002

    res["_title_gate"] = "pass"

    # 2) Crude company ICP demo (real version scrapes + uses the strict gate).
    non_icp = any(w in website for w in ["shop", "store", "app.", "/pricing", "pricing.", "ecom"])
    if non_icp:
        for k in list(res.keys()):
            res[k] = "N/A"
        res["ICPReview"] = "Non-ICP"
        res["ICP_reason"] = "Public pricing / low-value"
        res["_status"] = "ok"
        return res, 0.01

    # 3) ICP -> fill built-in + custom variables (custom overrides same-named built-in).
    custom_names = {s.get("name") for s in custom_specs if s.get("name")}
    res["ICPReview"] = "ICP"
    res["ICP_reason"] = "Marketing/branding agency"
    for k in keys:
        if k in ALWAYS_KEYS or k in custom_names:
            continue
        res[k] = _demo_value(k, company)
    for s in custom_specs:
        if s.get("name"):
            res[s["name"]] = fill_custom(s, lead)
    res["_status"] = "ok"
    return res, round(0.024 + 0.004 * len(custom_specs), 4)


# ------------------------- industry classification -------------------------

# Fixed umbrella taxonomy so segmentation is clean (one bucket per category,
# no near-duplicates). Override with the INDUSTRY_TAXONOMY env (comma-separated).
DEFAULT_TAXONOMY = [
    "Marketing & Advertising", "Creative & Branding Agency", "SaaS & Software",
    "IT Services & Consulting", "Cybersecurity", "Fintech & Financial Services",
    "Healthcare & Life Sciences", "Manufacturing & Industrial",
    "E-commerce & Retail", "Real Estate & Property", "Construction",
    "Logistics & Supply Chain", "Education & EdTech",
    "Professional Services (Legal/Accounting/HR)", "Staffing & Recruiting",
    "Media & Entertainment", "Energy & Utilities", "Telecommunications",
    "Hospitality & Travel", "Automotive & Transportation",
    "Nonprofit & Government", "Other / Unclear",
]


def taxonomy():
    env = os.getenv("INDUSTRY_TAXONOMY", "").strip()
    if env:
        items = [x.strip() for x in env.split(",") if x.strip()]
        if items:
            return items
    return list(DEFAULT_TAXONOMY)


def _snap_taxonomy(label, tax):
    """Map a model answer to the closest taxonomy bucket. The model is told to
    reply with an exact name; this is a safety net for off-list answers."""
    l = (label or "").strip().lower()
    if not l:
        return "Other / Unclear"
    for t in tax:                      # exact match
        if l == t.lower():
            return t
    for t in tax:                      # one fully contains the other
        tl = t.lower()
        if l in tl or tl in l:
            return t
    # significant-word overlap (>=4 chars; substring match handles compounds
    # like 'cyber security' -> 'Cybersecurity')
    lwords = {w for w in re.findall(r"[a-z]+", l) if len(w) >= 4}
    best, best_n = None, 0
    for t in tax:
        twords = re.findall(r"[a-z]+", t.lower())
        n = 0
        for lw in lwords:
            if any(lw == tw or (len(lw) >= 5 and lw in tw) or (len(tw) >= 5 and tw in lw) for tw in twords):
                n += 1
        if n > best_n:
            best, best_n = t, n
    return best if best_n > 0 else "Other / Unclear"


def classify_industry(lead, tax=None):
    """Return (industry_label, est_cost). Switches on ENRICH_MODE."""
    tax = tax or taxonomy()
    if os.getenv("ENRICH_MODE", "demo").lower() == "real":
        return _classify_real(lead, tax)
    return _classify_demo(lead, tax)


def _classify_demo(lead, tax):
    text = ((lead.get("website") or "") + " " + (lead.get("company") or "")).lower()
    cues = [
        ("cyber|security|infosec", "Cybersecurity"), ("saas|software|app|platform", "SaaS & Software"),
        ("market|advertis|seo|media|ads", "Marketing & Advertising"), ("health|medical|clinic|care|pharma", "Healthcare & Life Sciences"),
        ("bank|fintech|capital|invest|financ", "Fintech & Financial Services"), ("staff|recruit|talent", "Staffing & Recruiting"),
        ("real estate|property|realty", "Real Estate & Property"), ("construct|building", "Construction"),
        ("logistic|freight|supply|shipping", "Logistics & Supply Chain"), ("manufactur|industrial|factory", "Manufacturing & Industrial"),
        ("shop|store|ecom|retail", "E-commerce & Retail"), ("it |consult|managed service", "IT Services & Consulting"),
    ]
    for pat, ind in cues:
        if re.search(pat, text) and ind in tax:
            return ind, 0.0
    h = int(hashlib.md5(text.encode()).hexdigest(), 16)
    return tax[h % len(tax)], 0.0


def _classify_real(lead, tax):
    import sys
    if ENGINE_DIR not in sys.path:
        sys.path.insert(0, ENGINE_DIR)
    try:
        import run as engine
        from openai import OpenAI
    except Exception:
        return "", 0.0
    url = engine.normalize_url(lead.get("website") or "")
    if not url:
        return "", 0.0
    scraped = engine.scrape_company(url, max_pages=1, use_cache=True)  # homepage only = cheap
    content = scraped.get("content", "")
    if not content:
        return "", 0.0  # no website -> no industry (never guess)
    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), max_retries=6, timeout=60.0)
        listing = "\n".join(f"- {t}" for t in tax)
        resp = client.chat.completions.create(
            model=os.getenv("CLASSIFY_MODEL", "gpt-4o-mini"),
            max_completion_tokens=20,
            messages=[
                {"role": "system", "content": "You classify a company's primary industry from a fixed list. Reply with ONLY the exact industry name from the list, nothing else."},
                {"role": "user", "content": f"Pick the single best industry for this company from this list:\n{listing}\n\nReply with only one industry name from the list, copied exactly. Use website facts only.\n\nWEBSITE CONTENT:\n{content[:4000]}"},
            ],
        )
        label = (resp.choices[0].message.content or "").strip()
        return _snap_taxonomy(label, tax), 0.0004
    except Exception:
        return "", 0.0


def load_variable_set(name):
    """Full variable-set JSON from the engine, or {}."""
    try:
        with open(os.path.join(VAR_DIR, f"{name}.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def load_client_profile(name):
    """Full client profile JSON from the engine, or {}."""
    try:
        with open(os.path.join(PROFILE_DIR, f"{name}.json"), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def enrich(lead, base, selected=None, custom_specs=None, profile=None):
    """Entry point used by the job runner. Switches on ENRICH_MODE."""
    if os.getenv("ENRICH_MODE", "demo").lower() == "real":
        return _real_enrich(lead, base, selected, custom_specs, profile)
    return demo_enrich(lead, base, selected, custom_specs)


def _real_enrich(lead, base, selected=None, custom_specs=None, profile=None):
    """Run the live engine for one lead, using the workspace's profile + the
    effective variable set (custom variables override same-named built-ins)."""
    import sys
    if ENGINE_DIR not in sys.path:
        sys.path.insert(0, ENGINE_DIR)
    try:
        import run as engine
        from openai import OpenAI
    except Exception as exc:
        return ({"_status": "error", "_error": f"engine deps not installed: {exc}"[:300]}, 0.0)

    base_spec = {}
    if engine_set_exists(base):
        try:
            with open(os.path.join(VAR_DIR, f"{base}.json"), "r", encoding="utf-8") as fh:
                base_spec = json.load(fh)
        except Exception:
            base_spec = {}
    base_vars = {v.get("name"): v for v in base_spec.get("variables", [])}
    custom_by_name = {s["name"]: s for s in (custom_specs or []) if s.get("name")}

    # Build only the selected variables (+ the always-on gate fields).
    want = list(ALWAYS_KEYS) + [k for k in (selected or []) if k not in ALWAYS_KEYS]
    variables, seen = [], set()
    for k in want:
        if k in seen:
            continue
        seen.add(k)
        if k in custom_by_name:          # custom overrides built-in
            variables.append(custom_by_name[k])
        elif k in base_vars:
            variables.append(base_vars[k])
        elif k == "ICPReview":
            variables.append({"name": "ICPReview", "min_words": 1, "max_words": 1,
                              "allowed_values": ["ICP", "Non-ICP"], "purpose": "Decide ICP or Non-ICP."})
        elif k == "ICP_reason":
            variables.append({"name": "ICP_reason", "min_words": 3, "max_words": 5,
                              "purpose": "3 to 5 word reason for the ICP decision."})

    # Website-only rule for every workspace: drop the base rule that allowed CSV
    # row context, and add hard rules so the writer never cites Apollo/CSV data.
    base_rules = [r for r in base_spec.get("global_output_rules", [])
                  if "csv row context" not in str(r).lower()]
    website_rules = [
        "CRITICAL: Use ONLY facts found in the scraped website content. Do NOT use, infer, "
        "or cite any data from the CSV/row (revenue, employee counts, funding, locations, founding "
        "year, or ANY numbers) unless that exact fact also appears verbatim in the scraped website text.",
        "Never invent, assume, estimate, or extrapolate. If a fact is not on the website, do not state it.",
        "Do not put specific numbers (dollar amounts, revenue, employee counts, years) in the copy "
        "unless those exact numbers appear in the scraped website content.",
        "If the website has little or no usable scraped content, do not fabricate copy.",
    ]
    # ROLE LOCK: there are two companies in every email. The SENDER is the client
    # whose offer lives in the profile; the PROSPECT is the scraped website. The
    # model keeps confusing the two and pitches the prospect's OWN service back to
    # them (e.g. "We specialize in road pricing solutions by running road pricing
    # systems"). These rules pin down whose data goes in which slot. Client-agnostic.
    prof = profile or {}
    sender_name = (prof.get("client_name") or prof.get("name") or "the sender").strip() or "the sender"
    sender_offer = (prof.get("what_we_are_pitching") or prof.get("main_offer")
                    or prof.get("service_brief") or prof.get("target_outcome") or "").strip()
    role_lock = [
        "ROLE LOCK (the single most important rule): every email involves TWO companies. "
        f"The SENDER is OUR client, '{sender_name}', whose offer is described in the profile above. "
        "The PROSPECT is the company on the scraped website. You are writing outreach FROM the sender "
        "TO the prospect. (The sender changes per run - always use whoever the profile above describes, "
        "never a hard-coded company.)",
        "The ONLY service being sold is the SENDER's offer from the profile above. NEVER describe, pitch, "
        "sell, or summarize the PROSPECT's own service, product, method, or deliverable as if the sender "
        "provides it. The sender does NOT do the prospect's job. The sender helps the prospect get more "
        "clients, meetings, and revenue.",
    ]
    if sender_offer:
        role_lock.append(f"The SENDER's offer (this is what '{sender_name}' sells, and the ONLY thing you "
                         "pitch in every line): " + sender_offer)
    role_lock += [
        "value_proposition slot map: revenue_function, ascendly_mechanism and ascendly_solution are "
        "SENDER slots - they describe ONLY the sender's offer from the profile (the growth/lead/client "
        "outcome and how the sender delivers it), regardless of how those slots are named. The PROSPECT "
        "appears ONLY in company_category, personalized_observation and company_name. So in "
        "'We specialize in <X> for <prospect category>, by <Y>', X and Y are ALWAYS the sender's "
        "service from the profile, NEVER the prospect's service.",
        "Error to avoid (real failures): if the prospect sells road pricing, accounting, wealth "
        "management, masonry, or office automation, the line must be about the sender getting THEM more "
        "clients - NEVER about the sender doing road pricing / accounting / wealth management / masonry.",
        "Final check before returning: does sentence 1 describe the SENDER's service from the profile "
        "(bringing the prospect clients, meetings, pipeline), and not the prospect's own service? If it "
        "describes the prospect's service, REWRITE it before returning.",
    ]
    vs = {
        "variable_set_name": "dashboard",
        "max_tokens": base_spec.get("max_tokens", 2200),
        "temperature": base_spec.get("temperature", 0.7),
        "output_keys": [v["name"] for v in variables],
        "global_output_rules": base_rules + website_rules + role_lock,
        "variables": variables,
    }
    if base_spec.get("icp_definition"):
        vs["icp_definition"] = base_spec["icp_definition"]
    if base_spec.get("skip_icp") or (profile or {}).get("skip_icp"):
        vs["skip_icp"] = True

    row = dict(lead)
    row["website"] = lead.get("website") or lead.get("Website") or ""

    try:
        # max_retries gives exponential backoff on 429/rate limits so quality stays
        # consistent under concurrency instead of later leads failing/degrading.
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), max_retries=6, timeout=90.0)
        result = engine.process_row(client, profile or {}, vs, row, "website", max_pages=3, use_cache=True)
    except Exception as exc:
        return ({"_status": "error", "_error": f"{type(exc).__name__}: {exc}"[:300]}, 0.0)

    # tag title-gate rejections so the grid can show / count them
    reason = str(result.get("ICP_reason", "")).lower()
    if result.get("ICPReview") == "Non-ICP" and "title" in reason:
        result["_title_gate"] = "rejected"
    elif result.get("ICPReview") == "ICP":
        result["_title_gate"] = "pass"

    if result.get("ICPReview") == "ICP":
        cost = 0.045
    elif result.get("_title_gate") == "rejected":
        cost = 0.003
    else:
        cost = 0.012
    return (result, cost)
