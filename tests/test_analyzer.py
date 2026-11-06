import json

import pytest

from app.ai.analyzer import DeployAnalysis, analyze_repo, ensure_port_binding
from tests.fakes import GOOD_ANALYSIS, fake_github, fake_llm


@pytest.mark.parametrize(
    ("runtime", "cmd", "expected"),
    [
        ("python", "gunicorn app:app", "gunicorn app:app --bind 0.0.0.0:$PORT"),
        ("python", "gunicorn app:app -b 0.0.0.0:5000 -w 2", "gunicorn app:app -w 2 --bind 0.0.0.0:$PORT"),
        ("python", "uvicorn main:app --port 8000", "uvicorn main:app --host 0.0.0.0 --port $PORT"),
        ("python", "gunicorn app:app --bind 0.0.0.0:$PORT", "gunicorn app:app --bind 0.0.0.0:$PORT"),
        ("python", "python app.py", "python app.py"),  # can't fix safely; left to the healer
        ("node", "node server.js", "node server.js"),
    ],
)
def test_ensure_port_binding(runtime, cmd, expected):
    assert ensure_port_binding(runtime, cmd)[0] == expected


def test_analyze_repo_applies_guardrails():
    seen = []
    result = analyze_repo("https://github.com/acme/demo", github=fake_github(), llm=fake_llm(seen=seen))
    a = result.analysis
    assert a.start_command == "gunicorn app:app --bind 0.0.0.0:$PORT"
    assert [e.name for e in a.env_vars] == ["GREETING"]  # PORT dropped
    assert any("PORT" in note for note in result.adjustments)
    assert result.deploy_config()["env_vars"] == {"GREETING": "hello"}

    prompt = seen[0]["messages"][1]["content"]
    assert "===== FILE: app.py =====" in prompt and "static/style.css" in prompt  # tree listed, file not read
    assert "untrusted" in seen[0]["messages"][0]["content"]  # prompt-injection guard in system prompt

    record = result.to_record()
    assert record["files_read"] == ["README.md", "requirements.txt", "Procfile", "app.py"]
    assert record["model"] == "test-model"


def test_docker_without_dockerfile_falls_back_to_native():
    reply = json.dumps({**GOOD_ANALYSIS, "runtime": "docker", "build_command": "", "start_command": "gunicorn app:app"})
    result = analyze_repo("https://github.com/acme/demo", github=fake_github(), llm=fake_llm([reply]))
    assert result.analysis.runtime == "python"
    assert result.analysis.build_command == "pip install -r requirements.txt"


def test_schema_normalises_sloppy_model_output():
    a = DeployAnalysis.model_validate(
        {**GOOD_ANALYSIS, "runtime": "Node.js", "confidence": 85, "health_path": "health", "env_vars": []}
    )
    assert a.runtime == "node" and a.confidence == 0.85 and a.health_path == "/health"


def test_unknown_runtime_is_rejected_then_retried():
    bad = json.dumps({**GOOD_ANALYSIS, "runtime": "rust"})
    result = analyze_repo(
        "https://github.com/acme/demo", github=fake_github(), llm=fake_llm([bad, json.dumps(GOOD_ANALYSIS)])
    )
    assert result.analysis.runtime == "python"
