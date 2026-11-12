"""Continuous deployment step: deploy one commit to Render, wait for it, then smoke-test it.

Run by GitHub Actions after CI passes (see .github/workflows/ci.yml):

    python -m scripts.deploy --service-id srv-... --commit <sha> --url https://paytoprovision.onrender.com

Exits non-zero (failing the workflow) if the deploy fails, or if the site doesn't come back
healthy on the new commit. Needs RENDER_API_KEY and RENDER_OWNER_ID in the environment.
"""

import argparse
import os
import sys
import time

import httpx

from app.render_client import RenderClient, RenderDeploy

HEALTH_TIMEOUT_SECONDS = 300


def summary(line: str) -> None:
    """Print, and add to the GitHub Actions job summary when running there."""
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-id", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    started = time.monotonic()

    with RenderClient.from_settings() as render:
        deploy = render.trigger_deploy(args.service_id, commit_id=args.commit)
        summary(f"### Deploying `{args.commit[:7]}` to Render\n\nDeploy `{deploy.id}`")

        def on_status(d: RenderDeploy) -> None:
            print(f"  +{time.monotonic() - started:4.0f}s  {d.status}")

        deploy = render.wait_for_deploy(args.service_id, deploy.id, timeout=args.timeout, on_status=on_status)
        if not deploy.succeeded:
            summary(f"**Deploy failed: {deploy.status}**\n\n```")
            for log_type in ("build", "app"):
                for line in render.get_logs(args.service_id, log_type=log_type, since=deploy.created_at, max_lines=40):
                    summary(line.message)
            summary("```")
            return 1

    # Smoke test: the site must answer healthy, on the commit we just deployed.
    base = args.url.rstrip("/")
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    health: dict = {}
    while time.monotonic() < deadline:
        try:
            health = httpx.get(f"{base}/healthz", timeout=60).json()
            if health.get("status") == "ok" and health.get("commit", "").startswith(args.commit[:7]):
                break
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(10)
    else:
        summary(f"**Smoke test failed:** /healthz never reported `{args.commit[:7]}` (last: {health})")
        return 1

    pages = {
        path: httpx.get(f"{base}{path}", timeout=60).status_code for path in ("/", "/owner/new", "/owner/dashboard")
    }
    if any(code != 200 for code in pages.values()):
        summary(f"**Smoke test failed:** {pages}")
        return 1
    summary(f"\nLive in {time.monotonic() - started:.0f}s · /healthz ok on `{args.commit[:7]}` · pages {pages}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
