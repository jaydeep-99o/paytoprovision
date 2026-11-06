"""httpx MockTransport fakes for GitHub and the LLM, shared by tests."""

import json

import httpx

from app.ai.llm import LLMClient
from app.github_client import GitHubClient

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
