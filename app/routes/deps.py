"""Request-scoped dependencies shared by the routers (overridable in tests)."""

from collections.abc import Iterator

from fastapi import Request

from app.ai.llm import LLMClient, LLMError
from app.github_client import GitHubClient
from app.payments.paypal import PayPalClient, PayPalError

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


def get_paypal() -> Iterator[PayPalClient | None]:
    try:
        client = PayPalClient.from_settings()
    except PayPalError:  # credentials not configured
        yield None
        return
    try:
        yield client
    finally:
        client.close()


def get_toolkit():
    """PayPal Agent Toolkit API for the checkout agent (None if unavailable)."""
    from app.payments.toolkit import make_toolkit_api

    return make_toolkit_api()


def submit_provisioning_job(session_id) -> bool:
    """Indirection so tests can stub out background provisioning."""
    from app.provisioner import submit_provisioning

    return submit_provisioning(session_id)


def get_provision_submitter():
    return submit_provisioning_job


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
