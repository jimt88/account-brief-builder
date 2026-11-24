"""Public GitHub dependency evidence (Phase 8).

Dependency files are hard evidence of a company's stack and need no Gemini call.
The GitHub organisation comes only from a github.com/{org} link on the company's own
pages or a link the user pasted; it is never guessed from the company name.

Without a token the GitHub REST API allows 60 requests an hour per IP, so a brief uses
at most MAX_GITHUB_REQUESTS. An optional GITHUB_TOKEN (a free personal token with no
scopes) raises the limit to 5,000 an hour.
"""

import json
import os
import re
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

import requests

from brief.gemini_utils import normalise
from brief.jobs import parse_date
from brief.signals import Signal, SignalType
from brief.website import TIMEOUT_SECONDS, USER_AGENT, Page

API = "https://api.github.com"
MAX_GITHUB_REPOS = 10  # The most recently pushed public, non-fork, non-archived repos.
MAX_GITHUB_REQUESTS = 40  # Request budget per brief (API calls plus file downloads).
MAX_FILE_CHARS = 200_000

MANIFESTS = {"requirements.txt", "pyproject.toml", "package.json", "pom.xml", "build.gradle",
             "build.gradle.kts", "go.mod", "Gemfile", "composer.json"}  # Plus any *.csproj.

# Library name -> technology. Names are compared lower case with "_" read as "-";
# a library also matches with a version or flavour suffix (mysql2, ojdbc11, psycopg2-binary).
DB_LIBRARIES = {
    **dict.fromkeys(["pymongo", "motor", "mongoose", "mongodb", "mongo-driver", "mongoid", "mongodb.driver",
                     "mongodb-driver-sync", "mongodb-driver-reactivestreams", "spring-boot-starter-data-mongodb",
                     "mongodb/mongodb", "mongodb/laravel-mongodb"], "MongoDB"),
    **dict.fromkeys(["psycopg", "psycopg2", "pg", "asyncpg", "org.postgresql", "npgsql", "github.com/lib/pq",
                     "github.com/jackc/pgx"], "PostgreSQL"),
    **dict.fromkeys(["oracledb", "cx-oracle", "ojdbc", "com.oracle.database.jdbc", "oracle.manageddataaccess",
                     "github.com/godror/godror"], "Oracle"),
    **dict.fromkeys(["mysql", "mysqlclient", "mysql-connector", "pymysql", "mysql.data",
                     "github.com/go-sql-driver/mysql"], "MySQL"),
    **dict.fromkeys(["@aws-sdk/client-dynamodb", "@aws-sdk/lib-dynamodb", "aws-sdk-dynamodb", "dynamodb",
                     "dynamodb-enhanced", "pynamodb"], "DynamoDB"),
    **dict.fromkeys(["@azure/cosmos", "azure-cosmos", "microsoft.azure.cosmos"], "Cosmos DB"),
    **dict.fromkeys(["cassandra-driver", "java-driver-core", "github.com/gocql/gocql"], "Cassandra"),
    **dict.fromkeys(["redis", "ioredis", "jedis", "lettuce-core", "stackexchange.redis",
                     "github.com/redis/go-redis", "github.com/go-redis/redis"], "Redis"),
    **dict.fromkeys(["kafka-python", "confluent-kafka", "kafkajs", "spring-kafka", "kafka-clients", "confluent.kafka",
                     "github.com/segmentio/kafka-go", "github.com/ibm/sarama"], "Kafka"),
    **dict.fromkeys(["openai", "anthropic", "langchain", "llama-index", "transformers", "@anthropic-ai/sdk",
                     "@langchain/core", "langchain4j", "spring-ai-core", "github.com/sashabaranov/go-openai"],
                    "AI/LLM"),
}
BOTO3_NEEDS = "dynamodb"  # boto3 counts as DynamoDB only if the file also says "dynamodb".

