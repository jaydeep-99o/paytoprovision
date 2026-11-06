// Quick Poll: one question, live results. Votes live in memory.
const express = require("express");

const app = express();
app.use(express.urlencoded({ extended: false }));

const poll = {
  question: "Which feature should we build next?",
  options: { "Dark mode": 4, "Mobile app": 7, "Slack integration": 5, "CSV export": 2 },
};

const escape = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function page() {
  const total = Object.values(poll.options).reduce((a, b) => a + b, 0);
  const rows = Object.entries(poll.options)
    .map(([name, votes]) => {
      const pct = total ? Math.round((votes / total) * 100) : 0;
      return `<form method="post" action="/vote" class="opt">
        <input type="hidden" name="option" value="${escape(name)}">
        <button>${escape(name)}</button>
        <div class="bar"><span style="width:${pct}%"></span></div>
        <span class="pct">${pct}% · ${votes}</span>
      </form>`;
    })
    .join("");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1"><title>Quick Poll</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; }
    main { max-width: 640px; margin: 0 auto; padding: 40px 16px; }
    .opt { display: grid; grid-template-columns: 170px 1fr 80px; gap: 12px; align-items: center; margin: 14px 0; }
    button { background: #6366f1; color: #fff; border: 0; padding: 10px; border-radius: 10px; font-weight: 600; cursor: pointer; }
    .bar { height: 12px; background: #1e293b; border-radius: 99px; overflow: hidden; }
    .bar span { display: block; height: 100%; background: #22c55e; }
    .pct { color: #94a3b8; font-size: 14px; } p { color: #94a3b8; }
  </style></head><body><main>
  <h1>${escape(poll.question)}</h1><p>${total} votes so far. Click an option to vote.</p>${rows}
  </main></body></html>`;
}

app.get("/", (req, res) => res.send(page()));

app.post("/vote", (req, res) => {
  if (Object.hasOwn(poll.options, req.body.option)) poll.options[req.body.option] += 1;
  res.redirect("/");
});

app.get("/health", (req, res) => res.json({ status: "ok" }));

const port = process.env.PORT || 3000;
app.listen(port, () => console.log(`Quick Poll listening on port ${port}`));
