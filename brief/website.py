"""Read a company's public website: the home page plus a few useful pages.

Rules we follow:
  - public pages only: we never log in, and we skip pages that ask for a login
    or return 401/403 (paywalls, members-only areas),
  - we respect robots.txt,
  - we stay on the company's own domain (no LinkedIn or other sites),
  - we stay on the company's registrable domain (careers., investors. and other
    subdomains are fine) and never follow links to other sites,
  - we read at most MAX_PAGES pages: the home page, then the best-scoring URLs from
    the sitemap (careers, engineering, news, investors, strategy, ...), or the best
    home-page links if there is no sitemap. Each page has a TIMEOUT_SECONDS limit,
    4 are read at a time, and reading stops after CRAWL_TIME_BUDGET_S.

You can also pass up to MAX_EXTRA_PAGES extra links (a press release, a careers
page, a news article). These may be on other sites, but the same rules apply,
and sites that need a login (LinkedIn, Facebook, ...) are refused.

Optional page cache: pass `cache` (the adr_brief.page_cache collection) and a page
read in the last 24 hours is taken from MongoDB instead of being downloaded again.
A TTL index deletes cached pages after a day (scripts/setup_page_cache.py). Pages are
only cached after robots.txt allowed them, and robots.txt is still checked before any
real download.
"""

import ipaddress
import logging
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from pymongo.errors import PyMongoError

USER_AGENT = "Mozilla/5.0 (compatible; account-brief-builder/0.1)"
MAX_PAGES = 30  # Home page plus the best-scoring pages from the sitemap (or links).
CRAWL_TIME_BUDGET_S = 60  # Stop reading the site after this; keep what was read.
CRAWL_WORKERS = 4  # Pages downloaded at the same time.
MAX_SITEMAPS = 5  # Sitemap files (or sitemap indexes) read per site.
MAX_SITEMAP_URLS = 5_000
MAX_SITEMAP_BYTES = 10_000_000  # A sitemap is the site's own file; refuse anything bigger.
# Words in a sitemap URL's path that suggest useful signals (one point each).
SITEMAP_KEYWORDS = ["careers", "jobs", "engineering", "tech", "technology", "blog", "news", "press",
                    "media", "investors", "annual-report", "results", "strategy", "digital", "data",
                    "cloud", "ai", "platform", "about"]
RECENT_BONUS = 2  # Extra points for a sitemap lastmod in the last 12 months.
TIMEOUT_SECONDS = 10
MAX_REDIRECTS = 5
# The pages we look for after the home page, in order, and words that identify them.
TARGET_PAGES = {
    "about": ["about", "who-we-are", "company", "ueber-uns", "unternehmen", "a-propos",
              "entreprise", "sobre", "empresa", "chi-siamo", "azienda", "over-ons"],
    "careers": ["careers", "jobs", "join", "karriere", "carrieres", "empleo", "lavora"],
    "engineering blog": ["engineering", "tech-blog", "technology", "developers", "blog", "news", "press"],
}
MAX_CHARS_PER_PAGE = 8_000  # Keeps the Gemini prompt small (free tier).
MIN_CHARS = 200  # Less text than this usually means a JavaScript-only page.
LOGIN_WALL_CHARS = 1_500  # A password box with less text than this is a login wall, not an article.

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
PAGE_CACHE_COLLECTION = "page_cache"
CACHE_SECONDS = 24 * 60 * 60  # TTL: cached pages are deleted about a day after fetched_at.

SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".mp4", ".xml", ".svg")


@dataclass
class Page:
    url: str
    title: str
    text: str
    cached: bool = False  # True if it came from adr_brief.page_cache, not the web.
    source_type: str = "website"  # website, extra_link, job or news.
    published_at: datetime | None = None  # Known for job ads and news, not web pages.
    source_label: str = ""  # E.g. "Greenhouse job: Senior Backend Engineer".
    found_links: list[str] = field(default_factory=list)  # Job-board/GitHub links on the page.
    jobs_summary: dict = field(default_factory=dict)  # Only on a cached job board.


