"""Account Brief Builder - the Streamlit web app.

Pages:
  New account   type a company + website; the app reads its public pages, finds
                signals backed by exact quotes, scores the account, matches a
                MongoDB customer story with Atlas Vector Search, writes a brief
                and saves it in adr_brief.accounts.
  Pipeline      every saved account ranked by score (a MongoDB aggregation
                pipeline), with a status you can change and a CSV export.
  Brief         one account: brief, score breakdown, every quote with its link,
                and the matched story.
  How it works  the data flow and the MongoDB features used, and why.

Run locally:   streamlit run app.py
Settings come from st.secrets on Streamlit Community Cloud, or from .env locally:
MONGODB_URI, GEMINI_API_KEY and APP_PASSCODE.
"""

import hmac
import html
import json
import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from google import genai
from pymongo import MongoClient

from brief.accounts import DASHBOARD_PIPELINE, dashboard, pipeline_csv, pipeline_rows, set_status
from brief.build import ACCOUNTS_COLLECTION, STATUSES
from brief.errors import friendly_error
from brief.pipeline import BriefError, build_brief
from brief.signals import SIGNAL_LABELS, SignalType
from brief.story_search import DB_NAME

st.set_page_config(page_title="Account Brief Builder", page_icon="🍃", layout="centered")

PAGES = ["New account", "Pipeline", "Dashboard", "Brief", "How it works"]
PAGE_ICONS = {"New account": ":material/add_circle:", "Pipeline": ":material/leaderboard:",
              "Dashboard": ":material/monitoring:", "Brief": ":material/description:",
              "How it works": ":material/info:"}

# Small extra styling on top of the theme in .streamlit/config.toml. It only targets
# our own "abb-" classes, so Streamlit upgrades can't break it.
CSS = """
<style>
.abb-hero {background: linear-gradient(135deg, #001E2B 0%, #023430 100%); border-radius: 16px;
           padding: 20px 24px; display: flex; gap: 16px; align-items: center; margin-bottom: 4px;}
.abb-title {color: #FFFFFF; font-size: clamp(1.4rem, 4.5vw, 2rem); font-weight: 600; line-height: 1.2;}
.abb-title span {color: #00ED64;}
.abb-sub {color: #C1CCC6; font-size: 0.95rem; margin-top: 4px;}
.abb-callout {border-left: 4px solid #00ED64; background: #E3FCF7; border-radius: 10px;
              padding: 12px 16px; color: #001E2B; margin-bottom: 12px;}
.abb-callout .abb-label {font-size: 0.75rem; font-weight: 600; letter-spacing: 0.06em;
                         text-transform: uppercase; color: #00684A; margin-bottom: 4px;}
.abb-callout .abb-text {font-size: 1.1rem; font-weight: 500;}
.abb-quote {border-left: 3px solid #00ED64; padding-left: 12px; color: #3D4F58; font-style: italic;}
.abb-flow {display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px;
            counter-reset: step;}
.abb-step {border: 1px solid #C1CCC6; border-radius: 10px; padding: 10px 12px; background: #FFFFFF;
           counter-increment: step; font-size: 0.85rem; color: #3D4F58;}
.abb-step b {display: block; color: #001E2B; font-size: 0.9rem; margin-bottom: 2px;}
.abb-step b::before {content: counter(step) ". "; color: #00684A;}
.abb-step span {display: block;}
.abb-atlas {background: #E3FCF7; border-color: #00A35C;}
.abb-legend {font-size: 0.8rem; color: #5C6C75; margin-top: 6px; display: flex; align-items: center; gap: 6px;}
.abb-dot {width: 12px; height: 12px; border-radius: 3px; background: #E3FCF7; border: 1px solid #00A35C;}
.abb-kicker {font-size: 0.75rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase;
             color: #00684A; margin: 8px 0 2px;}
</style>
"""


