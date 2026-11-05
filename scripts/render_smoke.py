"""Render API smoke test: create -> poll -> hit URL -> read logs -> delete.

Creates a FREE web service from a public hello-world repo, waits for the deploy to
finish, requests its URL, prints the tail of the build log, then deletes the service
(always, unless --keep). Free instances cost nothing; the build uses a few of the
workspace's included pipeline minutes.

    python -m scripts.render_smoke
    python -m scripts.render_smoke --keep          # leave the service running
    python -m scripts.render_smoke --repo https://github.com/render-examples/express-hello-world \
        --runtime node --build "yarn" --start "node app.js"
"""

import argparse
import secrets
import sys
import time

import httpx

from app.config import get_settings
from app.render_client import RenderAPIError, RenderClient, RenderDeploy

DEFAULT_REPO = "https://github.com/render-examples/flask-hello-world"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--runtime", default="python", choices=["python", "node", "docker"])
    parser.add_argument("--build", default="pip install -r requirements.txt")
    parser.add_argument("--start", default="gunicorn app:app")
    parser.add_argument("--timeout", type=int, default=900, help="seconds to wait for the deploy")
    parser.add_argument("--keep", action="store_true", help="don't delete the service at the end")
    args = parser.parse_args()
    # Render logs contain emoji; the default Windows console encoding can't print them.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    settings = get_settings()
    name = f"{settings.preview_name_prefix}smoke-{secrets.token_hex(3)}"
    ok = False
    service_id = None

    with RenderClient.from_settings(settings) as render:
        try:
            owner_id = render.get_owner_id()
            print(f"[1/5] Workspace {owner_id}, region {render.region}")

            print(f"[2/5] Creating free web service '{name}' from {args.repo}")
            created = render.create_web_service(
                name=name,
                repo_url=args.repo,
                runtime=args.runtime,
                build_command=args.build,
                start_command=args.start,
            )
            service_id = created.service.id
            print(f"      service {service_id}  {created.service.dashboard_url}")
            print(f"      url     {created.service.url}")

            deploy_id = created.deploy_id or _wait_for_first_deploy(render, service_id)
            print(f"[3/5] Waiting for deploy {deploy_id} (timeout {args.timeout}s)")
            started = time.monotonic()

            def on_status(d: RenderDeploy) -> None:
                print(f"      +{time.monotonic() - started:5.0f}s  {d.status}")

            deploy = render.wait_for_deploy(service_id, deploy_id, timeout=args.timeout, on_status=on_status)

            print("[4/5] Build log (last 15 lines):")
            for line in render.get_logs(service_id, log_type="build", since=deploy.created_at, max_lines=15):
                print(f"      | {line.message}")

            if deploy.succeeded:
                url = render.get_service(service_id).url
                resp = httpx.get(url, timeout=60, follow_redirects=True)
                print(f"      GET {url} -> {resp.status_code} {resp.text[:80]!r}")
                ok = resp.status_code == 200
            else:
                print(f"      deploy ended as {deploy.status}")
        except KeyboardInterrupt:
            print("\ninterrupted")
        except Exception as exc:  # noqa: BLE001 - report and still clean up
            print(f"ERROR: {exc}")
        finally:
            if service_id and not args.keep:
                deleted = render.delete_service(service_id)
                gone = _confirm_gone(render, service_id)
                print(f"[5/5] Deleted {service_id}: {deleted}; confirmed gone: {gone}")
                ok = ok and gone
            elif service_id:
                print(f"[5/5] Kept {service_id}. Delete it later from the dashboard.")

    print("SMOKE TEST PASSED" if ok else "SMOKE TEST FAILED")
    return 0 if ok else 1


def _wait_for_first_deploy(render: RenderClient, service_id: str) -> str:
    """Fallback if the create response had no deployId: wait for the first deploy to show up."""
    for _ in range(12):
        deploy = render.get_latest_deploy(service_id)
        if deploy:
            return deploy.id
        time.sleep(5)
    return render.trigger_deploy(service_id).id


def _confirm_gone(render: RenderClient, service_id: str) -> bool:
    for _ in range(10):
        try:
            render.get_service(service_id)
        except RenderAPIError as exc:
            if exc.status_code in (404, 410):
                return True
            raise
        time.sleep(3)
    return False


if __name__ == "__main__":
    sys.exit(main())
