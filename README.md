# PayToProvision

**Turn any GitHub repo into a paid live demo.** A repo owner lists a repo and sets an hourly price; AI works
out how to deploy it; a buyer pays through an AI checkout agent with PayPal (sandbox); the app goes live on
Render with a countdown, self-heals failed builds with AI, refunds automatically if it can't, and is deleted
when time is up. The owner is paid out via PayPal Payouts.

> Built for the PayPal AI Hackathon. PayPal is used in **sandbox mode only**; no real money moves.

_Status: Phase 1 (scaffold). The full README, architecture diagram and judge instructions come in Phase 8._

## Stack

Python 3.12 · FastAPI · Jinja2 + Tailwind + HTMX · SQLAlchemy + Alembic on Neon Postgres · Render (app +
previews via the Render REST API) · Nebius Token Factory (NVIDIA Nemotron) · PayPal Orders/Refunds/Payouts/Webhooks + Agent Toolkit.

## Run locally

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements-dev.txt
cp .env.example .env            # fill in what you have; empty values use defaults
alembic upgrade head            # SQLite (ptp-dev.db) unless DATABASE_URL is set
uvicorn app.main:app --reload
```

Open http://localhost:8000. Health check: http://localhost:8000/healthz.

```bash
python -m scripts.check_env     # verifies each credential you've set, never prints secrets
python -m scripts.render_smoke  # creates a free Render service, waits for live, hits it, deletes it
pytest                          # unit tests (Render API mocked)
ruff check . && ruff format --check .
```

> **Windows note:** if `import sqlalchemy` fails with *"An Application Control policy has blocked this file"*,
> Smart App Control is blocking SQLAlchemy's compiled speedups. Install the pure-Python build instead:
> `set DISABLE_SQLALCHEMY_CEXT=1 && pip install --force-reinstall --no-deps --no-binary sqlalchemy sqlalchemy==2.1.8`

## Deploy to Render

The app itself is one free Render web service described in [`render.yaml`](render.yaml):
Render Dashboard → **New → Blueprint** → pick this repo → fill in the `sync: false` values
(`DATABASE_URL`, `RENDER_API_KEY`, …). Migrations run on boot. Health check: `/healthz`.

## Project layout

```
app/
  main.py            FastAPI app (pages + /healthz)
  config.py          settings from env (secrets as SecretStr)
  db.py              engine, session, UTC datetime + JSONB column types
  models.py          Listing, PreviewSession (+ state machine), TimelineEvent, WebhookEvent, Payout
  render_client.py   typed Render API client with retries/backoff
  templates/         Jinja2 + Tailwind pages
alembic/             migrations
scripts/             check_env.py, render_smoke.py
tests/               pytest
```