# ---- Page cache (adr_brief.page_cache) ----------------------------------------
# One document per requested URL: {url, final_url, title, text, fetched_at}. The home
# page's document also keeps `links`: the pages read after it last time, so a cached
# re-run needs no downloads at all.

def cached_page(cache, url: str) -> tuple[Page, list[str] | None] | None:
    """The cached page for url and its saved links (None if it wasn't read as a home
    page), or None. Pages older than CACHE_SECONDS are ignored even if the TTL monitor
    hasn't deleted them yet."""
    if cache is None:
        return None
    fresh_after = datetime.now(timezone.utc) - timedelta(seconds=CACHE_SECONDS)
    doc = cache.find_one({"url": url, "fetched_at": {"$gte": fresh_after}})
    if doc is None:
        return None
    page = Page(doc["final_url"], doc["title"], doc["text"], cached=True,
                source_type=doc.get("source_type", "website"), published_at=doc.get("published_at"),
                source_label=doc.get("source_label", ""), found_links=doc.get("found_links", []),
                jobs_summary=doc.get("jobs_summary", {}))
    return page, doc.get("links")


def store_page(cache, url: str, page: Page, links: list[str] | None = None) -> None:
    """Save (or refresh) one page in the cache. The TTL clock restarts at fetched_at."""
    if cache is None:
        return
    doc = {"url": url, "final_url": page.url, "title": page.title, "text": page.text,
           "fetched_at": datetime.now(timezone.utc), "source_type": page.source_type,
           "published_at": page.published_at, "source_label": page.source_label,
           "found_links": page.found_links, "jobs_summary": page.jobs_summary}
    update = {"$set": doc}
    if links is None:
        update["$unset"] = {"links": ""}  # Old links would be stale; re-read as a home page next time.
    else:
        doc["links"] = links
    try:
        cache.update_one({"url": url}, update, upsert=True)
    except PyMongoError as exc:  # The cache is a speed-up: never lose a page because of it.
        logging.getLogger(__name__).warning("Couldn't cache %s: %s", url, exc)


def normalise_website(website: str) -> str:
    """Turn 'example.com' into 'https://example.com/'."""
    website = website.strip()
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    return website


SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu", "ltd", "plc"}


def registrable_domain(host: str) -> str:
    """'careers.bankofireland.com' -> 'bankofireland.com'; 'shop.tesco.co.uk' -> 'tesco.co.uk'."""
    parts = host.lower().split(":")[0].strip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_site(url: str, home: str) -> bool:
    """True if url is on the same registrable domain as home (any subdomain, e.g.
    careers. or investors.), never another site."""
    host, home_host = urlparse(url).netloc, urlparse(home).netloc
    return bool(host) and registrable_domain(host) == registrable_domain(home_host)


def load_robots(home: str) -> RobotFileParser:
    robots = RobotFileParser()
    try:
        response = public_get(urljoin(home, "/robots.txt"))
        lines = response.text.splitlines() if response is not None and response.status_code == 200 else []
    except requests.RequestException:
        lines = []
    robots.parse(lines)  # No robots.txt means everything is allowed.
    return robots


def private_host(url: str) -> bool:
    """True if url points at this machine or a private network (e.g. 127.0.0.1, 10.x,
    169.254.x), directly or through DNS. Names that don't resolve are left to requests."""
    host = urlparse(url).hostname or ""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    return any(not ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


def public_get(url: str, timeout: float = TIMEOUT_SECONDS, max_bytes: int | None = None):
    """requests.get that follows up to MAX_REDIRECTS redirects itself and refuses any
    hop to a non-public address, so a page can't redirect the app to internal services.
    With max_bytes, the body is streamed and anything larger is refused (None).
    Returns the response (its .url is the final URL), or None."""
    cookies = requests.cookies.RequestsCookieJar()  # Some sites set a cookie on the redirect.
    for _ in range(MAX_REDIRECTS + 1):
        if urlparse(url).scheme.lower() not in ("http", "https") or private_host(url):
            return None
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                                    allow_redirects=False, cookies=cookies, stream=max_bytes is not None)
        except requests.RequestException:
            return None
        location = getattr(response, "headers", {}).get("Location")
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            response.url = url
            return response if max_bytes is None else read_capped(response, max_bytes)
        cookies.update(getattr(response, "cookies", None) or {})
        try:
            location = location.encode("latin1").decode("utf8")  # As requests does: headers arrive as latin-1.
        except UnicodeError:
            pass
        url = urljoin(url, location)
    return None


