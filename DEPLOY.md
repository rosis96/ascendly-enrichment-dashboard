# Deploying the dashboard (GitHub → Railway → custom domain)

> Push **only this `enrichment-dashboard` folder**. Never push the parent folder —
> it contains your `.env` (OpenAI key), `leads.csv` (PII), and the cache.

The repo already includes a `Dockerfile`, so Railway builds and runs it with no
extra config. The app binds to `$PORT` automatically.

---

## 1. Git

From inside the `enrichment-dashboard` folder:

```bash
cd "/Users/rosissitoula/outbound_personalization_v3 2/enrichment-dashboard"
git init
git add .
git commit -m "Lead enrichment dashboard"
```

Sanity check that no secrets/venv/db got staged:

```bash
git status           # should NOT list .venv/, data/, *.db, or any .env
```

Create a GitHub repo and push (pick one):

**With GitHub CLI:**
```bash
gh repo create ascendly-enrichment-dashboard --private --source=. --remote=origin --push
```

**Or manually:** create an empty private repo on github.com, then:
```bash
git remote add origin https://github.com/<you>/ascendly-enrichment-dashboard.git
git branch -M main
git push -u origin main
```

---

## 2. Railway

1. Go to railway.app → sign in with GitHub.
2. **New Project → Deploy from GitHub repo →** pick the repo. Authorize Railway to
   access it if asked.
3. Railway detects the `Dockerfile` and builds. Wait for the first deploy to finish.
4. **Set environment variables** (project → your service → **Variables**):
   - `DASH_PASSWORD` = a strong password (REQUIRED — locks the public URL).
   - `DASH_USER` = `admin` (optional; defaults to `admin`).
   - `REOON_API_KEY` = your Reoon key (optional; only if you want real verification
     live. Leave unset to stay in demo).
   - Leave `ENRICH_MODE` unset (stays demo until the real engine is wired).
5. **Get a URL:** service → **Settings → Networking → Generate Domain**. You'll get
   a `*.up.railway.app` URL. Open it — your browser will prompt for the username
   (`admin`) and `DASH_PASSWORD`.

### Persisting data (recommended)

By default the SQLite DB lives on the container's disk and **resets on each deploy**.
To keep your lists/leads between deploys:

1. Service → **Settings → Volumes → New Volume**, mount path: `/data`.
2. Add a variable: `DASHBOARD_DB_URL` = `sqlite:////data/app.db`
   (note the four slashes — three for `sqlite://` + the leading `/` of `/data`).
3. Redeploy.

---

## 3. Custom domain

1. Service → **Settings → Networking → Custom Domain → + Custom Domain**.
2. Enter the subdomain you want, e.g. `app.ascendly.one`
   (a subdomain is easiest; root domains need an ALIAS/ANAME record).
3. Railway shows a **CNAME target** (something like `xxxx.up.railway.app`).
4. In your DNS provider (where `ascendly.one` is managed), add:
   - Type: `CNAME`
   - Name/Host: `app`
   - Value/Target: the Railway-provided target
   - (Turn the proxy OFF if your DNS is Cloudflare — set it to "DNS only" — until
     SSL is verified.)
5. Back in Railway, wait for the domain to show **Active / SSL issued** (usually a
   few minutes, sometimes longer for DNS propagation).
6. Visit `https://app.ascendly.one` — you'll get the password prompt, then the app.

---

## Notes

- Redeploys are automatic on every `git push` to `main`.
- The app runs `ENRICH_MODE=demo` by default — no OpenAI key needed, no spend.
  Real enrichment + the Reoon-real path are wired but off until you flip the env.
- Selection / "test first N" / Stop safeguards apply in production too, so verify
  runs can't quietly drain Reoon credits.
