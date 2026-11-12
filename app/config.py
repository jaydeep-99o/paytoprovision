"""Application settings, read from environment variables (and `.env` in development).

Every variable here is documented in `.env.example`. Secrets are `SecretStr` so they
never show up in reprs, logs or tracebacks; call `.get_secret_value()` only at the
point where the secret is actually sent.
"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: `KEY=` (as copied from .env.example) means "use the default".
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    # App
    app_env: str = "development"
    app_base_url: str = "http://localhost:8000"
    secret_key: SecretStr = SecretStr("dev-insecure-change-me")
    admin_token: SecretStr = SecretStr("")
    internal_reap_token: SecretStr = SecretStr("")
    log_level: str = "INFO"

    # Database (Neon Postgres in production; a local SQLite file if unset)
    database_url: SecretStr = SecretStr("sqlite:///./ptp-dev.db")

    # Render
    render_api_key: SecretStr = SecretStr("")
    render_owner_id: str = ""
    render_region: str = "oregon"
    render_api_base_url: str = "https://api.render.com/v1"
    preview_name_prefix: str = "ptp-"
    preview_plan: str = "free"  # Render instance type for previews: free | starter | ...

    # GitHub (repo analysis; a token lifts the 60 req/h anonymous limit)
    github_token: SecretStr = SecretStr("")

    # Nebius Token Factory (OpenAI-compatible)
    nebius_api_key: SecretStr = SecretStr("")
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    nebius_model: str = "nvidia/nemotron-3-super-120b-a12b"

    # PayPal (sandbox only)
    paypal_client_id: str = ""
    paypal_client_secret: SecretStr = SecretStr("")
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"
    paypal_webhook_id: str = ""
    currency: str = "USD"
    platform_fee_percent: int = 20

    # Guardrails
    allowed_repos: str = ""  # comma-separated "owner/name"
    max_concurrent_previews: int = 3
    max_session_hours: int = 2
    max_heal_attempts: int = 2
    # Global daily caps that protect paid resources from abuse (0 = unlimited).
    ai_daily_call_limit: int = 400  # Nebius chat calls across analyzer, agent and healer
    deploy_daily_limit: int = 40  # Render deploys (builds), including AI-healing redeploys

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def allowed_repo_set(self) -> set[str]:
        return {r.strip().lower() for r in self.allowed_repos.split(",") if r.strip()}

    @property
    def sqlalchemy_url(self) -> str:
        """DATABASE_URL normalised for SQLAlchemy + psycopg 3.

        Neon (and most providers) hand out `postgresql://` or `postgres://` URLs;
        SQLAlchemy needs the driver spelled out.
        """
        url = self.database_url.get_secret_value()
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
