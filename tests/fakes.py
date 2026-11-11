"""httpx MockTransport fakes for GitHub and the LLM, shared by tests."""

import json

import httpx

from app.ai.llm import LLMClient
from app.github_client import GitHubClient
from app.payments.paypal import PayPalClient
from app.render_client import RenderClient

REPO_FILES = {
    "README.md": "# Demo\nA tiny Flask app. Run `python app.py` on port 5000.",
    "requirements.txt": "flask\ngunicorn\n",
    "Procfile": "web: gunicorn app:app",
    "app.py": "from flask import Flask\napp = Flask(__name__)\napp.run(port=5000)\n",
    "static/style.css": "body{}",
}

GOOD_ANALYSIS = {
    "title": "Demo App",
    "summary": "A tiny Flask app that says hello.",
    "stack": "Flask + gunicorn",
    "runtime": "python",
    "build_command": "pip install -r requirements.txt",
    "start_command": "gunicorn app:app",
    "port_notes": "Hardcoded 5000 locally; gunicorn binds $PORT.",
    "env_vars": [
        {"name": "PORT", "default": "5000", "required": True, "description": "port"},
        {"name": "GREETING", "default": "hello", "required": True, "description": "text to show"},
    ],
    "health_path": "/",
    "confidence": 0.9,
    "warnings": [],
}


def github_handler(files: dict[str, str] | None = None, *, full_name: str = "acme/demo"):
    files = REPO_FILES if files is None else files

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{full_name}":
            return httpx.Response(
                200,
                json={
                    "full_name": full_name,
                    "html_url": f"https://github.com/{full_name}",
                    "default_branch": "main",
                    "description": "demo",
                    "language": "Python",
                    "stargazers_count": 3,
                    "private": False,
                    "size": 10,
                },
            )
        if path.startswith(f"/repos/{full_name}/git/trees/"):
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob"} for p in files], "truncated": False})
        prefix = f"/repos/{full_name}/contents/"
        if path.startswith(prefix) and path[len(prefix) :] in files:
            return httpx.Response(200, text=files[path[len(prefix) :]])
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


def fake_github(**kwargs) -> GitHubClient:
    return GitHubClient("ghp_test", transport=httpx.MockTransport(github_handler(**kwargs)))


def llm_handler(replies: list[str], seen: list | None = None):
    """Return each reply in turn as the assistant message content."""
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        content = queue.pop(0) if queue else replies[-1]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content, "reasoning_content": "thinking..."},
                    }
                ]
            },
        )

    return handler


def fake_llm(replies: list[str] | None = None, seen: list | None = None) -> LLMClient:
    replies = replies or [json.dumps(GOOD_ANALYSIS)]
    return LLMClient("nb_test", model="test-model", transport=httpx.MockTransport(llm_handler(replies, seen)))


class FakePayPal:
    """Stateful PayPal sandbox stand-in. `calls` records (method, path, PayPal-Request-Id)."""

    def __init__(self):
        self.calls: list[tuple[str, str, str | None]] = []
        self.bodies: list[dict] = []
        self.orders = 0
        self.capture_error: str | None = None  # e.g. "INSTRUMENT_DECLINED"
        self.capture_status = "COMPLETED"
        self.refund_fails = False
        self.verify_ok = True

    def captured(self, order_id: str) -> dict:
        return {
            "id": order_id,
            "status": "COMPLETED",
            "payer": {"email_address": "buyer@example.com"},
            "purchase_units": [{"payments": {"captures": [{"id": f"CAP-{order_id}", "status": self.capture_status}]}}],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path, request.headers.get("PayPal-Request-Id")))
        if request.content and path != "/v1/oauth2/token":
            self.bodies.append(json.loads(request.content))
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 32400})
        if path == "/v2/checkout/orders" and method == "POST":
            self.orders += 1
            oid = f"ORDER-{self.orders}"
            return httpx.Response(
                201,
                json={
                    "id": oid,
                    "status": "CREATED",
                    "links": [{"rel": "approve", "href": f"https://sandbox.paypal.com/checkoutnow?token={oid}"}],
                },
            )
        if path.endswith("/capture"):
            oid = path.split("/")[-2]
            if self.capture_error:
                return httpx.Response(
                    422,
                    json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": self.capture_error}], "debug_id": "d1"},
                )
            return httpx.Response(201, json=self.captured(oid))
        if path.startswith("/v2/checkout/orders/") and method == "GET":
            return httpx.Response(200, json=self.captured(path.rsplit("/", 1)[-1]))
        if path.endswith("/refund"):
            if self.refund_fails:
                return httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"})
            return httpx.Response(201, json={"id": "REF-1", "status": "COMPLETED"})
        if path == "/v1/notifications/verify-webhook-signature":
            return httpx.Response(200, json={"verification_status": "SUCCESS" if self.verify_ok else "FAILURE"})
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})

    def client(self) -> PayPalClient:
        return PayPalClient("cid", "secret", transport=httpx.MockTransport(self.handler))


