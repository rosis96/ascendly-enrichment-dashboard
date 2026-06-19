"""Ascendly Lead Enrichment Dashboard — FastAPI backend.

Run from this directory:  uvicorn app:app --reload --port 8000
Then open http://localhost:8000
"""
import os
import csv
import io
import time
import base64
import threading
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import SessionLocal, init_db
from models import LeadList, Lead, Job, CustomVariable, HiddenVariable, Workspace
import engine_adapter as ea
from integrations import reoon


def _custom_specs(s, variable_set):
    """List of engine-format specs for a set's custom variables."""
    rows = s.query(CustomVariable).filter_by(variable_set=variable_set).order_by(CustomVariable.id).all()
    return [r.spec for r in rows]


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

init_db()

app = FastAPI(title="Ascendly Lead Enrichment Dashboard")


@app.middleware("http")
async def basic_auth(request, call_next):
    """Optional HTTP Basic auth. Active only when DASH_PASSWORD is set (e.g. in
    production). No effect locally if the env var is unset."""
    pw = os.getenv("DASH_PASSWORD")
    if pw:
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                user, pwd = base64.b64decode(auth[6:]).decode().split(":", 1)
                ok = (user == os.getenv("DASH_USER", "admin") and pwd == pw)
            except Exception:
                ok = False
        if not ok:
            return Response("Authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="dashboard"'})
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


def _run_job(job_id, target_ids):
    s = SessionLocal()
    try:
        job = s.get(Job, job_id)
        job.status = "running"
        s.commit()
        leads = s.query(Lead).filter(Lead.id.in_(target_ids)).all() if target_ids else []
        custom_specs = _custom_specs(s, job.variable_set)
        base = _base_of(s, job.variable_set)
        for ld in leads:
            if job_id in CANCEL:
                break
            ld.status = "running"
            s.commit()
            res, cost = ea.enrich(
                {"title": ld.title, "company": ld.company, "website": ld.website},
                base, job.enrichments, custom_specs,
            )
            ld.result = res
            ld.status = "skipped" if res.get("ICPReview") == "Non-ICP" else "done"
            job.done += 1
            job.cost = round((job.cost or 0) + cost, 4)
            if res.get("_title_gate") == "rejected":
                job.rejected += 1
            elif res.get("ICPReview") == "Non-ICP":
                job.nonicp += 1
            else:
                job.icp += 1
            s.commit()
            time.sleep(0.04)  # keeps the demo visibly progressing
        job = s.get(Job, job_id)
        job.status = "cancelled" if job_id in CANCEL else "done"
        s.commit()
    except Exception:
        job = s.get(Job, job_id)
        if job:
            job.status = "error"
            s.commit()
    finally:
        CANCEL.discard(job_id)
        s.close()


def _run_verify_job(job_id, target_ids, mode):
    s = SessionLocal()
    try:
        job = s.get(Job, job_id)
        job.status = "running"
        s.commit()
        leads = s.query(Lead).filter(Lead.id.in_(target_ids)).all() if target_ids else []
        summary = {"valid": 0, "risky": 0, "invalid": 0}
        for ld in leads:
            if job_id in CANCEL:
                break
            res = reoon.verify_one(ld.email, mode)
            b, label = reoon.bucket(res)
            ld.verify = res
            ld.email_status = label
            summary[b] += 1
            job.done += 1
            job.cost = round((job.cost or 0) + 1, 0)  # ~1 Reoon credit per email
            job.summary = dict(summary)
            s.commit()
            time.sleep(0.04)
        job = s.get(Job, job_id)
        job.status = "cancelled" if job_id in CANCEL else "done"
        s.commit()
    except Exception:
        job = s.get(Job, job_id)
        if job:
            job.status = "error"
            s.commit()
    finally:
        CANCEL.discard(job_id)
        s.close()


# ------------------------- API -------------------------

class CreateList(BaseModel):
    name: str
    variable_set: str = "ascendly_lean"


class RunBody(BaseModel):
    enrichments: list[str] = []
    lead_ids: list[int] = []     # run only these leads (selection)
    limit: Optional[int] = None  # otherwise run only the first N (test cap)
    only_safe: bool = True       # only enrich leads Reoon marked safe/deliverable


class VerifyBody(BaseModel):
    lead_ids: list[int] = []
    limit: Optional[int] = None
    mode: str = "power"          # reoon mode: power (accurate) or quick (fast)


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
    purpose: str = ""
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
        min_words=body.min_words, max_words=body.max_words, purpose=body.purpose,
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
        w = Workspace(slug=slug, name=body.name.strip() or "New workspace",
                      base_set=body.base_set or "", profile=profile)
        s.add(w)
        s.commit()
        return {"key": w.slug, "name": w.name, "base_set": w.base_set, "kind": "workspace"}
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
    for v in spec.get("variables", []):
        v["hidden"] = v.get("name") in hidden
    spec["variables"] = spec.get("variables", []) + custom_cards
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
    selectable = [k for k in all_keys if k not in ea.ALWAYS_KEYS and k not in hidden]
    return {
        "always": ea.ALWAYS_KEYS,
        "selectable": selectable + custom_names,
        "all": [k for k in all_keys if k not in hidden] + custom_names,
        "labels": labels,
    }


@app.get("/api/lists")
def get_lists():
    s = SessionLocal()
    try:
        out = []
        for l in s.query(LeadList).order_by(LeadList.created_at.desc()).all():
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
        if body.only_safe:
            targets = [ld for ld in candidates if _lead_safe(ld)]
        else:
            targets = candidates
        skipped_unsafe = len(candidates) - len(targets)
        # Reset only the leads we're about to run; earlier results are preserved.
        for ld in targets:
            ld.status = "pending"
            ld.result = {}
        job = Job(list_id=list_id, variable_set=l.variable_set, enrichments=body.enrichments,
                  status="queued" if targets else "done", total=len(targets))
        s.add(job)
        s.commit()
        job_id = job.id
        target_ids = [ld.id for ld in targets]
    finally:
        s.close()
    if target_ids:
        threading.Thread(target=_run_job, args=(job_id, target_ids), daemon=True).start()
    return {"job_id": job_id, "count": len(target_ids), "skipped_unsafe": skipped_unsafe}


@app.post("/api/lists/{list_id}/verify")
def verify_list(list_id: int, body: VerifyBody):
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        q = s.query(Lead).filter_by(list_id=list_id).order_by(Lead.id)
        if body.lead_ids:
            targets = q.filter(Lead.id.in_(body.lead_ids)).all()
        elif body.limit and body.limit > 0:
            targets = q.limit(body.limit).all()
        else:
            targets = q.all()
        for ld in targets:
            ld.verify = {}
            ld.email_status = ""
        job = Job(list_id=list_id, kind="verify", status="queued", total=len(targets),
                  variable_set=l.variable_set, summary={})
        s.add(job)
        s.commit()
        job_id = job.id
        target_ids = [ld.id for ld in targets]
    finally:
        s.close()
    threading.Thread(target=_run_verify_job, args=(job_id, target_ids, body.mode), daemon=True).start()
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


@app.get("/api/lists/{list_id}/export")
def export_list(list_id: int):
    s = SessionLocal()
    try:
        l = s.get(LeadList, list_id)
        if not l:
            raise HTTPException(404, "List not found")
        leads = s.query(Lead).filter_by(list_id=list_id).all()
        hidden = _hidden_names(s, l.variable_set)
        out_keys = [k for k in ea.output_keys_for(_base_of(s, l.variable_set)) if k not in hidden] + \
            [c.get("name") for c in _custom_specs(s, l.variable_set)]
        # raw columns first (union across rows), then verification, then enrichment
        raw_cols = []
        for ld in leads:
            for k in (ld.data or {}).keys():
                if k not in raw_cols:
                    raw_cols.append(k)
        header = raw_cols + ["email_status", "email_safe_to_send"] + out_keys
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        for ld in leads:
            row = [(ld.data or {}).get(c, "") for c in raw_cols]
            row.append(ld.email_status or "")
            row.append((ld.verify or {}).get("is_safe_to_send", ""))
            row += [(ld.result or {}).get(k, "") for k in out_keys]
            w.writerow(row)
        fname = (l.name or "list").replace(" ", "_").replace("/", "-") + ".csv"
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    finally:
        s.close()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    CANCEL.add(job_id)
    s = SessionLocal()
    try:
        job = s.get(Job, job_id)
        if job and job.status in ("queued", "running"):
            job.status = "cancelling"
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


# ------------------------- static frontend -------------------------

@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")