def get_setting(name: str) -> str | None:
    """Read a setting from Streamlit secrets (cloud) or the environment / .env (local)."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # No secrets file at all (normal when running locally).
        pass
    load_dotenv()
    return os.getenv(name)


def passcode_ok() -> bool:
    """Show a passcode box until the right APP_PASSCODE is entered."""
    expected = get_setting("APP_PASSCODE")
    if not expected:
        st.error("APP_PASSCODE is not set, so the app is locked. Add it to the app's secrets.")
        return False
    if st.session_state.get("unlocked"):
        return True
    with st.form("passcode"):
        st.markdown("**:material/lock: This demo is passcode protected**")
        entered = st.text_input("Passcode", type="password")
        submitted = st.form_submit_button("Unlock", type="primary", width="stretch", icon=":material/lock_open:")
    # compare_digest avoids leaking how many characters were right.
    if submitted and hmac.compare_digest(entered.encode(), expected.encode()):
        st.session_state.unlocked = True
        st.rerun()
    if submitted:
        st.error("Wrong passcode.")
    return False


@st.cache_resource
def get_db():
    """One MongoDB client for the whole app (Streamlit keeps it between runs)."""
    client = MongoClient(get_setting("MONGODB_URI"), serverSelectionTimeoutMS=10_000, appname="account-brief")
    return client[DB_NAME]


@st.cache_resource
def get_gemini() -> genai.Client:
    return genai.Client(api_key=get_setting("GEMINI_API_KEY"))


def accounts():
    return get_db()[ACCOUNTS_COLLECTION]


def md(text: str) -> str:
    """Make page text safe for st.markdown: "$" would otherwise start a maths formula."""
    return (text or "").replace("$", "\\$").replace("\n", "  \n")


def open_brief(domain: str) -> None:
    """Button callback: show this account on the Brief page."""
    st.session_state.selected = domain
    st.session_state.page = "Brief"


def label(signal_type: str) -> str:
    return SIGNAL_LABELS.get(SignalType(signal_type), signal_type)


def hero() -> None:
    """The dark MongoDB-green banner at the top of every page."""
    st.html(CSS + """
<div class="abb-hero">
  <div>
    <div class="abb-title">Account Brief <span>Builder</span></div>
    <div class="abb-sub">Evidence-backed account briefs for MongoDB ADRs, powered by Atlas Vector Search</div>
  </div>
