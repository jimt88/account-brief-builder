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
import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from google import genai
from pymongo import MongoClient

from brief.accounts import pipeline_csv, pipeline_rows, set_status
from brief.build import ACCOUNTS_COLLECTION, STATUSES
from brief.errors import friendly_error
from brief.pipeline import BriefError, build_brief
from brief.signals import SIGNAL_LABELS, SignalType
from brief.story_search import DB_NAME

st.set_page_config(page_title="Account Brief Builder", page_icon="🍃", layout="centered")

PAGES = ["New account", "Pipeline", "Brief", "How it works"]


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
        entered = st.text_input("Passcode", type="password")
        submitted = st.form_submit_button("Unlock")
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


# ---- Brief (built from the saved account document) --------------------------

def brief_as_markdown(a: dict) -> str:
    b, s, p = a["brief"], a["matched_story"], a["profile"]
    lines = [
        f"# Account brief: {a['company']}",
        f"{p['industry']}, {p['hq_country'] or 'HQ not stated'}. {p['what_they_do']}",
        "", f"## Score: {a['score']}/100"] + [f"- +{x['points']} {x['reason']}" for x in a["score_breakdown"]] + [
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
    st.header(a["company"])
    st.caption(f"{p['industry']} · {p['hq_country'] or 'HQ not stated'} · status: {a['status']} · "
               f"updated {a['updated_at']:%d %b %Y %H:%M} UTC")
    st.markdown(md(p["what_they_do"]))

    if a.get("warnings"):
        st.warning("**Check before using:**\n" + "\n".join(f"- {md(w)}" for w in a["warnings"]))
    else:
        st.success("Checks passed: lengths OK, every number is in the evidence, no vendor claims.")

    st.subheader(f"Score: {a['score']}/100")
    rows = [{"Reason": x["reason"], "Points": x["points"]} for x in a["score_breakdown"]]
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption(f"Total {sum(r['Points'] for r in rows)} (capped at 100).")
    else:
        st.write("No scoring signals found.")

    st.subheader("Why now")
    st.markdown("\n".join(f"- {md(x)}" for x in b["why_now"]))
    st.subheader("Who to contact (hypothesis)")
    st.markdown("\n".join(f"- **{x['side']}:** {md(x['role'])} - {md(x['why'])}" for x in b["personas"]))
    st.subheader("Value hook")
    st.markdown(md(b["value_hook"]))
    st.subheader("30-second cold-call opener")
    st.markdown(md(b["cold_call_opener"]))
    st.subheader("Email")
    st.markdown(f"**Subject:** {md(b['email']['subject'])}")
    st.markdown(md(b["email"]["body"]))
    st.subheader("Discovery questions")
    st.markdown("\n".join(f"{n}. {md(q)}" for n, q in enumerate(b["discovery_questions"], start=1)))

    st.subheader("Evidence (every quote is word for word from the page)")
    for n, x in enumerate(a["signals"], start=1):
        used = " · used in the brief" if n in b["signals_used"] else ""
        with st.container(border=True):
            st.markdown(f"**{n}. {label(x['type'])}**{used}")
            st.markdown(f"> {md(x['quote'])}")
            st.markdown(f"[Source]({x['source_url']})")

    st.subheader("Matched MongoDB customer story")
    with st.container(border=True):
        how = f"same industry: {s['industry_filter']}" if s.get("industry_filter") else "closest in any industry"
        st.markdown(f"**{s['company']}** · similarity {s['score']:.2f} · {how}")
        st.markdown(md(s["summary"]))
        st.markdown(f"*Why this story:* {md(s['why'])}")
        st.markdown(f"[Read the story]({s['url']})")

    with st.expander(f"Pages read ({len(a.get('pages_read', []))})"):
        st.markdown("\n".join(f"- {u}" for u in a.get("pages_read", [])))
    if a.get("history"):
        with st.expander(f"Earlier versions ({len(a['history'])})"):
            for h in reversed(a["history"]):
                when = f"{h['updated_at']:%d %b %Y %H:%M}" if h.get("updated_at") else "earlier"
                st.markdown(f"- {when}: score {h.get('score')} - {md((h.get('brief') or {}).get('value_hook', ''))}")

    st.download_button("Download brief (Markdown)", brief_as_markdown(a),
                       file_name=f"brief-{a['domain']}.md", mime="text/markdown")


# ---- Pages ------------------------------------------------------------------

def page_new_account() -> None:
    st.write("Type a company and its website. Public pages only: no logins, LinkedIn or paywalls. "
             "Takes about a minute.")
    with st.form("company"):
        company = st.text_input("Company name", placeholder="Ocado Group")
        website = st.text_input("Website", placeholder="ocadogroup.com")
        extra = st.text_area(
            "Extra links (optional, up to 3, one per line)",
            placeholder="https://www.ocadogroup.com/newsroom",
            help="Public pages worth reading too, e.g. a press release, careers page or news "
            "article. Useful when the main site is thin or needs JavaScript. No LinkedIn or paywalls.",
        )
        go = st.form_submit_button("Build brief", type="primary", width="stretch")
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
    st.button("Open the brief", type="primary", width="stretch",
              on_click=open_brief, args=(result.account["domain"],))


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
        column_order=["Company", "Score", "Top signal", "Status", "Updated"],
        disabled=["Company", "Score", "Top signal", "Updated"],
        column_config={
            "Status": st.column_config.SelectboxColumn(options=STATUSES, required=True),
            "Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d"),
            "Updated": st.column_config.DatetimeColumn(format="D MMM, HH:mm"),
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

    choice = st.selectbox("Open a brief", [r["domain"] for r in rows],
                          format_func=lambda d: next(r["company"] for r in rows if r["domain"] == d))
    st.button("Open brief", on_click=open_brief, args=(choice,), width="stretch")
    st.download_button("Export CSV (Salesforce lead columns)", pipeline_csv(rows),
                       file_name="pipeline.csv", mime="text/csv", width="stretch")


def page_brief() -> None:
    domain = st.session_state.get("selected")
    try:
        account = accounts().find_one({"domain": domain}) if domain else None
    except Exception as exc:
        st.error(friendly_error(exc))
        return
    if not account:
        st.info("Pick an account on the Pipeline page, or build a new one.")
        return
    show_brief(account)


def page_how_it_works() -> None:
    st.markdown("""
**The flow**

1. **Read public pages.** Home, About, Careers and the engineering blog (max 4 pages,
   10 s each). Respects robots.txt; never logs in; no LinkedIn or paywalls.
2. **Find signals.** Gemini lists signals (AI initiative, hiring engineers, legacy
   database, modernisation, scale or real time, new market or product), each with a
   quote. **Any quote that isn't word for word on the page is dropped.**
3. **Score 0–100.** +25 AI initiative, +20 hiring engineers, +20 legacy database,
   +15 modernisation, +10 scale or real time, +10 HQ in EMEA. The breakdown is saved.
4. **Match a customer story.** Atlas Vector Search finds the closest of 48 public
   MongoDB customer stories, pre-filtered to the same industry.
5. **Write the brief.** Gemini writes it from the verified evidence only; automatic
   checks flag numbers or vendor names that aren't in the evidence.
6. **Save the account** in MongoDB, ranked in the Pipeline.

**MongoDB features used, and why**

| Feature | Where | Why |
|---|---|---|
| Atlas (free M0 cluster) | Hosts `adr_brief` | Fully managed, free to start |
| Document model | One document per account and per story, with signals, score breakdown, brief and history embedded | Data that's read together is stored together: one read shows a whole brief |
| Atlas Vector Search with a filter | `stories.embedding` + `industry_group` | Matches by meaning, limited to the same industry, in the same database as the data |
| Aggregation pipeline | Pipeline page | Sorts and shapes accounts on the server |
| Schema validation | `stories`, `accounts` | Flexible documents, but bad data (e.g. a score of 150) is rejected |
| Unique indexes | `stories.source_url`, `accounts.domain` | Re-running never creates duplicates |
| Upsert + `$push` | Saving an account | One write updates the brief and keeps the old one in `history` |
| PyMongo driver | Everywhere | The supported way to connect (no Data API) |
""")


# ---- Main -------------------------------------------------------------------

st.title("Account Brief Builder")
if not passcode_ok():
    st.stop()
if not get_setting("MONGODB_URI") or not get_setting("GEMINI_API_KEY"):
    st.error("MONGODB_URI and GEMINI_API_KEY must be set in the app's secrets.")
    st.stop()

page = st.radio("Page", PAGES, horizontal=True, key="page", label_visibility="collapsed")
{"New account": page_new_account, "Pipeline": page_pipeline,
 "Brief": page_brief, "How it works": page_how_it_works}[page]()
