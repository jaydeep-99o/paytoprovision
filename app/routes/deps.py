"""Request-scoped dependencies shared by the routers (overridable in tests)."""

from collections.abc import Iterator

from fastapi import Request

from app.ai.llm import LLMClient, LLMError
from app.github_client import GitHubClient

OWNER_COOKIE = "ptp_owner"


def get_owner_key(request: Request) -> str:
    """The browser's owner key, assigned by the owner-cookie middleware."""
    return request.state.owner_key


def get_github() -> Iterator[GitHubClient]:
    client = GitHubClient.from_settings()
    try:
        yield client
    finally:
        client.close()


def get_llm() -> Iterator[LLMClient | None]:
    try:
        client = LLMClient.from_settings()
    except LLMError:  # NEBIUS_API_KEY not configured
        yield None
        return
    try:
        yield client
    finally:
        client.close()
