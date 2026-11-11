# Demo video script (target 2:45)

Record at 1080p, browser zoom 110%. Before recording:
- Main app awake (open it once) and, ideally, on the Starter plan (no cold start).
- Publish 3 listings from your browser: `ptp-demo-flask`, `ptp-demo-heal-python`, `ptp-demo-never-builds`.
- Start a **Weather Now** purchase ~3 minutes before you reach step 5, so the AI fix is visible on the timeline.
- PayPal sandbox buyer logged in, in the same browser profile.

| Time | Screen | Say |
|---|---|---|
| 0:00–0:15 | Home page | "Developers want people to try their apps, but hosting demos costs money and breaks. PayToProvision turns any GitHub repo into a paid live demo, powered by PayPal, AI and Render." |
| 0:15–0:45 | *List your repo* → paste `ptp-demo-flask` → review page | "An owner pastes a repo. AI on Nebius reads it and writes a deploy plan: runtime, commands, port, env vars, with a confidence score. It even fixed the hardcoded port so it binds Render's `$PORT`. Set a price, publish." |
| 0:45–1:25 | Listing page → agent chat | "Buyers talk to an AI checkout agent. *What does this app do?* … *I'd like 1 hour.* It quotes from the owner's price (it can't discount: the server computes the amount) and creates the order with the **PayPal Agent Toolkit**. I approve in the PayPal sandbox…" |
| 1:25–1:50 | Session page: steps → Live → open app | "Payment captured server-side, idempotently. The app is deployed on Render through its API: building, starting… live, with a private link and a countdown." |
| 1:50–2:25 | Weather Now session timeline | "This repo crashes on boot. The AI read the logs (`requests` is missing), changed the build command, redeployed, and it's live. Every change is on the timeline: old → new." |
| 2:25–2:40 | Invoice Studio session | "This one needs a private package. The AI says it can't be fixed without code changes, so PayPal refunds the buyer automatically." |
| 2:40–2:50 | Owner dashboard | "When time's up, the reaper deletes the environment and pays the owner 80% through PayPal Payouts. PayToProvision: paid live demos for any repo." |
