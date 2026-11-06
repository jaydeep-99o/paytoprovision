"""Standup Board: post what you did, what's next and what's blocking you."""

import os
from datetime import UTC, datetime

from flask import Flask, redirect, render_template_string, request, url_for

app = Flask(__name__)
UPDATES = [
    {"name": "Ada", "done": "Shipped the login page", "next": "Password reset", "blocked": "", "at": "09:12"},
    {
        "name": "Lin",
        "done": "Fixed flaky CI",
        "next": "Upgrade Postgres",
        "blocked": "Waiting on DB access",
        "at": "09:20",
    },
]

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Standup Board</title>
<style>
  body { font-family: system-ui, sans-serif; background: #f5f6fa; color: #1c1e29; margin: 0; }
  main { max-width: 720px; margin: 0 auto; padding: 32px 16px; }
  h1 { margin: 0 0 4px; } .muted { color: #6b7080; }
  form, .card { background: #fff; border-radius: 14px; padding: 16px; box-shadow: 0 1px 3px #0001; margin-top: 16px; }
  input { width: 100%; box-sizing: border-box; padding: 9px 11px; margin: 4px 0 10px; border: 1px solid #d8dbe5; border-radius: 9px; }
  button { background: #4f46e5; color: #fff; border: 0; padding: 10px 16px; border-radius: 9px; font-weight: 600; }
  .blocked { color: #b42318; } .row { display: flex; justify-content: space-between; }
</style></head>
<body><main>
  <h1>Standup Board</h1><p class="muted">Async standups for small teams. {{ updates|length }} updates today.</p>
  <form method="post" action="{{ url_for('post_update') }}">
    <input name="name" placeholder="Your name" required maxlength="40">
    <input name="done" placeholder="What did you do yesterday?" required maxlength="140">
    <input name="next" placeholder="What's next today?" required maxlength="140">
    <input name="blocked" placeholder="Anything blocking you? (optional)" maxlength="140">
    <button>Post update</button>
  </form>
  {% for u in updates|reverse %}
  <div class="card">
    <div class="row"><strong>{{ u.name }}</strong><span class="muted">{{ u.at }} UTC</span></div>
    <p>✅ {{ u.done }}<br>➡️ {{ u.next }}{% if u.blocked %}<br><span class="blocked">⛔ {{ u.blocked }}</span>{% endif %}</p>
  </div>
  {% endfor %}
</main></body></html>"""


@app.get("/")
def index():
    return render_template_string(PAGE, updates=UPDATES)


@app.post("/updates")
def post_update():
    form = {k: request.form.get(k, "").strip()[:140] for k in ("name", "done", "next", "blocked")}
    if form["name"] and form["done"] and form["next"]:
        UPDATES.append({**form, "at": datetime.now(UTC).strftime("%H:%M")})
    return redirect(url_for("index"))


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