TECH_SIGNAL_TYPES = {"MongoDB": SignalType.MONGODB_IN_USE, "Kafka": SignalType.SCALE_OR_REAL_TIME,
                     "AI/LLM": SignalType.AI_INITIATIVE}  # Everything else: competitor_database.

NOT_ORGS = {"about", "apps", "collections", "contact", "customer-stories", "enterprise", "events", "explore",
            "features", "join", "login", "marketplace", "new", "notifications", "pricing", "readme", "security",
            "settings", "site", "solutions", "sponsors", "topics", "trending"}


def org_votes(links: list[str]) -> Counter:
    """Org name -> weight. A link to the org itself (github.com/acme) counts
    PROFILE_LINK_WEIGHT times as much as a link to one repo (often a third party's)."""
    orgs = Counter()
    for link in links:
        url = urlparse(link if "://" in link else "https://" + link)
        if url.netloc.lower().removeprefix("www.") != "github.com":
            continue
        parts = [p for p in url.path.split("/") if p]
        if parts[:1] == ["orgs"]:
            parts = parts[1:]
        if parts and parts[0].lower() not in NOT_ORGS and re.fullmatch(r"[A-Za-z0-9-]{1,39}", parts[0]):
            orgs[parts[0].lower()] += PROFILE_LINK_WEIGHT if len(parts) == 1 else 1  # GitHub ignores case.
    return orgs


PROFILE_LINK_WEIGHT = 3


def find_github_org(links: list[str], preferred: list[str] | None = None) -> str | None:
    """The org the rep pasted (preferred links) if any, else the org most linked from
    the company's pages, or None. Never guessed from the company name."""
    orgs = org_votes(preferred or []) or org_votes(links)
    return orgs.most_common(1)[0][0] if orgs else None


def library_key(name: str) -> str:
    return re.sub(r"\[.*?\]", "", name.strip().lower().replace("_", "-"))


def technology_of(name: str, file_text: str = "") -> str | None:
    """Map a dependency name to a technology, or None."""
    name = re.sub(r"/v\d+$", "", library_key(name))  # Go major versions: mongo-driver/v2.
    if name.startswith("@types/"):  # Type definitions only, not the library itself.
        return None
    if name == "boto3":
        return "DynamoDB" if BOTO3_NEEDS in file_text.lower() else None
    candidates = {name, name.split("/")[-1]} if not name.startswith("github.com/") else {name}
    for candidate in candidates:
        for library, technology in DB_LIBRARIES.items():
            if (candidate == library or candidate.startswith(library + "-")
                    or candidate.startswith(library + "/") or re.fullmatch(re.escape(library) + r"\d+", candidate)):
                return technology
    return None


def uncommented(lines: list[str], start: str, end: str) -> list[str]:
    """Lines that are not inside a block comment (<!-- --> or /* */)."""
    kept, inside = [], False
    for line in lines:
        if inside:
            inside = end not in line
            continue
        if line.lstrip().startswith(start):
            inside = end not in line
            continue
        kept.append(line)
    return kept


TOML_NAME = r"[A-Za-z0-9][A-Za-z0-9._\-\[\]]*"


def toml_code(line: str) -> str:
    """A TOML line with strings blanked and the comment removed, to read its structure."""
    return re.sub(r'"[^"]*"|\'[^\']*\'', '""', line).split("#")[0].strip()


