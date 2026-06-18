# Ascendly Lead Enrichment Dashboard

A Clay-style dashboard for the outbound personalization pipeline. **Fully separate
from the existing local pipeline** — it has its own duplicated copy of the engine
under `engine/` and never touches your running `run.py`, `cache/`, or data.

## What works today (v0)

- Named, filterable lists (import a CSV, give the batch a name)
- Column-mapped CSV import (First/Last name, Title, Company, Website, Email)
- **Reoon email verification** (Verify button) with valid / risky / invalid pills
- Choose which enrichment variables to output (the rest stay in the background)
- One-click **Run** (enrich) and **Verify**, both as background jobs with live progress
- **Safeguards against credit burn:** row selection, "test first N" cap (default 10),
  a confirm prompt over 50 leads, and a hard **Stop** that cancels mid-run
- Title gate → strict ICP → personalization, shown as status pills in the grid
- **Export CSV** (raw columns + verification + enrichment results)
- Collapsible / drag-to-resize sidebar

Enrichment runs in **demo mode** (deterministic, no OpenAI key, no cost, no scraping).
Reoon verification runs **real** when `REOON_API_KEY` is set, otherwise a demo
verifier is used. Instantly/Bison campaign push is intentionally deferred — export
the CSV and upload it wherever you like for now.

## Reoon

Set your key before starting the server to use the real verifier:

```bash
export REOON_API_KEY="your_key_here"
```

Without it, the credits chip shows "Reoon: demo" and verification is simulated.
Verify uses power mode (accurate) by default. The same selection / limit / Stop
safeguards apply, so a verify run can't blow through your Reoon credits either.

## Run it

```bash
cd enrichment-dashboard/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Open http://localhost:8000 → Import → choose `sample/leads_sample.csv` → name it →
Create list → pick enrichments → Run.

## Layout

```
enrichment-dashboard/
  backend/      FastAPI app, SQLite models, engine adapter, integration stubs
  engine/       INDEPENDENT copy of the pipeline (run.py + profiles + variable sets)
  frontend/     Clay-style dashboard (HTML/CSS/JS)
  sample/       sample leads CSV
  data/         SQLite db + uploads (gitignored)
```

## Switching demo → real (later)

`engine_adapter.enrich()` switches on `ENRICH_MODE`. When the integration docs and
keys are in, `_real_enrich()` will import `engine/run.py`'s `process_row` and the
provider modules under `backend/integrations/` will handle Reoon verify + Instantly/
Bison campaign push. Until then, leave `ENRICH_MODE=demo`.
