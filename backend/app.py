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

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, Response, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import SessionLocal, init_db
from models import LeadList, Lead, Job, CustomVariable, HiddenVariable, Workspace, EnrichRule
import engine_adapter as ea
from integrations import reoon


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


def _profile_for(s, key, base):
    """Client profile for enrichment: a workspace's editable profile, or the
    engine client's full profile file."""
    ws = s.query(Workspace).filter_by(slug=key).first()
    if ws:
        prof = dict(ws.profile or {})
        # The role-lock needs the active client's name. Workspace profiles don't
        # store one, so surface the workspace name as the client/sender identity.
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


def _parse_csv(content_bytes):
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
        out.append({
            "first_name": _pick(hl, r, "first name", "firstname", "first_name"),
            "last_name": _pick(hl, r, "last name", "lastname", "last_name"),
            "title": _pick(hl, r, "title", "jobtitle", "job title"),
            "company": _pick(hl, r, "company", "companyname", "company name"),
            "website": _pick(hl, r, "website", "url", "domain", "company website"),
            "email": _pick(hl, r, "email"),
            "data": rowmap,
        })
    return out


ENRICH_WORKERS = int(os.getenv("ENRICH_WORKERS", "10"))
VERIFY_WORKERS = int(os.getenv("VERIFY_WORKERS", "10"))
# Upper bound on user-chosen concurrency. Classification is cheap and benefits from
# high parallelism, so the ceiling is 100; for enrichment, keeping it ~10 is wiser
# (rate limits/quality). Override via env if needed.
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "100"))


