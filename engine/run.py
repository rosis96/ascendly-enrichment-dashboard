#!/usr/bin/env python3

import os
import re
import json
import time
import hashlib
import argparse
from pathlib import Path
from urllib.parse import urlparse, urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

import warnings
try:
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception:
    pass

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
from dotenv import load_dotenv
from tqdm import tqdm
from openai import OpenAI

load_dotenv()

# --- Model split: cheap model for extraction/classification, strong model for writing ---
# Back-compat: OPENAI_MODEL is used as a fallback if the specific vars are unset.
_LEGACY_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "gpt-4o-mini")
WRITER_MODEL = os.getenv("WRITER_MODEL", "gpt-4o")

# Temperatures. Extraction stays deterministic; writing gets a little room for natural voice.
# If a model rejects a custom temperature, set WRITER_TEMPERATURE=none in .env to omit it.
EXTRACT_TEMPERATURE = os.getenv("EXTRACT_TEMPERATURE", "0")
WRITER_TEMPERATURE = os.getenv("WRITER_TEMPERATURE", "0.6")

# Content budgets. The old 2500 cap starved both the extractor and the writer.
MAX_TOTAL_CONTENT_CHARS = int(os.getenv("MAX_TOTAL_CONTENT_CHARS", "9000"))
EXTRACT_CONTENT_CHARS = int(os.getenv("EXTRACT_CONTENT_CHARS", "9000"))
# The writer already receives the distilled verified facts, so it needs less raw
# page text than the extractor. Smaller = cheaper input tokens per lead.
WRITER_CONTENT_CHARS = int(os.getenv("WRITER_CONTENT_CHARS", "6000"))
REPAIR_CONTENT_CHARS = 1800
REQUEST_DELAY = 0.2
CHECKPOINT_EVERY = 10

# Words/phrases your rules forbid. Listed in the JSON but never previously enforced.
BANNED_WORDS = [
    "leverage", "robust", "seamless", "scalable", "synergy", "transformative",
    "game-changing", "cutting-edge", "future-ready", "unlock", "empower",
    "empowers", "empowering", "elevate", "elevates", "enhance", "enhances",
    "enhancing",
]
BANNED_PHRASES = [
    "quick question", "just checking in", "touching base", "circling back",
    "hope you are well", "hope you're well", "i came across", "great website",
    "impressive work", "amazing services", "strong online presence",
    "innovative solutions", "professional team",
]


def _temp_kwargs(temp_value):
    """Return {'temperature': float} or {} if the env asked to omit it."""
    t = str(temp_value).strip().lower()
    if t in {"", "none", "default", "off"}:
        return {}
    try:
        return {"temperature": float(t)}
    except ValueError:
        return {}

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

PRIORITY_URL_KEYWORDS = [
    "case-study", "case-studies", "portfolio", "projects", "work",
    "clients", "customers", "testimonials", "reviews", "results",
    "about", "services", "solutions", "product", "platform",
    "features", "industries", "who-we-serve",
    # pricing/plans help the strict ICP gate detect public, self-serve pricing
    "pricing", "plans", "packages"
]

LOW_VALUE_URL_KEYWORDS = [
    "privacy", "terms", "cookie", "login", "sign-in", "signup",
    "careers", "jobs", "blog/page", "tag/", "category/", "author/"
]


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_url_column(df):
    candidates = {"website", "url", "domain", "website url", "company url", "company website"}
    for col in df.columns:
        if col.lower().strip() in candidates:
            return col
    raise ValueError(f"No URL column found. Expected website/url/domain. Found: {list(df.columns)}")


def normalize_url(url):
    url = str(url).strip()
    if not url or url.lower() in {"nan", "none"}:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def same_domain(base_url, link):
    try:
        base_host = urlparse(base_url).netloc.replace("www.", "")
        link_host = urlparse(link).netloc.replace("www.", "")
        return base_host == link_host
    except Exception:
        return False


def cache_key(url):
    return re.sub(r"[^a-zA-Z0-9]+", "_", url).strip("_")[:180] + ".json"


