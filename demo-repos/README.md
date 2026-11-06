# Demo repos

Four tiny apps used to demo PayToProvision. Each folder is pushed as its **own public GitHub repo**
and added to `ALLOWED_REPOS`. They exercise every path of the product.

Published: [ptp-demo-flask](https://github.com/jaydeep-99o/ptp-demo-flask) ·
[ptp-demo-express](https://github.com/jaydeep-99o/ptp-demo-express) ·
[ptp-demo-heal-python](https://github.com/jaydeep-99o/ptp-demo-heal-python) ·
[ptp-demo-never-builds](https://github.com/jaydeep-99o/ptp-demo-never-builds)

| Folder → repo | What it is | What should happen |
|---|---|---|
| `ptp-demo-flask` | Standup board (Flask + gunicorn) | Analyzer gets it right first time → deploys → live. |
| `ptp-demo-express` | Quick poll (Express) | Analyzer picks `npm install` / `npm start`; app reads `process.env.PORT` → live. |
| `ptp-demo-heal-python` | "Weather Now" (Flask) | Two AI moments. **Analyzer:** `app.py` hardcodes port 5000, so it chooses `gunicorn app:app --bind 0.0.0.0:$PORT`. **Healer:** the build succeeds but boot crashes with `ModuleNotFoundError: No module named 'requests'` (imported by `forecast.py`, missing from `requirements.txt`). The healer reads the logs, adds `requests` to the build command and redeploys → live. |
| `ptp-demo-never-builds` | "Invoice Studio" (Flask) | Depends on a private Git package that doesn't exist, and imports it at startup. Build fails; the healer can't fix it with config alone → after 2 attempts → automatic PayPal refund. |

> Don't add hints about the bugs to the demo repos' own READMEs: the analyzer reads them, and the
> point of `ptp-demo-heal-python` is that the missing dependency only shows up at runtime.

## Publish them

From this folder, for each app (needs the GitHub CLI, logged in):

```bash
cd ptp-demo-flask && git init -b main && git add . && git commit -m "Initial commit" \
  && gh repo create <your-user>/ptp-demo-flask --public --source . --push && cd ..
```

Then set on Render (and in `.env` locally):

```
ALLOWED_REPOS=<your-user>/ptp-demo-flask,<your-user>/ptp-demo-express,<your-user>/ptp-demo-heal-python,<your-user>/ptp-demo-never-builds
```

## Demo script

1. List `ptp-demo-flask` → show the AI analysis → publish → buy 1 hour → live link.
2. List `ptp-demo-heal-python` → point out the AI changed the start command to bind `$PORT` →
   buy → build fails at boot → watch the AI fix appear on the timeline → live.
3. List `ptp-demo-never-builds` → buy → AI tries twice → refund lands in the sandbox buyer account.
