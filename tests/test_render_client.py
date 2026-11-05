import json

import httpx
import pytest

from app.render_client import RenderAPIError, RenderClient

API_KEY = "rnd_test_secret_key_123"


def make_client(handler, **kwargs) -> tuple[RenderClient, list[float]]:
    sleeps: list[float] = []
    client = RenderClient(
        API_KEY,
        owner_id="tea-owner",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        **kwargs,
    )
    return client, sleeps


SERVICE = {
    "id": "srv-123",
    "name": "ptp-demo",
    "type": "web_service",
    "suspended": "not_suspended",
    "dashboardUrl": "https://dashboard.render.com/web/srv-123",
    "repo": "https://github.com/acme/demo",
    "serviceDetails": {"url": "https://ptp-demo.onrender.com", "runtime": "python", "plan": "free"},
}


def test_create_web_service_sends_free_plan_and_parses_response():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"service": SERVICE, "deployId": "dep-1"})

    client, _ = make_client(handler)
    created = client.create_web_service(
        name="ptp-demo",
        repo_url="https://github.com/acme/demo",
        runtime="python",
        build_command="pip install -r requirements.txt",
        start_command="gunicorn app:app",
        env_vars={"FOO": "bar"},
        health_check_path="/health",
    )

    body = seen["body"]
    assert seen["auth"] == f"Bearer {API_KEY}"
    assert body["type"] == "web_service"
    assert body["ownerId"] == "tea-owner"
    assert body["serviceDetails"]["plan"] == "free"
    assert body["serviceDetails"]["envSpecificDetails"] == {
        "buildCommand": "pip install -r requirements.txt",
        "startCommand": "gunicorn app:app",
    }
    assert body["serviceDetails"]["healthCheckPath"] == "/health"
    assert body["envVars"] == [{"key": "FOO", "value": "bar"}]
    assert created.deploy_id == "dep-1"
    assert created.service.url == "https://ptp-demo.onrender.com"
    assert created.service.dashboard_url.endswith("srv-123")


def test_native_runtime_requires_commands():
    client, _ = make_client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError):
        client.create_web_service(name="x", repo_url="https://github.com/a/b", runtime="node")


def test_get_retries_429_honouring_retry_after():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "slow down"})
        return httpx.Response(200, json=SERVICE)

    client, sleeps = make_client(handler)
    assert client.get_service("srv-123").name == "ptp-demo"
    assert len(calls) == 3
    assert sleeps == [2.0, 2.0]


def test_post_is_not_retried_on_500():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"message": "boom"})

    client, _ = make_client(handler)
    with pytest.raises(RenderAPIError) as excinfo:
        client.trigger_deploy("srv-123")
    assert len(calls) == 1
    assert excinfo.value.status_code == 500


def test_gives_up_after_max_retries():
    client, sleeps = make_client(lambda r: httpx.Response(503, text="unavailable"), max_retries=2)
    with pytest.raises(RenderAPIError) as excinfo:
        client.get_service("srv-123")
    assert excinfo.value.status_code == 503
    assert len(sleeps) == 2


def test_api_key_never_appears_in_errors_or_repr():
    client, _ = make_client(lambda r: httpx.Response(401, json={"id": "unauthorized", "message": "invalid key"}))
    with pytest.raises(RenderAPIError) as excinfo:
        client.get_service("srv-123")
    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in repr(client)
    assert excinfo.value.api_message == "invalid key"


def test_delete_returns_false_when_already_gone():
    responses = iter([httpx.Response(204), httpx.Response(404, json={"message": "not found"})])
    client, _ = make_client(lambda r: next(responses))
    assert client.delete_service("srv-123") is True
    assert client.delete_service("srv-123") is False


def test_wait_for_deploy_reports_each_status_change():
    statuses = iter(["build_in_progress", "build_in_progress", "update_in_progress", "live"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "dep-1", "status": next(statuses), "createdAt": "2026-11-05T10:00:00Z"})

    client, sleeps = make_client(handler)
    seen = []
    deploy = client.wait_for_deploy("srv-123", "dep-1", poll_interval=5, on_status=lambda d: seen.append(d.status))
    assert deploy.succeeded
    assert seen == ["build_in_progress", "update_in_progress", "live"]
    assert sleeps == [5, 5, 5]


def test_failed_deploy_is_terminal():
    client, _ = make_client(lambda r: httpx.Response(200, json={"id": "dep-1", "status": "build_failed"}))
    deploy = client.wait_for_deploy("srv-123", "dep-1")
    assert deploy.is_terminal and not deploy.succeeded


def test_get_logs_pages_backwards_and_returns_oldest_first():
    pages = [
        {
            "hasMore": True,
            "nextStartTime": "2026-11-05T09:00:00Z",
            "nextEndTime": "2026-11-05T10:00:01Z",
            "logs": [
                {"id": "3", "message": "ModuleNotFoundError: flask", "timestamp": "2026-11-05T10:00:03Z"},
                {
                    "id": "2",
                    "message": "\x1b[34;1m==>\x1b[0;22m Running build command",
                    "timestamp": "2026-11-05T10:00:02Z",
                },
            ],
        },
        {
            "hasMore": False,
            "logs": [{"id": "1", "message": "Cloning repo", "timestamp": "2026-11-05T10:00:01Z"}],
        },
    ]
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    client, _ = make_client(handler)
    lines = client.get_logs("srv-123", log_type="build")

    assert [line.message for line in lines] == [
        "Cloning repo",
        "==> Running build command",  # ANSI colour codes stripped
        "ModuleNotFoundError: flask",
    ]
    first = requests[0].url.params
    assert first["resource"] == "srv-123" and first["type"] == "build" and first["ownerId"] == "tea-owner"
    assert requests[1].url.params["endTime"] == "2026-11-05T10:00:01Z"


def test_list_services_filters_by_prefix():
    other = {**SERVICE, "id": "srv-999", "name": "my-real-app"}
    client, _ = make_client(
        lambda r: httpx.Response(200, json=[{"service": SERVICE, "cursor": "a"}, {"service": other, "cursor": "b"}])
    )
    assert [s.id for s in client.list_services(name_prefix="ptp-")] == ["srv-123"]


def test_owner_id_is_looked_up_when_not_configured():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/owners"
        return httpx.Response(
            200, json=[{"owner": {"id": "tea-abc", "name": "JD", "email": "x", "type": "user"}, "cursor": "c"}]
        )

    client = RenderClient(API_KEY, transport=httpx.MockTransport(handler))
    assert client.get_owner_id() == "tea-abc"
