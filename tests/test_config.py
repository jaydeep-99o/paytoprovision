import re
from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_env_example_documents_every_setting():
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(), flags=re.MULTILINE))
    expected = {name.upper() for name in Settings.model_fields}
    assert expected - documented == set(), "add these to .env.example"


def test_database_url_normalised_for_psycopg():
    s = Settings(_env_file=None, database_url="postgres://u:p@host/db?sslmode=require")
    assert s.sqlalchemy_url == "postgresql+psycopg://u:p@host/db?sslmode=require"
    s = Settings(_env_file=None, database_url="postgresql://u:p@host/db")
    assert s.sqlalchemy_url == "postgresql+psycopg://u:p@host/db"


def test_secrets_hidden_from_repr():
    s = Settings(_env_file=None, render_api_key="rnd_supersecret", database_url="postgresql://u:hunter2@h/db")
    assert "rnd_supersecret" not in repr(s)
    assert "hunter2" not in repr(s)


def test_allowed_repos_parsed_case_insensitively():
    s = Settings(_env_file=None, allowed_repos=" Acme/Demo , acme/other,, ")
    assert s.allowed_repo_set == {"acme/demo", "acme/other"}
