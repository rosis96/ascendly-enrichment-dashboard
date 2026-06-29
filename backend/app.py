"""Ascendly Lead Enrichment Dashboard — FastAPI backend.

Run from this directory:  uvicorn app:app --reload --port 8000
Then open http://localhost:8000
"""
import os
import csv
import io
import time
import base64
import hmac
import hashlib
import threading
from typing import Optional

import json
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import FileResponse, Response, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import SessionLocal, init_db
from models import LeadList, Lead, Job, CustomVariable, HiddenVariable, Workspace, EnrichRule, ImportField, WorkspaceConfig
from sqlalchemy import or_, func
import engine_adapter as ea
from integrations import reoon
from integrations import esp as esp_detect


def _custom_specs(s, variable_set):
    """List of engine-format specs for a set's custom variables."""
    rows = s.query(CustomVariable).filter_by(variable_set=variable_set).order_by(CustomVariable.id).all()
    return [r.spec for r in rows]


def _rules_text(s, variable_set):
    row = s.query(EnrichRule).filter_by(variable_set=variable_set).first()
    return row.text if row else ""


def _rules_list(s, variable_set):
    """User correction rules as a list of non-empty lines. Read fresh per lead so
    edits saved mid-run apply to leads not yet processed (live corrections)."""
    return [ln.strip() for ln in _rules_text(s, variable_set).splitlines() if ln.strip()]


def _base_of(s, key):
    """Resolve a format-set key (engine set name OR workspace slug) to the engine
    base set its built-in variables come from. Blank workspace -> ''."""
    if ea.engine_set_exists(key):
        return key
    ws = s.query(Workspace).filter_by(slug=key).first()
    if ws:
        return ws.base_set or ""
    return key


def _hidden_names(s, variable_set):
    return {r.name for r in s.query(HiddenVariable).filter_by(variable_set=variable_set).all()}


def _lead_safe(ld):
    """True only when Reoon judged the email deliverable (valid/safe bucket)."""
    if not ld.verify:
        return False
    return reoon.bucket(ld.verify)[0] == "valid"


# Source of truth = the workspace's Client Profile section. Map its fields onto the
# engine writer's profile keys. ICP is NOT set here — the dedicated ICP engine owns
# the decision and the writer runs with skip_icp once ICP has decided.
_CLIENTPROFILE_MAP = [
    ("service_brief", "what_client_does"),
    ("business_overview", "what_client_does"),
    ("main_offer", "main_offer"),
    ("what_we_are_pitching", "what_we_pitch"),
    ("target_outcome", "target_outcome"),
    ("buyer_personas", "buyer_persona"),
    ("deal_size", "deal_size"),
    ("geo", "geo"),
    ("notes", "notes"),
    ("permanent_instructions", "permanent_instructions"),
]


def _builder_sections(s, key):
    row = s.query(WorkspaceConfig).filter_by(variable_set=key).first()
    return (row.sections if row and isinstance(row.sections, dict) else {}) or {}


def _section_nonempty(sec):
    return isinstance(sec, dict) and any(str(sec.get(k) or "").strip() for k in sec)


def _clientprofile_to_profile(cp, ws_name, prev_profile):
    """Build the engine writer profile from the Client Profile section."""
    prof = {}
    for pkey, skey in _CLIENTPROFILE_MAP:
        v = cp.get(skey)
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        if v and str(v).strip():
            prof[pkey] = v
    if isinstance(prev_profile, dict) and prev_profile.get("skip_icp"):
        prof["skip_icp"] = prev_profile["skip_icp"]
    prof.setdefault("client_name", ws_name)
    return prof


def _icp_config_for(s, key):
    """The workspace's ICP/Non-ICP configuration, or None if not meaningfully set.
    When present it is the SINGLE source of truth for ICP classification."""
    sections = _builder_sections(s, key)
    icp = sections.get("icp") or {}
    has_logic = bool((icp.get("icp_description") or "").strip()
                     or (icp.get("non_icp_description") or "").strip()
                     or [r for r in (icp.get("hard_rejection_rules") or []) if str(r).strip()])
    if not has_logic:
        return None
    cp = sections.get("client_profile") or {}
    icp = dict(icp)
    icp["_client_profile"] = cp
    return icp


def _profile_for(s, key, base):
    """Writer client profile. SOURCE OF TRUTH = the Client Profile section. Falls
    back to legacy strategy/profile only when Client Profile isn't filled yet, so
    unconfigured workspaces keep working. Old data is never deleted."""
    ws = s.query(Workspace).filter_by(slug=key).first()
    sections = _builder_sections(s, key)
    cp = sections.get("client_profile") or {}
    if _section_nonempty(cp):
        prev = dict(ws.profile or {}) if ws else {}
        return _clientprofile_to_profile(cp, (ws.name if ws else key), prev)
    # legacy fallbacks (older 'strategy' config, then the original profile)
    strat = sections.get("strategy") or {}
    if _section_nonempty(strat):
        prof = {}
        for pkey, skey in [("service_brief", "business_overview"), ("main_offer", "offers"),
                           ("what_we_are_pitching", "positioning"), ("target_outcome", "objectives"),
                           ("buyer_personas", "buyer_personas"), ("deal_size", "deal_size"),
                           ("geo", "geo"), ("permanent_instructions", "instructions")]:
            v = strat.get(skey)
            if v:
                prof[pkey] = v if isinstance(v, str) else json.dumps(v)
        prof.setdefault("client_name", ws.name if ws else key)
        return prof
    if ws:
        prof = dict(ws.profile or {})
        prof.setdefault("client_name", ws.name)
        return prof
    client = (base or key).split("_")[0]
    return ea.load_client_profile(client)

init_db()

app = FastAPI(title="Ascendly Lead Enrichment Dashboard")


SESSION_COOKIE = "dash_session"


def _session_token():
    """Opaque session value tied to the configured password. Knowing it requires
    knowing the password, but it is not the password itself."""
    pw = os.getenv("DASH_PASSWORD") or ""
    user = os.getenv("DASH_USER", "admin")
    secret = os.getenv("DASH_SECRET", pw)
    return hmac.new(secret.encode(), f"{user}:{pw}".encode(), hashlib.sha256).hexdigest()


@app.middleware("http")
async def auth_gate(request, call_next):
    """Cookie-session auth (replaces the browser Basic-auth popup). Active only
    when DASH_PASSWORD is set. The login page and assets stay open so the form can
    render; everything else needs a valid session cookie."""
    pw = os.getenv("DASH_PASSWORD")
    if pw:
        path = request.url.path
        # "/" serves the SPA shell to everyone (returns 200 so platform health
        # checks pass); its data calls 401 and the frontend redirects to /login.
        open_paths = (path in ("/", "/login", "/healthz", "/api/login", "/api/logout")
                      or path.startswith("/assets"))
        if not open_paths:
            tok = request.cookies.get(SESSION_COOKIE, "")
            if not hmac.compare_digest(tok, _session_token()):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Authentication required"}, status_code=401)
                return RedirectResponse("/login", status_code=302)
    return await call_next(request)


@app.middleware("http")
async def no_cache_assets(request, call_next):
    """Stop the browser caching the SPA shell + assets, so a new deploy is seen
    immediately without a hard refresh."""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/assets"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

# Job ids requested to stop. Checked before each lead so a run halts promptly
# instead of churning through (and billing) the whole list.
CANCEL = set()


# ------------------------- helpers -------------------------

def _pick(headers_lower, row, *candidates):
    for c in candidates:
        if c in headers_lower:
            return str(row[headers_lower[c]] or "").strip()
    return ""


def _to_int(s):
    """Pull a whole number out of a messy cell ('1,200', '51-100', '~$45') -> int,
    or None. For ranges, takes the first number."""
    import re as _re
    if s is None:
        return None
    m = _re.search(r"\d[\d,]*", str(s))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


_FIXED_MAP_FIELDS = {"first_name", "last_name", "email", "title", "company", "website",
                     "employees", "country", "state", "seniority", "industry"}

# Standard columns emitted first in every export (the mapped fields live in Lead
# columns now, not in `data`), then the custom `data` columns, then enrichment.
_STD_EXPORT = [("First Name", "first_name"), ("Last Name", "last_name"), ("Title", "title"),
               ("Company", "company"), ("Website", "website"), ("Email", "email"),
               ("Industry", "industry"), ("ESP", "esp"), ("Employees", "employees"),
               ("Country", "country"), ("State", "state"), ("Seniority", "seniority"),
               ("Email Status", "email_status")]
# raw `data` keys that duplicate a standard column are skipped (mostly affects
# older leads imported before mapping, whose data held the whole row).
_STD_RAW_SKIP = {"first name", "last name", "first_name", "last_name", "firstname", "lastname",
                 "title", "jobtitle", "job title", "company", "companyname", "company name",
                 "website", "url", "domain", "company website", "email", "industry",
                 "# employees", "employees", "country", "state", "seniority", "esp", "status"}


def _std_export_row(ld):
    out = []
    for _, attr in _STD_EXPORT:
        v = getattr(ld, attr, "")
        out.append("" if v is None else v)
    return out


def _parse_csv(content_bytes, mapping=None):
    """Parse an uploaded CSV into lead dicts. `mapping` (system_field -> CSV header)
    comes from the import column-mapping step; when present we use it exactly. With
    no mapping we fall back to best-guess header matching (backward compatible).
    The full original row is always kept in `data`."""
    text = content_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    headers = [h.strip() for h in rows[0]]
    hl = {h.lower(): i for i, h in enumerate(headers)}
    out = []
    for r in rows[1:]:
        if not any((c or "").strip() for c in r):
            continue
        r = r + [""] * (len(headers) - len(r))
        rowmap = {headers[i]: r[i] for i in range(len(headers))}
        if mapping:
            def mv(field):
                col = mapping.get(field)
                return str(rowmap.get(col, "") or "").strip() if col else ""
            # Store ONLY the columns the user mapped. Standard fields go to Lead
            # columns; any other mapped (custom) field goes in data under its name.
            # Unmapped CSV columns are dropped entirely.
            data = {}
            for mk, col in mapping.items():
                if mk not in _FIXED_MAP_FIELDS and col:
                    data[mk] = str(rowmap.get(col, "") or "").strip()
            rec = {
                "first_name": mv("first_name"), "last_name": mv("last_name"),
                "title": mv("title"), "company": mv("company"),
                "website": mv("website"), "email": mv("email"),
                "employees": _to_int(mv("employees")),
                "country": mv("country"), "state": mv("state"),
                "seniority": mv("seniority"), "data": data,
            }
            ind = mv("industry")
            if ind:
                rec["industry"] = ind   # CSV already carries our industry -> pre-classified
            out.append(rec)
        else:
            out.append({
                "first_name": _pick(hl, r, "first name", "firstname", "first_name"),
                "last_name": _pick(hl, r, "last name", "lastname", "last_name"),
                "title": _pick(hl, r, "title", "jobtitle", "job title"),
                "company": _pick(hl, r, "company", "companyname", "company name"),
                "website": _pick(hl, r, "website", "url", "domain", "company website"),
                "email": _pick(hl, r, "email"),
                "employees": _to_int(_pick(hl, r, "# employees", "employees", "employee count",
                                           "num employees", "company size", "headcount", "size")),
                "country": _pick(hl, r, "country"),
                "state": _pick(hl, r, "state", "region", "province"),
                "seniority": _pick(hl, r, "seniority", "seniority level"),
                "data": rowmap,
            })
    return out