def read_capped(response, max_bytes: int):
    """Read a streamed response's body, or None (connection closed) if it's over max_bytes."""
    body = b""
    try:
        for part in response.iter_content(64 * 1024):
            body += part
            if len(body) > max_bytes:
                response.close()
                return None
    except requests.RequestException:
        return None
    response._content = body  # .text and .content now read this body.
    return response


def fetch(url: str) -> tuple[str, BeautifulSoup] | None:
    """Download one public HTML page. Returns (final_url, soup) or None."""
    response = public_get(url)
    if response is None:
        return None
    if response.status_code != 200 or "html" not in response.headers.get("Content-Type", ""):
        return None  # 401/403 = login or paywall, 404 = missing; skip all of them.
    return response.url, BeautifulSoup(response.text, "html.parser")


# Off-site links worth following up: job boards (brief/jobs.py) and GitHub (later).
OUTSIDE_LINK_HOSTS = re.compile(
    r"(^|\.)(greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com|workable\.com|myworkdayjobs\.com"
    r"|taleo\.net|github\.com)$|\.jobs\.personio\.(de|com)$|successfactors\.")


def outside_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Job-board and GitHub links in <a href>, <iframe src> and <script src>.
    Call before page_text(), which removes scripts."""
    found = []
    for tag, attr in (("a", "href"), ("iframe", "src"), ("script", "src")):
        for element in soup.find_all(tag, **{attr: True}):
            url = urljoin(base_url, element[attr])
            if OUTSIDE_LINK_HOSTS.search(urlparse(url).netloc.lower()) and url not in found:
                found.append(url)
    return found[:50]


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


def parse_sitemap(xml_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """([(page url, lastmod)], [child sitemap urls]) from a sitemap or sitemap index."""
    try:
        root = ElementTree.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ElementTree.ParseError:
        return [], []
    pages, children = [], []
    for entry in root:
        kind = entry.tag.rsplit("}", 1)[-1]
        fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in entry}
        if not fields.get("loc"):
            continue
        if kind == "sitemap":
            children.append(fields["loc"])
        elif kind == "url":
            pages.append((fields["loc"], fields.get("lastmod", "")))
    return pages, children


def score_url(url: str, lastmod: str = "", now: datetime | None = None) -> int:
    """Points for useful words in the URL path, plus RECENT_BONUS if lastmod is in the
    last 12 months. 0 for legal/cookie/privacy/login/cart pages, PDFs and other files."""
    path = urlparse(url).path.lower()
    if path.endswith(SKIP_EXTENSIONS) or any(w in path for w in SKIP_WORDS):
        return 0
    tokens = set(re.split(r"[^a-z0-9]+", path))
    points = sum(1 for k in SITEMAP_KEYWORDS if (k in path if "-" in k else k in tokens))
    if points and lastmod:
        try:
            modified = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
            modified = modified if modified.tzinfo else modified.replace(tzinfo=timezone.utc)
            if (now or datetime.now(timezone.utc)) - modified <= timedelta(days=365):
                points += RECENT_BONUS
        except ValueError:
            pass
    return points


def sitemap_links(home: str, robots: RobotFileParser, deadline: float) -> list[str]:
    """Same-site URLs from the sitemaps named in robots.txt and /sitemap.xml (max
    MAX_SITEMAPS files, sitemap indexes followed), best score first."""
    queue = list(robots.site_maps() or []) + [urljoin(home, "/sitemap.xml")]
    seen, entries, files = set(), {}, 0
    while queue and files < MAX_SITEMAPS and time.monotonic() < deadline:
        sitemap = queue.pop(0)
        if sitemap in seen or not same_site(sitemap, home) or sitemap.lower().endswith(".gz"):
            continue
        seen.add(sitemap)
        files += 1
        response = public_get(sitemap, max_bytes=MAX_SITEMAP_BYTES,
                              timeout=min(TIMEOUT_SECONDS, max(1.0, deadline - time.monotonic())))
        if response is None or response.status_code != 200:
            continue
        pages, children = parse_sitemap(response.text)
        queue += children
        for url, lastmod in pages[:MAX_SITEMAP_URLS]:
            if same_site(url, home):
                entries.setdefault(urldefrag(url)[0].rstrip("/"), lastmod)
    now = datetime.now(timezone.utc)
    scored = {url: score_url(url, lastmod, now) for url, lastmod in entries.items()}
    return sorted((u for u, points in scored.items() if points),
                  key=lambda u: (-scored[u], len(urlparse(u).path)))


def read_website(website: str, max_pages: int = MAX_PAGES, notes: list[str] | None = None,
                 cache=None) -> list[Page]:
    """Return up to max_pages public pages from the company's website: the home page,
    then the best-scoring sitemap URLs (or, with no sitemap, the best links on the home
    page). Pages are read 4 at a time and reading stops after CRAWL_TIME_BUDGET_S.
    If `notes` is given, a friendly line is added for each page we couldn't read.
    If `cache` is given, pages read in the last 24 hours come from MongoDB."""
    notes = notes if notes is not None else []
    home = normalise_website(website)
    deadline = time.monotonic() + CRAWL_TIME_BUDGET_S
    robots_by_host, robots_lock = {}, threading.Lock()

    def robots(url: str) -> RobotFileParser:
        """robots.txt of url's host (subdomains have their own), downloaded once."""
        host = urlparse(url).netloc.lower()
        with robots_lock:
            if host not in robots_by_host:
                robots_by_host[host] = load_robots(url)
            return robots_by_host[host]

    hit = cached_page(cache, home)
    if hit and hit[1] is not None:  # Cached as a home page, so we know its sub-pages.
        home_page, candidates = hit
        home_url, pages = home_page.url, [home_page]
    else:
        if not robots(home).can_fetch(USER_AGENT, home):
            raise ValueError(f"robots.txt does not allow reading {home}")
        first = fetch(home)
        if first is None:
            raise ValueError(f"Could not open {home} (it may be down, blocked or not public).")
        home_url, home_soup = first
        candidates = (sitemap_links(home_url, robots(home), deadline)
                      or pick_target_links(pick_links(home_soup, home_url, home_url)))  # Before stripping menus.
        pages = []
        if not looks_like_login(home_soup):
            title = home_soup.title.get_text(strip=True) if home_soup.title else home_url
            links = outside_links(home_soup, home_url)
            pages.append(Page(home_url, title, page_text(home_soup)[:MAX_CHARS_PER_PAGE], found_links=links))

    def read_one(url: str):
        """(page, cached?, job-board links from a thin page) for one candidate, or None."""
        hit = cached_page(cache, url)
        if hit:
            return hit[0], True, []
        if not robots(url).can_fetch(USER_AGENT, url):
            notes.append(f"Skipped {url}: the site's robots.txt asks readers not to open it.")
            return None
        result = fetch(url)
        if result is None:
            notes.append(f"Couldn't read {url}: it didn't open within {TIMEOUT_SECONDS} s, or isn't public.")
            return None
        final_url, soup = result
        if looks_like_login(soup):
            notes.append(f"Skipped {url}: it asks for a login.")
            return None
        title = soup.title.get_text(strip=True) if soup.title else final_url
        links = outside_links(soup, final_url)
        text = page_text(soup)
        if len(text) < MIN_CHARS:
            notes.append(f"Skipped {url}: almost no text (it probably needs JavaScript).")
            return None, False, links  # E.g. a careers page that only embeds a job board.
        return Page(final_url, title, text[:MAX_CHARS_PER_PAGE], found_links=links), False, []

    seen, read_links, thin_page_links = {home_url.rstrip("/")}, [], []
    todo = [u for u in dict.fromkeys(candidates) if u.rstrip("/") not in seen]
    out_of_time = False
    while todo and len(pages) < max_pages and not out_of_time:
        if time.monotonic() >= deadline:
            out_of_time = True
            break
        wave, todo = todo[:max_pages - len(pages)], todo[max_pages - len(pages):]
        pool = ThreadPoolExecutor(CRAWL_WORKERS)
        futures = {url: pool.submit(read_one, url) for url in wave}
        done, _ = wait(futures.values(), timeout=max(0.0, deadline - time.monotonic()))
        pool.shutdown(wait=False, cancel_futures=True)
        out_of_time = len(done) < len(futures)
        for url, future in futures.items():  # Keep the candidates' order (best first).
            if future not in done or future.exception() or future.result() is None:
                continue
            page, cached, links = future.result()
            thin_page_links += links
            if page is None or page.url.rstrip("/") in seen or len(pages) >= max_pages:
                continue
            seen.add(page.url.rstrip("/"))
            pages.append(page)
            read_links.append(url)
            if not cached:
                store_page(cache, url, page)
    if out_of_time:
        notes.append(f"Stopped reading the site after {CRAWL_TIME_BUDGET_S} s; kept {len(pages)} pages.")

    # Keep job-board links from thin pages on the home page (and in its cache entry).
    if pages and thin_page_links:
        pages[0].found_links = list(dict.fromkeys(pages[0].found_links + thin_page_links))

    # Cache the home page with the links that worked, so next time needs no downloads.
    # Not after a timeout: the next brief should try the pages we didn't get to.
    if (pages and not pages[0].cached and pages[0].url == home_url and len(pages[0].text) >= MIN_CHARS
            and not out_of_time):
        store_page(cache, home, pages[0], links=read_links)
    return [p for p in pages if len(p.text) >= MIN_CHARS]