</div>""")


def callout(title: str, text: str) -> None:
    """A highlighted box, used for the value hook."""
    st.html(f'<div class="abb-callout"><div class="abb-label">{html.escape(title)}</div>'
            f'<div class="abb-text">{html.escape(text)}</div></div>')


def badge(text: str, colour: str, icon: str = "") -> str:
    """Markdown for a small coloured label; several fit on one line."""
    text = (text or "").replace("[", "(").replace("]", ")")
    return f":{colour}-badge[{icon} {md(text)}]".replace("[ ", "[")


def account_labels(rows: list[dict]) -> dict[str, str]:
    """Domain -> name for account pickers. Shared names get their domain added, because
    Streamlit tells options apart by their label."""
    names = [r["company"] for r in rows]
    return {r["domain"]: r["company"] if names.count(r["company"]) == 1 else f"{r['company']} ({r['domain']})"
            for r in rows}


def score_colour(score: int) -> str:
    return "green" if score >= 50 else "orange" if score >= 25 else "gray"


# ---- Brief (built from the saved account document) --------------------------


PASSAGE_ORIGINS = {"vector": "Vector Search", "keyword": "keyword scan", "both": "Vector Search + keywords",
                   "added": "added (home page or source mix)"}


SIGNAL_GROUPS = ["Jobs", "News", "GitHub", "Website"]


def signal_group(signal: dict) -> str:
    """Jobs, News, GitHub or Website (pasted extra links and undated old signals count as Website)."""
    return {"job": "Jobs", "news": "News", "github": "GitHub"}.get(signal.get("source_type") or "", "Website")


def show_passages(passages: dict | None) -> None:
    """The chunks Atlas chose for Gemini (brief/passages.py), if this brief used them."""
    if not passages or not passages.get("picked"):
        return
    picked = passages["picked"]
    with st.expander(f"Passages Atlas picked for Gemini ({len(picked)} of {passages['total']})"):
        if passages.get("note"):
            st.caption(passages["note"])
        rows = [{"Source": p["source_type"].replace("_", " ").title(),
                 "Date": p["published_at"].strftime("%Y-%m-%d") if p.get("published_at") else "",
                 "Picked by": PASSAGE_ORIGINS.get(p["origin"], p["origin"]),
                 "URL": p["url"]} for p in picked]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"URL": st.column_config.LinkColumn("URL")})


def brief_as_markdown(a: dict) -> str:
    b, s, p = a["brief"], a["matched_story"], a["profile"]
    lines = [
        f"# Account brief: {a['company']}",
        f"{p['industry']}, {p['hq_country'] or 'HQ not stated'}. {p['what_they_do']}",
        "", f"## Score: {a['score']}/100"] + [f"- +{x['points']} {x['reason']}" + (f" ({x['maths']})" if x.get("maths") else "")
                                              for x in a["score_breakdown"]] + [
        "", "## Why now"] + [f"- {x}" for x in b["why_now"]] + [
        "", "## Who to contact (hypothesis)"] + [f"- {x['side']}: {x['role']} - {x['why']}" for x in b["personas"]] + [
        "", "## Value hook", b["value_hook"],
        "", "## 30-second cold-call opener", b["cold_call_opener"],
        "", "## Email", f"**Subject:** {b['email']['subject']}", "", b["email"]["body"],
        "", "## Discovery questions"] + [f"{n}. {q}" for n, q in enumerate(b["discovery_questions"], start=1)] + [
        "", "## Evidence"] + [f'{n}. {label(x["type"])} - "{x["quote"]}" ({x["source_url"]})'
                              for n, x in enumerate(a["signals"], start=1)] + [
        "", "## Similar MongoDB customer story", f"{s['company']}: {s['summary']} ({s['url']})", f"Why: {s['why']}"]
    if a.get("warnings"):
        lines += ["", "## Check before using"] + [f"- {w}" for w in a["warnings"]]
    return "\n".join(lines) + "\n"


def show_brief(a: dict) -> None:
    b, s, p = a["brief"], a["matched_story"], a["profile"]
    st.header(a["company"], anchor=False)
    st.markdown(" ".join([badge(p["industry"], "green", ":material/domain:"),
                          badge(p["hq_country"] or "HQ not stated", "gray", ":material/location_on:"),
                          badge(a["status"], "blue", ":material/flag:")]
                         + ([badge("Existing MongoDB user: expansion play", "violet", ":material/trending_up:")]
                            if any(x["type"] == SignalType.MONGODB_IN_USE.value for x in a["signals"]) else [])))
    st.markdown(md(p["what_they_do"]))

    counts = {group: sum(1 for x in a["signals"] if signal_group(x) == group) for group in SIGNAL_GROUPS}
    with st.container(horizontal=True, wrap=True):
        st.metric("Score", f"{a['score']}/100", border=True)
        st.metric("Verified signals", len(a["signals"]), border=True,
                  help=" · ".join(f"{g} {n}" for g, n in counts.items() if n) or None)
        st.metric("Matched story", s["company"], border=True, help=f"Similarity {s['score']:.2f}")
    if a["score_breakdown"]:
        top = a["score_breakdown"][0]
        same_type = [x for x in a["signals"] if x["type"] == top.get("type")]
        # The line's evidence URL points at the signal that scored, not just the first of its type.
        quote = next((x for x in same_type if x["source_url"] == top.get("evidence")),
                     same_type[0] if same_type else None)
        st.markdown(f"**Top reason to call:** {md(top['reason'])}")
        if quote:
            st.html(f'<div class="abb-quote">"{html.escape(quote["quote"])}"</div>')

    if a.get("warnings"):
        st.warning("**Check before using:**\n" + "\n".join(f"- {md(w)}" for w in a["warnings"]))
    else:
        st.caption(":material/verified: Checks passed: lengths OK, every number is in the evidence, "
                   "no vendor claims.")

    outreach, evidence, story, details = st.tabs(
        [":material/campaign: Outreach", ":material/fact_check: Evidence",
         ":material/auto_stories: Story", ":material/history: Details"])

    with outreach:
        callout("Value hook", b["value_hook"])
        st.html('<div class="abb-kicker">Why now</div>')
        st.markdown("\n".join(f"- {md(x)}" for x in b["why_now"]))
        st.html('<div class="abb-kicker">Who to contact (hypotheses to check)</div>')
        for column, x in zip(st.columns(len(b["personas"]) or 1), b["personas"]):
            with column.container(border=True):
                st.markdown(f"**{x['side']}:** {md(x['role'])}")
                st.caption(x["why"])
        call, email = st.tabs([":material/call: Cold-call opener (30 s)", ":material/mail: Email"])
        with call:
            st.code(b["cold_call_opener"], language=None, wrap_lines=True)
        with email:
            st.code(f"Subject: {b['email']['subject']}\n\n{b['email']['body']}", language=None, wrap_lines=True)
        st.caption("Copy with the icon at the top right of the text.")
        st.html('<div class="abb-kicker">Discovery questions</div>')
        st.markdown("\n".join(f"{n}. {md(q)}" for n, q in enumerate(b["discovery_questions"], start=1)))

    with evidence:
        st.subheader("Why this score", anchor=False)
        rows = [{"Reason": x["reason"], "Points": x["points"], "Maths": x.get("maths", ""),
                 "Evidence": x.get("evidence") or None} for x in a["score_breakdown"]]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                         column_config={"Evidence": st.column_config.LinkColumn("Evidence", display_text="link")})
            how = (" Each signal: base points × source strength (job ad or GitHub 1.0, news 0.9, extra link 0.8, "
                   "website 0.7) × recency (≤90 days 1.0, ≤180 0.75, ≤365 0.5, older 0.25, no date 0.8)."
                   if a.get("score_version", 1) >= 2 else " Scored with the older version 1 rules.")
            st.caption(f"Total {sum(r['Points'] for r in rows)} (capped at 100).{how}")
        else:
            st.write("No scoring signals found.")
        if a.get("jobs"):
            j = a["jobs"]
            st.markdown(f":material/work: **Hiring:** [{j['platform']}]({j['board_url']}): "
                        f"{j['total_open']} open roles, {j['technical_open']} technical")
        if a.get("news"):
            st.markdown(f":material/newspaper: **News (last 3 months, GDELT):** {a['news']['articles_found']} "
                        f"articles with the company in the headline, {a['news']['articles_read']} readable")
        if a.get("github"):
            g = a["github"]
            st.markdown(f":material/code: **GitHub:** [{g['org']}](https://github.com/{g['org']}): "
                        f"{g['repos_checked']} recent repos, {g['files_read']} dependency files read")
        st.subheader("Signals", anchor=False)
        st.caption("Every quote is word for word from the linked page.")
        numbered = list(enumerate(a["signals"], start=1))
        for group in SIGNAL_GROUPS:
            in_group = [(n, x) for n, x in numbered if signal_group(x) == group]
            if not in_group:
                continue
            st.html(f'<div class="abb-kicker">{group} ({len(in_group)})</div>')
            for n, x in in_group:
                with st.container(border=True):
                    used = " " + badge("used in the brief", "green", ":material/check:") if n in b["signals_used"] else ""
                    tech = " " + badge(x["technology"], "gray", ":material/database:") if x.get("technology") else ""
                    st.markdown(f"**{n}. {label(x['type'])}**{tech}{used}")
                    st.html(f'<div class="abb-quote">"{html.escape(x["quote"])}"</div>')
                    date = f" · {x['published_at'][:10]}" if x.get("published_at") else ""
                    st.markdown(f"[Source]({x['source_url']}){date}")
        show_passages(a.get("passages"))

    with story:
        with st.container(border=True):
            st.subheader(s["company"], anchor=False)
            how = f"Same industry: {s['industry_filter']}" if s.get("industry_filter") else "Closest in any industry"
            badges = [badge(how, "green", ":material/filter_alt:"), badge(f"similarity {s['score']:.2f}", "gray")]
            ranks = s.get("ranks") or {}
            if s.get("match_mode") == "hybrid":
                rank = lambda name: f"#{ranks[name]}" if ranks.get(name) else "not found"  # noqa: E731
                badges.append(badge(f"hybrid: meaning {rank('vector')}, keywords {rank('text')}", "blue",
                                    ":material/join:"))
            st.markdown(" ".join(badges))
            st.markdown(md(s["summary"]))
            st.markdown(f"**Why this story:** {md(s['why'])}")
            st.link_button("Read the story on mongodb.com", s["url"], icon=":material/open_in_new:")
        st.caption("Found with Atlas Vector Search over 48 public MongoDB customer stories.")

    with details:
        st.caption(f"Updated {a['updated_at']:%d %b %Y %H:%M} UTC")
        st.markdown(f"**Pages read ({len(a.get('pages_read', []))})**")
        st.markdown("\n".join(f"- {u}" for u in a.get("pages_read", [])))
        if a.get("history"):
            st.markdown(f"**Earlier versions ({len(a['history'])})**")
            for h in reversed(a["history"]):
                when = f"{h['updated_at']:%d %b %Y %H:%M}" if h.get("updated_at") else "earlier"
                st.markdown(f"- {when}: score {h.get('score')} - {md((h.get('brief') or {}).get('value_hook', ''))}")

    st.download_button("Download brief (Markdown)", brief_as_markdown(a), icon=":material/download:",
                       file_name=f"brief-{a['domain']}.md", mime="text/markdown", width="stretch")


# ---- Pages ------------------------------------------------------------------

def page_new_account() -> None:
    st.subheader("Research a new account", anchor=False)
    # A horizontal container keeps the three cards in one row on phones too.
    steps = st.container(horizontal=True, wrap=False)
    for icon, title, text in [
        (":material/travel_explore:", "Read", "Website, job ads, news and GitHub"),
        (":material/fact_check:", "Verify", "Signals with exact quotes"),
        (":material/hub:", "Match", "Closest MongoDB story"),
    ]:
        with steps.container(border=True):
            st.markdown(f"**{icon} {title}**")
            st.caption(text)
    with st.form("company"):
        name_col, site_col = st.columns(2)
        company = name_col.text_input("Company name", placeholder="Ocado Group")
        website = site_col.text_input("Website", placeholder="ocadogroup.com")
        extra = st.text_area(
            "Extra links (optional, up to 3, one per line)",
            placeholder="https://www.ocadogroup.com/newsroom",
            help="Public pages worth reading too, e.g. a press release, careers page or news "
            "article. Useful when the main site is thin or needs JavaScript. No LinkedIn or paywalls.",
        )
        go = st.form_submit_button("Build brief", type="primary", width="stretch", icon=":material/bolt:")
    if not go:
        return
    if not company.strip() or not website.strip():
        st.error("Please fill in both the company name and the website.")
        return
    with st.status("Building the brief ...", expanded=True) as status:
        try:
            result = build_brief(company.strip(), website.strip(), get_db(), get_gemini(),
                                 progress=st.write, extra_urls=extra.split())
            status.update(label=f"Brief ready: score {result.score}/100", state="complete", expanded=False)
        except BriefError as exc:
            status.update(label="Couldn't build the brief", state="error")
            st.error(str(exc))
            return
        except Exception as exc:
            status.update(label="Something went wrong", state="error")
            st.error(friendly_error(exc))
            return
    st.button("Open the brief", type="primary", width="stretch", icon=":material/description:",
              on_click=open_brief, args=(result.account["domain"],))


def show_dashboard(numbers: dict) -> None:
    """Metric tiles and bar charts, all from one $facet result."""
    totals = numbers["totals"]
    with st.container(horizontal=True, wrap=False):
        st.metric("Accounts", totals["accounts"], border=True)
        st.metric("Avg score", totals["avg_score"], border=True)
        st.metric("Score 50+", totals["score_50_plus"], border=True)
    charts = [
        ("By status", pd.DataFrame(numbers["by_status"]), "status", "count"),
        ("Avg score by industry", pd.DataFrame(numbers["by_industry"]), "industry", "avg_score"),
        ("Signals found", pd.DataFrame(numbers["signal_types"]), "signal", "count"),
        ("Signals by source", pd.DataFrame(numbers["by_source"]), "source", "count"),
        ("Technologies seen", pd.DataFrame(numbers["technologies"]), "technology", "count"),
    ]
    for (title, frame, x, y), column in zip(charts, st.columns(2) + st.columns(2) + st.columns(2)):
        with column:
            st.markdown(f"**{title}**")
            if frame.empty:
                st.caption("Nothing yet.")
            else:
                # Horizontal bars keep long labels readable on a phone.
                st.bar_chart(frame, x=x, y=y, horizontal=True, sort=False, height=240,
                             x_label="", y_label="", color="#00684A")
    st.caption("Every number and chart on this page comes from one MongoDB query: a `$facet` "
               "aggregation that runs all the groupings in a single trip to the database.")
    with st.expander("See the MongoDB query behind these charts"):
        st.code(json.dumps(DASHBOARD_PIPELINE, indent=2), language="json")


def page_pipeline() -> None:
    try:
        rows = pipeline_rows(accounts())
    except Exception as exc:
        st.error(friendly_error(exc))
        return
    if not rows:
        st.info("No accounts yet. Build one on the New account page.")
        return
    st.caption("Ranked by score with a MongoDB aggregation pipeline. Change a status and it's saved straight away.")
    table = pd.DataFrame([{
        "Company": r["company"], "Score": r["score"], "Top signal": r["top_signal"],
        "Status": r["status"], "Updated": r["updated_at"], "domain": r["domain"],
    } for r in rows])
    # The editor's key changes whenever the data changes, so an old edit can never be
    # replayed onto a different account after the ranking moves.
    version = "|".join(f"{r['domain']}:{r['status']}:{r['updated_at']}" for r in rows)
    edited = st.data_editor(
        table, hide_index=True, width="stretch", key=f"pipeline_{hash(version)}",
        height=35 * (len(table) + 1) + 3,  # Every row visible, no inner scrollbar.
        column_order=["Company", "Score", "Top signal", "Status", "Updated"],
        disabled=["Company", "Score", "Top signal", "Updated"],
        column_config={
            "Company": st.column_config.TextColumn(width=130),
            "Top signal": st.column_config.TextColumn(width="medium"),
            "Status": st.column_config.SelectboxColumn(options=STATUSES, required=True, width="small"),
            "Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d", width="small"),
            "Updated": st.column_config.DatetimeColumn(format="D MMM", width="small"),
        },
    )
    changed = edited[edited["Status"] != table["Status"]]
    if not changed.empty:
        for _, row in changed.iterrows():
            set_status(accounts(), row["domain"], row["Status"])
        st.session_state.saved_message = f"Saved status for {', '.join(changed['Company'])}"
        st.rerun()  # Reload from MongoDB so the table and the CSV show the saved status.
    if "saved_message" in st.session_state:
        st.toast(st.session_state.pop("saved_message"))

    st.download_button("Export CSV (Salesforce lead columns)", pipeline_csv(rows), icon=":material/download:",
                       file_name="pipeline.csv", mime="text/csv", width="stretch")


def page_dashboard() -> None:
    try:
        numbers = dashboard(accounts())
    except Exception as exc:
        st.error(friendly_error(exc))
        return
    show_dashboard(numbers)  # Shown even when empty: zero tiles and the query.


def page_brief() -> None:
    try:
        rows = pipeline_rows(accounts())
    except Exception as exc:
        st.error(friendly_error(exc))
        return
    if not rows:
        st.info("No accounts yet. Build one on the New account page.")
        return
    domains = [r["domain"] for r in rows]
    selected = st.session_state.get("selected")
    domain = st.selectbox("Account", domains, index=domains.index(selected) if selected in domains else 0,
                          format_func=account_labels(rows).get)
    st.session_state.selected = domain
    try:
        account = accounts().find_one({"domain": domain})
    except Exception as exc:
        st.error(friendly_error(exc))
        return
    if not account:
        st.info("That account was not found. Pick another one.")
        return
    show_brief(account)


# (title, detail, runs in Atlas?) for the architecture strip on How it works.
ARCHITECTURE = [
    ("Public sources", "Website (30 pages), job ads, news, GitHub", False),
    ("page_cache + page_chunks", "24-hour TTL cache; pages cut into chunks", True),
    ("$vectorSearch", "Voyage AI picks the 15 best passages", True),
    ("Gemini", "Signals with quotes; unverifiable quotes dropped", False),
    ("Score v2", "Points × source × age, 0-100", False),
    ("stories: $rankFusion", "Vector Search + Atlas Search, same industry", True),
    ("Gemini", "Writes the brief from verified evidence", False),
    ("accounts", "Upsert with history; Pipeline and $facet Dashboard", True),
]


def architecture_html() -> str:
    boxes = "".join(
        f'<div class="abb-step{" abb-atlas" if atlas else ""}"><b>{html.escape(title)}</b>'
        f"<span>{html.escape(detail)}</span></div>"
        for title, detail, atlas in ARCHITECTURE)
    return (CSS + f'<div class="abb-flow">{boxes}</div>'
            '<div class="abb-legend"><span class="abb-dot"></span>Runs in MongoDB Atlas</div>')


SCORING_POINTS = """
| Signal | Points |
|---|---|
| AI initiative | 25 |
| Legacy database | 20 |
| Hiring engineers | 20 |
| Competitor database | 15 |
| Modernisation | 15 |
| Uses MongoDB (expansion) | 15 |
| Scale or real time | 10 |
"""

SCORING_SOURCE = """
| Source | × |
|---|---|
| Job ad or GitHub | 1.0 |
| News | 0.9 |
| Pasted link | 0.8 |
| Website | 0.7 |
"""

SCORING_AGE = """
| Age | × |
|---|---|
| Up to 3 months | 1.0 |
| Up to 6 months | 0.75 |
| Up to a year | 0.5 |
| Older | 0.25 |
| No date | 0.8 |
"""


MONGODB_FEATURES = """
| Feature | What it does here | Why it helps |
|---|---|---|
| Atlas (free M0) | Hosts the `adr_brief` database | Fully managed, free |
| Document model | One document per account: signals, score maths, brief, story and history | One read shows a whole brief; new fields need no migration |
| Vector Search + Voyage AI | Picks the best passages and the closest customer story by meaning | Atlas creates the embeddings itself, so the app has no embedding code |
| Atlas Search + `$rankFusion` | Mixes keyword and meaning ranks for the story match | A story strong on both wins |
| Aggregation pipeline | Ranks the Pipeline page | Sorting and shaping run in the database |
| `$facet` | Computes every Dashboard tile and chart | One query, one round trip |
| TTL indexes | Delete cached pages and chunks after 24 hours | Fast re-runs; MongoDB cleans up by itself |
| Schema validation | Rejects bad data (e.g. a score of 150) | Flexible documents with guardrails |
| Unique indexes | One document per company and per story | Re-running updates instead of duplicating |
| Atomic upsert | Saves the new brief and archives the old one in one write | Nothing lost if two people rebuild at once |
| Least-privilege user | The app can only read and write `adr_brief` | Safe by default |
"""


def page_how_it_works() -> None:
    st.markdown("**In 30 seconds:** type a company and its website. The app reads public pages, "
                "keeps only claims it can quote word for word, scores the account, finds the closest "
                "MongoDB customer story and writes a call-ready brief. Everything is saved in MongoDB Atlas.")

    st.subheader("The flow", anchor=False)
    st.markdown("""