ENRICH_WORKERS = int(os.getenv("ENRICH_WORKERS", "10"))
VERIFY_WORKERS = int(os.getenv("VERIFY_WORKERS", "10"))
# Upper bound on user-chosen concurrency. Classification is cheap and benefits from
# high parallelism, so the ceiling is 100; for enrichment, keeping it ~10 is wiser
# (rate limits/quality). Override via env if needed.
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "100"))
# Classification is the cheapest pass (homepage scrape + tiny model call) so it can
# run with very high concurrency. Separate, higher ceiling just for classify.
MAX_CLASSIFY_WORKERS = int(os.getenv("MAX_CLASSIFY_WORKERS", "500"))
# Lowered: each concurrent classify holds a parsed HTML page in memory; 100 at once
# OOMs a small container. 40 is a safe default; raise via env if you add RAM.
CLASSIFY_WORKERS = int(os.getenv("CLASSIFY_WORKERS", "40"))
# Most leads the per-list grid endpoint will load at once (huge lists are browsed
# via the paginated Database view). Prevents OOM on 100k+ lists.
LIST_LEAD_CAP = int(os.getenv("LIST_LEAD_CAP", "1000"))
# Fast classify: leads per single OpenAI call (no scraping). One call ~= this many
# leads, so throughput = workers * batch leads per round-trip.
CLASSIFY_BATCH = int(os.getenv("CLASSIFY_BATCH", "25"))
# Fast classify does no scraping (tiny memory), so it can run far more concurrent
# API calls than the deep/scraping path.
CLASSIFY_FAST_WORKERS = int(os.getenv("CLASSIFY_FAST_WORKERS", "60"))


def _clamp_workers(n, default, maximum=None):
    maximum = maximum or MAX_WORKERS
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, maximum))


def _lead_row(ld):
    """Deliberately MINIMAL row. Only the website (to scrape), the title (title
    gate) and the company name (grounding) — plus names/email for scrubbing.
    No other CSV columns, so the writer cannot cite Apollo data (revenue,
    employee counts, locations, etc.); it must use website facts only."""
    return {
        "website": ld.website or "",
        "Title": ld.title or "",
        "title": ld.title or "",
        "Company": ld.company or "",
        "companyName": ld.company or "",
        "company": ld.company or "",
        "First Name": ld.first_name or "",
        "Last Name": ld.last_name or "",
        "email": ld.email or "",
    }


def _apply_agg(job, agg):
    if job.kind == "verify":
        job.cost = agg.get("cr", 0)
        job.summary = {"valid": agg.get("valid", 0), "risky": agg.get("risky", 0), "invalid": agg.get("invalid", 0)}
    elif job.kind == "pipeline":
        job.cost = round(agg.get("cost", 0), 4)
        job.summary = {k: agg.get(k, 0) for k in ("verified", "safe", "enriched", "unsafe", "rejected", "cr")}
    elif job.kind == "classify":
        job.cost = round(agg.get("cost", 0), 4)
        job.summary = {"classified": agg.get("classified", 0), "nosite": agg.get("nosite", 0)}
    elif job.kind == "titlecheck":
        job.cost = 0
        job.summary = {"tpass": agg.get("tpass", 0), "trej": agg.get("trej", 0)}
    elif job.kind == "esp":
        job.cost = 0
        job.summary = {"microsoft": agg.get("microsoft", 0), "google": agg.get("google", 0),
                       "other": agg.get("other", 0), "unknown": agg.get("unknown", 0)}
    else:
        job.cost = round(agg.get("cost", 0), 4)
        job.icp = agg.get("icp", 0)
        job.nonicp = agg.get("nonicp", 0)
        job.rejected = agg.get("rejected", 0)


def _write_job(job_id, done, agg, status=None):
    s = SessionLocal()
    try:
        job = s.get(Job, job_id)
        if not job:
            return
        job.done = done
        _apply_agg(job, agg)
        if status:
            job.status = status
        s.commit()
    finally:
        s.close()


def _process_concurrent(job_id, target_ids, worker_fn, workers):
    """Run worker_fn(lead_id) across a thread pool. Submits only a bounded WINDOW
    of tasks at a time (topping up as each finishes) so a huge list (100k+) can't
    blow up memory by queuing every future at once. Progress is written on a timer,
    not per-lead, to avoid hammering the DB. Cancel-aware and resumable."""
    from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
    _write_job(job_id, 0, {}, status="running")
    agg, done = {}, 0
    workers = max(1, workers)
    window = workers * 2          # max futures in flight at once (bounds memory)
    last_write = 0.0

    def task(lid):
        if job_id in CANCEL:
            return None
        try:
            return worker_fn(lid)
        except Exception:
            return {"_error": 1}

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            it = iter(target_ids)
            inflight = set()
            for _ in range(window):
                lid = next(it, None)
                if lid is None:
                    break
                inflight.add(ex.submit(task, lid))
            while inflight:
                completed, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in completed:
                    r = fut.result()
                    done += 1
                    if r:
                        for k, v in r.items():
                            agg[k] = agg.get(k, 0) + v
                    if job_id not in CANCEL:          # top up the window
                        lid = next(it, None)
                        if lid is not None:
                            inflight.add(ex.submit(task, lid))
                now = time.time()
                if now - last_write >= 2.0:           # throttle progress writes
                    _write_job(job_id, done, agg)
                    last_write = now
        _write_job(job_id, done, agg, status="cancelled" if job_id in CANCEL else "done")
    except Exception:
        _write_job(job_id, done, agg, status="error")
    finally:
        CANCEL.discard(job_id)


ICP_MAX_PAGES = int(os.getenv("ICP_MAX_PAGES", "1"))


def _run_icp_gate(s, ld, icp_cfg):
    """The dedicated ICP step. Scrapes the homepage, classifies via the workspace's
    ICP config (hard rules first), stores the structured fields + the 3 indexed
    columns, and routes by tier. Returns (proceed, inc, fields):
      proceed=True  -> ICP / Possible ICP: continue to enrichment
      proceed=False -> Non-ICP (reject) / Needs Review (queue): already stored."""
    cp = icp_cfg.get("_client_profile") or {}
    content = ea.scrape_text(ld.website or "", max_pages=ICP_MAX_PAGES)
    fields, cost = ea.classify_icp(cp, icp_cfg, content)
    decision = fields.get("final_decision") or "Needs Review"
    ld.icp_decision = decision
    try:
        ld.icp_score = int(float(fields.get("fit_score") or 0))
    except Exception:
        ld.icp_score = 0
    try:
        ld.icp_confidence = int(float(fields.get("confidence") or 0))
    except Exception:
        ld.icp_confidence = 0
    res = dict(ld.result or {})
    res["_icp"] = fields
    res["ICPReview"] = "ICP" if decision in ("ICP", "Possible ICP") else "Non-ICP"
    res["ICP_reason"] = (fields.get("primary_icp_reason") or fields.get("primary_reject_reason")
                         or fields.get("summary") or "")
    ld.result = res
    if decision in ("ICP", "Possible ICP"):
        s.commit()
        return True, {"cost": cost}, fields
    ld.status = "review" if decision == "Needs Review" else "skipped"
    s.commit()
    inc = {"cost": cost, ("review" if decision == "Needs Review" else "nonicp"): 1}
    return False, inc, fields


def _enrich_one(lead_id, base, enrichments, custom_specs, profile, variable_set=None, icp_cfg=None):
    s = SessionLocal()
    try:
        ld = s.get(Lead, lead_id)
        if not ld:
            return None
        ld.status = "running"
        s.commit()
        # --- ICP gate (single source of truth) — only when configured ---
        icp_fields = None
        if icp_cfg:
            proceed, inc0, icp_fields = _run_icp_gate(s, ld, icp_cfg)
            if not proceed:
                return inc0
        # Writer runs WITHOUT its own ICP gating when the ICP engine already decided.
        writer_profile = dict(profile or {})
        if icp_cfg:
            writer_profile["skip_icp"] = True
        extra_rules = _rules_list(s, variable_set) if variable_set else []
        res, cost = ea.enrich(_lead_row(ld), base, enrichments, custom_specs, writer_profile, extra_rules)
        if res.get("_status") == "error" or not res.get("ICPReview"):
            keep = {"_icp": icp_fields} if icp_fields else {}
            keep.update({"_status": "error", "_error": str(res.get("_error", "No website content"))[:200]})
            ld.result = keep
            ld.status = "error"
            s.commit()
            return {"error": 1, "cost": cost}
        if icp_fields:                       # ICP belongs to the gate, not the writer:
            res["_icp"] = icp_fields          # overwrite so the writer's old-style
            res["ICPReview"] = "ICP"          # "Company manufactures..."/"not a service
            res["ICP_reason"] = (icp_fields.get("primary_icp_reason")  # provider" reason
                                 or icp_fields.get("summary")          # never shows.
                                 or res.get("ICP_reason") or "")
        ld.result = res
        ld.status = "skipped" if res.get("ICPReview") == "Non-ICP" else "done"
        s.commit()
        inc = {"cost": cost}
        if res.get("_title_gate") == "rejected":
            inc["rejected"] = 1
        elif res.get("ICPReview") == "Non-ICP":
            inc["nonicp"] = 1
        else:
            inc["icp"] = 1
        return inc
    finally:
        s.close()


def _verify_one(lead_id, mode):
    s = SessionLocal()
    try:
        ld = s.get(Lead, lead_id)
        if not ld:
            return None
        res = reoon.verify_one(ld.email, mode)
        bucket, label = reoon.bucket(res)
        ld.verify = res
        ld.email_status = label
        s.commit()
        return {"cr": 1, bucket: 1}
    finally:
        s.close()


def _pipeline_one(lead_id, mode, base, enrichments, custom_specs, profile, variable_set=None, icp_cfg=None):
    s = SessionLocal()
    try:
        ld = s.get(Lead, lead_id)
        if not ld:
            return None
        inc = {}
        if not ld.verify:
            res = reoon.verify_one(ld.email, mode)
            _, label = reoon.bucket(res)
            ld.verify = res
            ld.email_status = label
            s.commit()
            inc["cr"] = 1
            inc["verified"] = 1
        safe = reoon.bucket(ld.verify)[0] == "valid"
        if safe:
            inc["safe"] = 1
            if not ld.result:
                ld.status = "running"
                s.commit()
                # --- ICP gate (single source of truth) — only when configured ---
                icp_fields = None
                if icp_cfg:
                    proceed, inc0, icp_fields = _run_icp_gate(s, ld, icp_cfg)
                    for k, v in inc0.items():
                        inc[k] = inc.get(k, 0) + v
                    if not proceed:
                        return inc
                writer_profile = dict(profile or {})
                if icp_cfg:
                    writer_profile["skip_icp"] = True
                extra_rules = _rules_list(s, variable_set) if variable_set else []
                r, cost = ea.enrich(_lead_row(ld), base, enrichments, custom_specs, writer_profile, extra_rules)
                inc["cost"] = inc.get("cost", 0) + cost
                if r.get("_status") == "error" or not r.get("ICPReview"):
                    keep = {"_icp": icp_fields} if icp_fields else {}
                    keep.update({"_status": "error", "_error": str(r.get("_error", "No website content"))[:200]})
                    ld.result = keep
                    ld.status = "error"
                    s.commit()
                    inc["error"] = 1
                else:
                    if icp_fields:
                        r["_icp"] = icp_fields
                        r["ICPReview"] = "ICP"
                        r["ICP_reason"] = (icp_fields.get("primary_icp_reason")
                                           or icp_fields.get("summary") or r.get("ICP_reason") or "")
                    ld.result = r
                    ld.status = "skipped" if r.get("ICPReview") == "Non-ICP" else "done"
                    s.commit()
                    if r.get("_title_gate") == "rejected":
                        inc["rejected"] = 1
                    else:
                        inc["enriched"] = 1
        else:
            inc["unsafe"] = 1
        return inc
    finally:
        s.close()


