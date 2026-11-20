"""Read a company's public website: the home page plus a few useful pages.

Rules we follow:
  - public pages only: we never log in, and we skip pages that ask for a login
    or return 401/403 (paywalls, members-only areas),
  - we respect robots.txt,
  - we stay on the company's own domain (no LinkedIn or other sites),
  - we read at most MAX_PAGES pages (home, about, careers, engineering blog),
    each with a TIMEOUT_SECONDS limit, so it's quick and polite.

You can also pass up to MAX_EXTRA_PAGES extra links (a press release, a careers
page, a news article). These may be on other sites, but the same rules apply,
and sites that need a login (LinkedIn, Facebook, ...) are refused.
"""

from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; account-brief-builder/0.1)"
MAX_PAGES = 4  # Home page + about + careers + engineering blog.
TIMEOUT_SECONDS = 10
# The pages we look for after the home page, in order, and words that identify them.
TARGET_PAGES = {
    "about": ["about", "who-we-are", "company", "ueber-uns", "unternehmen", "a-propos",
              "entreprise", "sobre", "empresa", "chi-siamo", "azienda", "over-ons"],
    "careers": ["careers", "jobs", "join", "karriere", "carrieres", "empleo", "lavora"],
    "engineering blog": ["engineering", "tech-blog", "technology", "developers", "blog", "news", "press"],
}
MAX_CHARS_PER_PAGE = 8_000  # Keeps the Gemini prompt small (free tier).
MIN_CHARS = 200  # Less text than this usually means a JavaScript-only page.

# Words in a link (URL or link text) that suggest the page has useful signals.
# Includes a few common German/French/Spanish/Italian/Dutch equivalents.
USEFUL_WORDS = [
    "about", "company", "who-we-are", "strategy", "news", "press", "media",
    "blog", "careers", "jobs", "technology", "engineering", "digital",
    "innovation", "ai", "data", "cloud", "investor", "sustainability",
    "products", "solutions", "platform",
    "ueber-uns", "unternehmen", "karriere", "presse",
    "a-propos", "entreprise", "carrieres", "actualites",
    "sobre", "empresa", "noticias", "chi-siamo", "azienda", "over-ons",
]
# Links we never follow.
SKIP_WORDS = [
    "login", "log-in", "signin", "sign-in", "register", "account", "cart",
    "checkout", "basket", "privacy", "cookie", "terms", "legal", "imprint",
    "impressum", "contact", "search", "subscribe", "newsletter",
    "policies", "policy", "disclaimer",
]
# Sites that hide content behind a login; we never read them.
BLOCKED_SITES = ["linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "glassdoor."]
MAX_EXTRA_PAGES = 3

SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".mp4", ".xml", ".svg")


@dataclass
class Page:
    url: str
    title: str
    text: str


def normalise_website(website: str) -> str:
    """Turn 'example.com' into 'https://example.com/'."""
    website = website.strip()
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    return website


def same_site(url: str, home: str) -> bool:
    """True if url is on the same domain as home (www. and subdomains allowed)."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    home_host = urlparse(home).netloc.lower().removeprefix("www.")
    return host == home_host or host.endswith("." + home_host)


def load_robots(home: str) -> RobotFileParser:
    robots = RobotFileParser()
    try:
        response = requests.get(urljoin(home, "/robots.txt"), headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
        lines = response.text.splitlines() if response.status_code == 200 else []
    except requests.RequestException:
        lines = []
    robots.parse(lines)  # No robots.txt means everything is allowed.
    return robots


def fetch(url: str) -> tuple[str, BeautifulSoup] | None:
    """Download one public HTML page. Returns (final_url, soup) or None."""
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
    except requests.RequestException:
        return None
    if response.status_code != 200 or "html" not in response.headers.get("Content-Type", ""):
        return None  # 401/403 = login or paywall, 404 = missing; skip all of them.
    return response.url, BeautifulSoup(response.text, "html.parser")


def page_text(soup: BeautifulSoup) -> str:
    """Visible text only: drop scripts, menus, headers and footers."""
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg", "form"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def looks_like_login(soup: BeautifulSoup) -> bool:
    return soup.find("input", attrs={"type": "password"}) is not None


def pick_links(soup: BeautifulSoup, base_url: str, home: str) -> list[str]:
    """Rank same-site links by how many useful words they contain."""
    scored = {}
    for a in soup.find_all("a", href=True):
        url = urldefrag(urljoin(base_url, a["href"]))[0].rstrip("/")
        if not url.startswith("http") or not same_site(url, home):
            continue
        haystack = (urlparse(url).path + " " + a.get_text(" ")).lower()
        if url.lower().endswith(SKIP_EXTENSIONS) or any(w in haystack for w in SKIP_WORDS):
            continue
        score = sum(1 for w in USEFUL_WORDS if w in haystack)
        if score:
            scored[url] = max(score, scored.get(url, 0))
    ranked = sorted(scored, key=scored.get, reverse=True)
    # Spread the picks across site sections (e.g. /news, /careers) so we don't
    # read five pages from the same section: best link per section first.
    first, rest, sections = [], [], set()
    for url in ranked:
        section = urlparse(url).netloc + "/" + urlparse(url).path.strip("/").split("/")[0]
        (rest if section in sections else first).append(url)
        sections.add(section)
    return first + rest


def pick_target_links(candidates: list[str]) -> list[str]:
    """Best link for each target page (about, careers, engineering blog), then the rest.
    Only the last part of the path counts, so /company/news/... isn't mistaken for About."""
    picked = []
    for words in TARGET_PAGES.values():
        for url in candidates:
            last = urlparse(url).path.lower().rstrip("/").rsplit("/", 1)[-1]
            if url not in picked and any(w in last for w in words):
                picked.append(url)
                break
    return picked + [u for u in candidates if u not in picked]


