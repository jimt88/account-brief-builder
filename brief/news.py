"""Recent news about the company from GDELT (free, no key, no login).

GDELT's DOC 2.0 API lists news articles worldwide. News is event evidence ("Acme
launches AI assistant", "Acme acquires X"): dated and specific.

  1. ONE GDELT call per brief: the exact company name in quotes, last 3 months,
     newest first. For short or common names the website's name is added too.
  2. Keep only articles whose TITLE contains the company name, drop duplicate titles,
     keep at most MAX_NEWS_ARTICLES (English first, other languages kept).
  3. Open each article with the same rules as the website reader (robots.txt, no
     logins or paywalls, skip pages that need JavaScript), as Pages with
     source_type="news" and the date GDELT first saw the article.

If GDELT is slow, busy or down, the brief carries on without news.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

import requests

from brief.website import (MAX_CHARS_PER_PAGE, MIN_CHARS, USER_AGENT, Page, blocked_site, cached_page,
                           fetch, load_robots, looks_like_login, page_text, store_page)

GDELT_URL = ("https://api.gdeltproject.org/api/v2/doc/doc?query={query}&mode=ArtList&format=json"
             "&maxrecords=50&timespan=3months&sort=DateDesc")
GDELT_TIMEOUT_SECONDS = 20
MAX_NEWS_ARTICLES = 8
SHORT_NAME_CHARS = 5  # "N26", "DSV": too short to search on their own.
MAX_WORKERS = 4
NEWS_CACHE_PREFIX = "news:"  # Cache key prefix: a news article and the same URL read as a website stay apart.


def gdelt_query(company: str, website: str = "") -> str:
    """The GDELT search: the exact name in quotes, plus the domain stem for short names
    (e.g. "DSV" dsv) so unrelated uses of the same letters are less likely."""
    query = f'"{company}"' if " " in company or len(company) >= SHORT_NAME_CHARS else company
    host = urlparse(website if "//" in website else f"https://{website}").netloc.lower().removeprefix("www.")
    stem = host.split(".")[0] if host else ""
    if len(company) < SHORT_NAME_CHARS and stem and stem != company.lower():
        query += f" {stem}"
    return query


def parse_seendate(value: str) -> datetime | None:
    """GDELT dates look like 20261120T101500Z."""
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def name_in_title(company: str, title: str) -> bool:
    """Whole-word, case-insensitive match, so "Allegro" doesn't match "allegrou"."""
    return re.search(rf"(?<!\w){re.escape(company)}(?!\w)", title, re.IGNORECASE) is not None


def pick_articles(data: dict, company: str) -> list[dict]:
    """Articles whose title names the company, one per title, English first, newest first."""
    seen, picked = set(), []
    for article in data.get("articles") or []:
        title = " ".join((article.get("title") or "").split())
        key = re.sub(r"\W+", " ", title.lower()).strip()
        if not article.get("url") or not name_in_title(company, title) or key in seen:
            continue
        seen.add(key)
        picked.append({"url": article["url"], "title": title, "language": article.get("language", ""),
                       "domain": article.get("domain", ""), "published_at": parse_seendate(article.get("seendate"))})
    picked.sort(key=lambda a: a["language"] != "English")  # Stable: keeps newest first within each group.
    return picked[:MAX_NEWS_ARTICLES]


def search_gdelt(company: str, website: str = "") -> dict:
    """One GDELT call. Raises ValueError with a friendly reason if it fails."""
    url = GDELT_URL.format(query=quote_plus(gdelt_query(company, website)))
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=GDELT_TIMEOUT_SECONDS)
    except requests.RequestException:
        raise ValueError(f"GDELT news search didn't answer within {GDELT_TIMEOUT_SECONDS} s") from None
    if response.status_code == 429:
        raise ValueError("GDELT news search is busy (one request every 5 seconds)")
    if response.status_code != 200:
        raise ValueError(f"GDELT news search failed ({response.status_code})")
    try:
        return response.json() if response.text.strip() else {}
    except ValueError:
        raise ValueError("GDELT news search returned something unexpected") from None


def read_article(article: dict, cache=None) -> Page | None:
    """Open one article with the website reader's rules. None if it can't be read."""
    url = article["url"]
    hit = cached_page(cache, NEWS_CACHE_PREFIX + url)  # Own cache key: website readers never mix with news.
    if hit:
        return hit[0]
    if blocked_site(url) or not load_robots(url).can_fetch(USER_AGENT, url):
        return None
    result = fetch(url)  # 401/403 (paywalls, logins) and non-HTML return None.
    if result is None:
        return None
    final_url, soup = result
    if looks_like_login(soup):  # Before page_text(), which removes forms.
        return None
    text = page_text(soup)
    if len(text) < MIN_CHARS:
        return None
    page = Page(final_url, article["title"], text[:MAX_CHARS_PER_PAGE], source_type="news",
                published_at=article["published_at"], source_label=f"News: {article['domain']}")
    store_page(cache, NEWS_CACHE_PREFIX + url, page)
    return page


def read_news(company: str, website: str = "", cache=None) -> tuple[list[Page], dict, str | None]:
    """(news pages, news_summary {articles_found, articles_read}, note)."""
    try:
        articles = pick_articles(search_gdelt(company, website), company)
    except ValueError as exc:
        return [], {}, f"Skipped news: {exc}."
    with ThreadPoolExecutor(MAX_WORKERS) as pool:
        pages = [p for p in pool.map(lambda a: read_article(a, cache), articles) if p]
    summary = {"articles_found": len(articles), "articles_read": len(pages)}
    note = None if articles else "No recent news articles with the company's name in the headline."
    return pages, summary, note