def blocked_site(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == b or host.endswith("." + b) or (b.endswith(".") and b in host) for b in BLOCKED_SITES)


def read_extra_pages(urls: list[str], cache=None,
                     links_out: list[str] | None = None) -> tuple[list[Page], list[str]]:
    """Read the extra links the user pasted. Returns (pages, reasons for any skipped).
    If `cache` is given, links read in the last 24 hours come from MongoDB. Job-board
    links found on pages with too little text are added to `links_out`."""
    pages, skipped = [], []
    for url in urls[:MAX_EXTRA_PAGES]:
        url = normalise_website(url)
        if blocked_site(url):
            skipped.append(f"{url}: this site needs a login, so it isn't read")
            continue
        hit = cached_page(cache, url)
        if hit:
            hit[0].source_type, hit[0].published_at, hit[0].source_label = "extra_link", None, ""
            pages.append(hit[0])
            continue
        if not load_robots(url).can_fetch(USER_AGENT, url):
            skipped.append(f"{url}: the site's robots.txt doesn't allow reading it")
            continue
        result = fetch(url)
        if result is None:
            skipped.append(f"{url}: couldn't open it (not public, missing or not a web page)")
            continue
        final_url, soup = result
        links = outside_links(soup, final_url)
        login = looks_like_login(soup)  # Before page_text(), which removes forms.
        text = page_text(soup)
        if (login and len(text) < LOGIN_WALL_CHARS) or len(text) < MIN_CHARS:
            if links_out is not None:
                links_out += links
            skipped.append(f"{url}: no readable text (login page or needs JavaScript)")
            continue
        title = soup.title.get_text(strip=True) if soup.title else final_url
        pages.append(Page(final_url, title, text[:MAX_CHARS_PER_PAGE], source_type="extra_link", found_links=links))
        store_page(cache, url, pages[-1])
    for url in urls[MAX_EXTRA_PAGES:]:
        skipped.append(f"{url}: only the first {MAX_EXTRA_PAGES} extra links are read")
    return pages, skipped