def _run_job(job_id, target_ids, workers=None):
    from functools import partial
    s = SessionLocal()
    try:
        job = s.get(Job, job_id)
        vset = job.variable_set
        base = _base_of(s, job.variable_set)
        custom_specs = _custom_specs(s, job.variable_set)
        profile = _profile_for(s, job.variable_set, base)
        icp_cfg = _icp_config_for(s, job.variable_set)
        enrichments = job.enrichments
    finally:
        s.close()
    _process_concurrent(job_id, target_ids,
                        partial(_enrich_one, base=base, enrichments=enrichments,
                                custom_specs=custom_specs, profile=profile, variable_set=vset,
                                icp_cfg=icp_cfg),
                        _clamp_workers(workers, ENRICH_WORKERS))


def _run_verify_job(job_id, target_ids, mode, workers=None):
    from functools import partial
    _process_concurrent(job_id, target_ids, partial(_verify_one, mode=mode),
                        _clamp_workers(workers, VERIFY_WORKERS))


def _classify_one(lead_id, tax):
    # Hold the DB connection only briefly: read the lead, RELEASE the connection
    # during the slow homepage scrape, then reopen briefly to write. This lets us
    # run many workers (e.g. 100) without exhausting the connection pool.
    s = SessionLocal()
    try:
        ld = s.get(Lead, lead_id)
        if not ld:
            return None
        website, company = ld.website, ld.company
    finally:
        s.close()
    label, cost = ea.classify_industry({"website": website, "company": company}, tax)
    s = SessionLocal()
    try:
        ld = s.get(Lead, lead_id)
        if ld:
            ld.industry = label or ""
            s.commit()
    finally:
        s.close()
    return {"cost": cost, "classified": 1} if label else {"cost": cost, "nosite": 1}


def _run_classify_job(job_id, target_ids, workers=None):
    from functools import partial
    _process_concurrent(job_id, target_ids, partial(_classify_one, tax=ea.taxonomy()),
                        _clamp_workers(workers, CLASSIFY_WORKERS, MAX_CLASSIFY_WORKERS))


def _classify_batch(id_chunk, tax):
    """Classify a CHUNK of leads in one OpenAI call (no scraping). Reads the chunk,
    releases the DB during the call, then bulk-writes the labels."""
    s = SessionLocal()
    try:
        rows = (s.query(Lead.id, Lead.company, Lead.website, Lead.email)
                .filter(Lead.id.in_(id_chunk)).all())
    finally:
        s.close()
    if not rows:
        return None
    leads = [{"company": c, "website": w, "email": e} for (_id, c, w, e) in rows]
    labels, cost = ea.classify_industry_batch(leads, tax)
    mappings, classified, unclear = [], 0, 0
    for (lid, *_), label in zip(rows, labels):
        label = label or "Other / Unclear"
        mappings.append({"id": lid, "industry": label})
        if label == "Other / Unclear":
            unclear += 1
        else:
            classified += 1
    s = SessionLocal()
    try:
        s.bulk_update_mappings(Lead, mappings)
        s.commit()
    finally:
        s.close()
    return {"cost": cost, "classified": classified, "unclear": unclear}


def _run_classify_fast_job(job_id, target_ids, workers=None):
    """Fast database classify: batches many leads per OpenAI call and runs many
    batches concurrently. No website scraping, so it's 10-50x faster and uses
    little memory. Bounded window + per-lead progress (counts whole batches)."""
    from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
    tax = ea.taxonomy()
    workers = _clamp_workers(workers, CLASSIFY_FAST_WORKERS, MAX_CLASSIFY_WORKERS)
    chunks = [target_ids[i:i + CLASSIFY_BATCH] for i in range(0, len(target_ids), CLASSIFY_BATCH)]
    _write_job(job_id, 0, {}, status="running")
    agg, done, last_write = {}, 0, 0.0
    window = workers * 2

    def task(chunk):
        if job_id in CANCEL:
            return None, 0
        try:
            return _classify_batch(chunk, tax), len(chunk)
        except Exception:
            return {"_error": 1}, len(chunk)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            it = iter(chunks)
            inflight = set()
            for _ in range(window):
                ch = next(it, None)
                if ch is None:
                    break
                inflight.add(ex.submit(task, ch))
            while inflight:
                completed, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in completed:
                    r, nch = fut.result()
                    done += nch
                    if r:
                        for k, v in r.items():
                            agg[k] = agg.get(k, 0) + v
                    if job_id not in CANCEL:
                        ch = next(it, None)
                        if ch is not None:
                            inflight.add(ex.submit(task, ch))
                now = time.time()
                if now - last_write >= 2.0:
                    _write_job(job_id, done, agg)
                    last_write = now
        _write_job(job_id, done, agg, status="cancelled" if job_id in CANCEL else "done")
    except Exception:
        _write_job(job_id, done, agg, status="error")
    finally:
        CANCEL.discard(job_id)


def _esp_one(lead_id):
    # Brief DB holds: read email, release during the DNS lookup, then write.
    s = SessionLocal()
    try:
        ld = s.get(Lead, lead_id)
        if not ld:
            return None
        email = ld.email
    finally:
        s.close()
    label = esp_detect.detect(email)
    s = SessionLocal()
    try:
        ld = s.get(Lead, lead_id)
        if ld:
            ld.esp = label
            s.commit()
    finally:
        s.close()
    bucket = {"Microsoft": "microsoft", "Google": "google", "Other": "other"}.get(label, "unknown")
    return {bucket: 1}


def _run_esp_job(job_id, target_ids, workers=None):
    from functools import partial
    _process_concurrent(job_id, target_ids, partial(_esp_one),
                        _clamp_workers(workers, ENRICH_WORKERS))


def _run_title_job(job_id, target_ids):
    """Title check is pure string matching (no scraping, no API), so we process it
    in bulk chunks instead of one-lead-at-a-time: far faster on big lists and it
    doesn't hammer the DB. Marks each lead title_status = pass | rejected."""
    _write_job(job_id, 0, {}, status="running")
    agg, done = {}, 0
    CHUNK = 500
    try:
        for i in range(0, len(target_ids), CHUNK):
            if job_id in CANCEL:
                break
            chunk = target_ids[i:i + CHUNK]
            s = SessionLocal()
            try:
                leads = s.query(Lead).filter(Lead.id.in_(chunk)).all()
                mappings = []
                for ld in leads:
                    st = "pass" if ea.title_passes(ld.title or "") else "rejected"
                    mappings.append({"id": ld.id, "title_status": st})
                    key = "tpass" if st == "pass" else "trej"
                    agg[key] = agg.get(key, 0) + 1
                    done += 1
                if mappings:
                    s.bulk_update_mappings(Lead, mappings)
                    s.commit()
            finally:
                s.close()
            _write_job(job_id, done, agg)  # progress per chunk
        _write_job(job_id, done, agg, status="cancelled" if job_id in CANCEL else "done")
    except Exception:
        _write_job(job_id, done, agg, status="error")
    finally:
        CANCEL.discard(job_id)


def _run_pipeline_job(job_id, target_ids, mode, workers=None):
    from functools import partial
    s = SessionLocal()
    try:
        job = s.get(Job, job_id)
        vset = job.variable_set
        base = _base_of(s, job.variable_set)
        custom_specs = _custom_specs(s, job.variable_set)
        profile = _profile_for(s, job.variable_set, base)
        icp_cfg = _icp_config_for(s, job.variable_set)
        enrichments = job.enrichments
    finally:
        s.close()
    _process_concurrent(job_id, target_ids,
                        partial(_pipeline_one, mode=mode, base=base, enrichments=enrichments,
                                custom_specs=custom_specs, profile=profile, variable_set=vset,
                                icp_cfg=icp_cfg),
                        _clamp_workers(workers, ENRICH_WORKERS))


# ------------------------- API -------------------------

class CreateList(BaseModel):
    name: str
    variable_set: str = "ascendly_lean"


class RunBody(BaseModel):
    enrichments: list[str] = []
    lead_ids: list[int] = []     # run only these leads (selection)
    limit: Optional[int] = None  # otherwise run only the first N (test cap)
    only_safe: bool = True       # only enrich leads Reoon marked safe/deliverable
    skip_done: bool = True        # resume: skip leads already enriched
    workers: Optional[int] = None  # parallel rows at once (clamped to MAX_WORKERS)


class VerifyBody(BaseModel):
    lead_ids: list[int] = []
    limit: Optional[int] = None
    mode: str = "power"          # reoon mode: power (accurate) or quick (fast)
    skip_done: bool = True        # resume: skip leads already verified
    workers: Optional[int] = None


class PipelineBody(BaseModel):
    enrichments: list[str] = []
    lead_ids: list[int] = []
    limit: Optional[int] = None
    mode: str = "power"
    skip_done: bool = True
    workers: Optional[int] = None


class ClassifyBody(BaseModel):
    lead_ids: list[int] = []
    limit: Optional[int] = None
    skip_done: bool = True
    workers: Optional[int] = None


@app.get("/api/variable-sets")
def variable_sets():
    s = SessionLocal()
    try:
        ws = [w.slug for w in s.query(Workspace).order_by(Workspace.created_at).all()]
    finally:
        s.close()
    return ea.list_variable_sets() + ws


class Placeholder(BaseModel):
    token: str
    description: str = ""
    min_words: Optional[int] = None
    max_words: Optional[int] = None
    examples: list[str] = []


class CustomVarBody(BaseModel):
    variable_set: str
    label: str
    template: str = ""
    purpose: str = ""           # free-form "how to write it" guidance
    examples: list[str] = []    # example outputs (for free-form variables)
    min_words: Optional[int] = None
    max_words: Optional[int] = None
    placeholders: list[Placeholder] = []
    id: Optional[int] = None    # set to update an existing custom variable


class DuplicateBody(BaseModel):
    variable_set: str
    name: str


class HideBody(BaseModel):
    variable_set: str
    name: str
    hidden: bool = True


@app.get("/api/custom-variables")
def list_custom(variable_set: str = "ascendly_lean"):
    s = SessionLocal()
    try:
        rows = s.query(CustomVariable).filter_by(variable_set=variable_set).order_by(CustomVariable.id).all()
        return [{"id": r.id, "name": r.name, "label": r.label, "spec": r.spec} for r in rows]
    finally:
        s.close()