def pyproject_lines(lines: list[str]) -> list[tuple[str, str]]:
    """Dependencies in pyproject.toml: the [project] dependencies array, optional
    dependencies, dependency groups and Poetry's "name = version" tables. Other
    tables (metadata, tool settings) and # comments are ignored."""
    found, section, array = [], "", None  # array: "names" (scan it) or "skip" (Poetry constraints).
    for line in lines:
        code = toml_code(line)
        if not code:
            continue
        if array is None:
            header = re.fullmatch(r"\[+([^\]]+)\]+", code)
            if header:
                section = header.group(1).strip()
                continue
            deps_table = section.endswith("dependencies") or section == "dependency-groups"
            poetry = section.startswith("tool.poetry")
            key = re.match(r'"?([A-Za-z0-9][A-Za-z0-9._\-]*)"?\s*=', line.strip())
            if not key or not (deps_table or (section == "project" and key.group(1) == "dependencies")):
                continue
            if poetry:  # name = "^1.0" | {version = ...} | [{...}, {...}]: the key is the package.
                if key.group(1) != "python":
                    found.append((key.group(1), line))
                array = "skip" if code.endswith("[") or ("[" in code and "]" not in code) else None
                continue
            if "[" not in code:
                continue
            array = "names"
            line_part = line[line.index("=") + 1:]
        else:
            line_part = line
        if array == "names":
            for match in re.finditer(r'"([^"]*)"|#', line_part):  # Strings until a comment.
                if match.group(0) == "#":
                    break
                name = re.match(TOML_NAME, match.group(1))
                if name:
                    found.append((name.group(0), line))
        if "]" in toml_code(line_part if array == "names" else line):
            array = None
    return found


def dependency_lines(filename: str, text: str) -> list[tuple[str, str]]:
    """(dependency name, the exact line it is declared on) for one manifest."""
    found = []
    lines = text.splitlines()
    if filename in ("package.json", "composer.json"):
        try:
            data = json.loads(text)
        except ValueError:
            return []
        sections = ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies",
                    "require", "require-dev"]
        names = dict.fromkeys(name for section in sections for name in (data.get(section) or {}))
        for name in names:  # Quote the exact '"name": "version"' text: works on one-line files too.
            match = re.search(rf'"{re.escape(name)}"\s*:\s*"[^"]*"', text)
            if match:
                found.append((name, match.group(0)))
    elif filename == "requirements.txt":
        for line in lines:
            match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._\-\[\]]*)", line)
            if match and not line.lstrip().startswith(("#", "-")):
                found.append((match.group(1), line))
    elif filename == "pyproject.toml":
        found = pyproject_lines(lines)
    elif filename == "pom.xml":
        for line in uncommented(lines, "<!--", "-->"):
            for match in re.finditer(r"<(?:artifactId|groupId)>\s*([\w.\-]+)\s*</", line):
                found.append((match.group(1), line))
    elif filename.startswith("build.gradle"):
        for line in uncommented(lines, "/*", "*/"):
            if line.lstrip().startswith("//"):
                continue
            for match in re.finditer(r"""["']([\w.\-]+):([\w.\-]+)(?::[^"']*)?["']""", line):
                found += [(match.group(1), line), (match.group(2), line)]
    elif filename == "go.mod":
        for line in lines:
            match = re.match(r"\s*(?:require\s+)?([\w.\-]+\.[a-z]+/[\w.\-/]+)\s+v\d", line)
            if match:
                found.append((match.group(1), line))
    elif filename == "Gemfile":
        for line in lines:
            match = re.match(r"""\s*gem\s+["']([\w.\-]+)["']""", line)
            if match:
                found.append((match.group(1), line))
    elif filename.endswith(".csproj"):
        for line in lines:
            for match in re.finditer(r'<PackageReference\s+Include="([\w.\-]+)"', line):
                found.append((match.group(1), line))
    return found


def dependency_signals(filename: str, text: str, html_url: str, pushed_at: datetime | None) -> list[Signal]:
    """One signal per technology found in this file, quoting its dependency line."""
    signals = {}
    for name, line in dependency_lines(filename, text):
        technology = technology_of(name, text)
        if technology and technology not in signals:
            signals[technology] = Signal(
                type=TECH_SIGNAL_TYPES.get(technology, SignalType.COMPETITOR_DATABASE),
                quote=line.strip().rstrip(","), source_url=html_url, technology=technology,
                source_type="github", published_at=pushed_at.date().isoformat() if pushed_at else "")
    return list(signals.values())