1. **Read.** Up to 30 website pages from the sitemap, plus job ads (public job boards),
   recent news (GDELT) and GitHub dependency files. Public pages only: robots.txt is
   respected, and there are no logins, LinkedIn or paywalls.
2. **Pick passages.** Pages are cut into chunks; one Atlas Vector Search query picks the
   15 most relevant for Gemini.
3. **Find signals.** Gemini lists buying signals with a quote each. **Any quote that isn't
   word for word on the page is dropped.**
4. **Score and match.** The account gets a score (below), and hybrid search finds the
   closest of 48 MongoDB customer stories in the same industry.
5. **Write and save.** Gemini writes the brief from verified evidence only; checks flag
   any number or vendor not in the evidence. The account is saved and ranked.
""")

    st.subheader("How scoring works", anchor=False)
    st.markdown("Each signal type counts **once**, using its strongest piece of evidence: "
                "**points × source × age**, rounded. The total is capped at 100.")
    points, source, age = st.columns([5, 4, 4])
    points.markdown(SCORING_POINTS)
    source.markdown(SCORING_SOURCE)
    age.markdown(SCORING_AGE)
    st.markdown("**Bonuses:** +5 for 5 or more open technical roles (+10 for 20 or more), "
                "+10 for a headquarters in EMEA.")
    st.info("**Example:** an AI initiative in a 1-month-old job ad scores 25 × 1.0 × 1.0 = **25**. "
            "The same claim on an undated website page scores 25 × 0.7 × 0.8 = **14**. "
            "Fresh, hard evidence ranks an account higher.", icon=":material/calculate:")
    st.caption("The Evidence tab of every brief shows each line's maths and a link to its source.")

    st.subheader("Architecture", anchor=False)
    st.html(architecture_html())

    st.subheader("MongoDB features used, and why", anchor=False)
    st.markdown(MONGODB_FEATURES)


# ---- Main -------------------------------------------------------------------

hero()
if not passcode_ok():
    st.stop()
if not get_setting("MONGODB_URI") or not get_setting("GEMINI_API_KEY"):
    st.error("MONGODB_URI and GEMINI_API_KEY must be set in the app's secrets.")
    st.stop()
if get_setting("GITHUB_TOKEN"):  # Optional: brief/github.py reads it from the environment.
    os.environ["GITHUB_TOKEN"] = get_setting("GITHUB_TOKEN")

st.session_state.setdefault("page", PAGES[0])
page = st.segmented_control("Page", PAGES, key="page", required=True, width="stretch",
                            format_func=lambda name: f"{PAGE_ICONS[name]} {name}", label_visibility="collapsed")
{"New account": page_new_account, "Pipeline": page_pipeline, "Dashboard": page_dashboard,
 "Brief": page_brief, "How it works": page_how_it_works}[page]()
st.divider()
st.caption("Built on MongoDB Atlas: document model, Atlas Search, Vector Search with Voyage AI and aggregation pipelines. "
           "Text by Gemini; every quote is checked against its public source page.")