@app.post("/api/custom-variables")
def create_custom(body: CustomVarBody):
    spec = ea.build_custom_spec(
        label=body.label, template=body.template,
        placeholders=[p.dict() for p in body.placeholders],
        min_words=body.min_words, max_words=body.max_words,
        purpose=body.purpose, examples=body.examples,
    )
    s = SessionLocal()
    try:
        row = s.get(CustomVariable, body.id) if body.id else None
        if not row:
            row = s.query(CustomVariable).filter_by(variable_set=body.variable_set, name=spec["name"]).first()
        if row:
            row.variable_set = body.variable_set
            row.name = spec["name"]
            row.label = spec["label"]
            row.spec = spec
        else:
            row = CustomVariable(variable_set=body.variable_set, name=spec["name"],
                                 label=spec["label"], spec=spec)
            s.add(row)
        s.commit()
        return {"id": row.id, "name": row.name, "label": row.label, "spec": row.spec}
    finally:
        s.close()


@app.post("/api/custom-variables/duplicate")
def duplicate_variable(body: DuplicateBody):
    s = SessionLocal()
    try:
        cv = s.query(CustomVariable).filter_by(variable_set=body.variable_set, name=body.name).first()
        if cv:
            src, base_label = cv.spec, cv.label
        else:
            bs = ea.get_builtin_spec(_base_of(s, body.variable_set), body.name)
            src, base_label = bs, (bs.get("label") if bs else body.name)
        if not src:
            raise HTTPException(404, "Variable not found")
        spec = ea.duplicate_spec(src, (base_label or body.name) + " copy")
        existing = {r.name for r in s.query(CustomVariable).filter_by(variable_set=body.variable_set).all()}
        base, n, i = spec["name"], spec["name"], 2
        while n in existing:
            n = f"{base}_{i}"; i += 1
        spec["name"] = n
        row = CustomVariable(variable_set=body.variable_set, name=spec["name"], label=spec["label"], spec=spec)
        s.add(row)
        s.commit()
        return {"id": row.id, "name": row.name, "label": row.label}
    finally:
        s.close()


@app.delete("/api/custom-variables/{var_id}")
def delete_custom(var_id: int):
    s = SessionLocal()
    try:
        row = s.get(CustomVariable, var_id)
        if row:
            s.delete(row)
            s.commit()
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/hidden")
def set_hidden(body: HideBody):
    s = SessionLocal()
    try:
        row = s.query(HiddenVariable).filter_by(variable_set=body.variable_set, name=body.name).first()
        if body.hidden and not row:
            s.add(HiddenVariable(variable_set=body.variable_set, name=body.name))
            s.commit()
        elif not body.hidden and row:
            s.delete(row)
            s.commit()
        return {"ok": True, "hidden": body.hidden}
    finally:
        s.close()


class WorkspaceBody(BaseModel):
    name: str
    base_set: str = ""              # engine set to clone, or "" for blank
    profile: dict = {}


class ProfileBody(BaseModel):
    profile: dict = {}


@app.get("/api/workspaces")
def list_workspaces():
    """Engine clients (mapped to their lean set) + user-created workspaces."""
    s = SessionLocal()
    try:
        sets = set(ea.list_variable_sets())
        out = []
        for client in ea.list_profiles():
            key = f"{client}_lean" if f"{client}_lean" in sets else next(
                (x for x in sorted(sets) if x.startswith(client + "_")), f"{client}_lean")
            out.append({"key": key, "name": client.capitalize(), "kind": "engine", "base_set": key})
        for w in s.query(Workspace).order_by(Workspace.created_at.desc()).all():
            out.append({"key": w.slug, "name": w.name, "kind": "workspace", "base_set": w.base_set or ""})
        return out
    finally:
        s.close()


def _materialize_workspace(s, slug, base_set):
    """Copy a base set's variables into the workspace as its own (deletable)
    custom variables, so the workspace is self-contained with no live link to
    the Ascendly base. Always-on gate fields are generated by the engine, so
    they are not copied. Existing custom overrides are left untouched."""
    if not base_set or not ea.engine_set_exists(base_set):
        return
    spec = ea.load_variable_set(base_set)
    for v in spec.get("variables", []):
        name = v.get("name")
        if not name or name in ea.ALWAYS_KEYS:
            continue
        if s.query(CustomVariable).filter_by(variable_set=slug, name=name).first():
            continue
        cspec = dict(v)
        cspec["custom"] = True
        label = cspec.get("label") or name.replace("_", " ").title()
        cspec["label"] = label
        s.add(CustomVariable(variable_set=slug, name=name, label=label, spec=cspec))
    s.commit()


@app.post("/api/workspaces")
def create_workspace(body: WorkspaceBody):
    s = SessionLocal()
    try:
        base = ea.slugify(body.name)
        slug, i = base, 2
        while s.query(Workspace).filter_by(slug=slug).first() or ea.engine_set_exists(slug):
            slug = f"{base}_{i}"; i += 1
        profile = body.profile or {}
        if not profile and body.base_set:
            profile = ea.get_profile_raw(body.base_set.split("_")[0])
        # standalone from day one (base_set stays empty); clone copies the vars in
        w = Workspace(slug=slug, name=body.name.strip() or "New workspace", base_set="", profile=profile)
        s.add(w)
        s.commit()
        if body.base_set:
            _materialize_workspace(s, slug, body.base_set)
        return {"key": w.slug, "name": w.name, "base_set": "", "kind": "workspace"}
    finally:
        s.close()


@app.on_event("startup")
def _cleanup_stale_jobs():
    """A restart/redeploy kills in-flight worker threads. Mark any job still
    'running' as cancelled so the UI never shows a stuck, unstoppable run."""
    s = SessionLocal()
    try:
        s.query(Job).filter(Job.status.in_(["running", "queued", "cancelling"])).update(
            {Job.status: "cancelled"}, synchronize_session=False)
        s.commit()
    except Exception:
        pass
    finally:
        s.close()


@app.on_event("startup")
def _migrate_workspaces_standalone():
    """One-time: make existing cloned workspaces standalone (no inherited base)."""
    s = SessionLocal()
    try:
        for w in s.query(Workspace).all():
            if w.base_set:
                _materialize_workspace(s, w.slug, w.base_set)
                w.base_set = ""
        s.commit()
    except Exception:
        pass
    finally:
        s.close()


def _to_export_var(spec):
    """A spec -> the editable import schema (round-trips through Paste JSON)."""
    out = {"label": spec.get("label") or (spec.get("name", "").replace("_", " ").title())}
    if spec.get("min_words"):
        out["min_words"] = spec["min_words"]
    if spec.get("max_words"):
        out["max_words"] = spec["max_words"]
    guidance = spec.get("purpose") or spec.get("definition") or ""
    extra = []
    for k in ("instructions", "writing_rules", "hard_requirements"):
        val = spec.get(k)
        if isinstance(val, list):
            extra += [str(x) for x in val if isinstance(x, str)]
    if extra and not spec.get("template"):
        guidance = (guidance + " " if guidance else "") + " ".join(extra)
    if guidance:
        out["guidance"] = guidance
    if spec.get("template"):
        out["template"] = spec["template"]
    if spec.get("example_outputs"):
        out["examples"] = spec["example_outputs"]
    ph = spec.get("placeholders") or {}
    if ph:
        plist = []
        for tok, p in ph.items():
            item = {"token": tok}
            for k in ("description", "min_words", "max_words", "examples"):
                if p.get(k):
                    item[k] = p[k]
            plist.append(item)
        out["placeholders"] = plist
    return out


@app.get("/api/format-json/{variable_set}")
def export_format_json(variable_set: str):
    """Download a workspace's full config (profile + all variables) as JSON in the
    same shape Paste JSON accepts — so you can copy, edit the context, and reuse."""
    s = SessionLocal()
    try:
        ws = s.query(Workspace).filter_by(slug=variable_set).first()
        if ws:
            profile = dict(ws.profile or {})
        else:
            profile = ea.get_profile_raw((variable_set or "").split("_")[0])
        base = _base_of(s, variable_set)
        customs = s.query(CustomVariable).filter_by(variable_set=variable_set).order_by(CustomVariable.id).all()
        custom_names = {c.name for c in customs}
        variables = []
        base_spec = ea.load_variable_set(base) if base else {}
        for v in base_spec.get("variables", []):
            name = v.get("name")
            if not name or name in ea.ALWAYS_KEYS or name in custom_names:
                continue
            variables.append(_to_export_var(v))
        for c in customs:
            variables.append(_to_export_var(c.spec))
        return {"profile": profile, "variables": variables}
    finally:
        s.close()


def _coerce_profile_value(v):
    """Turn any JSON value into a readable string so a rich pasted profile (with
    arrays/objects like target_outcome, positioning, avoid_words) imports cleanly
    and still feeds the writer, instead of being silently dropped."""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return ", ".join(_coerce_profile_value(x) for x in v if x not in (None, ""))
    if isinstance(v, dict):
        return " | ".join(f"{k.replace('_', ' ')}: {_coerce_profile_value(val)}" for k, val in v.items())
    return "" if v is None else str(v)


class ImportJsonBody(BaseModel):
    profile: dict = {}
    variables: list[dict] = []


@app.post("/api/workspaces/{slug}/import")
def import_workspace_json(slug: str, body: ImportJsonBody):
    """Paste a JSON config (e.g. built by ChatGPT) to set the client profile and
    custom variables for a workspace, instead of building them by hand."""
    s = SessionLocal()
    try:
        w = s.query(Workspace).filter_by(slug=slug).first()
        if not w:
            raise HTTPException(404, "Import is only available for your own workspaces, not built-in clients.")
        if body.profile:
            merged = dict(w.profile or {})
            for k, v in body.profile.items():
                merged[k] = _coerce_profile_value(v)
            w.profile = merged
        added = 0
        for v in body.variables:
            if not isinstance(v, dict):
                continue
            spec = ea.build_custom_spec(
                label=v.get("label") or v.get("name") or "Variable",
                template=v.get("template", "") or "",
                placeholders=v.get("placeholders", []) or [],
                min_words=v.get("min_words"), max_words=v.get("max_words"),
                purpose=v.get("purpose") or v.get("guidance", "") or "",
                examples=v.get("examples", []) or [],
            )
            existing = s.query(CustomVariable).filter_by(variable_set=slug, name=spec["name"]).first()
            if existing:
                existing.label = spec["label"]
                existing.spec = spec
            else:
                s.add(CustomVariable(variable_set=slug, name=spec["name"], label=spec["label"], spec=spec))
            added += 1
        s.commit()
        return {"ok": True, "variables_imported": added, "profile_updated": bool(body.profile)}
    finally:
        s.close()


@app.patch("/api/workspaces/{slug}")
def update_workspace(slug: str, body: ProfileBody):
    s = SessionLocal()
    try:
        w = s.query(Workspace).filter_by(slug=slug).first()
        if not w:
            raise HTTPException(404, "Workspace not found")
        merged = dict(w.profile or {})
        merged.update(body.profile or {})
        w.profile = merged
        s.commit()
        return {"ok": True}
    finally:
        s.close()


