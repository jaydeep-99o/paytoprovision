import pytest

from app.github_client import GitHubError, parse_repo_url, select_key_files
from tests.fakes import fake_github


@pytest.mark.parametrize(
    ("url", "owner", "name", "branch"),
    [
        ("https://github.com/acme/demo", "acme", "demo", None),
        ("https://github.com/acme/demo.git", "acme", "demo", None),
        ("github.com/acme/demo/", "acme", "demo", None),
        ("https://www.github.com/Acme-Co/my.repo_v2", "Acme-Co", "my.repo_v2", None),
        ("https://github.com/acme/demo/tree/dev", "acme", "demo", "dev"),
        ("  https://github.com/acme/demo?tab=readme  ", "acme", "demo", None),
    ],
)
def test_parse_repo_url(url, owner, name, branch):
    ref = parse_repo_url(url)
    assert (ref.owner, ref.name, ref.branch) == (owner, name, branch)


@pytest.mark.parametrize(
    "url", ["", "https://gitlab.com/acme/demo", "https://github.com/acme", "acme/demo", "https://github.com/a/b/c/d"]
)
def test_parse_repo_url_rejects_non_repo_links(url):
    with pytest.raises(GitHubError):
        parse_repo_url(url)


def test_select_key_files_orders_readme_manifests_then_entries():
    paths = {
        "src/app.py",
        "app.py",
        "main.py",
        "server.py",
        "run.py",
        "README.md",
        "Dockerfile",
        "package.json",
        "x.txt",
    }
    assert select_key_files(paths) == ["README.md", "package.json", "Dockerfile", "app.py", "main.py", "server.py"]


def test_snapshot_reads_key_files_only():
    snap = fake_github().snapshot(parse_repo_url("https://github.com/acme/demo"))
    assert snap.branch == "main"
    assert list(snap.files) == ["README.md", "requirements.txt", "Procfile", "app.py"]
    assert "static/style.css" in snap.tree and "static/style.css" not in snap.files


def test_snapshot_truncates_big_files():
    gh = fake_github(files={"app.py": "x" * 20_000})
    snap = gh.snapshot(parse_repo_url("https://github.com/acme/demo"), max_chars_per_file=1000)
    assert len(snap.files["app.py"]) < 1100 and snap.files["app.py"].endswith("[truncated]")


def test_missing_repo_gives_friendly_error():
    with pytest.raises(GitHubError, match="not found"):
        fake_github().snapshot(parse_repo_url("https://github.com/acme/other"))
