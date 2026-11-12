"""Typed client for the Render REST API (https://api-docs.render.com).

Covers what PayToProvision needs: create a free web service from a public repo,
read service and deploy status, fetch build/app logs, change config and redeploy,
delete. Requests are retried with backoff on 429/5xx/network errors.

The API key lives only in the httpx client's headers. It is never logged and never
put in exception messages.
"""

import logging
import random
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

Runtime = Literal["node", "python", "docker"]
LogType = Literal["build", "app", "request"]

# Deploy statuses, from the Render API `deployStatus` enum.
IN_PROGRESS_STATUSES = {"created", "queued", "build_in_progress", "update_in_progress", "pre_deploy_in_progress"}
FAILED_STATUSES = {"build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated"}
LIVE_STATUS = "live"

# Render build logs carry terminal colour codes ("\x1b[34;1m==>"); strip them so
# logs read cleanly in the UI and in LLM prompts.
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|\([A-Za-z0-9])")  # CSI codes and charset switches

_IDEMPOTENT_METHODS = {"GET", "DELETE", "PUT", "PATCH"}
_MAX_BACKOFF_SECONDS = 30.0
_MAX_RETRY_AFTER_SECONDS = 60.0


class RenderAPIError(Exception):
    def __init__(self, status_code: int, message: str, *, code: str | None = None, method: str = "", path: str = ""):
        self.status_code = status_code
        self.code = code
        self.api_message = message
        super().__init__(f"Render API {method} {path} -> {status_code}: {message}")


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_by_name=True, validate_by_alias=True)


class RenderService(_Model):
    id: str
    name: str
    type: str
    suspended: str | None = None
    dashboard_url: str | None = Field(None, alias="dashboardUrl")
    repo: str | None = None
    branch: str | None = None
    url: str | None = None  # public https://<name>.onrender.com URL, from serviceDetails.url
    created_at: datetime | None = Field(None, alias="createdAt")

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "RenderService":
        details = data.get("serviceDetails") or {}
        return cls.model_validate({**data, "url": details.get("url")})


class RenderDeploy(_Model):
    id: str
    status: str
    trigger: str | None = None
    commit_id: str | None = None
    created_at: datetime | None = Field(None, alias="createdAt")
    finished_at: datetime | None = Field(None, alias="finishedAt")

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "RenderDeploy":
        commit = data.get("commit") or {}
        return cls.model_validate({**data, "commit_id": commit.get("id")})

    @property
    def is_terminal(self) -> bool:
        return self.status not in IN_PROGRESS_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == LIVE_STATUS


class LogLine(_Model):
    timestamp: datetime
    message: str
    type: str | None = None


class CreatedService(_Model):
    service: RenderService
    deploy_id: str | None = None