@app.delete("/api/workspaces/{slug}")
def delete_workspace(slug: str):
    s = SessionLocal()
    try:
        w = s.query(Workspace).filter_by(slug=slug).first()
        if w:
            s.delete(w)
            s.commit()
        return {"ok": True}
    finally:
        s.close()


@app.get("/api/engine-sets")
def engine_sets():
    return ea.list_variable_sets()


@app.get("/api/profiles")
def profiles():
    return ea.list_profiles()


@app.get("/api/profiles/{name}")
def profile(name: str):
    return ea.get_profile(name)


@app.get("/api/format/{variable_set}")
def format_spec(variable_set: str):
    s = SessionLocal()
    try:
        ws = s.query(Workspace).filter_by(slug=variable_set).first()
        base = _base_of(s, variable_set)
        spec = ea.format_spec(base) if base else {"variable_set": variable_set, "client": "", "variables": []}
        spec["variable_set"] = variable_set
        spec["workspace"] = bool(ws)
        spec["profile_editable"] = bool(ws)
        if ws:
            spec["client_name"] = ws.name
            spec["profile_fields"] = ea.profile_fields_from(ws.profile or {}) or ea.profile_blank_fields()
        hidden = _hidden_names(s, variable_set)
        rows = s.query(CustomVariable).filter_by(variable_set=variable_set).order_by(CustomVariable.id).all()
        custom_cards = []
        for r in rows:
            card = ea.custom_card(r.spec)
            card["id"] = r.id
            custom_cards.append(card)
    finally:
        s.close()
    cset = {c["name"] for c in custom_cards}  # custom overrides same-named built-in
    builtins = [v for v in spec.get("variables", []) if v.get("name") not in cset]
    for v in builtins:
        v["hidden"] = v.get("name") in hidden
    spec["variables"] = builtins + custom_cards
    return spec


@app.get("/api/enrichments")
def enrichments(variable_set: str = "ascendly_lean"):
    s = SessionLocal()
    try:
        base = _base_of(s, variable_set)
        hidden = _hidden_names(s, variable_set)
        rows = s.query(CustomVariable).filter_by(variable_set=variable_set).order_by(CustomVariable.id).all()
        custom_names = [r.name for r in rows]
        labels = {r.name: r.label for r in rows}
    finally:
        s.close()
    all_keys = ea.output_keys_for(base)
    cset = set(custom_names)  # custom variables override same-named built-ins
    selectable = [k for k in all_keys if k not in ea.ALWAYS_KEYS and k not in hidden and k not in cset]
    return {
        "always": ea.ALWAYS_KEYS,
        "selectable": selectable + custom_names,
        "all": [k for k in all_keys if k not in hidden and k not in cset] + custom_names,
        "labels": labels,
    }


@app.get("/api/lists")
def get_lists(variable_set: Optional[str] = None):
    s = SessionLocal()
    try:
        q = s.query(LeadList)
        if variable_set:  # scope lists to the current workspace
            q = q.filter_by(variable_set=variable_set)
        out = []
        for l in q.order_by(LeadList.created_at.desc()).all():
            count = s.query(Lead).filter_by(list_id=l.id).count()
            out.append({"id": l.id, "name": l.name, "variable_set": l.variable_set,
                        "count": count, "created_at": l.created_at.isoformat()})
        return out
    finally:
        s.close()


@app.post("/api/lists")
def create_list(body: CreateList):
    s = SessionLocal()
    try:
        l = LeadList(name=body.name.strip() or "Untitled list", variable_set=body.variable_set)
        s.add(l)
        s.commit()
        return {"id": l.id, "name": l.name, "variable_set": l.variable_set, "count": 0}
    finally:
        s.close()


class IdsBody(BaseModel):
    lead_ids: list[int] = []


@app.post("/api/lists/{list_id}/clear")
def clear_results(list_id: int, body: IdsBody):
    """Clear enrichment results (selected leads, or the whole list if none given).
    Verification is kept."""
    s = SessionLocal()
    try:
        q = s.query(Lead).filter_by(list_id=list_id)
        if body.lead_ids:
            q = q.filter(Lead.id.in_(body.lead_ids))
        n = q.update({Lead.result: {}, Lead.status: "pending"}, synchronize_session=False)
        s.commit()
        return {"ok": True, "cleared": n}
    finally:
        s.close()


@app.delete("/api/lists/{list_id}/leads")
def delete_leads(list_id: int, ids: str):
    s = SessionLocal()
    try:
        wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        n = 0
        if wanted:
            n = s.query(Lead).filter(Lead.list_id == list_id, Lead.id.in_(wanted)).delete(synchronize_session=False)
            s.commit()
        return {"ok": True, "deleted": n}
    finally:
        s.close()


SAVED_LEADS_NAME = "Saved leads"


@app.delete("/api/lists/{list_id}")
def delete_list(list_id: int, keep: int = 0):
    """Delete a list. keep=1 -> remove the list but KEEP its leads in the workspace
    database (they move into a 'Saved leads' list). keep=0 -> delete leads too."""
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            return {"ok": True}
        if keep:
            # move this list's leads into the workspace's "Saved leads" list
            saved = (s.query(LeadList)
                     .filter_by(variable_set=l.variable_set, name=SAVED_LEADS_NAME).first())
            if not saved:
                saved = LeadList(name=SAVED_LEADS_NAME, variable_set=l.variable_set)
                s.add(saved)
                s.commit()
            if saved.id != list_id:
                s.query(Lead).filter_by(list_id=list_id).update(
                    {Lead.list_id: saved.id}, synchronize_session=False)
                s.query(Job).filter_by(list_id=list_id).delete(synchronize_session=False)
                s.query(LeadList).filter_by(id=list_id).delete(synchronize_session=False)
                s.commit()
            return {"ok": True, "kept": True, "saved_list": saved.name}
        # full delete: bulk statements, fast even for thousands of leads
        s.query(Job).filter_by(list_id=list_id).delete(synchronize_session=False)
        s.query(Lead).filter_by(list_id=list_id).delete(synchronize_session=False)
        s.query(LeadList).filter_by(id=list_id).delete(synchronize_session=False)
        s.commit()
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/lists/{list_id}/upload")
async def upload(list_id: int, file: UploadFile = File(...), mapping: Optional[str] = Form(None)):
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        m = None
        if mapping:
            try:
                m = json.loads(mapping)
                if not isinstance(m, dict):
                    m = None
            except Exception:
                m = None
        rows = _parse_csv(await file.read(), m)
        for r in rows:
            s.add(Lead(list_id=list_id, **r))
        s.commit()
        return {"imported": len(rows), "list_id": list_id}
    finally:
        s.close()


@app.get("/api/lists/{list_id}")
def get_list(list_id: int):
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        # CAP: never load an entire huge list into memory (that OOMs the container).
        # The grid windows to a few hundred rows anyway; big lists are browsed via
        # the paginated Database view.
        total = s.query(func.count(Lead.id)).filter_by(list_id=list_id).scalar() or 0
        leads = (s.query(Lead).filter_by(list_id=list_id)
                 .order_by(Lead.id).limit(LIST_LEAD_CAP).all())
        job = (s.query(Job).filter_by(list_id=list_id)
               .order_by(Job.created_at.desc()).first())
        return {
            "list": {"id": l.id, "name": l.name, "variable_set": l.variable_set,
                     "count": total, "shown": len(leads), "truncated": total > len(leads)},
            "leads": [{
                "id": ld.id, "first_name": ld.first_name, "last_name": ld.last_name,
                "title": ld.title, "company": ld.company, "website": ld.website,
                "email": ld.email, "status": ld.status, "result": ld.result or {},
                "verify": ld.verify or {}, "email_status": ld.email_status or "",
                "industry": ld.industry or "", "title_status": ld.title_status or "",
                "esp": ld.esp or "",
            } for ld in leads],
            "job": None if not job else {
                "id": job.id, "kind": job.kind or "enrich", "status": job.status,
                "total": job.total, "done": job.done,
                "icp": job.icp, "nonicp": job.nonicp, "rejected": job.rejected,
                "summary": job.summary or {}, "cost": round(job.cost or 0, 4),
                "enrichments": job.enrichments or [],
            },
        }
    finally:
        s.close()


@app.post("/api/lists/{list_id}/run")
def run_list(list_id: int, body: RunBody):
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        q = s.query(Lead).filter_by(list_id=list_id).order_by(Lead.id)
        if body.lead_ids:
            candidates = q.filter(Lead.id.in_(body.lead_ids)).all()
        elif body.limit and body.limit > 0:
            candidates = q.limit(body.limit).all()
        else:
            candidates = q.all()
        # Verify-first: only enrich leads Reoon marked safe/deliverable.
        safe_leads = [ld for ld in candidates if _lead_safe(ld)] if body.only_safe else list(candidates)
        skipped_unsafe = len(candidates) - len(safe_leads)
        if body.skip_done:
            targets = [ld for ld in safe_leads if not ld.result]  # resume: skip already enriched
        else:
            targets = safe_leads
            for ld in targets:
                ld.status = "pending"
                ld.result = {}
        skipped_done = len(safe_leads) - len(targets)
        job = Job(list_id=list_id, variable_set=l.variable_set, enrichments=body.enrichments,
                  status="queued" if targets else "done", total=len(targets))
        s.add(job)
        s.commit()
        job_id = job.id
        target_ids = [ld.id for ld in targets]
    finally:
        s.close()
    if target_ids:
        threading.Thread(target=_run_job, args=(job_id, target_ids, body.workers), daemon=True).start()
    return {"job_id": job_id, "count": len(target_ids),
            "skipped_unsafe": skipped_unsafe, "skipped_done": skipped_done}


@app.post("/api/lists/{list_id}/verify")
def verify_list(list_id: int, body: VerifyBody):
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        q = s.query(Lead).filter_by(list_id=list_id).order_by(Lead.id)
        if body.lead_ids:
            candidates = q.filter(Lead.id.in_(body.lead_ids)).all()
        elif body.limit and body.limit > 0:
            candidates = q.limit(body.limit).all()
        else:
            candidates = q.all()
        if body.skip_done:
            targets = [ld for ld in candidates if not ld.verify]  # resume: skip verified
        else:
            targets = candidates
            for ld in targets:
                ld.verify = {}
                ld.email_status = ""
        skipped_done = len(candidates) - len(targets)
        job = Job(list_id=list_id, kind="verify", status="queued" if targets else "done",
                  total=len(targets), variable_set=l.variable_set, summary={})
        s.add(job)
        s.commit()
        job_id = job.id
        target_ids = [ld.id for ld in targets]
    finally:
        s.close()
    if target_ids:
        threading.Thread(target=_run_verify_job, args=(job_id, target_ids, body.mode, body.workers), daemon=True).start()
    return {"job_id": job_id, "count": len(target_ids), "skipped_done": skipped_done}