def read_website(website: str, max_pages: int = MAX_PAGES, notes: list[str] | None = None) -> list[Page]:
    """Return up to max_pages public pages from the company's website.
    If `notes` is given, a friendly line is added for each page we couldn't read."""
    notes = notes if notes is not None else []
    home = normalise_website(website)
    robots = load_robots(home)
    if not robots.can_fetch(USER_AGENT, home):
        raise ValueError(f"robots.txt does not allow reading {home}")

    first = fetch(home)
    if first is None:
        raise ValueError(f"Could not open {home} (it may be down, blocked or not public).")
    home_url, home_soup = first

    candidates = pick_target_links(pick_links(home_soup, home_url, home_url))  # Before stripping menus.
    pages, seen = [], {home_url.rstrip("/")}
    if not looks_like_login(home_soup):
        title = home_soup.title.get_text(strip=True) if home_soup.title else home_url
        pages.append(Page(home_url, title, page_text(home_soup)[:MAX_CHARS_PER_PAGE]))

    for url in candidates:
        if len(pages) >= max_pages:
            break
        if url in seen:
            continue
        if not robots.can_fetch(USER_AGENT, url):
            notes.append(f"Skipped {url}: the site's robots.txt asks readers not to open it.")
            continue
        seen.add(url)
        result = fetch(url)
        if result is None:
            notes.append(f"Couldn't read {url}: it didn't open within {TIMEOUT_SECONDS} s, or isn't public.")
            continue
        final_url, soup = result
        if looks_like_login(soup):
            notes.append(f"Skipped {url}: it asks for a login.")
            continue
        if final_url.rstrip("/") in seen - {url}:
            continue
        seen.add(final_url.rstrip("/"))
        title = soup.title.get_text(strip=True) if soup.title else final_url
        text = page_text(soup)
        if len(text) >= MIN_CHARS:
            pages.append(Page(final_url, title, text[:MAX_CHARS_PER_PAGE]))
        else:
            notes.append(f"Skipped {url}: almost no text (it probably needs JavaScript).")

    return [p for p in pages if len(p.text) >= MIN_CHARS]


def blocked_site(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == b or host.endswith("." + b) or (b.endswith(".") and b in host) for b in BLOCKED_SITES)


def read_extra_pages(urls: list[str]) -> tuple[list[Page], list[str]]:
    """Read the extra links the user pasted. Returns (pages, reasons for any skipped)."""
    pages, skipped = [], []
    for url in urls[:MAX_EXTRA_PAGES]:
        url = normalise_website(url)
        if blocked_site(url):
            skipped.append(f"{url}: this site needs a login, so it isn't read")
            continue
        if not load_robots(url).can_fetch(USER_AGENT, url):
            skipped.append(f"{url}: the site's robots.txt doesn't allow reading it")
            continue
        result = fetch(url)
        if result is None:
            skipped.append(f"{url}: couldn't open it (not public, missing or not a web page)")
            continue
        final_url, soup = result
        text = page_text(soup)
        if looks_like_login(soup) or len(text) < MIN_CHARS:
            skipped.append(f"{url}: no readable text (login page or needs JavaScript)")
            continue
        title = soup.title.get_text(strip=True) if soup.title else final_url
        pages.append(Page(final_url, title, text[:MAX_CHARS_PER_PAGE]))
    for url in urls[MAX_EXTRA_PAGES:]:
        skipped.append(f"{url}: only the first {MAX_EXTRA_PAGES} extra links are read")
    return pages, skipped