class FakeRender:
    """Render API stand-in for provisioning and reaping."""

    def __init__(self, deploy_statuses=("build_in_progress", "update_in_progress", "live")):
        self.deploy_statuses = list(deploy_statuses)
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.services: list[dict] = []  # returned by list_services
        self.patches: list[dict] = []  # PATCH /services/{id} bodies (healer config changes)
        self.env_puts: list[list] = []  # PUT /services/{id}/env-vars bodies
        self.triggered = 0  # POST /services/{id}/deploys
        self.cancelled: list[str] = []
        self.now = 0.0  # fake clock: every poll's sleep moves it 30s forward

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path.endswith("/cancel"):
            self.cancelled.append(path.split("/")[-2])
            return httpx.Response(200, json={"id": path.split("/")[-2], "status": "canceled"})
        if method == "PATCH" and path.startswith("/v1/services/"):
            self.patches.append(json.loads(request.content))
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "name": "svc", "type": "web_service"})
        if method == "PUT" and path.endswith("/env-vars"):
            self.env_puts.append(json.loads(request.content))
            return httpx.Response(200, json=[])
        if method == "POST" and path.endswith("/deploys"):
            self.triggered += 1
            return httpx.Response(201, json={"id": f"dep-{self.triggered + 1}", "status": "created"})
        if path == "/v1/services" and method == "POST":
            body = json.loads(request.content)
            self.created.append(body)
            svc = {
                "id": f"srv-{len(self.created)}",
                "name": body["name"],
                "type": "web_service",
                "serviceDetails": {"url": f"https://{body['name']}.onrender.com"},
            }
            return httpx.Response(201, json={"service": svc, "deployId": "dep-1"})
        if path == "/v1/services" and method == "GET":
            return httpx.Response(200, json=[{"service": s, "cursor": "c"} for s in self.services])
        if "/deploys/" in path:
            status = self.deploy_statuses.pop(0) if len(self.deploy_statuses) > 1 else self.deploy_statuses[0]
            return httpx.Response(200, json={"id": "dep-1", "status": status, "createdAt": "2026-11-06T10:00:00Z"})
        if path.endswith("/deploys") and method == "GET":
            return httpx.Response(
                200, json=[{"deploy": {"id": "dep-1", "status": self.deploy_statuses[0]}, "cursor": "c"}]
            )
        if path == "/v1/logs":
            return httpx.Response(
                200,
                json={
                    "hasMore": False,
                    "logs": [
                        {
                            "id": "1",
                            "message": "ModuleNotFoundError: No module named 'requests'",
                            "timestamp": "2026-11-06T10:01:00Z",
                        }
                    ],
                },
            )
        if method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "not found"})

    def client(self) -> RenderClient:
        def sleep(_seconds: float) -> None:
            self.now += 30

        return RenderClient(
            "rnd_test",
            owner_id="tea-x",
            transport=httpx.MockTransport(self.handler),
            sleep=sleep,
            clock=lambda: self.now,
        )