@app.post("/api/lists/{list_id}/run-pipeline")
def run_pipeline(list_id: int, body: PipelineBody):
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        q = s.query(Lead).filter_by(list_id=list_id).order_by(Lead.id)
        if body.lead_ids:
            candidates = q.filter(Lead.id.in_(body.lead_ids)).all()
        elif body.limit and body.limit > 0:
            candidates = q.limit(body.limit).all()
        else:
            candidates = q.all()

        def done(ld):
            if not ld.verify:
                return False
            safe = reoon.bucket(ld.verify)[0] == "valid"
            return (not safe) or bool(ld.result)  # nothing left to do

        if body.skip_done:
            targets = [ld for ld in candidates if not done(ld)]
        else:
            targets = candidates
            for ld in targets:
                ld.result = {}  # re-enrich (verification is kept to save credits)
        job = Job(list_id=list_id, kind="pipeline", status="queued" if targets else "done",
                  total=len(targets), variable_set=l.variable_set, enrichments=body.enrichments, summary={})
        s.add(job)
        s.commit()
        job_id = job.id
        target_ids = [ld.id for ld in targets]
    finally:
        s.close()
    if target_ids:
        threading.Thread(target=_run_pipeline_job, args=(job_id, target_ids, body.mode, body.workers), daemon=True).start()
    return {"job_id": job_id, "count": len(target_ids)}


@app.get("/api/status")
def status():
    from db import DB_URL
    if DB_URL.startswith("postgresql"):
        backend, persistent = "Postgres (Neon)", True
    elif "/data/" in DB_URL:
        backend, persistent = "SQLite on Volume", True
    elif DB_URL.startswith("sqlite"):
        backend, persistent = "SQLite (local file)", False
    else:
        backend, persistent = "Custom", True
    return {"db": backend, "persistent": persistent}


@app.get("/api/taxonomy")
def get_taxonomy():
    return ea.taxonomy()


class ImportFieldBody(BaseModel):
    name: str


@app.get("/api/import-fields")
def list_import_fields():
    s = SessionLocal()
    try:
        return [r.name for r in s.query(ImportField).order_by(ImportField.id).all()]
    finally:
        s.close()


@app.post("/api/import-fields")
def add_import_field(body: ImportFieldBody):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Field name required")
    s = SessionLocal()
    try:
        exists = s.query(ImportField).filter(func.lower(ImportField.name) == name.lower()).first()
        if not exists:
            s.add(ImportField(name=name))
            s.commit()
        return [r.name for r in s.query(ImportField).order_by(ImportField.id).all()]
    finally:
        s.close()


@app.delete("/api/import-fields/{name}")
def delete_import_field(name: str):
    s = SessionLocal()
    try:
        r = s.query(ImportField).filter(func.lower(ImportField.name) == name.lower()).first()
        if r:
            s.delete(r)
            s.commit()
        return [x.name for x in s.query(ImportField).order_by(ImportField.id).all()]
    finally:
        s.close()


class RulesBody(BaseModel):
    text: str = ""


@app.get("/api/rules/{variable_set}")
def get_rules(variable_set: str):
    s = SessionLocal()
    try:
        return {"variable_set": variable_set, "text": _rules_text(s, variable_set)}
    finally:
        s.close()


@app.put("/api/rules/{variable_set}")
def save_rules(variable_set: str, body: RulesBody):
    """Save the correction rules for a format set / workspace. Applied live: any
    in-flight run reads these fresh for each lead not yet enriched."""
    s = SessionLocal()
    try:
        row = s.query(EnrichRule).filter_by(variable_set=variable_set).first()
        if not row:
            row = EnrichRule(variable_set=variable_set, text=body.text or "")
            s.add(row)
        else:
            row.text = body.text or ""
        s.commit()
        rules = _rules_list(s, variable_set)
        return {"ok": True, "count": len(rules)}
    finally:
        s.close()


# --------------------- Workspace Builder config (storage only) ---------------------
# These endpoints just STORE the new configuration sections a workspace can have.
# The existing enrichment pipeline does not consume them yet; this is the
# Workspace Builder's persistence layer and nothing here changes how leads are
# processed today.

DEFAULT_QUALIFICATION_QUESTIONS = [
    "What does this company sell?",
    "Who do they sell to?",
    "Is this B2B or B2C?",
    "Is the likely deal size high enough?",
    "Can buyers be identified and reached through outbound?",
    "Is there a large enough TAM for outbound?",
    "Does the company need more pipeline, better follow-up, or revenue recovery?",
    "Can we realistically provide results for this company?",
    "Are there any hard rejection signals?",
    "Final decision: ICP / Possible ICP / Needs Review / Non-ICP",
]
DEFAULT_HARD_REJECTION_RULES = [
    "Low-ticket SaaS with public pricing under $5k/month",
    "Local-only business",
    "Consumer business (B2C)",
    "Ecommerce",
    "Tiny TAM",
    "Mostly tender / government / distributor / channel sales",
    "No clear buyer universe",
    "Cannot identify at least 1,000+ realistic target accounts/buyers",
    "Cannot realistically deliver pipeline in 90-120 days",
]
# The structured fields the ICP engine returns for every lead (Vision: explainable
# + filterable). Order = display order.
ICP_OUTPUT_FIELDS = [
    "final_decision", "fit_score", "confidence", "business_model", "buyer_reachability",
    "serviceability", "tam_estimate", "revenue_model", "primary_icp_reason",
    "primary_reject_reason", "summary",
]

_CONFIG_DEFAULTS = {
    # Section 1 — Client Profile (explains the client + offer only)
    "client_profile": {
        "what_client_does": "", "main_offer": "", "what_we_pitch": "",
        "target_outcome": "", "buyer_persona": "", "deal_size": "",
        "geo": "", "notes": "", "permanent_instructions": "",
    },
    # Section 2 — ICP / Non-ICP (the classification logic; single source of truth).
    # Starts EMPTY so a new workspace is a clean slate. Users add their own rules,
    # or click "Load example template" in the UI to pull in the starter set.
    "icp": {
        "icp_description": "",
        "non_icp_description": "",
        "hard_rejection_rules": [],
        "qualification_questions": [],
    },
    # --- legacy/hidden sections kept so older saved configs round-trip safely ---
    "strategy": {},
    "knowledge": [],
    "analysis": [],
    "decision": [],
    "assets": [],
    "export": {"fields": [], "notes": ""},
}


def _config_with_defaults(sections):
    sections = dict(sections or {})
    out = {}
    for key, default in _CONFIG_DEFAULTS.items():
        val = sections.get(key)
        if val is None:
            out[key] = json.loads(json.dumps(default))  # deep copy
        else:
            out[key] = val
    # keep any forward-compatible extra keys untouched
    for k, v in sections.items():
        if k not in out:
            out[k] = v
    return out


def _prefill_config_from_profile(s, variable_set, sections):
    """Seed the new Client Profile + ICP sections from any existing client profile
    or older 'strategy' config so the page isn't blank on first open. Read-only
    seed — never overwrites stored data, never auto-saved."""
    ws = s.query(Workspace).filter_by(slug=variable_set).first()
    prof = (ws.profile if ws else None) or {}
    strat = sections.get("strategy") or {}

    cp = sections.get("client_profile") or {}
    if not any((cp.get(k) or "").strip() for k in cp):
        cp_map = {
            "what_client_does": prof.get("service_brief") or prof.get("business_overview") or strat.get("business_overview"),
            "main_offer": prof.get("main_offer") or strat.get("offers"),
            "what_we_pitch": prof.get("what_we_are_pitching") or strat.get("positioning"),
            "target_outcome": prof.get("target_outcome") or strat.get("objectives"),
            "buyer_persona": prof.get("buyer_personas") or strat.get("buyer_personas"),
            "deal_size": prof.get("deal_size") or strat.get("deal_size"),
            "geo": prof.get("geo") or strat.get("geo"),
            "notes": prof.get("notes") or strat.get("notes"),
            "permanent_instructions": prof.get("permanent_instructions") or strat.get("instructions"),
        }
        for k, v in cp_map.items():
            if v and not (cp.get(k) or "").strip():
                cp[k] = v if isinstance(v, str) else json.dumps(v)
        sections["client_profile"] = cp

    icp = sections.get("icp") or {}
    if not (icp.get("icp_description") or "").strip():
        seed = prof.get("icp_summary") or prof.get("icp") or strat.get("icp")
        if seed:
            icp["icp_description"] = seed if isinstance(seed, str) else json.dumps(seed)
    if not (icp.get("non_icp_description") or "").strip():
        seed = prof.get("non_icp") or strat.get("non_icp")
        if seed:
            icp["non_icp_description"] = seed if isinstance(seed, str) else json.dumps(seed)
    sections["icp"] = icp
    return sections


@app.get("/api/workspaces/{variable_set}/config")
def get_workspace_config(variable_set: str):
    s = SessionLocal()
    try:
        row = s.query(WorkspaceConfig).filter_by(variable_set=variable_set).first()
        sections = _config_with_defaults(row.sections if row else {})
        # No auto-seeding / auto-prefill: a workspace shows ONLY what was saved (or
        # empty). This avoids "old/template config appearing in a new workspace".
        return {"variable_set": variable_set, "sections": sections,
                "saved": bool(row),
                "icp_output_fields": ICP_OUTPUT_FIELDS,
                "icp_example": {"hard_rejection_rules": list(DEFAULT_HARD_REJECTION_RULES),
                                "qualification_questions": list(DEFAULT_QUALIFICATION_QUESTIONS)}}
    finally:
        s.close()


class ConfigBody(BaseModel):
    sections: dict = {}


@app.put("/api/workspaces/{variable_set}/config")
def save_workspace_config(variable_set: str, body: ConfigBody):
    """Upsert the workspace's builder sections. Storage only — does not touch the
    enrichment pipeline, leads, profile, or variables."""
    s = SessionLocal()
    try:
        sections = _config_with_defaults(body.sections or {})
        row = s.query(WorkspaceConfig).filter_by(variable_set=variable_set).first()
        if not row:
            row = WorkspaceConfig(variable_set=variable_set, sections=sections)
            s.add(row)
        else:
            row.sections = sections
        s.commit()
        counts = {k: (len(v) if isinstance(v, list) else 1) for k, v in sections.items()}
        return {"ok": True, "variable_set": variable_set, "counts": counts}
    finally:
        s.close()


class ResetConfigBody(BaseModel):
    confirm: str = ""


@app.post("/api/admin/reset-config")
def reset_config(body: ResetConfigBody):
    """Wipe ALL workspace CONFIG across every workspace — ICP / Non-ICP, Client
    Profile, Formats (variables), Rules — and clear stored client profiles.

    Does NOT touch leads, lists, classifications (industry/ESP/title/ICP),
    enrichment results, or email verification. Workspace rows are kept so the
    lists stay reachable; only their config is cleared. Requires the exact
    confirmation phrase 'RESET CONFIG'."""
    if (body.confirm or "").strip().upper() != "RESET CONFIG":
        raise HTTPException(400, "Type the exact phrase RESET CONFIG to confirm.")
    s = SessionLocal()
    try:
        wiped = {
            "workspace_configs": s.query(WorkspaceConfig).delete(synchronize_session=False),
            "custom_variables": s.query(CustomVariable).delete(synchronize_session=False),
            "enrich_rules": s.query(EnrichRule).delete(synchronize_session=False),
            "hidden_variables": s.query(HiddenVariable).delete(synchronize_session=False),
        }
        wiped["workspace_profiles_cleared"] = s.query(Workspace).update(
            {Workspace.profile: {}}, synchronize_session=False)
        s.commit()
        return {"ok": True, "wiped": wiped,
                "kept": "all leads, lists, classifications, enrichment results, and email verification"}
    finally:
        s.close()