class RenderClient:
    def __init__(
        self,
        api_key: str,
        *,
        owner_id: str | None = None,
        region: str = "oregon",
        base_url: str = "https://api.render.com/v1",
        timeout: float = 30.0,
        max_retries: int = 4,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not api_key:
            raise ValueError("RENDER_API_KEY is not set")
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        self._owner_id = owner_id or None
        self.region = region
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kwargs: Any) -> "RenderClient":
        s = settings or get_settings()
        return cls(
            s.render_api_key.get_secret_value(),
            owner_id=s.render_owner_id,
            region=s.render_region,
            base_url=s.render_api_base_url,
            **kwargs,
        )

    def __repr__(self) -> str:
        return f"RenderClient(base_url='{self._http.base_url}', owner_id={self._owner_id!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "RenderClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- transport

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None) -> Any:
        idempotent = method in _IDEMPOTENT_METHODS
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._http.request(method, path, params=params, json=json)
            except httpx.TransportError as exc:
                # A POST that failed to connect never reached Render, so it is safe to resend.
                safe = idempotent or isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                if safe and attempt <= self.max_retries:
                    self._backoff(attempt, method, path, reason=type(exc).__name__)
                    continue
                raise RenderAPIError(0, f"network error ({type(exc).__name__})", method=method, path=path) from exc

            # 429 means the request was rejected unprocessed, so any method can retry.
            retryable = resp.status_code == 429 or (resp.status_code >= 500 and idempotent)
            if retryable and attempt <= self.max_retries:
                self._backoff(
                    attempt, method, path, reason=str(resp.status_code), retry_after=resp.headers.get("Retry-After")
                )
                continue
            if resp.status_code >= 400:
                raise self._error(resp, method, path)
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

    def _backoff(self, attempt: int, method: str, path: str, *, reason: str, retry_after: str | None = None) -> None:
        delay = min(_MAX_BACKOFF_SECONDS, 2.0 ** (attempt - 1)) + random.uniform(0, 0.5)
        if retry_after:
            try:
                delay = min(_MAX_RETRY_AFTER_SECONDS, max(float(retry_after), 0.0))
            except ValueError:
                pass
        log.warning(
            "Render %s %s failed (%s); retry %d/%d in %.1fs", method, path, reason, attempt, self.max_retries, delay
        )
        self._sleep(delay)

    @staticmethod
    def _error(resp: httpx.Response, method: str, path: str) -> RenderAPIError:
        message, code = resp.text[:500] or resp.reason_phrase, None
        try:
            body = resp.json()
            if isinstance(body, dict):
                message = body.get("message") or message
                code = body.get("code")
        except ValueError:
            pass
        return RenderAPIError(resp.status_code, message, code=code, method=method, path=path)

    # ---------------------------------------------------------------- workspace

    def get_owner_id(self) -> str:
        """Workspace ID for new services: RENDER_OWNER_ID, else the key's first workspace."""
        if self._owner_id:
            return self._owner_id
        owners = self._request("GET", "/owners", params={"limit": 20}) or []
        if not owners:
            raise RenderAPIError(404, "API key has no workspaces", method="GET", path="/owners")
        if len(owners) > 1:
            log.warning(
                "API key can see %d Render workspaces; using the first. Set RENDER_OWNER_ID to pick one.", len(owners)
            )
        self._owner_id = owners[0]["owner"]["id"]
        return self._owner_id

    def list_owners(self) -> list[dict[str, Any]]:
        return [item["owner"] for item in self._request("GET", "/owners", params={"limit": 100}) or []]

    # ---------------------------------------------------------------- services

    def create_web_service(
        self,
        *,
        name: str,
        repo_url: str,
        runtime: Runtime,
        build_command: str | None = None,
        start_command: str | None = None,
        branch: str | None = None,
        env_vars: dict[str, str] | None = None,
        health_check_path: str | None = None,
        root_dir: str | None = None,
        dockerfile_path: str = "./Dockerfile",
        plan: str = "free",
    ) -> CreatedService:
        """Create a web service from a public Git repo. Render starts the first deploy immediately."""
        if runtime == "docker":
            env_specific: dict[str, Any] = {"dockerfilePath": dockerfile_path, "dockerContext": "."}
        else:
            if not build_command or not start_command:
                raise ValueError("native runtimes need both build_command and start_command")
            env_specific = {"buildCommand": build_command, "startCommand": start_command}

        details: dict[str, Any] = {
            "runtime": runtime,
            "plan": plan,
            "region": self.region,
            "envSpecificDetails": env_specific,
        }
        if health_check_path:
            details["healthCheckPath"] = health_check_path

        body: dict[str, Any] = {
            "type": "web_service",
            "name": name,
            "ownerId": self.get_owner_id(),
            "repo": repo_url,
            "autoDeployTrigger": "off",  # previews are pinned; we deploy explicitly
            "envVars": [{"key": k, "value": v} for k, v in (env_vars or {}).items()],
            "serviceDetails": details,
        }
        if branch:
            body["branch"] = branch
        if root_dir:
            body["rootDir"] = root_dir

        data = self._request("POST", "/services", json=body)
        return CreatedService(service=RenderService.from_api(data["service"]), deploy_id=data.get("deployId"))

    def get_service(self, service_id: str) -> RenderService:
        return RenderService.from_api(self._request("GET", f"/services/{service_id}"))

    def list_services(self, *, name_prefix: str | None = None) -> list[RenderService]:
        """All web services in the workspace, optionally filtered by name prefix (client-side;
        the API's `name` filter is exact-match only)."""
        services: list[RenderService] = []
        params: dict[str, Any] = {"ownerId": self.get_owner_id(), "type": "web_service", "limit": 100}
        while True:
            page = self._request("GET", "/services", params=params) or []
            services.extend(RenderService.from_api(item["service"]) for item in page)
            if len(page) < params["limit"]:
                break
            params["cursor"] = page[-1]["cursor"]
        if name_prefix:
            services = [s for s in services if s.name.startswith(name_prefix)]
        return services

    def update_service(
        self,
        service_id: str,
        *,
        runtime: Runtime | None = None,
        build_command: str | None = None,
        start_command: str | None = None,
        health_check_path: str | None = None,
    ) -> RenderService:
        """Change build/start config. Does NOT deploy; call `trigger_deploy` afterwards."""
        details: dict[str, Any] = {}
        env_specific = {
            k: v for k, v in {"buildCommand": build_command, "startCommand": start_command}.items() if v is not None
        }
        if env_specific:
            details["envSpecificDetails"] = env_specific
        if runtime:
            details["runtime"] = runtime
        if health_check_path is not None:
            details["healthCheckPath"] = health_check_path
        data = self._request("PATCH", f"/services/{service_id}", json={"serviceDetails": details})
        return RenderService.from_api(data)

    def set_env_vars(self, service_id: str, env_vars: dict[str, str]) -> None:
        """Replace ALL env vars on the service. Does NOT deploy; call `trigger_deploy` afterwards."""
        self._request(
            "PUT", f"/services/{service_id}/env-vars", json=[{"key": k, "value": v} for k, v in env_vars.items()]
        )

    def delete_service(self, service_id: str) -> bool:
        """Delete a service. Returns False if it was already gone."""
        try:
            self._request("DELETE", f"/services/{service_id}")
        except RenderAPIError as exc:
            if exc.status_code in (404, 410):
                return False
            raise
        return True

    # ---------------------------------------------------------------- deploys

    def list_deploys(self, service_id: str, *, limit: int = 10) -> list[RenderDeploy]:
        page = self._request("GET", f"/services/{service_id}/deploys", params={"limit": limit}) or []
        return [RenderDeploy.from_api(item["deploy"]) for item in page]

    def get_latest_deploy(self, service_id: str) -> RenderDeploy | None:
        deploys = self.list_deploys(service_id, limit=1)
        return deploys[0] if deploys else None

    def get_deploy(self, service_id: str, deploy_id: str) -> RenderDeploy:
        return RenderDeploy.from_api(self._request("GET", f"/services/{service_id}/deploys/{deploy_id}"))

    def trigger_deploy(
        self, service_id: str, *, clear_cache: bool = False, commit_id: str | None = None
    ) -> RenderDeploy:
        body: dict[str, Any] = {"clearCache": "clear" if clear_cache else "do_not_clear"}
        if commit_id:
            body["commitId"] = commit_id
        data = self._request("POST", f"/services/{service_id}/deploys", json=body)
        if data:  # 201 returns the deploy
            return RenderDeploy.from_api(data)
        # 202 (queued) has no body; the new deploy is the latest one.
        latest = self.get_latest_deploy(service_id)
        if latest is None:
            raise RenderAPIError(
                202, "deploy queued but not listed yet", method="POST", path=f"/services/{service_id}/deploys"
            )
        return latest

    def wait_for_deploy(
        self,
        service_id: str,
        deploy_id: str,
        *,
        timeout: float = 900,
        poll_interval: float = 10,
        on_status: Callable[[RenderDeploy], None] | None = None,
        max_starting_seconds: float | None = None,
    ) -> RenderDeploy:
        """Poll until the deploy reaches a terminal status (live or failed). Calls `on_status`
        whenever the status changes. Raises TimeoutError if it is still running at `timeout`.

        With `max_starting_seconds`, returns early (still `update_in_progress`, i.e. not
        terminal) once the app has been starting that long: Render keeps retrying a failing
        health check for many minutes, so callers can give up sooner."""
        deadline = self._clock() + timeout
        last_status = None
        starting_since: float | None = None
        while True:
            deploy = self.get_deploy(service_id, deploy_id)
            if deploy.status != last_status:
                last_status = deploy.status
                if on_status:
                    on_status(deploy)
            if deploy.is_terminal:
                return deploy
            now = self._clock()
            if deploy.status == "update_in_progress":
                if starting_since is None:
                    starting_since = now
                if max_starting_seconds is not None and now - starting_since >= max_starting_seconds:
                    return deploy
            if now >= deadline:
                raise TimeoutError(f"deploy {deploy_id} still {deploy.status} after {timeout:.0f}s")
            self._sleep(poll_interval)

    def cancel_deploy(self, service_id: str, deploy_id: str) -> RenderDeploy:
        return RenderDeploy.from_api(self._request("POST", f"/services/{service_id}/deploys/{deploy_id}/cancel"))

    # ---------------------------------------------------------------- logs

    def get_logs(
        self,
        service_id: str,
        *,
        log_type: LogType = "build",
        since: datetime | None = None,
        until: datetime | None = None,
        max_lines: int = 200,
    ) -> list[LogLine]:
        """The most recent `max_lines` log lines of one type, oldest first.

        Render defaults the window to the last hour; pass `since` (e.g. the deploy's
        created_at) to scope it to one deploy.
        """
        params: dict[str, Any] = {
            "ownerId": self.get_owner_id(),
            "resource": service_id,
            "type": log_type,
            "direction": "backward",
            "limit": min(max_lines, 100),
        }
        if since:
            params["startTime"] = since.isoformat()
        if until:
            params["endTime"] = until.isoformat()

        lines: list[LogLine] = []
        while len(lines) < max_lines:
            data = self._request("GET", "/logs", params=params) or {}
            for entry in data.get("logs") or []:
                message = _ANSI_ESCAPE.sub("", entry.get("message", ""))
                lines.append(LogLine(timestamp=entry["timestamp"], message=message, type=log_type))
            if not data.get("hasMore") or not data.get("logs"):
                break
            params["startTime"] = data["nextStartTime"]
            params["endTime"] = data["nextEndTime"]
        lines = lines[:max_lines]
        lines.sort(key=lambda line: line.timestamp)
        return lines