def clean_text(text):
    text = re.sub(r"\n{3,}", "\n\n", text or "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def discover_priority_links(home_url, max_pages):
    urls = [home_url]

    try:
        resp = requests.get(home_url, headers=BROWSER_HEADERS, timeout=12)
        if resp.status_code >= 400:
            return urls

        soup = BeautifulSoup(resp.text, "html.parser")
        found = []

        for a in soup.find_all("a", href=True):
            href = a.get("href", "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue

            full = urljoin(home_url + "/", href).split("#")[0].rstrip("/")
            if not same_domain(home_url, full):
                continue

            path = urlparse(full).path.lower()
            if any(bad in path for bad in LOW_VALUE_URL_KEYWORDS):
                continue

            score = 0
            for kw in PRIORITY_URL_KEYWORDS:
                if kw in path:
                    score += 3

            text = a.get_text(" ", strip=True).lower()
            for kw in PRIORITY_URL_KEYWORDS:
                if kw.replace("-", " ") in text:
                    score += 2

            if score > 0 and full not in [u for _, u in found] and full != home_url:
                found.append((score, full))

        found.sort(key=lambda x: x[0], reverse=True)
        urls.extend([u for _, u in found[: max_pages - 1]])

    except Exception:
        pass

    deduped = []
    for u in urls:
        if u not in deduped:
            deduped.append(u)

    return deduped[:max_pages]


def fetch_html(url):
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code < 400 and resp.text:
            return resp.text
    except Exception:
        pass
    return ""


def url_variants(url):
    """Try www/non-www and http fallbacks so a site that blocks one form still gets read."""
    variants = [url]
    if "://www." in url:
        variants.append(url.replace("://www.", "://", 1))
    else:
        variants.append(url.replace("://", "://www.", 1))
    if url.startswith("https://"):
        variants.append("http://" + url[len("https://"):])
    seen, out = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def scrape_url(url, use_cache=True):
    path = CACHE_DIR / cache_key(url)

    if use_cache and path.exists():
        return load_json(path)

    data = {"url": url, "text": ""}
    html = ""
    for v in url_variants(url):
        html = fetch_html(v)
        if html:
            break

    if html:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
                tag.decompose()
            data["text"] = clean_text(soup.get_text("\n", strip=True))
        except Exception:
            pass

    # Only cache successful scrapes. Caching empties would stop a resume from
    # re-trying failed sites with the hardened scraper.
    if use_cache and data["text"]:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    return data


def scrape_company(website_url, max_pages, use_cache=True):
    urls = discover_priority_links(website_url, max_pages=max_pages)
    parts = []
    scraped_urls = []

    for url in urls:
        page = scrape_url(url, use_cache=use_cache)
        if page.get("text"):
            scraped_urls.append(url)
            parts.append(f"\n\n--- PAGE: {url} ---\n{page['text']}")
        time.sleep(REQUEST_DELAY)

    content = clean_text("\n".join(parts))[:MAX_TOTAL_CONTENT_CHARS]
    return {"urls": scraped_urls, "content": content}


def variable_instructions(variable_set):
    """Send each variable's COMPLETE spec to the model, verbatim.

    The specs are hand-researched and must not be cropped. We surface the
    hard word range up top (that's what the validator enforces), then dump the
    entire spec object so the model receives every rule, example, priority
    order, and template exactly as written.
    """
    blocks = []
    for var in variable_set["variables"]:
        name = var.get("name", "")
        min_w = var.get("min_words", "natural")
        max_w = var.get("max_words", "natural")
        spec = {k: v for k, v in var.items() if k not in ("name", "min_words", "max_words")}
        block = (
            f"VARIABLE: {name}\n"
            f"HARD WORD RANGE: {min_w}-{max_w} words\n"
            f"FULL SPECIFICATION (follow exactly, do not ignore any part):\n"
            f"{json.dumps(spec, ensure_ascii=False, indent=2)}"
        )
        blocks.append(block)
    return "\n\n" + ("\n\n" + ("=" * 60) + "\n\n").join(blocks)


def proof_grounding_block(verified_facts, row_context):
    """Tell the writer which proof it may use, for the value_proposition's
    personalized_observation. Names not on this list (and not the prospect's
    own company) must not be presented as proof."""
    names = [str(n).strip() for n in verified_facts.get("verified_clients_or_projects", []) if str(n).strip()]
    company = str(row_context.get("companyName") or row_context.get("Company") or "").strip()
    proof_line = ", ".join(names[:8]) if names else "(no specific client/project names verified)"
    return (
        "PROOF GROUNDING FOR value_proposition (personalized_observation):\n"
        f"  PROSPECT COMPANY NAME (use this for company_name): {company or '(see row context)'}\n"
        f"  VERIFIED CLIENT/PROJECT NAMES YOU MAY CITE AS PROOF: {proof_line}\n"
        "Build the observation only from the verified facts and website content below. "
        "You may describe the prospect's own services, positioning, methodology, metrics, "
        "or industry focus freely. But do NOT name any client, customer, partner, or project "
        "that is not in the verified list above. Never invent a client name or a statistic."
    )


WRITER_PROFILE_DROP_KEYS = {"icp", "non_icp", "icp_review_rules", "icp_review_output", "icp_summary"}


def writer_profile(client_profile):
    """The writer never decides ICP (the extractor already did), so the ICP
    rule sections are dead weight on every writer call. Drop them; keep the
    offer/tone/research sections the writer actually uses."""
    return {k: v for k, v in client_profile.items() if k not in WRITER_PROFILE_DROP_KEYS}


def variation_directive(variable_set, row_context, verified_facts=None):
    """Force per-company variation AND give every personalization a different
    specific detail and a different angle, so first_line, the value-prop
    observation, and product_complimentary never circle the same fact."""
    names = {v.get("name") for v in variable_set.get("variables", [])}
    if not ({"value_proposition", "personalized_first_line", "product_complimentary"} & names):
        return ""  # set has no cold-email personalization variables (e.g. full-email follow-ups)
    vp = next((v for v in variable_set["variables"] if v.get("name") == "value_proposition"), {})
    mechs = vp.get("mechanism_variants", [])
    sols = vp.get("solution_variants", [])
    seed = str(row_context.get("companyName") or row_context.get("website") or "x")
    n = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16)
    m = int(hashlib.md5((seed + "|sol").encode("utf-8")).hexdigest(), 16)

    obs_angles = [
        "who they serve or their target market",
        "their scale, reach, footprint, or how long they have operated",
        "what makes their approach or positioning different from peers",
        "the specific niche, segment, or use case they focus on",
        "how they combine or sequence their offerings",
    ]
    angle = obs_angles[n % len(obs_angles)]

    facts = verified_facts or {}
    flagship = str(facts.get("flagship_offering", "") or "").strip()

    out = ["WRITE FOR THIS SPECIFIC COMPANY. Do not let the email read like a template:"]
    if mechs and sols:
        out.append(f"- In sentence 1, phrase the mechanism around this idea (reword naturally, never verbatim): {mechs[n % len(mechs)]}")
        out.append(f"- In sentence 3, use this solution lever: {sols[m % len(sols)]}")
    out += [
        "- Build the personalization from the SPECIFIC, hard-to-fake details in the verified facts above "
        "(flagship_offering, named_projects_or_case_studies, specific_services, notable_specifics, differentiator). "
        "Name the actual thing. A generic line a competitor could copy word-for-word is a failure.",
        "- Give each personalization a DIFFERENT specific detail AND a different angle:",
        "    * personalized_first_line: the single sharpest specific detail (a named product, framework, project, metric, or niche). One thing, not a list.",
        f"    * value_proposition observation ('I have seen how ...'): a DIFFERENT detail, on this angle - {angle}. Must not reuse the first line's detail or wording.",
        "    * product_complimentary: a THIRD, different specific thing (another named service, product, resource, or project) with one easy question about THAT thing.",
        "- No specific detail may appear in more than one of those three variables. If you genuinely found only one strong specific, "
        "use it once in the first line and take clearly different angles for the other two.",
        "- Use the company's FULL name exactly as it appears; never shorten a name like 'Provencher & Company' to '& Company'. "
        "If the name ends in s, write its possessive with a trailing apostrophe only (Global Navigators' not Global Navigators's).",
        "- For 'We specialize in ___', lead with a noun from THIS company's own world "
        "(their buyer, audience, industry, or category). Do not reuse the same opening phrasing across companies.",
        "- Choose results that fit this specific company; never reuse one generic result set.",
    ]
    if flagship:
        out.append(f"- The most distinctive thing on their site appears to be: {flagship}. Feature it in exactly ONE variable, not several.")
    return "\n".join(out)


def _skip_icp(variable_set, client_profile):
    """True when this campaign should do NO ICP gating (write for every lead)."""
    return bool((variable_set or {}).get("skip_icp") or (client_profile or {}).get("skip_icp"))


def _effective_output_rules(variable_set, client_profile):
    """The variable set's global_output_rules, with ICP-gating lines removed when
    skip_icp is on, so the writer is never told to N/A-out a 'Non-ICP' company."""
    rules = list(variable_set.get("global_output_rules", []))
    if _skip_icp(variable_set, client_profile):
        rules = [r for r in rules
                 if "non-icp" not in r.lower() and "first decide icpreview" not in r.lower()]
        rules.append('Every company in this campaign is in scope. ALWAYS set ICPReview to "ICP".')
        rules.append('Never return "N/A" for ICP or fit reasons. Generate ALL variables for EVERY company using the website facts.')
    return rules


def _icp_output_clause(variable_set, client_profile):
    """The ICP bullet lines for the writer's STRICT OUTPUT section."""
    if _skip_icp(variable_set, client_profile):
        return (
            '- ICPReview is ALWAYS "ICP" for every company. Do not classify, exclude, or mark anyone Non-ICP.\n'
            "- Write ICP_reason as a short 3-5 word note on what the company does.\n"
            '- Generate ALL variables for EVERY company using the website facts. Never return "N/A" for fit or ICP reasons.'
        )
    return (
        "- First decide ICPReview using the client profile ICP rules, then write ICP_reason (3-5 words).\n"
        "- ICP_reason must ALWAYS be filled, even when ICPReview is Non-ICP.\n"
        '- If ICPReview is Non-ICP, every key EXCEPT ICPReview and ICP_reason must be "N/A".\n'
        "- If ICPReview is ICP, generate all variables."
    )


def _distinct_personalization_line(variable_set):
    """Only emit the first-line vs observation distinctness rule when both of
    those variables actually exist in the set (cold-email sets, not follow-ups)."""
    names = {v.get("name") for v in variable_set.get("variables", [])}
    if {"personalized_first_line", "value_proposition"} <= names:
        return ('- personalized_first_line and the value_proposition observation ("I have seen how ...") '
                "must describe DIFFERENT aspects of the company. Never restate the first line inside the value proposition.")
    return ""


def build_prompt(client_profile, variable_set, website_url, website_content, row_context,
                 verified_facts=None):
    output_keys = variable_set["output_keys"]
    verified_facts = verified_facts or {}

    # IMPORTANT: everything that is IDENTICAL across leads goes FIRST (client
    # profile, rules, full variable spec, output instructions). OpenAI then
    # automatically caches this long prefix and bills repeats at a discount.
    # The per-lead parts (facts, proof, row, website) go LAST.
    return f"""
You are generating outbound personalization variables.

CLIENT PROFILE:
{json.dumps(writer_profile(client_profile), ensure_ascii=False, indent=2)}

VARIABLE SET RULES:
{json.dumps(_effective_output_rules(variable_set, client_profile), ensure_ascii=False, indent=2)}

VARIABLES TO GENERATE:
{variable_instructions(variable_set)}

STRICT OUTPUT:
- Return one valid JSON object only.
- No markdown.
- No explanation.
- Use exactly these keys: {", ".join(output_keys)}.
- Every value must be a string.
{_icp_output_clause(variable_set, client_profile)}
- Never use contact first name, last name, full name, founder name, or employee name.
- Use only website content and CSV row context.
- Do not invent facts.
{_distinct_personalization_line(variable_set)}

=== PER-LEAD INPUT BELOW ===

VERIFIED FACTS YOU ARE ALLOWED TO USE:
{proof_context_text(verified_facts)}

{proof_grounding_block(verified_facts, row_context)}

{variation_directive(variable_set, row_context, verified_facts)}

ROW CONTEXT:
{json.dumps(row_context, ensure_ascii=False, indent=2)}

WEBSITE URL:
{website_url}

WEBSITE CONTENT:
{website_content[:WRITER_CONTENT_CHARS]}

Return JSON only.
""".strip()


def safe_json_loads(raw):
    raw = (raw or "").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError("Could not parse JSON")


# Default (liberal) ICP procedure, used for any campaign whose variable set does
# NOT define a strict icp_definition. Keeps existing behavior for other clients.
LIBERAL_ICP_PROCEDURE = """ICP DECISION PROCEDURE (follow in order):
1. DEFAULT to ICP. Any legitimate business that sells to other businesses, has real deal/contract value, or could benefit from booked sales meetings is ICP. This includes B2B, SaaS, software, technology, IT, consulting, agencies, professional services, healthcare, financial, legal, marketing, creative, industrial, manufacturing, logistics, construction, real estate, and similar companies.
2. Return Non-ICP ONLY if the company clearly matches one of these: a purely local consumer business (restaurant, cafe, salon, gym, spa, barber), a tiny low-ticket retail/ecommerce store, a personal blog or hobby/creator site, a parked or empty domain, or a site with no discernible real business.
3. If you are UNSURE, or the site is thin but looks like a real company, choose ICP. Do NOT use Non-ICP as a safe fallback for uncertainty. Only mark Non-ICP when you are confident it fits step 2."""

# Profile sections that carry ICP-decision language. In strict mode these are
# dropped from the extractor prompt so only the variable set's strict
# icp_definition governs the decision (no conflicting liberal guidance).
ICP_PROFILE_KEYS = {"icp", "non_icp", "icp_review_rules", "icp_summary", "icp_review_output"}


def _icp_procedure_text(variable_set):
    """Return (procedure_text, strict_flag).

    If the variable set defines an `icp_definition`, render it into an
    authoritative STRICT procedure. Otherwise fall back to the liberal default
    so other campaigns are unaffected.
    """
    icpdef = (variable_set or {}).get("icp_definition")
    if not icpdef:
        return LIBERAL_ICP_PROCEDURE, False

    lines = [
        "ICP DECISION PROCEDURE (STRICT - follow in order). These rules are "
        "AUTHORITATIVE and OVERRIDE any ICP guidance elsewhere in the client profile:"
    ]
    for i, step in enumerate(icpdef.get("procedure") or [], 1):
        lines.append(f"{i}. {step}")

    cats = icpdef.get("icp_categories") or []
    if cats:
        lines.append("")
        lines.append("ALLOWED ICP CATEGORIES (return ICP only if it clearly fits one of these):")
        lines.extend(f"- {c}" for c in cats)

    hard = icpdef.get("hard_non_icp") or []
    if hard:
        lines.append("")
        lines.append("HARD Non-ICP OVERRIDES (return Non-ICP even if it otherwise looks like a fit):")
        lines.extend(f"- {h}" for h in hard)

    lines.append("")
    lines.append(f"When in doubt, return {icpdef.get('default', 'Non-ICP')}.")
    return "\n".join(lines), True


def _extractor_profile(client_profile, strict):
    """In strict mode, drop the profile's own ICP sections so they cannot
    dilute the variable set's strict gate. Offer/research context is kept."""
    if not strict:
        return client_profile
    return {k: v for k, v in client_profile.items() if k not in ICP_PROFILE_KEYS}


def extract_verified_facts(client, client_profile, url, content, row_context, variable_set=None):
    icp_procedure, strict_icp = _icp_procedure_text(variable_set)
    profile_for_extractor = _extractor_profile(client_profile, strict_icp)

    prompt = f"""
Extract only verified facts from the scraped website content.

Return valid JSON only with exactly these keys:
ICPReview, icp_reason, has_public_pricing, ideal_client_types, verified_clients_or_projects, verified_services, verified_industries, company_category, case_study_confidence, flagship_offering, specific_services, named_projects_or_case_studies, niche_or_specialty, differentiator, notable_specifics

Your most important job is to capture SPECIFIC, HARD-TO-FAKE details a person could only know by actually reading THIS website. Always prefer named, concrete things over generic descriptions.

Rules:
- verified_clients_or_projects must contain only names directly visible in the website content or CSV row.
- Do not invent client names, project names, brands, logos, industries, or results.
- If no named clients/projects/logos/case studies are visible, return an empty array for verified_clients_or_projects.
- case_study_confidence must be High, Medium, Low, or None.
- flagship_offering: the SINGLE most distinctive named product, service, framework, methodology, or program, copied in their exact wording. Use "" if nothing named stands out.
- specific_services: array of the most specific NAMED services or offerings, in their words (not generic categories like "consulting" or "marketing").
- named_projects_or_case_studies: array of specific named projects, case studies, or named clients visible on the site. Empty array if none.
- niche_or_specialty: the specific niche, segment, customer type, or use case they focus on, in their words. "" if unclear.
- differentiator: one specific thing that sets them apart (a named method, an unusual combination of services, a credential, a stated focus). "" if unclear.
- notable_specifics: array of concrete facts found only on their site, e.g. years in business, named markets/locations served, named tools or technologies, specific metrics or numbers they state, awards. Each item must be grounded verbatim in the content. Empty array if none.
- Every value in these specific fields must be grounded in the scraped content. Never invent.
- has_public_pricing: "Yes" if the website shows ANY public pricing (a pricing/plans/packages page, listed dollar amounts, "Starting at", tiered or monthly plan prices, or a self-serve checkout/signup), otherwise "No". Base this only on what is visible in the scraped content.
- ideal_client_types: array of the specific types of clients or customers THIS company serves, in their words (named client brands they list, the industries or buyer types they target, the kind of customer they focus on). This captures who they win as clients so outreach can speak to it. Empty array if unclear. Never invent.
- ICPReview must be exactly "ICP" or "Non-ICP".
- icp_reason must be 3 to 5 words explaining WHY the company is ICP or why it is Non-ICP, specific to this company (e.g. "high-value B2B SaaS" or "local consumer restaurant"). Always fill it.

{icp_procedure}

CLIENT PROFILE:
{json.dumps(profile_for_extractor, ensure_ascii=False, indent=2)}

ROW CONTEXT:
{json.dumps(row_context, ensure_ascii=False, indent=2)}

WEBSITE URL:
{url}

SCRAPED WEBSITE CONTENT:
{content[:EXTRACT_CONTENT_CHARS]}
""".strip()

    messages = [
        {"role": "system", "content": "You extract verified facts only. Return valid JSON only. Do not write copy."},
        {"role": "user", "content": prompt},
    ]

    data = None
    for _ in range(2):  # one retry on a bad/unparseable response
        try:
            response = client.chat.completions.create(
                model=EXTRACT_MODEL,
                max_completion_tokens=1300,
                response_format={"type": "json_object"},
                **_temp_kwargs(EXTRACT_TEMPERATURE),
                messages=messages,
            )
            data = safe_json_loads(response.choices[0].message.content or "")
            break
        except Exception:
            data = None
            continue

    if not isinstance(data, dict):
        data = {}  # graceful fallback: defaults below make this a clean Non-ICP

    data.setdefault("ICPReview", "Non-ICP")
    data.setdefault("icp_reason", "")
    data.setdefault("has_public_pricing", "No")
    data.setdefault("ideal_client_types", [])
    data.setdefault("verified_clients_or_projects", [])
    data.setdefault("verified_services", [])
    data.setdefault("verified_industries", [])
    data.setdefault("company_category", "")
    data.setdefault("case_study_confidence", "None")
    data.setdefault("flagship_offering", "")
    data.setdefault("specific_services", [])
    data.setdefault("named_projects_or_case_studies", [])
    data.setdefault("niche_or_specialty", "")
    data.setdefault("differentiator", "")
    data.setdefault("notable_specifics", [])

    if not isinstance(data["verified_clients_or_projects"], list):
        data["verified_clients_or_projects"] = []
    if not isinstance(data["verified_services"], list):
        data["verified_services"] = []
    if not isinstance(data["verified_industries"], list):
        data["verified_industries"] = []
    for _k in ("specific_services", "named_projects_or_case_studies", "notable_specifics", "ideal_client_types"):
        if not isinstance(data.get(_k), list):
            data[_k] = []

    return data


def proof_context_text(facts):
    proof = facts.get("verified_clients_or_projects", [])
    services = facts.get("verified_services", [])
    industries = facts.get("verified_industries", [])

    return json.dumps({
        "ICPReview": facts.get("ICPReview", "Non-ICP"),
        "flagship_offering": facts.get("flagship_offering", ""),
        "specific_services": (facts.get("specific_services") or [])[:8],
        "named_projects_or_case_studies": (facts.get("named_projects_or_case_studies") or [])[:8],
        "niche_or_specialty": facts.get("niche_or_specialty", ""),
        "differentiator": facts.get("differentiator", ""),
        "notable_specifics": (facts.get("notable_specifics") or [])[:8],
        "ideal_client_types": (facts.get("ideal_client_types") or [])[:8],
        "verified_clients_or_projects": proof[:8],
        "verified_services": services[:8],
        "verified_industries": industries[:8],
        "company_category": facts.get("company_category", ""),
        "case_study_confidence": facts.get("case_study_confidence", "None")
    }, ensure_ascii=False, indent=2)


def grounded_proof_names(facts, content):
    """Verified names that literally appear in the scraped content."""
    content_l = str(content).lower()
    out = []
    for n in facts.get("verified_clients_or_projects", []):
        n = str(n).strip()
        if n and n.lower() in content_l:
            out.append(n)
    return out


def should_allow_case_study_format(facts, content=None):
    # Allow case-study format only when at least 2 verified names are actually
    # present in the scraped text. Ties the decision to real grounding, not a
    # self-reported confidence label (which the extractor often under-rates).
    if content is not None:
        return len(grounded_proof_names(facts, content)) >= 2
    proof = [x for x in facts.get("verified_clients_or_projects", []) if str(x).strip()]
    return len(proof) >= 2


def brand_like_tokens(text):
    # Capitalized multi-word chunks that look like names. Split on sentence
    # boundaries first so a period never stitches two sentences into one token.
    ignore = {
        "But", "Rather", "That", "This", "Clients", "Your", "The", "We",
        "Strong", "Serious", "Real", "Portfolio", "Instead", "Client",
        "Trusted", "Technology", "And", "From", "Our", "I", "A", "An",
    }
    tokens = []
    for frag in re.split(r"[.!?\n]+", str(text)):
        for c in re.findall(r"\b[A-Z][A-Za-z0-9&'-]*(?:\s+[A-Z][A-Za-z0-9&'-]*){0,3}\b", frag):
            c = c.strip()
            if c and c.split()[0] not in ignore:
                tokens.append(c)
    return tokens


# Ascendly's own mechanism/solution words are legitimately capitalized in the
# value_proposition template, so they must not be flagged as "invented" names.
ASCENDLY_SAFE_TOKENS = {
    "outreach", "retargeting", "visitor", "identification", "lead", "capture",
    "follow-up", "follow", "up", "pipeline", "automation", "revenue", "systems",
    "demand", "engagement", "multi-channel", "ai-personalization", "ai",
    "personalization", "we", "and", "i", "our", "ascendly",
}


def proof_name_is_allowed(name, facts, content, row_context=None):
    name_l = str(name).lower().strip()
    if not name_l:
        return True

    # Ascendly's own solution/mechanism words.
    if all(w in ASCENDLY_SAFE_TOKENS for w in name_l.split()):
        return True

    # The prospect's own company name is always fine.
    if row_context:
        company = str(row_context.get("companyName") or row_context.get("Company") or "").lower().strip()
        if company and (name_l in company or company in name_l):
            return True

    # Anything literally on the scraped page is fine.
    if name_l in str(content).lower():
        return True

    # Verified client/project names are fine.
    for p in facts.get("verified_clients_or_projects", []):
        p_l = str(p).lower().strip()
        if p_l and (name_l == p_l or name_l in p_l or p_l in name_l):
            return True

    return False


def validate_proof_usage(data, facts, content, row_context=None):
    """Block invented client/proof names in the value_proposition. Ascendly's
    own mechanism words and the prospect's own company name are whitelisted."""
    issues = []
    email = str(data.get("value_proposition", "") or "")
    if not email or email.upper() == "N/A":
        return issues

    seen = set()
    for name in brand_like_tokens(email):
        if name in seen:
            continue
        seen.add(name)
        if not proof_name_is_allowed(name, facts, content, row_context):
            issues.append(f"value_proposition: possibly invented name: {name}")
    return issues


def call_openai(client, client_profile, variable_set, url, content, row_context,
                verified_facts=None):
    prompt = build_prompt(client_profile, variable_set, url, content, row_context,
                          verified_facts=verified_facts)

    response = client.chat.completions.create(
        model=WRITER_MODEL,
        max_completion_tokens=variable_set.get("max_tokens", 2000),
        response_format={"type": "json_object"},
        **_temp_kwargs(variable_set.get("temperature", WRITER_TEMPERATURE)),
        messages=[
            {
                "role": "system",
                "content": "Return valid JSON only. Follow exact variable keys, formats, and word ranges."
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    raw = response.choices[0].message.content or ""
    data = safe_json_loads(raw)

    for key in variable_set["output_keys"]:
        data.setdefault(key, "")

    return {key: str(data.get(key, "")) for key in variable_set["output_keys"]}


def word_count(text):
    return len(re.findall(r"\b[\w'-]+\b", str(text)))


def target_count(value):
    parts = [x.strip(" &") for x in re.split(r",|&", str(value)) if x.strip(" &")]
    return len(parts)


def validate(data, variable_set):
    issues = []
    skip_icp = bool(variable_set.get("skip_icp"))
    skip_style = bool(variable_set.get("skip_style_checks"))

    for key in variable_set["output_keys"]:
        if key not in data:
            issues.append(f"{key}: missing")

    for var in variable_set["variables"]:
        name = var["name"]
        value = str(data.get(name, "")).strip()

        if name == "ICPReview":
            if value not in {"ICP", "Non-ICP"}:
                issues.append("ICPReview: must be ICP or Non-ICP")
            continue

        if value.upper() == "N/A":
            if skip_icp:
                issues.append(f"{name}: must not be N/A (skip_icp on); write a real value from the website content")
            continue

        if "—" in value or "–" in value:
            issues.append(f"{name}: contains em dash")

        low = value.lower()
        if not skip_style:
            for w in BANNED_WORDS:
                if re.search(rf"\b{re.escape(w)}\b", low):
                    issues.append(f"{name}: remove banned word '{w}'")
            for ph in BANNED_PHRASES:
                if ph in low:
                    issues.append(f"{name}: remove banned phrase '{ph}'")

        min_w = var.get("min_words")
        max_w = var.get("max_words")
        if min_w and max_w:
            wc = word_count(value)
            if wc < min_w:
                issues.append(f"{name}: {wc} words, need {min_w}-{max_w} (add {min_w - wc} more)")
            elif wc > max_w:
                issues.append(f"{name}: {wc} words, need {min_w}-{max_w} (cut {wc - max_w})")

        if name == "product_complimentary":
            # Transition is now OPTIONAL (your new spec), but it must still be a question.
            if not value.endswith("?"):
                issues.append(f"{name}: must end with a question mark")

        if name == "company_category":
            allowed = [str(s).lower() for s in var.get("must_end_with_one_of", [])]
            if allowed:
                words = re.findall(r"[A-Za-z]+", value)
                if words and words[-1].lower() not in allowed:
                    issues.append(f"{name}: must end with one of {', '.join(allowed)}")

        if name == "ideal_customers":
            if target_count(value) != 3:
                issues.append(f"{name}: must have exactly 3 targets in the form A, B, & C")

    return issues


def repair(client, client_profile, variable_set, url, content, row_context, data, issues):
    # Only send the specs for the fields that actually failed. The full spec is
    # ~9.5k tokens; on a repair we usually need 1-3 fields, so this cuts repair
    # input by ~70-90% with no loss of fix quality.
    failed = set()
    for iss in issues:
        head = str(iss).split(":", 1)[0].strip()
        if head:
            failed.add(head)
    failed_vars = [v for v in variable_set["variables"] if v.get("name") in failed]
    if failed_vars:
        mini = {"variables": failed_vars}
        rules_text = variable_instructions(mini)
    else:
        rules_text = variable_instructions(variable_set)

    skip_note = ""
    if bool(variable_set.get("skip_icp")):
        skip_note = ('EVERY company in this campaign is in scope. Keep ICPReview as "ICP". '
                     'Never return "N/A" for any field for fit or ICP reasons. Write a real, '
                     "specific value for every failed field using the website content.\n\n")

    prompt = f"""
Fix only the fields that failed validation.
Keep all valid fields unchanged.
Return the full corrected JSON object only.

{skip_note}FAILED ISSUES:
{json.dumps(issues, ensure_ascii=False, indent=2)}

CURRENT JSON:
{json.dumps(data, ensure_ascii=False, indent=2)}

OUTPUT KEYS:
{json.dumps(variable_set["output_keys"], ensure_ascii=False)}

RULES FOR THE FAILED FIELDS ONLY:
{rules_text}

ROW CONTEXT:
{json.dumps(row_context, ensure_ascii=False, indent=2)}

WEBSITE CONTENT CONDENSED:
{content[:REPAIR_CONTENT_CHARS]}
""".strip()

    response = client.chat.completions.create(
        model=WRITER_MODEL,
        max_completion_tokens=variable_set.get("max_tokens", 2000),
        response_format={"type": "json_object"},
        **_temp_kwargs(variable_set.get("temperature", WRITER_TEMPERATURE)),
        messages=[
            {
                "role": "system",
                "content": "Repair JSON fields only. Return valid JSON only."
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    repaired = safe_json_loads(response.choices[0].message.content or "")
    merged = dict(data)

    for key in variable_set["output_keys"]:
        if key in repaired:
            merged[key] = str(repaired[key])

    return merged



def force_followup_linebreaks(data):
    """
    Force paragraph breaks for follow-up body fields.
    This prevents Google Sheets / CSV output from becoming one clumsy paragraph.
    """
    if not isinstance(data, dict):
        return data

    paragraph_starters = [
        "Because ",
        "We run ",
        "I'd love ",
        "I’d love ",
        "Right now, ",
        "We fix ",
        "Let's skip ",
        "Let’s skip ",
        "Honestly, ",
        "A brand ",
        "I built ",
        "It got me thinking ",
        "The fix ",
        "I mapped ",
        "Most outbound ",
        "We connect ",
        "I want ",
        "Usually, ",
        "We can ",
        "I'm putting ",
        "I’m putting ",
        "The company ",
        "If scaling ",
        "If it is, ",
        "I'll stop ",
        "I’ll stop ",
        "If it becomes ",
        "Either way, "
    ]

    for key in list(data.keys()):
        if not (key.startswith("step") and key.endswith("_body")):
            continue

        text = str(data.get(key, "") or "").strip()

        if not text or text.upper() == "N/A":
            data[key] = text
            continue

        # Convert literal escaped newlines if the model returned "\\n"
        text = text.replace("\\n\\n", "\n\n").replace("\\n", "\n")

        # Remove accidental greetings/placeholders
        text = text.replace("Hey {{prospect_name}},", "").replace("Hi {{prospect_name}},", "")
        text = text.replace("{{prospect_name}},", "").replace("{{prospect_name}}", "")
        text = text.replace("{{company_name}}", "the company")
        text = text.replace("{{agency_success_metric}}", "a measurable lift in booked meetings")

        # If it already has paragraph breaks, just clean spacing
        if "\n\n" not in text:
            for starter in paragraph_starters:
                text = text.replace(" " + starter, "\n\n" + starter)

        # Clean excessive line breaks/spaces
        lines = [line.strip() for line in text.splitlines()]
        cleaned = []
        for line in lines:
            if line:
                cleaned.append(line)

        text = "\n\n".join(cleaned)

        # Remove any remaining bracket/merge-token artifacts
        text = text.replace("[", "").replace("]", "")
        text = text.replace("{{", "").replace("}}", "")

        data[key] = text.strip()

    return data



def scrub_names(data, row_context):
    name_keys = [
        "firstName", "First Name", "firstname", "first_name", "FirstName",
        "lastName", "Last Name", "lastname", "last_name", "LastName",
        "name", "Name", "fullName", "Full Name", "contactName", "Contact Name"
    ]

    names = []
    for key in name_keys:
        value = str(row_context.get(key, "") or "").strip()
        if value and value.lower() not in {"nan", "none", "null"}:
            names.extend([p for p in value.replace(",", " ").split() if len(p) >= 2])

    if not names:
        return data

    cleaned = dict(data)
    for key, value in cleaned.items():
        new_value = str(value)
        for name in sorted(set(names), key=len, reverse=True):
            new_value = re.sub(rf"\b{re.escape(name)}\b,?\s*", "", new_value)
        cleaned[key] = re.sub(r"\s{2,}", " ", new_value).strip()

    return cleaned


def normalize_icp(raw):
    """Turn whatever the model returned into ICP / Non-ICP robustly.

    - empty / failed extraction -> Non-ICP (don't write copy on nothing)
    - anything containing 'non' -> Non-ICP
    - anything containing 'icp' -> ICP
    - any other non-empty answer (model unsure/verbose) -> ICP

    This deliberately leans ICP when the model is unsure, so a real business
    is never dropped to Non-ICP just because the wording wasn't exactly 'ICP'.
    """
    s = str(raw or "").strip().lower()
    if "non" in s or "not icp" in s or "not a fit" in s:
        return "Non-ICP"
    if "icp" in s:
        return "ICP"
    if s in {"", "n/a", "na"}:
        return "Non-ICP"   # genuinely empty / extraction failed -> don't write copy
    return "ICP"           # any other hedge ("unsure", "maybe", "yes") -> lean ICP


def _title_gate_config(variable_set):
    """Return the enabled title_gate config (from the strict icp_definition),
    or None when this campaign does not gate on title."""
    icpdef = (variable_set or {}).get("icp_definition") or {}
    tg = icpdef.get("title_gate") or {}
    return tg if tg.get("enabled") else None


def _title_from_row(row_context, tg):
    cols = tg.get("source_columns") or [tg.get("source_column", "Title")]
    lower_map = {str(k).strip().lower(): v for k, v in row_context.items()}
    for col in cols:
        v = lower_map.get(str(col).strip().lower())
        if v is not None:
            v = str(v).strip()
            if v and v.lower() not in {"nan", "none", "null"}:
                return v
    return ""


def _approved_titles_flat(tg):
    out = []
    for v in (tg.get("approved_levels") or {}).values():
        if isinstance(v, list):
            out.extend(str(x) for x in v)
    return out


# Strong seniority cues for the no-API fallback path. Kept deliberately strict so
# the fallback never passes a junior title just because a word overlaps.
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


def _norm_title(s):
    s = re.sub(r"[^a-z0-9& ]+", " ", str(s).lower())
    return re.sub(r"\s+", " ", s).strip()


def _keyword_title_pass(title, tg):
    t = _norm_title(title)
    padded = f" {t} "
    # whole approved titles (drop parenthetical notes), matched as phrases
    for a in _approved_titles_flat(tg):
        a = _norm_title(a.split(" (")[0])
        if a and f" {a} " in padded:
            return True
    # seniority cues, matched on word boundaries so short abbreviations
    # (cco, cro, vp, ...) never match inside a longer word like "account"
    for cue in _TITLE_SENIOR_CUES:
        if re.search(rf"(?<![a-z0-9]){re.escape(cue.strip())}(?![a-z0-9])", t):
            return True
    return False


def title_gate_check(oai_client, variable_set, row_context):
    """First-pass screen on the contact's job title. Returns (passed, reason).

    Uses the cheap extract model to judge vague/multi Apollo titles by meaning,
    with a strict keyword fallback if the API call fails. When the title fails,
    the caller must mark the lead Non-ICP WITHOUT scraping the website.
    """
    tg = _title_gate_config(variable_set)
    if not tg:
        return True, ""  # no gate for this campaign

    title = _title_from_row(row_context, tg)
    if not title:
        return False, "Title missing or unknown"

    approved_block = json.dumps(tg.get("approved_levels", {}), ensure_ascii=False, indent=2)
    rules_block = "\n".join(f"- {r}" for r in (tg.get("analysis_rules") or []))
    prompt = f"""You are screening a sales prospect by JOB TITLE ONLY.
Decide if this contact is a senior decision-maker we are allowed to contact.

APPROVED SENIOR LEVELS:
{approved_block}

RULES:
{rules_block}

CONTACT TITLE: {title}

Return JSON only: {{"decision": "approved" or "rejected", "reason": "3-5 words"}}""".strip()

    for _ in range(2):
        try:
            resp = oai_client.chat.completions.create(
                model=EXTRACT_MODEL,
                max_completion_tokens=60,
                response_format={"type": "json_object"},
                **_temp_kwargs(EXTRACT_TEMPERATURE),
                messages=[
                    {"role": "system", "content": "You classify the seniority of a job title. Return valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
            )
            data = safe_json_loads(resp.choices[0].message.content or "")
            decision = str(data.get("decision", "")).strip().lower()
            reason = str(data.get("reason", "")).strip()
            if decision == "approved":
                return True, reason or "Senior decision-maker"
            if decision == "rejected":
                return False, reason or "Title not a decision-maker"
        except Exception:
            continue

    # API failed: strict keyword fallback so we still gate rather than pass everyone.
    if _keyword_title_pass(title, tg):
        return True, "Senior title matched"
    return False, "Title not a decision-maker"


def process_row(oai_client, client_profile, variable_set, row, url_col, max_pages, use_cache):
    url = normalize_url(row.get(url_col, ""))
    if not url:
        return {"_status": "error", "_error": "Empty URL"}

    row_context = {
        k: str(v) for k, v in row.items()
        if pd.notna(v) and str(v).strip() and not str(k).startswith("_")
    }

    # TITLE GATE (first check). If the contact's title is not a senior
    # decision-maker, mark Non-ICP and STOP, before any scraping or company ICP.
    skip_icp = bool(variable_set.get("skip_icp") or client_profile.get("skip_icp"))
    if not skip_icp:
        title_ok, title_reason = title_gate_check(oai_client, variable_set, row_context)
        if not title_ok:
            result = {key: "N/A" for key in variable_set["output_keys"]}
            result["ICPReview"] = "Non-ICP"
            result["ICP_reason"] = title_reason or "Title not a decision-maker"
            result["_status"] = "ok"
            result["_error"] = ""
            result["_scraped_urls"] = ""
            return result

    scraped = scrape_company(url, max_pages=max_pages, use_cache=use_cache)

    if not scraped["content"]:
        return {
            "_status": "error",
            "_error": "No website content scraped",
            "_scraped_urls": ""
        }

    verified_facts = extract_verified_facts(
        client=oai_client,
        client_profile=client_profile,
        url=url,
        content=scraped["content"],
        row_context=row_context,
        variable_set=variable_set
    )

    icp_review = normalize_icp(verified_facts.get("ICPReview", ""))
    icp_reason = str(verified_facts.get("icp_reason", "") or "").strip()

    # Optional per-client switch: when skip_icp is set (in the variable set or the
    # client profile), there is NO Non-ICP gating. Every scraped lead is treated as
    # in-scope and gets a full set of variables. The extractor still runs so its
    # verified facts continue to ground the writer and prevent hallucinated names.
    skip_icp = bool(variable_set.get("skip_icp") or client_profile.get("skip_icp"))
    if skip_icp:
        icp_review = "ICP"
        if not icp_reason:
            icp_reason = "ICP, all leads in scope"

    # If Non-ICP, skip the writer entirely: every field is N/A EXCEPT ICPReview
    # and ICP_reason (which must always explain the decision).
    if icp_review != "ICP":
        result = {key: "N/A" for key in variable_set["output_keys"]}
        result["ICPReview"] = "Non-ICP"
        result["ICP_reason"] = icp_reason or "Non-ICP, low outbound fit"
        result["_status"] = "ok"
        result["_error"] = ""
        result["_scraped_urls"] = " | ".join(scraped["urls"])
        return result

    data = call_openai(
        oai_client,
        client_profile,
        variable_set,
        url,
        scraped["content"],
        row_context,
        verified_facts=verified_facts,
    )

    # Force ICPReview and ICP_reason from the extraction stage (single source of truth).
    data["ICPReview"] = icp_review
    if icp_reason and not skip_icp:
        data["ICP_reason"] = icp_reason

    data = scrub_names(data, row_context)
    data = force_followup_linebreaks(data)

    issues = validate(data, variable_set)
    issues.extend(validate_proof_usage(data, verified_facts, scraped["content"], row_context))

    if issues:
        data = repair(oai_client, client_profile, variable_set, url, scraped["content"], row_context, data, issues)
        data["ICPReview"] = icp_review
        if icp_reason and not skip_icp:
            data["ICP_reason"] = icp_reason
        data = scrub_names(data, row_context)
        data = force_followup_linebreaks(data)
        issues = validate(data, variable_set)
        issues.extend(validate_proof_usage(data, verified_facts, scraped["content"], row_context))

    result = {key: data.get(key, "") for key in variable_set["output_keys"]}
    result["_status"] = "ok" if not issues else "ok_warning"
    result["_error"] = "; ".join(issues)
    result["_scraped_urls"] = " | ".join(scraped["urls"])

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--client-profile", required=True)
    parser.add_argument("--variable-set", required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    oai_key = os.getenv("OPENAI_API_KEY")
    if not oai_key:
        raise EnvironmentError("OPENAI_API_KEY missing in .env")

    client_profile = load_json(Path("client_profiles") / f"{args.client_profile}.json")
    variable_set = load_json(Path("variable_sets") / f"{args.variable_set}.json")

    df = pd.read_csv(args.input)
    if args.limit:
        df = df.head(args.limit).copy()

    url_col = find_url_column(df)

    output_cols = variable_set["output_keys"] + ["_status", "_error", "_scraped_urls"]
    for col in output_cols:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].astype("object")

    oai_client = OpenAI(api_key=oai_key)

    print(f"Client profile: {args.client_profile}")
    print(f"Variable set: {args.variable_set}")
    print(f"Extract model: {EXTRACT_MODEL}  |  Writer model: {WRITER_MODEL}")
    print(f"Rows: {len(df)}")
    print(f"URL column: {url_col}")
    print(f"Max pages: {args.max_pages}")

    rows = []
    for idx, row in df.iterrows():
        if args.resume and str(row.get("_status", "")).startswith("ok"):
            continue
        rows.append((idx, row))

    success = 0
    failed = 0

    def worker(item):
        idx, row = item
        try:
            result = process_row(
                oai_client=oai_client,
                client_profile=client_profile,
                variable_set=variable_set,
                row=row,
                url_col=url_col,
                max_pages=args.max_pages,
                use_cache=not args.no_cache
            )
        except Exception as e:
            # Never let a single row kill the whole batch. Mark it failed and move on.
            result = {key: "" for key in variable_set["output_keys"]}
            result["_status"] = "error"
            result["_error"] = f"{type(e).__name__}: {e}"[:300]
            result["_scraped_urls"] = ""
        return idx, result

    if args.workers <= 1:
        iterator = map(worker, rows)
        for n, (idx, result) in enumerate(tqdm(iterator, total=len(rows), desc="Processing"), start=1):
            for col, value in result.items():
                df.at[idx, col] = "" if value is None else str(value)

            if str(result.get("_status", "")).startswith("ok"):
                success += 1
            else:
                failed += 1
                tqdm.write(f"[FAIL] {df.at[idx, url_col]}: {result.get('_error', '')}")

            if n % CHECKPOINT_EVERY == 0:
                df.to_csv(args.output, index=False)

    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(worker, item) for item in rows]

            for n, future in enumerate(tqdm(as_completed(futures), total=len(futures), desc=f"Processing with {args.workers} workers"), start=1):
                idx, result = future.result()

                for col, value in result.items():
                    df.at[idx, col] = "" if value is None else str(value)

                if str(result.get("_status", "")).startswith("ok"):
                    success += 1
                else:
                    failed += 1
                    tqdm.write(f"[FAIL] {df.at[idx, url_col]}: {result.get('_error', '')}")

                if n % CHECKPOINT_EVERY == 0:
                    df.to_csv(args.output, index=False)

    df.to_csv(args.output, index=False)

    print(f"\nDone. {success} ok, {failed} failed.")
    print(f"Saved to: {args.output}")


if __name__ == "__main__":
    main()