@app.post("/api/lists/{list_id}/classify")
def classify_list(list_id: int, body: ClassifyBody):
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        q = s.query(Lead).filter_by(list_id=list_id).order_by(Lead.id)
        if body.lead_ids:
            candidates = q.filter(Lead.id.in_(body.lead_ids)).all()
        elif body.limit and body.limit > 0:
            candidates = q.limit(body.limit).all()
        else:
            candidates = q.all()
        targets = [ld for ld in candidates if not ld.industry] if body.skip_done else candidates
        job = Job(list_id=list_id, kind="classify", status="queued" if targets else "done",
                  total=len(targets), variable_set=l.variable_set, summary={})
        s.add(job)
        s.commit()
        job_id = job.id
        target_ids = [ld.id for ld in targets]
    finally:
        s.close()
    if target_ids:
        threading.Thread(target=_run_classify_job, args=(job_id, target_ids, body.workers), daemon=True).start()
    return {"job_id": job_id, "count": len(target_ids)}


class TitleBody(BaseModel):
    lead_ids: list[int] = []
    limit: Optional[int] = None
    skip_done: bool = True


class EspBody(BaseModel):
    lead_ids: list[int] = []
    limit: Optional[int] = None
    skip_done: bool = True
    workers: Optional[int] = None


@app.post("/api/lists/{list_id}/esp-check")
def esp_check_list(list_id: int, body: EspBody):
    """Detect each lead's email provider (Microsoft/Google/Other) via MX records.
    Free - DNS only, no API or credits."""
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        q = s.query(Lead).filter_by(list_id=list_id).order_by(Lead.id)
        if body.lead_ids:
            candidates = q.filter(Lead.id.in_(body.lead_ids)).all()
        elif body.limit and body.limit > 0:
            candidates = q.limit(body.limit).all()
        else:
            candidates = q.all()
        targets = [ld for ld in candidates if not ld.esp] if body.skip_done else candidates
        job = Job(list_id=list_id, kind="esp", status="queued" if targets else "done",
                  total=len(targets), variable_set=l.variable_set, summary={})
        s.add(job)
        s.commit()
        job_id = job.id
        target_ids = [ld.id for ld in targets]
    finally:
        s.close()
    if target_ids:
        threading.Thread(target=_run_esp_job, args=(job_id, target_ids, body.workers), daemon=True).start()
    return {"job_id": job_id, "count": len(target_ids)}


@app.post("/api/lists/{list_id}/title-check")
def title_check_list(list_id: int, body: TitleBody):
    """Standalone title check: mark each lead title_status pass/rejected by the
    decision-maker title rules. No scraping, no API, no credits."""
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        q = s.query(Lead).filter_by(list_id=list_id).order_by(Lead.id)
        if body.lead_ids:
            candidates = q.filter(Lead.id.in_(body.lead_ids)).all()
        elif body.limit and body.limit > 0:
            candidates = q.limit(body.limit).all()
        else:
            candidates = q.all()
        targets = [ld for ld in candidates if not ld.title_status] if body.skip_done else candidates
        job = Job(list_id=list_id, kind="titlecheck", status="queued" if targets else "done",
                  total=len(targets), variable_set=l.variable_set, summary={})
        s.add(job)
        s.commit()
        job_id = job.id
        target_ids = [ld.id for ld in targets]
    finally:
        s.close()
    if target_ids:
        threading.Thread(target=_run_title_job, args=(job_id, target_ids), daemon=True).start()
    return {"job_id": job_id, "count": len(target_ids)}


@app.post("/api/lists/{list_id}/split-by-industry")
def split_by_industry(list_id: int):
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        # distinct industries present (cheap), then copy each group in batches so a
        # huge list doesn't load all leads into memory at once.
        industries = [row[0] for row in
                      s.query(Lead.industry).filter_by(list_id=list_id).distinct().all()]
        created = []
        for ind in sorted(industries, key=lambda x: (x or "")):
            label = ind or "Unclassified"
            cond = (Lead.industry == ind) if ind else ((Lead.industry == None) | (Lead.industry == ""))  # noqa: E711
            ids = [i for (i,) in s.query(Lead.id).filter(Lead.list_id == list_id).filter(cond).all()]
            if not ids:
                continue
            nl = LeadList(name=f"{l.name} · {label}", variable_set=l.variable_set)
            s.add(nl)
            s.flush()
            nlid = nl.id
            cnt = 0
            for j in range(0, len(ids), 1000):
                chunk = ids[j:j + 1000]
                for ld in s.query(Lead).filter(Lead.id.in_(chunk)).all():
                    s.add(Lead(list_id=nlid, first_name=ld.first_name, last_name=ld.last_name,
                               title=ld.title, company=ld.company, website=ld.website, email=ld.email,
                               data=ld.data, result=ld.result, verify=ld.verify,
                               email_status=ld.email_status, industry=ld.industry, status=ld.status))
                    cnt += 1
                s.commit()
            created.append({"industry": label, "count": cnt})
        return {"created": created}
    finally:
        s.close()


@app.get("/api/reoon/balance")
def reoon_balance():
    if not reoon.enabled():
        return {"enabled": False}
    try:
        b = reoon.check_balance()
        return {"enabled": True,
                "daily": b.get("remaining_daily_credits"),
                "instant": b.get("remaining_instant_credits"),
                "api_status": b.get("api_status")}
    except Exception as exc:
        return {"enabled": True, "error": str(exc)}


def _do_export(list_id, wanted):
    # header info + filename from a small sample (memory-safe), then stream rows
    s0 = SessionLocal()
    try:
        l = s0.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        fname = (l.name or "list").replace(" ", "_").replace("/", "-") + ".csv"
        vset = l.variable_set
        hidden = _hidden_names(s0, vset)
        custom_names = [c.get("name") for c in _custom_specs(s0, vset)]
        cset = set(custom_names)
        out_keys = [k for k in ea.output_keys_for(_base_of(s0, vset))
                    if k not in hidden and k not in cset] + custom_names

        def base_query(s):
            q = s.query(Lead).filter_by(list_id=list_id)
            if wanted:
                q = q.filter(Lead.id.in_(wanted))
            return q.order_by(Lead.id)

        sample = base_query(s0).limit(500).all()
        raw_cols = []
        for ld in sample:
            for k in (ld.data or {}).keys():
                if k not in raw_cols and k.lower() not in _STD_RAW_SKIP:
                    raw_cols.append(k)
    finally:
        s0.close()
    header = [lbl for lbl, _ in _STD_EXPORT] + raw_cols + ["Safe to send"] + out_keys

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        s = SessionLocal()
        try:
            q = s.query(Lead).filter_by(list_id=list_id)
            if wanted:
                q = q.filter(Lead.id.in_(wanted))
            for ld in q.order_by(Lead.id).yield_per(1000):
                row = _std_export_row(ld)
                row += [(ld.data or {}).get(c, "") for c in raw_cols]
                row.append((ld.verify or {}).get("is_safe_to_send", ""))
                row += [(ld.result or {}).get(k, "") for k in out_keys]
                w.writerow(row)
                if buf.tell() > 64000:
                    yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            if buf.tell():
                yield buf.getvalue()
        finally:
            s.close()

    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/lists/{list_id}/export")
def export_list(list_id: int, ids: Optional[str] = None):
    # GET kept for small selections; large ones use POST (below) to avoid 431.
    wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()] if ids else None
    return _do_export(list_id, wanted)


class ExportBody(BaseModel):
    ids: list[int] = []


@app.post("/api/lists/{list_id}/export")
def export_list_post(list_id: int, body: ExportBody):
    """IDs in the body (no URL length limit), so exporting a full/large selection
    can't hit HTTP 431. Empty ids = whole list."""
    return _do_export(list_id, body.ids or None)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    CANCEL.add(job_id)  # live workers see this and stop
    s = SessionLocal()
    try:
        job = s.get(Job, job_id)
        if job and job.status in ("queued", "running", "cancelling"):
            # set cancelled immediately so the UI clears even if the worker
            # already died (e.g. the app slept) and can't update it itself
            job.status = "cancelled"
            s.commit()
    finally:
        s.close()
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    s = SessionLocal()
    try:
        job = s.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return {"id": job.id, "kind": job.kind or "enrich", "status": job.status,
                "total": job.total, "done": job.done,
                "icp": job.icp, "nonicp": job.nonicp, "rejected": job.rejected,
                "summary": job.summary or {}, "cost": round(job.cost or 0, 4)}
    finally:
        s.close()


# ------------------------- Database (Apollo-style) view -------------------------

class DBFilters(BaseModel):
    industry: Optional[str] = None        # single (legacy)
    esp: Optional[str] = None             # single (legacy)
    industries: list[str] = []            # multi-select (OR); "__unclassified__" allowed
    esps: list[str] = []                  # multi-select (OR)
    title_status: Optional[str] = None
    email_status: Optional[str] = None
    country: Optional[str] = None
    seniority: Optional[str] = None
    employees_min: Optional[int] = None
    employees_max: Optional[int] = None
    q: Optional[str] = None
    lead_ids: list[int] = []   # explicit selection; when set, overrides the filters
    page: int = 1
    page_size: int = 50


def _ws_list_ids(s, slug):
    return [lid for (lid,) in s.query(LeadList.id).filter_by(variable_set=slug).all()]


def _database_query(s, list_ids, f):
    """A Lead query across all of a workspace's lists, with the active filters."""
    if not list_ids:
        return s.query(Lead).filter(Lead.id < 0)  # empty
    q = s.query(Lead).filter(Lead.list_id.in_(list_ids))
    # industry: multi-select list, or single (legacy); "__unclassified__" = no industry
    inds = list(f.get("industries") or [])
    if not inds and f.get("industry"):
        inds = [f["industry"]]
    if inds:
        named = [x for x in inds if x and x != "__unclassified__"]
        conds = []
        if named:
            conds.append(Lead.industry.in_(named))
        if "__unclassified__" in inds:
            conds.append((Lead.industry == None) | (Lead.industry == ""))  # noqa: E711
        if conds:
            q = q.filter(or_(*conds))
    # esp: multi-select list, or single (legacy)
    esps = list(f.get("esps") or [])
    if not esps and f.get("esp"):
        esps = [f["esp"]]
    if esps:
        q = q.filter(Lead.esp.in_(esps))
    if f.get("title_status"):
        q = q.filter(Lead.title_status == f["title_status"])
    if f.get("email_status"):
        q = q.filter(Lead.email_status.ilike(f"%{f['email_status']}%"))
    if f.get("country"):
        q = q.filter(Lead.country.ilike(f"%{f['country']}%"))
    if f.get("seniority"):
        q = q.filter(Lead.seniority.ilike(f"%{f['seniority']}%"))
    if f.get("employees_min") is not None:
        q = q.filter(Lead.employees != None, Lead.employees >= f["employees_min"])  # noqa: E711
    if f.get("employees_max") is not None:
        q = q.filter(Lead.employees != None, Lead.employees <= f["employees_max"])  # noqa: E711
    if f.get("q"):
        like = f"%{f['q'].strip()}%"
        q = q.filter(or_(Lead.first_name.ilike(like), Lead.last_name.ilike(like),
                         Lead.company.ilike(like), Lead.email.ilike(like)))
    return q


