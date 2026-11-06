"""Minimal GitHub REST client for repo analysis: metadata, file tree and the handful of
files an AI needs to work out how to deploy a repo.

Only public repos are supported. A GITHUB_TOKEN lifts the anonymous 60 req/h limit.
"""

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings

API = "https://api.github.com"

_REPO_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<name>[A-Za-z0-9._-]{1,100}?)(?:\.git)?"
    r"(?:/tree/(?P<branch>[^/?#]+))?/?(?:[?#].*)?$"
)

# Files that tell us how a repo is built and started, checked at the repo root.
MANIFESTS = [
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
    "setup.py",
    "Procfile",
    "Dockerfile",
    "runtime.txt",
    ".python-version",
    ".nvmrc",
    "render.yaml",
    "app.json",
    "manage.py",
]
READMES = ["README.md", "README.rst", "README.txt", "README", "readme.md"]
# Typical entry points; the first few that exist are read.
ENTRY_FILES = [
    "app.py",
    "main.py",
    "server.py",
    "run.py",
    "index.js",
    "server.js",
    "app.js",
    "main.js",
    "src/index.js",
    "src/server.js",
    "src/app.js",
    "src/index.ts",
    "src/server.ts",
    "src/main.py",
    "src/app.py",
]
MAX_ENTRY_FILES = 3
MAX_REPO_SIZE_KB = 200_000  # far beyond what a free instance can build anyway


class GitHubError(Exception):
    """A problem worth showing to the user as-is."""


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str
    branch: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"


def parse_repo_url(url: str) -> RepoRef:
    match = _REPO_URL.match((url or "").strip())
    if not match:
        raise GitHubError("That doesn't look like a GitHub repo link. Use https://github.com/owner/repo.")
    return RepoRef(match["owner"], match["name"], match["branch"])


def select_key_files(paths: set[str]) -> list[str]:
    """The files worth sending to the AI, in reading order: README, manifests, entry points."""
    chosen: list[str] = []
    readme = next((p for p in READMES if p in paths), None)
    if readme:
        chosen.append(readme)
    chosen += [p for p in MANIFESTS if p in paths]
    chosen += [p for p in ENTRY_FILES if p in paths][:MAX_ENTRY_FILES]
    return chosen


@dataclass
class RepoSnapshot:
    full_name: str
    html_url: str
    branch: str
    description: str
    language: str | None
    stars: int
    tree: list[str]  # file paths (capped)
    files: dict[str, str] = field(default_factory=dict)  # path -> (truncated) content
    tree_truncated: bool = False

    def metadata(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "html_url": self.html_url,
            "branch": self.branch,
            "description": self.description,
            "language": self.language,
            "stars": self.stars,
        }


class GitHubClient:
    def __init__(self, token: str = "", *, timeout: float = 20.0, transport: httpx.BaseTransport | None = None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(base_url=API, headers=headers, timeout=timeout, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "GitHubClient":
        return cls((settings or get_settings()).github_token.get_secret_value())

    def __repr__(self) -> str:
        return "GitHubClient()"

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._http.get(path, **kwargs)
        except httpx.TransportError as exc:
            raise GitHubError("Couldn't reach GitHub. Try again in a moment.") from exc
        if resp.status_code == 404:
            raise GitHubError("Repo not found. Is it public, and is the link right?")
        if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubError("GitHub rate limit reached. Try again in a few minutes.")
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub returned an error ({resp.status_code}).")
        return resp

    def get_repo(self, ref: RepoRef) -> dict[str, Any]:
        return self._get(f"/repos/{ref.full_name}").json()

    def get_tree(self, ref: RepoRef, branch: str) -> tuple[list[str], bool]:
        data = self._get(f"/repos/{ref.full_name}/git/trees/{branch}", params={"recursive": "1"}).json()
        paths = [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]
        return paths, bool(data.get("truncated"))

    def get_file(self, ref: RepoRef, path: str, branch: str, *, max_chars: int) -> str:
        resp = self._get(
            f"/repos/{ref.full_name}/contents/{path}",
            params={"ref": branch},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        text = resp.content[: max_chars * 4].decode("utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [truncated]"
        return text

    def snapshot(self, ref: RepoRef, *, max_chars_per_file: int = 8000, max_tree: int = 400) -> RepoSnapshot:
        repo = self.get_repo(ref)
        if repo.get("private"):
            raise GitHubError("Only public repos are supported.")
        if repo.get("size", 0) > MAX_REPO_SIZE_KB:
            raise GitHubError("This repo is too large for a free preview instance.")
        branch = ref.branch or repo.get("default_branch") or "main"
        paths, truncated = self.get_tree(ref, branch)
        if not paths:
            raise GitHubError("This repo has no files on that branch.")

        snap = RepoSnapshot(
            full_name=repo.get("full_name", ref.full_name),
            html_url=repo.get("html_url", ref.url),
            branch=branch,
            description=repo.get("description") or "",
            language=repo.get("language"),
            stars=repo.get("stargazers_count", 0),
            tree=sorted(paths)[:max_tree],
            tree_truncated=truncated or len(paths) > max_tree,
        )
        for path in select_key_files(set(paths)):
            snap.files[path] = self.get_file(ref, path, branch, max_chars=max_chars_per_file)
        return snap
