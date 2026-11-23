"""Offline tests for brief/github.py: dependency files, the boto3 rule, and no
GitHub calls unless the company links its org."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from brief import github
from brief.github import (dependency_signals, find_github_org, read_github, technology_of,
                          verify_dependency_signals)
from brief.signals import SignalType
from brief.website import Page

FIXTURES = Path(__file__).parent / "fixtures" / "github"
PUSHED = datetime(2026, 11, 1, tzinfo=timezone.utc)


def signals_for(name: str) -> dict:
    text = (FIXTURES / name).read_text()
    return {s.technology: s for s in dependency_signals(name, text, f"https://github.com/acme/app/blob/main/{name}", PUSHED)}


def test_package_json():
    found = signals_for("package.json")
    assert set(found) == {"MongoDB", "Kafka", "DynamoDB"}  # The description's "redis" is not a dependency.
    assert found["MongoDB"].quote == '"mongoose": "^8.4.0"'
    assert found["MongoDB"].type == SignalType.MONGODB_IN_USE
    assert found["Kafka"].type == SignalType.SCALE_OR_REAL_TIME
    assert found["DynamoDB"].type == SignalType.COMPETITOR_DATABASE
    assert found["MongoDB"].source_type == "github" and found["MongoDB"].published_at == "2026-11-01"


def test_requirements_txt():
    found = signals_for("requirements.txt")
    assert {k: v.quote for k, v in found.items()} == {
        "PostgreSQL": "psycopg2-binary==2.9.9", "AI/LLM": "openai>=1.30", "Oracle": "cx_Oracle==8.3.0"}
    assert found["AI/LLM"].type == SignalType.AI_INITIATIVE


def test_pom_xml():
    found = signals_for("pom.xml")
    assert {k: v.quote for k, v in found.items()} == {
        "Oracle": "<groupId>com.oracle.database.jdbc</groupId>", "Kafka": "<artifactId>spring-kafka</artifactId>"}


def test_other_manifests():
    assert technology_of("go.mongodb.org/mongo-driver") == "MongoDB"
    assert technology_of("github.com/jackc/pgx/v5") == "PostgreSQL"
    assert technology_of("MongoDB.Driver") == "MongoDB"
    assert technology_of("mysql2") == "MySQL"
    assert technology_of("express") is None and technology_of("pgadmin") is None
    gradle = "implementation 'org.postgresql:postgresql:42.7.3'"
    assert [s.technology for s in dependency_signals("build.gradle", gradle, "u", None)] == ["PostgreSQL"]


def test_boto3_counts_as_dynamodb_only_with_dynamodb_in_the_file():
    assert technology_of("boto3", "boto3==1.34\n") is None
    assert technology_of("boto3", "boto3==1.34\n# tables live in DynamoDB\n") == "DynamoDB"
    found = dependency_signals("requirements.txt", "boto3==1.34\nboto3-stubs[dynamodb]\n", "u", None)
    assert [(s.technology, s.quote) for s in found] == [("DynamoDB", "boto3==1.34")]


def test_quote_check_drops_lines_not_in_the_file():
    page = Page(url="u", title="f", text='"mongoose": "^8.4.0"', source_type="github")
    good, bad = dependency_signals("package.json", '{"dependencies": {\n  "mongoose": "^8.4.0"\n}}', "u", None) * 2
    bad = bad.model_copy(update={"quote": '"mongoose": "^9"'})
    assert verify_dependency_signals([good, bad], [page]) == [good]


def test_org_comes_only_from_links():
    assert find_github_org(["https://acme.com/about", "https://jobs.lever.co/acme"]) is None
    assert find_github_org(["https://github.com/features", "https://github.com/login"]) is None
    assert find_github_org(["https://github.com/acme-eng/app", "https://github.com/orgs/acme-eng",
                            "https://github.com/someone"]) == "acme-eng"


def test_no_org_link_means_no_github_calls():
    with mock.patch.object(github.requests, "get") as get:
        assert read_github(["https://acme.com/careers"]) == ([], [], {}, None)
    get.assert_not_called()


class FakeResponse:
    def __init__(self, data=None, text="", status=200, headers=None):
        self.status_code, self.headers = status, headers or {}
        self.text = text or json.dumps(data)
        self._data = data

    def json(self):
        return self._data


def fake_github(repos: int = 3, files: dict | None = None):
    files = files or {"package.json": (FIXTURES / "package.json").read_text()}
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if "/orgs/acme/repos" in url:
            return FakeResponse([{"full_name": f"acme/r{i}", "pushed_at": f"2026-11-01T00:00:{59 - i:02d}Z",
                                  "fork": i == 1, "archived": False} for i in range(repos)])
        if url.endswith("/contents/"):
            repo = url.split("/repos/")[1].split("/contents")[0]
            return FakeResponse([{"name": n, "type": "file", "download_url": f"https://raw/{repo}/{n}",
                                  "html_url": f"https://github.com/{repo}/blob/main/{n}"} for n in files]
                                + [{"name": "src", "type": "dir"}])
        if url.startswith("https://raw/"):
            return FakeResponse(text=files[url.rsplit("/", 1)[1]])
        return FakeResponse(status=404)
    return get, calls


def test_read_github_one_signal_per_technology_newest_repo():
    get, calls = fake_github()
    with mock.patch.object(github.requests, "get", side_effect=get):
        signals, pages, summary, note = read_github(["https://github.com/acme"])
    assert summary == {"org": "acme", "repos_checked": 2, "files_read": 2}  # acme/r1 is a fork.
    assert note is None and len(calls) == 5
    assert sorted(s.technology for s in signals) == ["DynamoDB", "Kafka", "MongoDB"]
    assert all(s.source_url == "https://github.com/acme/r0/blob/main/package.json" for s in signals)
    assert all(p.source_type == "github" for p in pages)


def test_read_github_stops_at_the_request_budget():
    manifest = (FIXTURES / "package.json").read_text()
    get, calls = fake_github(repos=30, files={n: manifest for n in ("package.json", "composer.json", "a.csproj")})
    with mock.patch.object(github.requests, "get", side_effect=get):
        _, _, summary, _ = read_github(["https://github.com/acme"])
    assert len(calls) == github.MAX_GITHUB_REQUESTS
    assert summary["repos_checked"] == github.MAX_GITHUB_REPOS


def test_rate_limit_gives_a_note():
    limited = FakeResponse(status=403, headers={"x-ratelimit-remaining": "0"})
    with mock.patch.object(github.requests, "get", return_value=limited) as get:
        signals, _, summary, note = read_github(["https://github.com/acme"])
    assert signals == [] and get.call_count == 1 and "GITHUB_TOKEN" in note


def test_token_is_sent_when_set(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    assert github.Budget().headers["Authorization"] == "Bearer test-token"
    monkeypatch.delenv("GITHUB_TOKEN")
    assert "Authorization" not in github.Budget().headers


def test_commented_and_metadata_entries_are_not_dependencies():
    pom = "<!-- <artifactId>kafka-clients</artifactId> -->\n<!--\n<artifactId>ojdbc11</artifactId>\n-->\n"
    gradle = "// implementation 'org.apache.kafka:kafka-clients:3.7.0'\n/*\nimplementation 'redis.clients:jedis:5'\n*/\n"
    toml = ('[project]\nname = "redis-tools"\ndependencies = [\n  # "kafka-python",\n  "pymongo>=4",\n]\n'
            '[tool.ruff]\nredis = "x"\n[tool.poetry.dependencies]\npython = "^3.11"\nopenai = "^1"\n')
    assert dependency_signals("pom.xml", pom, "https://github.com/a/b/blob/main/f", None) == []
    assert dependency_signals("build.gradle", gradle, "https://github.com/a/b/blob/main/f", None) == []
    assert {s.technology for s in dependency_signals("pyproject.toml", toml, "https://github.com/a/b/blob/main/f", None)} == {"MongoDB", "AI/LLM"}


def test_go_major_versions_and_type_packages():
    assert technology_of("go.mongodb.org/mongo-driver/v2") == "MongoDB"
    assert technology_of("github.com/redis/go-redis/v9") == "Redis"
    assert technology_of("@types/mongodb") is None and technology_of("@types/pg") is None