def verify_dependency_signals(signals: list[Signal], pages: list[Page]) -> list[Signal]:
    """The same word-for-word check as Gemini's signals: the line must be in the file."""
    texts = {p.url: normalise(p.text) for p in pages}
    return [s for s in signals if normalise(s.quote) and normalise(s.quote) in texts.get(s.source_url, "")]


class Budget:
    """Counts requests so a brief never uses more than MAX_GITHUB_REQUESTS."""

    def __init__(self, limit: int = MAX_GITHUB_REQUESTS):
        self.left, self.rate_limited = limit, False
        token = os.getenv("GITHUB_TOKEN", "").strip()
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def get(self, url: str):
        if self.left <= 0 or self.rate_limited:
            return None
        self.left -= 1
        try:
            response = requests.get(url, headers=self.headers, timeout=TIMEOUT_SECONDS)
        except requests.RequestException:
            return None
        if response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
            self.rate_limited = True
            return None
        return response if response.status_code == 200 else None


def list_repos(org: str, budget: Budget) -> list[dict]:
    """The MAX_GITHUB_REPOS most recently pushed public repos, without forks or archived ones."""
    response = budget.get(f"{API}/orgs/{org}/repos?type=public&sort=pushed&per_page=100")
    if response is None and not budget.rate_limited:  # Not an organisation: maybe a user account.
        response = budget.get(f"{API}/users/{org}/repos?type=owner&sort=pushed&per_page=100")
    if response is None:
        return []
    repos = [r for r in response.json() if not r.get("fork") and not r.get("archived")]
    repos.sort(key=lambda r: r.get("pushed_at") or "", reverse=True)
    return repos[:MAX_GITHUB_REPOS]


def read_github(links: list[str], preferred: list[str] | None = None) -> tuple[list[Signal], list[Page], dict, str | None]:
    """(verified dependency signals, manifest pages, github_summary, note). Makes no
    request at all unless the links contain a github.com/{org} link. `preferred` are
    links the rep pasted: a GitHub org there wins over ones found on the pages."""
    org = find_github_org(links, preferred)
    if org is None:
        return [], [], {}, None
    budget = Budget()
    repos = list_repos(org, budget)
    pages, signals = [], []
    for repo in repos:
        listing = budget.get(f"{API}/repos/{repo['full_name']}/contents/")
        if listing is None:
            continue
        pushed_at = parse_date(repo.get("pushed_at"))
        for item in listing.json():
            name = item.get("name", "")
            if item.get("type") != "file" or not item.get("download_url") or not (
                    name in MANIFESTS or name.endswith(".csproj")):
                continue
            response = budget.get(item["download_url"])
            if response is None:
                continue
            text = response.text[:MAX_FILE_CHARS]
            page = Page(url=item["html_url"], title=f"{repo['full_name']}/{name}", text=text,
                        source_type="github", published_at=pushed_at, source_label="GitHub")
            pages.append(page)
            signals += dependency_signals(name, text, page.url, pushed_at)
    newest = {}  # One signal per technology: the most recently pushed repo wins (repos are newest first).
    for s in verify_dependency_signals(signals, pages):
        newest.setdefault(s.technology, s)
    summary = {"org": org, "repos_checked": len(repos), "files_read": len(pages)}
    note = None
    if budget.rate_limited:
        note = ("GitHub's hourly limit was reached (60 requests an hour without a token); "
                "add GITHUB_TOKEN for more.")
    elif not repos:
        note = f"No public repositories found for github.com/{org}."
    return list(newest.values()), pages, summary, note


def summary_line(summary: dict, signals: list[Signal]) -> str:
    found = ", ".join(s.technology for s in signals) or "no database or AI libraries"
    return (f"GitHub {summary['org']}: {summary['repos_checked']} repos, "
            f"{summary['files_read']} dependency files ({found})")