def _db_lead(ld):
    return {
        "id": ld.id, "list_id": ld.list_id,
        "first_name": ld.first_name or "", "last_name": ld.last_name or "",
        "title": ld.title or "", "company": ld.company or "", "email": ld.email or "",
        "website": ld.website or "", "industry": ld.industry or "", "esp": ld.esp or "",
        "employees": ld.employees, "country": ld.country or "", "state": ld.state or "",
        "seniority": ld.seniority or "", "email_status": ld.email_status or "",
        "title_status": ld.title_status or "",
        "data": ld.data or {},   # full original row, for all-columns view + detail drawer
    }


@app.post("/api/workspaces/{slug}/database")
def database_view(slug: str, body: DBFilters):
    s = SessionLocal()
    try:
        list_ids = _ws_list_ids(s, slug)
        grand_total = (s.query(func.count(Lead.id)).filter(Lead.list_id.in_(list_ids)).scalar()
                       if list_ids else 0)
        q = _database_query(s, list_ids, body.dict())
        total = q.count()
        size = max(1, min(body.page_size, 200))
        page = max(1, body.page)
        leads = q.order_by(Lead.id).offset((page - 1) * size).limit(size).all()

        def facet(col):
            rows = q.with_entities(col, func.count()).group_by(col).all()
            return sorted([{"value": v, "count": c} for v, c in rows if v],
                          key=lambda x: -x["count"])

        # union of the raw column names on this page, so the grid can show every
        # imported field (LinkedIn, Company Address, custom fields, etc.)
        data_columns = []
        for ld in leads:
            for k in (ld.data or {}).keys():
                if k not in data_columns:
                    data_columns.append(k)
        return {
            "grand_total": grand_total, "total": total, "page": page, "page_size": size,
            "pages": (total + size - 1) // size if size else 1,
            "facets": {"industry": facet(Lead.industry), "esp": facet(Lead.esp)},
            "data_columns": data_columns,
            "leads": [_db_lead(ld) for ld in leads],
        }
    finally:
        s.close()


@app.post("/api/workspaces/{slug}/database/export")
def database_export(slug: str, body: DBFilters):
    """Streamed export: rows are pulled from the DB in batches and written out as
    they go, so exporting hundreds of thousands of leads never loads them all into
    memory at once (that was an OOM source)."""
    f = body.dict()
    ids = list(body.lead_ids or [])

    def base_query(s):
        if ids:
            return s.query(Lead).filter(Lead.id.in_(ids)).order_by(Lead.id)
        return _database_query(s, _ws_list_ids(s, slug), f).order_by(Lead.id)

    # column header from a small sample only (memory-safe)
    s0 = SessionLocal()
    try:
        sample = base_query(s0).limit(500).all()
        raw_cols = []
        for ld in sample:
            for k in (ld.data or {}).keys():
                if k not in raw_cols and k.lower() not in _STD_RAW_SKIP:
                    raw_cols.append(k)
    finally:
        s0.close()
    header = [lbl for lbl, _ in _STD_EXPORT] + raw_cols + ["Title Check", "Safe to send"]

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        s = SessionLocal()
        try:
            for ld in base_query(s).yield_per(1000):
                row = _std_export_row(ld)
                row += [(ld.data or {}).get(c, "") for c in raw_cols]
                row += [ld.title_status or "", (ld.verify or {}).get("is_safe_to_send", "")]
                w.writerow(row)
                if buf.tell() > 64000:
                    yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            if buf.tell():
                yield buf.getvalue()
        finally:
            s.close()

    return StreamingResponse(gen(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{slug}_database.csv"'})


class DBSendBody(DBFilters):
    target: str
    list_name: str = ""
    lead_ids: list[int] = []


@app.post("/api/workspaces/{slug}/database/send")
def database_send(slug: str, body: DBSendBody):
    """Copy a filtered (or explicitly selected) set of leads into a target
    workspace as a new list, ready for verify + enrich. Source stays intact."""
    s = SessionLocal()
    try:
        if not (s.query(Workspace).filter_by(slug=body.target).first() or ea.engine_set_exists(body.target)):
            raise HTTPException(404, "Target workspace not found")
        # Read just the source IDs first (cheap), then copy in batches. Avoids an
        # open streaming cursor during writes (which deadlocks) and avoids loading
        # all source rows into memory at once.
        if body.lead_ids:
            src_ids = list(body.lead_ids)
        else:
            list_ids = _ws_list_ids(s, slug)
            src_ids = [lid for (lid,) in _database_query(s, list_ids, body.dict())
                       .with_entities(Lead.id).all()]
        if not src_ids:
            return {"copied": 0}
        name = (body.list_name or "").strip() or f"From {slug}"
        nl = LeadList(name=name, variable_set=body.target)
        s.add(nl)
        s.commit()
        nlid = nl.id
        n = 0
        for i in range(0, len(src_ids), 1000):
            chunk = src_ids[i:i + 1000]
            for ld in s.query(Lead).filter(Lead.id.in_(chunk)).all():
                s.add(Lead(list_id=nlid, first_name=ld.first_name, last_name=ld.last_name,
                           title=ld.title, company=ld.company, website=ld.website, email=ld.email,
                           data=ld.data, industry=ld.industry, esp=ld.esp, employees=ld.employees,
                           country=ld.country, state=ld.state, seniority=ld.seniority,
                           title_status=ld.title_status, verify=ld.verify, email_status=ld.email_status))
                n += 1
            s.commit()
        return {"copied": n, "list_id": nlid, "list_name": name, "target": body.target}
    finally:
        s.close()


class RunAllBody(BaseModel):
    kind: str                      # classify | esp | titlecheck
    workers: Optional[int] = None
    skip_done: bool = True
    limit: Optional[int] = None    # process at most this many leads this run (chunking)
    mode: str = "fast"             # classify only: fast (no scrape, batched) | deep (reads site)


@app.post("/api/workspaces/{slug}/run-all")
def run_all(slug: str, body: RunAllBody):
    """Run Classify / ESP / Title-check across EVERY lead in the workspace (all
    lists), skipping leads already done. One click for the whole master database."""
    s = SessionLocal()
    try:
        list_ids = _ws_list_ids(s, slug)
        if not list_ids:
            return {"job_id": None, "count": 0}
        q = s.query(Lead.id).filter(Lead.list_id.in_(list_ids))
        kind = body.kind
        if kind == "classify":
            if body.skip_done:
                q = q.filter((Lead.industry == None) | (Lead.industry == ""))  # noqa: E711
        elif kind == "esp":
            if body.skip_done:
                q = q.filter((Lead.esp == None) | (Lead.esp == ""))  # noqa: E711
        elif kind == "titlecheck":
            if body.skip_done:
                q = q.filter((Lead.title_status == None) | (Lead.title_status == ""))  # noqa: E711
        else:
            raise HTTPException(400, "kind must be classify, esp, or titlecheck")
        # Process in a bounded chunk this run (user-chosen). Ordered so repeated
        # runs march through the remaining undone leads instead of overlapping.
        q = q.order_by(Lead.id)
        if body.limit and body.limit > 0:
            q = q.limit(int(body.limit))
        target_ids = [lid for (lid,) in q.all()]
        job = Job(list_id=list_ids[0], kind=kind, status="queued" if target_ids else "done",
                  total=len(target_ids), variable_set=slug, summary={})
        s.add(job)
        s.commit()
        job_id = job.id
    finally:
        s.close()
    if target_ids:
        if kind == "classify":
            runner = _run_classify_job if (body.mode or "fast").lower() == "deep" else _run_classify_fast_job
            threading.Thread(target=runner, args=(job_id, target_ids, body.workers), daemon=True).start()
        elif kind == "esp":
            threading.Thread(target=_run_esp_job, args=(job_id, target_ids, body.workers), daemon=True).start()
        else:
            threading.Thread(target=_run_title_job, args=(job_id, target_ids), daemon=True).start()
    return {"job_id": job_id, "count": len(target_ids)}


@app.post("/api/workspaces/{slug}/dedupe")
def dedupe(slug: str):
    """Remove duplicate leads by email across the whole workspace. Keeps ONE per
    email, preferring a classified lead (has an industry); deletes the rest.
    Leads with no email are left untouched."""
    s = SessionLocal()
    try:
        list_ids = _ws_list_ids(s, slug)
        if not list_ids:
            return {"removed": 0}
        rows = (s.query(Lead.id, Lead.email, Lead.industry)
                .filter(Lead.list_id.in_(list_ids),
                        Lead.email != None, Lead.email != "").all())  # noqa: E711
        groups = {}
        for lid, email, industry in rows:
            key = (email or "").strip().lower()
            if not key:
                continue
            classified = bool(industry and str(industry).strip())
            groups.setdefault(key, []).append((lid, classified))
        to_delete = []
        for items in groups.values():
            if len(items) < 2:
                continue
            # keeper: classified first, then lowest id; delete the rest
            items.sort(key=lambda t: (0 if t[1] else 1, t[0]))
            to_delete.extend(lid for lid, _ in items[1:])
        removed = 0
        for i in range(0, len(to_delete), 1000):
            chunk = to_delete[i:i + 1000]
            removed += s.query(Lead).filter(Lead.id.in_(chunk)).delete(synchronize_session=False)
        s.commit()
        return {"removed": removed}
    finally:
        s.close()


@app.get("/api/workspaces/{slug}/active-job")
def active_job(slug: str):
    """The latest still-running database-wide job for this workspace, so the
    Database view can reconnect to it after a reload (the job keeps running on the
    server regardless of the browser)."""
    s = SessionLocal()
    try:
        list_ids = _ws_list_ids(s, slug)
        if not list_ids:
            return {"job": None}
        job = (s.query(Job)
               .filter(Job.list_id.in_(list_ids),
                       Job.kind.in_(["classify", "esp", "titlecheck"]),
                       Job.status.in_(["queued", "running"]))
               .order_by(Job.id.desc()).first())
        if not job:
            return {"job": None}
        return {"job": {"id": job.id, "kind": job.kind, "status": job.status,
                        "done": job.done, "total": job.total}}
    finally:
        s.close()


# ------------------------- auth + static frontend -------------------------

class LoginBody(BaseModel):
    password: str = ""
    user: str = ""


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))


@app.post("/api/login")
def do_login(body: LoginBody):
    pw = os.getenv("DASH_PASSWORD")
    if not pw:
        return {"ok": True}  # auth disabled (local dev)
    user = os.getenv("DASH_USER", "admin")
    user_ok = (not body.user) or hmac.compare_digest(body.user, user)
    if user_ok and hmac.compare_digest(body.password or "", pw):
        resp = JSONResponse({"ok": True})
        resp.set_cookie(SESSION_COOKIE, _session_token(), httponly=True,
                        samesite="lax", max_age=60 * 60 * 24 * 30, path="/")
        return resp
    return JSONResponse({"ok": False, "detail": "Wrong password"}, status_code=401)


@app.post("/api/logout")
def do_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")