def _clamp_workers(n, default):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, MAX_WORKERS))


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
    """Run worker_fn(lead_id) across a thread pool. Each worker uses its own DB
    session and returns a dict of counter increments. Cancel-aware and resumable."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _write_job(job_id, 0, {}, status="running")
    agg, done = {}, 0

    def task(lid):
        if job_id in CANCEL:
            return None
        try:
            return worker_fn(lid)
        except Exception:
            return {"_error": 1}

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futures = [ex.submit(task, lid) for lid in target_ids]
            for fut in as_completed(futures):
                r = fut.result()
                done += 1
                if r:
                    for k, v in r.items():
                        agg[k] = agg.get(k, 0) + v
                _write_job(job_id, done, agg)  # live progress on every lead
        _write_job(job_id, done, agg, status="cancelled" if job_id in CANCEL else "done")
    except Exception:
        _write_job(job_id, done, agg, status="error")
    finally:
        CANCEL.discard(job_id)


def _enrich_one(lead_id, base, enrichments, custom_specs, profile, variable_set=None):
    s = SessionLocal()
    try:
        ld = s.get(Lead, lead_id)
        if not ld:
            return None
        ld.status = "running"
        s.commit()
        extra_rules = _rules_list(s, variable_set) if variable_set else []
        res, cost = ea.enrich(_lead_row(ld), base, enrichments, custom_specs, profile, extra_rules)
        if res.get("_status") == "error" or not res.get("ICPReview"):
            # website unreachable / unreadable -> no copy, just mark it
            ld.result = {"_status": "error", "_error": str(res.get("_error", "No website content"))[:200]}
            ld.status = "error"
            s.commit()
            return {"error": 1, "cost": cost}
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


def _pipeline_one(lead_id, mode, base, enrichments, custom_specs, profile, variable_set=None):
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
                extra_rules = _rules_list(s, variable_set) if variable_set else []
                r, cost = ea.enrich(_lead_row(ld), base, enrichments, custom_specs, profile, extra_rules)
                inc["cost"] = cost
                if r.get("_status") == "error" or not r.get("ICPReview"):
                    ld.result = {"_status": "error", "_error": str(r.get("_error", "No website content"))[:200]}
                    ld.status = "error"
                    s.commit()
                    inc["error"] = 1
                else:
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
        enrichments = job.enrichments
    finally:
        s.close()
    _process_concurrent(job_id, target_ids,
                        partial(_enrich_one, base=base, enrichments=enrichments,
                                custom_specs=custom_specs, profile=profile, variable_set=vset),
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
        enrichments = job.enrichments
    finally:
        s.close()
    _process_concurrent(job_id, target_ids,
                        partial(_pipeline_one, mode=mode, base=base, enrichments=enrichments,
                                custom_specs=custom_specs, profile=profile, variable_set=vset),
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
            merged.update({k: v for k, v in body.profile.items() if isinstance(v, str)})
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


@app.delete("/api/lists/{list_id}")
def delete_list(list_id: int):
    s = SessionLocal()
    try:
        # Bulk deletes (one SQL statement each) — fast even for thousands of leads
        # over a remote Postgres, instead of per-row ORM cascade.
        s.query(Job).filter_by(list_id=list_id).delete(synchronize_session=False)
        s.query(Lead).filter_by(list_id=list_id).delete(synchronize_session=False)
        s.query(LeadList).filter_by(id=list_id).delete(synchronize_session=False)
        s.commit()
        return {"ok": True}
    finally:
        s.close()


@app.post("/api/lists/{list_id}/upload")
async def upload(list_id: int, file: UploadFile = File(...)):
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        rows = _parse_csv(await file.read())
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
        leads = s.query(Lead).filter_by(list_id=list_id).all()
        job = (s.query(Job).filter_by(list_id=list_id)
               .order_by(Job.created_at.desc()).first())
        return {
            "list": {"id": l.id, "name": l.name, "variable_set": l.variable_set, "count": len(leads)},
            "leads": [{
                "id": ld.id, "first_name": ld.first_name, "last_name": ld.last_name,
                "title": ld.title, "company": ld.company, "website": ld.website,
                "email": ld.email, "status": ld.status, "result": ld.result or {},
                "verify": ld.verify or {}, "email_status": ld.email_status or "",
                "industry": ld.industry or "", "title_status": ld.title_status or "",
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
        leads = s.query(Lead).filter_by(list_id=list_id).all()
        groups = {}
        for ld in leads:
            groups.setdefault(ld.industry or "Unclassified", []).append(ld)
        created = []
        for ind, items in sorted(groups.items()):
            nl = LeadList(name=f"{l.name} · {ind}", variable_set=l.variable_set)
            s.add(nl)
            s.flush()
            for ld in items:
                s.add(Lead(list_id=nl.id, first_name=ld.first_name, last_name=ld.last_name,
                           title=ld.title, company=ld.company, website=ld.website, email=ld.email,
                           data=ld.data, result=ld.result, verify=ld.verify,
                           email_status=ld.email_status, industry=ld.industry, status=ld.status))
            created.append({"industry": ind, "count": len(items)})
        s.commit()
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
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        q = s.query(Lead).filter_by(list_id=list_id)
        if wanted:  # export only the selected / filtered leads
            q = q.filter(Lead.id.in_(wanted))
        leads = q.all()
        hidden = _hidden_names(s, l.variable_set)
        custom_names = [c.get("name") for c in _custom_specs(s, l.variable_set)]
        cset = set(custom_names)
        out_keys = [k for k in ea.output_keys_for(_base_of(s, l.variable_set))
                    if k not in hidden and k not in cset] + custom_names
        # raw columns first (union across rows), then verification, then enrichment
        raw_cols = []
        for ld in leads:
            for k in (ld.data or {}).keys():
                if k not in raw_cols:
                    raw_cols.append(k)
        header = raw_cols + ["industry", "email_status", "email_safe_to_send"] + out_keys
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        for ld in leads:
            row = [(ld.data or {}).get(c, "") for c in raw_cols]
            row.append(ld.industry or "")
            row.append(ld.email_status or "")
            row.append((ld.verify or {}).get("is_safe_to_send", ""))
            row += [(ld.result or {}).get(k, "") for k in out_keys]
            w.writerow(row)
        fname = (l.name or "list").replace(" ", "_").replace("/", "-") + ".csv"
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    finally:
        s.close()


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
