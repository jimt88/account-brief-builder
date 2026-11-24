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
import re
import time

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

st.set_page_config(page_title="Account Brief Builder", page_icon="🍃", layout="wide")

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
/* Wide layout, but not edge to edge on big screens. */
[data-testid="stMainBlockContainer"] {max-width: 1180px;}
.abb-grid {display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px;}
.abb-card {border: 1px solid #E1E7E5; border-radius: 12px; padding: 14px 16px; background: #FFFFFF;}
.abb-card h4 {margin: 0 0 10px; font-size: 0.95rem; font-weight: 600; color: #001E2B;}
.abb-row {margin-bottom: 9px; font-size: 0.85rem; color: #3D4F58;}
.abb-row-top {display: flex; justify-content: space-between; gap: 8px;}
.abb-row-top b {color: #001E2B; font-weight: 600; white-space: nowrap;}
.abb-bar {height: 6px; border-radius: 3px; background: #E8EDEB; margin-top: 3px; overflow: hidden;}
.abb-bar i {display: block; height: 100%; background: #00A35C; border-radius: 3px;}
.abb-funnel {display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px;}
.abb-stage {border: 1px solid #E1E7E5; border-radius: 12px; padding: 10px 12px; background: #F5F7F6;}
.abb-stage b {display: block; font-size: 1.6rem; font-weight: 600; color: #001E2B; line-height: 1.2;}
.abb-stage span {font-size: 0.8rem; color: #5C6C75;}
.abb-stage.abb-win {background: #E3FCF7; border-color: #00A35C;}
.abb-steps {display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px;}
.abb-tile {border-radius: 12px; padding: 14px; background: #F5F7F6; border: 1px solid #E1E7E5;}
.abb-tile .abb-num {width: 28px; height: 28px; border-radius: 50%; background: #00684A; color: #FFFFFF;
                    display: flex; align-items: center; justify-content: center; font-weight: 600;
                    font-size: 0.85rem; margin-bottom: 8px;}
.abb-tile b {display: block; color: #001E2B; margin-bottom: 2px;}
.abb-tile span {font-size: 0.85rem; color: #3D4F58;}
.abb-feature {border: 1px solid #E1E7E5; border-left: 4px solid #00A35C; border-radius: 10px;
              padding: 12px 14px; background: #FFFFFF; font-size: 0.85rem; color: #3D4F58;}
.abb-feature b {display: block; color: #001E2B; font-size: 0.95rem; margin-bottom: 4px;}
.abb-arch {border: 1px solid #E1E7E5; border-radius: 14px; padding: 14px; background: #F9FBFA;}
.abb-arch small {display: block; font-size: 0.68rem; font-weight: 600; letter-spacing: 0.06em;
                 text-transform: uppercase; color: #5C6C75; margin-bottom: 4px;}
.abb-arch-sources {display: flex; flex-wrap: wrap; align-items: center; gap: 6px;}
.abb-arch-sources small {width: 100%;}
.abb-arch-sources span {border: 1px solid #C1CCC6; border-radius: 999px; padding: 3px 12px; background: #FFFFFF;
                        font-size: 0.8rem; color: #001E2B;}
.abb-arch-down {color: #00684A; font-size: 1.3rem; font-weight: 600; line-height: 1.8rem; padding-left: 24px;}
.abb-arch-flow {display: flex; align-items: stretch; gap: 6px;}
.abb-arch-stage {flex: 1 1 0; min-width: 0; display: flex; flex-direction: column; border: 1px solid #C1CCC6;
                 border-radius: 12px; background: #FFFFFF; overflow: hidden;}
.abb-arch-title {display: flex; align-items: center; gap: 8px; padding: 10px 12px 6px; font-weight: 600;
                 color: #001E2B;}
.abb-arch-title i {font-style: normal; width: 24px; height: 24px; border-radius: 50%; background: #00684A;
                   color: #FFFFFF; display: flex; align-items: center; justify-content: center;
                   font-size: 0.78rem; flex-shrink: 0;}
.abb-arch-app {padding: 0 12px 10px; font-size: 0.82rem; color: #3D4F58; flex: 1;}
.abb-arch-atlas {background: #E3FCF7; border-top: 1px solid #00A35C; padding: 8px 12px 10px;}
.abb-arch-atlas small {color: #00684A;}
.abb-arch-atlas code {display: block; background: #FFFFFF; border: 1px solid #A6E5D0; border-radius: 6px;
                      padding: 3px 7px; margin-top: 4px; font-size: 0.74rem; color: #00684A;
                      white-space: normal;}
.abb-arch-arrow {align-self: center; color: #00684A; font-size: 1.3rem; font-weight: 600;}
.abb-arch-legend {display: flex; flex-wrap: wrap; gap: 16px; margin-top: 10px; font-size: 0.78rem; color: #5C6C75;}
.abb-arch-legend i {display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px;
                    vertical-align: -1px;}
.abb-key-app {background: #FFFFFF; border: 1px solid #C1CCC6;}
.abb-key-atlas {background: #E3FCF7; border: 1px solid #00A35C;}
@media (max-width: 820px) {
  .abb-arch-flow {flex-direction: column;}
  .abb-arch-stage {flex: none;}
  .abb-arch-arrow {transform: rotate(90deg); padding-left: 0; align-self: flex-start; margin-left: 20px;}
}
.abb-tag {display: inline-block; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.05em;
           text-transform: uppercase; color: #00684A; margin-bottom: 4px;}
.abb-feature em {font-style: normal; color: #00684A; font-weight: 600;}
.abb-kicker {font-size: 0.75rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase;
             color: #00684A; margin: 8px 0 2px;}
.abb-flow {border: 1px solid #E1E7E5; border-radius: 14px; padding: 16px; background: #F9FBFA;}
.abb-flow-head {display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 12px;}
.abb-flow-head b {color: #001E2B; font-size: 1rem;}
.abb-flow-head > span {font-size: 0.78rem; color: #5C6C75;}
.abb-progress {height: 8px; border-radius: 4px; background: #E8EDEB; overflow: hidden; margin-bottom: 14px;}
.abb-progress i {display: block; height: 100%; border-radius: 4px; background-size: 28px 28px;
                 background-image: repeating-linear-gradient(45deg, #00A35C 0 10px, #00ED64 10px 20px);
                 animation: abb-stripes 0.8s linear infinite;}
.abb-progress.abb-done i {animation: none; background: #00A35C;}
.abb-step {display: flex; gap: 12px; position: relative; padding-bottom: 14px;}
.abb-step:last-child {padding-bottom: 0;}
.abb-step:not(:last-child)::before {content: ""; position: absolute; left: 13px; top: 32px; bottom: 4px;
                                    width: 2px; background: #E1E7E5;}
.abb-step.abb-ok:not(:last-child)::before {background: #00A35C;}
.abb-dot {width: 28px; height: 28px; border-radius: 50%; flex-shrink: 0; display: flex; align-items: center;
          justify-content: center; font-size: 0.8rem; font-weight: 600; background: #FFFFFF;
          border: 2px solid #C1CCC6; color: #5C6C75; box-sizing: border-box; position: relative;}
.abb-step b {display: block; color: #001E2B; font-size: 0.92rem;}
.abb-step span {font-size: 0.8rem; color: #5C6C75;}
.abb-ok .abb-dot {background: #00A35C; border-color: #00A35C; color: #FFFFFF;}
.abb-now .abb-dot {border-color: #00ED64; color: #00684A; animation: abb-pulse 1.4s ease-out infinite;}
.abb-now .abb-dot::after {content: ""; position: absolute; inset: -6px; border-radius: 50%;
                          border: 2px solid transparent; border-top-color: #00A35C; animation: abb-spin 0.9s linear infinite;}
.abb-now b {color: #00684A;}
.abb-err .abb-dot {background: #DB3030; border-color: #DB3030; color: #FFFFFF;}
.abb-live {font-size: 0.78rem; color: #00684A; margin-top: 2px; overflow-wrap: anywhere;}
.abb-dots::after {content: ""; animation: abb-ellipsis 1.2s steps(4) infinite;}
@keyframes abb-ellipsis {0% {content: "";} 25% {content: ".";} 50% {content: "..";} 75% {content: "...";}}
@keyframes abb-pulse {0% {box-shadow: 0 0 0 0 rgba(0, 237, 100, 0.55);} 70% {box-shadow: 0 0 0 10px rgba(0, 237, 100, 0);}
                      100% {box-shadow: 0 0 0 0 rgba(0, 237, 100, 0);}}
@keyframes abb-spin {to {transform: rotate(360deg);}}
@keyframes abb-stripes {from {background-position: 0 0;} to {background-position: 28px 0;}}
[class*="st-key-excard_"] {flex: 1 1 140px !important; min-width: 140px; border-radius: 12px !important;}
[class*="st-key-excard_on_"] {border-color: #00A35C !important; background: #E3FCF7;}
/* Pipeline table rows. */
[class*="st-key-prow_"] {padding: 4px 8px; border-bottom: 1px solid #E8EDEB; border-radius: 6px;}
[class*="st-key-prow_"]:last-child {border-bottom: none;}
[class*="st-key-prow_"]:not(.st-key-prow_head):hover {background: #F5F7F6;}
.st-key-prow_head {font-size: 0.75rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
                   color: #5C6C75; border-bottom: 1px solid #C1CCC6;}
.st-key-prow_head p {font-size: 0.75rem !important;}
[class*="st-key-pco_"] button {justify-content: flex-start; text-align: left; font-weight: 600; color: #00684A;}
[class*="st-key-pco_"] button:hover {text-decoration: underline;}
[class*="st-key-psig_"] p {font-size: 0.85rem; color: #3D4F58;}
@media (max-width: 700px) {
  [class*="st-key-prow_"] > div:has(> [class*="st-key-psig_"]), [class*="st-key-prow_"] > div:has(> [class*="st-key-pdate_"]) {display: none !important;}
  [class*="st-key-prow_"] {flex-wrap: nowrap !important;}
  [class*="st-key-prow_"] > * {min-width: 0;}
  /* Streamlit puts the fixed width on the wrapper around each keyed container. */
  [class*="st-key-prow_"] > div:has(> [class*="st-key-pco_"]) {flex: 1 1 0 !important; width: auto !important; min-width: 0 !important;}
  [class*="st-key-pco_"], [class*="st-key-pst_"] {width: auto !important;}
  [class*="st-key-prow_"] > div:has(> [class*="st-key-pst_"]) {flex: 0 0 124px !important; width: 124px !important;}
}
/* Dashboard. */
.abb-kpis {display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px;}
.abb-kpi {border: 1px solid #E1E7E5; border-top: 4px solid #00ED64; border-radius: 12px; padding: 12px 16px;
          background: #FFFFFF;}
.abb-kpi span {display: block; font-size: 0.78rem; font-weight: 600; color: #5C6C75;}
.abb-kpi b {display: block; font-size: 2rem; font-weight: 600; color: #001E2B; line-height: 1.2;}
.abb-kpi small {font-size: 0.78rem; color: #5C6C75;}
.abb-chevrons {display: flex; gap: 4px; align-items: stretch;}
.abb-chev, .abb-parked {flex: 1 1 0; min-width: 0; padding: 10px 14px 10px 26px; background: #F5F7F6;
                        clip-path: polygon(0 0, calc(100% - 14px) 0, 100% 50%, calc(100% - 14px) 100%, 0 100%, 14px 50%);}
.abb-chev:first-child {padding-left: 14px; border-radius: 10px 0 0 10px;
                       clip-path: polygon(0 0, calc(100% - 14px) 0, 100% 50%, calc(100% - 14px) 100%, 0 100%);}
.abb-chev b, .abb-parked b {display: block; font-size: 1.5rem; font-weight: 600; color: #001E2B; line-height: 1.2;}
.abb-chev span, .abb-parked span {display: block; font-size: 0.8rem; color: #3D4F58; white-space: nowrap;
                                  overflow: hidden; text-overflow: ellipsis;}
.abb-chev small, .abb-parked small {font-size: 0.72rem; color: #5C6C75;}
.abb-chev.abb-win {background: #00684A;}
.abb-chev.abb-win b, .abb-chev.abb-win span, .abb-chev.abb-win small {color: #FFFFFF;}
.abb-parked {flex: 0 0 110px; margin-left: 10px; clip-path: none; border-radius: 10px; padding-left: 14px;
             border: 1px dashed #C1CCC6; background: #FFFFFF;}
@media (max-width: 700px) {
  .abb-chevrons {display: grid; grid-template-columns: 1fr 1fr; gap: 8px;}
  .abb-chev, .abb-chev:first-child, .abb-parked {clip-path: none; border-radius: 10px; padding-left: 14px; margin-left: 0;}
}
/* How it works: scoring and tables. */
.abb-formula {display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 8px;}
.abb-formula > i {font-style: normal; font-size: 1.4rem; font-weight: 600; color: #00684A;}
.abb-term {flex: 1 1 130px; border: 1px solid #C1CCC6; border-radius: 12px; padding: 10px 14px; background: #FFFFFF;}
.abb-term small {display: block; font-size: 0.7rem; color: #5C6C75; text-transform: uppercase; letter-spacing: 0.05em;}
.abb-term b {display: block; font-size: 1.15rem; color: #001E2B;}
.abb-term span {font-size: 0.8rem; color: #00684A; font-weight: 600;}
.abb-term-out {background: #00684A; border-color: #00684A;}
.abb-term-out small, .abb-term-out b, .abb-term-out span {color: #FFFFFF;}
.abb-note {font-size: 0.88rem; color: #3D4F58; margin: 4px 0 12px;}
.abb-chips {display: flex; flex-wrap: wrap; gap: 8px;}
.abb-chip {border: 1px solid #A6E5D0; background: #E3FCF7; border-radius: 999px; padding: 4px 12px;
           font-size: 0.82rem; color: #001E2B;}
.abb-chip b {color: #00684A;}
.abb-table {width: 100%; border-collapse: separate; border-spacing: 0; border: 1px solid #E1E7E5;
            border-radius: 12px; overflow: hidden; font-size: 0.86rem; background: #FFFFFF;}
.abb-table th {background: #001E2B; color: #FFFFFF; text-align: left; padding: 10px 14px; font-weight: 600;}
.abb-table td {padding: 9px 14px; border-top: 1px solid #E8EDEB; color: #3D4F58; vertical-align: top;}
.abb-table td b {color: #001E2B;}
.abb-table .abb-n {text-align: right; white-space: nowrap;}
.abb-table .abb-group td {background: #E3FCF7; color: #00684A; font-weight: 600; font-size: 0.72rem;
                          letter-spacing: 0.06em; text-transform: uppercase; padding: 6px 14px;}
.abb-table .abb-why {color: #00684A;}
@media (max-width: 700px) {
  .abb-stack thead {display: none;}
  .abb-stack tr, .abb-stack td {display: block;}
  .abb-stack td {border-top: none; padding: 2px 14px;}
  .abb-stack tr:not(.abb-group) {border-top: 1px solid #E8EDEB; padding: 8px 0;}
  .abb-stack .abb-group td {padding: 6px 14px;}
  .abb-stack .abb-why::before {content: "Why: "; font-weight: 600;}
}
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
    with st.columns([1, 2, 1])[1], st.form("passcode"):
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


def cite(text: str, signals: list[dict]) -> str:
    """Markdown with every "[n]" linked to signal n's source page."""
    def link(match: re.Match) -> str:
        n = int(match.group(1))
        if not 1 <= n <= len(signals):
            return match.group(0)
        url = signals[n - 1]["source_url"].replace(" ", "%20").replace("(", "%28").replace(")", "%29")
        return f'[\\[{n}\\]]({url} "Signal {n}: {label(signals[n - 1]["type"])}")'
    return re.sub(r"\[(\d+)\]", link, md(text))


def cite_html(text: str, signals: list[dict]) -> str:
    """HTML version of cite(), for the value hook box."""
    def link(match: re.Match) -> str:
        n = int(match.group(1))
        if not 1 <= n <= len(signals):
            return match.group(0)
        return f'<a href="{html.escape(signals[n - 1]["source_url"])}" target="_blank">[{n}]</a>'
    return re.sub(r"\[(\d+)\]", link, html.escape(text))


def hero() -> None:
    """The dark MongoDB-green banner at the top of every page."""
    st.html(CSS + """
<div class="abb-hero">
  <div>
    <div class="abb-title">Account Brief <span>Builder</span></div>
    <div class="abb-sub">Evidence-backed account briefs for MongoDB ADRs, powered by Atlas Vector Search</div>
  </div>
</div>""")


def callout(title: str, text: str, signals: list[dict] | None = None) -> None:
    """A highlighted box, used for the value hook."""
    st.html(f'<div class="abb-callout"><div class="abb-label">{html.escape(title)}</div>'
            f'<div class="abb-text">{cite_html(text, signals or [])}</div></div>')


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
        callout("Value hook", b["value_hook"], a["signals"])
        st.html('<div class="abb-kicker">Why now</div>')
        st.markdown("\n".join(f"- {cite(x, a['signals'])}" for x in b["why_now"]))
        st.html('<div class="abb-kicker">Who to contact (hypotheses to check)</div>')
        for column, x in zip(st.columns(len(b["personas"]) or 1), b["personas"]):
            with column.container(border=True):
                st.markdown(f"**{x['side']}:** {cite(x['role'], a['signals'])}")
                st.caption(cite(x["why"], a["signals"]))
        call, email = st.tabs([":material/call: Cold-call opener (30 s)", ":material/mail: Email"])
        with call:
            st.code(b["cold_call_opener"], language=None, wrap_lines=True)
        with email:
            st.code(f"Subject: {b['email']['subject']}\n\n{b['email']['body']}", language=None, wrap_lines=True)
        st.caption("Copy with the icon at the top right of the text.")
        st.html('<div class="abb-kicker">Discovery questions</div>')
        st.markdown("\n".join(f"{n}. {cite(q, a['signals'])}" for n, q in enumerate(b["discovery_questions"], start=1)))

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

EXAMPLES = [  # (company, website, industry, why it's a good demo)
    ("Monzo", "monzo.com", "Fintech", "Lots of public job ads"),
    ("Mistral AI", "mistral.ai", "AI", "Strong AI and hiring signals"),
    ("DSV", "dsv.com", "Logistics", "Global freight at scale"),
    ("Ocado Group", "ocadogroup.com", "Retail tech", "Robotics and AI"),
]

# The 5 steps build_brief reports as "1/5 ..." to "5/5 ...".
BUILD_STEPS = [
    ("Read", "Website (up to 30 pages), job ads, news and GitHub"),
    ("Find and score signals", "Atlas picks the best passages; Gemini keeps only exact quotes; score 0-100"),
    ("Match a story", "Closest MongoDB customer story, by meaning and keywords"),
    ("Write", "Value hook, call opener, email and discovery questions"),
    ("Save", "One write to MongoDB Atlas, keeping the last 5 versions"),
]


def use_example(company: str, website: str) -> None:
    """Button callback: fill the form; nothing runs until Build brief is pressed."""
    st.session_state.new_company = company
    st.session_state.new_website = website


def flow_html(step: int = 0, done: bool = False, failed: bool = False, seconds: float = 0,
              latest: str = "", share: float = 0) -> str:
    """The 5 build steps. Idle (step 0) it explains them; while building, the current
    step spins and the bar moves; finished steps get a tick."""
    if failed:
        head = "<b>Stopped</b><span>See the message on the left</span>"
    elif done:
        head = f"<b>Brief ready</b><span>{seconds:.0f} s</span>"
    elif step:
        head = f'<b>Building<span class="abb-dots"></span></b><span>{seconds:.0f} s · usually 1-2 min</span>'
    else:
        head = "<b>What happens</b><span>About 1-2 minutes</span>"
    bar = ""
    if step or done:
        bar = (f'<div class="abb-progress{" abb-done" if done or failed else ""}">'
               f'<i style="width:{100 if done else max(share, 0.04) * 100:.0f}%"></i></div>')
    rows = []
    for n, (title, text) in enumerate(BUILD_STEPS, start=1):
        state = ("abb-ok" if done or n < step else "abb-err" if failed and n == step
                 else "abb-now" if n == step else "")
        live = f'<div class="abb-live">{html.escape(latest)}</div>' if state == "abb-now" and latest else ""
        dot = "✓" if state == "abb-ok" else "!" if state == "abb-err" else str(n)
        rows.append(f'<div class="abb-step {state}"><div class="abb-dot">{dot}</div><div><b>{html.escape(title)}</b>'
                    f"<span>{html.escape(text)}</span>{live}</div></div>")
    return CSS + f'<div class="abb-flow"><div class="abb-flow-head">{head}</div>{bar}{"".join(rows)}</div>'


def recent_accounts() -> None:
    try:
        rows = sorted(pipeline_rows(accounts()), key=lambda r: r.get("updated_at") or 0, reverse=True)[:4]
    except Exception:
        return  # The form still works; the list is a nice-to-have.
    if not rows:
        return
    st.html('<div class="abb-kicker">Recently researched</div>')
    names = account_labels(rows)
    for column, r in zip(st.columns(4), rows):
        with column.container(border=True, height="stretch"):
            st.markdown(f"**{md(names[r['domain']])}** " + badge(f"{r['score']}/100", score_colour(r["score"])))
            st.caption(md(r["top_signal"]))
            st.button("Open brief", key=f"recent_{r['domain']}", on_click=open_brief, args=(r["domain"],),
                      type="tertiary", icon=":material/arrow_forward:", icon_position="right")


def page_new_account() -> None:
    form_col, side_col = st.columns([3, 2], gap="large")
    with form_col:
        st.subheader("Research a new account", anchor=False)
        st.caption("Type a company and its website, or start with an example. The app reads its public "
                   "website, job ads, news and GitHub, keeps only quotes it can verify, and writes a call-ready brief.")
        st.html('<div class="abb-kicker">Start with an example</div>')
        with st.container(horizontal=True, wrap=True, gap="small"):
            for company, website, industry, hint in EXAMPLES:
                chosen = st.session_state.get("new_website") == website
                with st.container(border=True, key=f"excard_{'on' if chosen else 'off'}_{website.split('.')[0]}"):
                    st.markdown(f"**{company}**  \n:gray[{industry}]")
                    st.caption(hint)
                    st.button("Selected" if chosen else "Use", key=f"example_{website}", on_click=use_example,
                              args=(company, website), type="primary" if chosen else "secondary",
                              icon=":material/check:" if chosen else ":material/bolt:", width="stretch")
        st.html('<div class="abb-kicker">Or type your own</div>')
        with st.form("company", border=True):
            name_col, site_col = st.columns(2)
            company = name_col.text_input("Company name", placeholder="Ocado Group", key="new_company")
            website = site_col.text_input("Website", placeholder="ocadogroup.com", key="new_website")
            with st.expander("Extra links (optional)"):
                extra = st.text_area(
                    "Up to 3 public pages, one per line",
                    placeholder="https://www.ocadogroup.com/newsroom",
                    help="E.g. a press release, careers page or news article. Useful when the main "
                    "site is thin or needs JavaScript. No LinkedIn or paywalls.",
                )
            go = st.form_submit_button("Build brief", type="primary", width="stretch", icon=":material/bolt:")
    with side_col:
        flow = st.empty()
        flow.html(flow_html())
    recent_box = st.container()  # Filled last, so a brief built just now is listed.
    try:
        build_from_form(go, company, website, extra, form_col, flow)
    finally:
        with recent_box:
            recent_accounts()


def build_from_form(go: bool, company: str, website: str, extra: str, form_col, flow) -> None:
    if not go:
        return
    if not company.strip() or not website.strip():
        form_col.error("Please fill in both the company name and the website.")
        return
    start, state = time.monotonic(), {"step": 1, "reads": 0, "latest": ""}

    def share() -> float:
        # Step 1 is the long one (reading pages), so it creeps forward with each page read.
        within = min(state["reads"], 40) / 40 * 0.18 if state["step"] == 1 else 0.05
        return (state["step"] - 1) / len(BUILD_STEPS) + within

    def on_progress(message: str) -> None:
        number = re.match(r"(\d)/5 ", message)
        if number:
            state["step"] = int(number.group(1))
        elif state["step"] == 1:
            state["reads"] += 1
        state["latest"] = message[4:] if number else message
        flow.html(flow_html(state["step"], seconds=time.monotonic() - start, latest=state["latest"], share=share()))
        log.markdown(f":green[:material/check:] {md(message)}")

    with form_col:
        flow.html(flow_html(1, seconds=0, latest=f"Reading {website.strip()} ...", share=0.02))
        with st.status("Building the brief (1-2 minutes) ...", expanded=False) as status:
            log = st.container()
            try:
                result = build_brief(company.strip(), website.strip(), get_db(), get_gemini(),
                                     progress=on_progress, extra_urls=extra.split())
            except BriefError as exc:
                status.update(label="Couldn't build the brief", state="error", expanded=True)
                flow.html(flow_html(state["step"], failed=True))
                st.error(str(exc))
                return
            except Exception as exc:
                status.update(label="Something went wrong", state="error", expanded=True)
                flow.html(flow_html(state["step"], failed=True))
                st.error(friendly_error(exc))
                return
            status.update(label=f"Brief ready: score {result.score}/100 (details)", state="complete")
        flow.html(flow_html(done=True, seconds=time.monotonic() - start))
        st.button("Open the brief", type="primary", width="stretch", icon=":material/description:",
                  on_click=open_brief, args=(result.account["domain"],))


def bar_card(title: str, rows: list[tuple[str, str, float]]) -> str:
    """A ranked list with a thin bar per row: (label, value text, bar share 0-1)."""
    body = "".join(
        f'<div class="abb-row"><div class="abb-row-top"><span>{html.escape(name)}</span>'
        f'<b>{html.escape(value)}</b></div><div class="abb-bar"><i style="width:{share * 100:.0f}%"></i>'
        f"</div></div>" for name, value, share in rows) or '<div class="abb-row">Nothing yet.</div>'
    return f'<div class="abb-card"><h4>{html.escape(title)}</h4>{body}</div>'


def counted(rows: list[dict], name: str) -> list[tuple[str, str, float]]:
    top = max((r["count"] for r in rows), default=1) or 1
    return [(str(r[name]), str(r["count"]), r["count"] / top) for r in rows]


def kpis_html(totals: dict, statuses: dict) -> str:
    accounts_total, meetings = totals["accounts"], statuses.get("Meeting set", 0)
    share = f"{meetings / accounts_total:.0%} of accounts" if accounts_total else "none yet"
    tiles = [("Accounts", accounts_total, "researched and saved"),
             ("Avg score", totals["avg_score"], "out of 100"),
             ("Score 50+", totals["score_50_plus"], "call these first"),
             ("Meetings set", meetings, share)]
    return CSS + '<div class="abb-kpis">' + "".join(
        f'<div class="abb-kpi"><span>{name}</span><b>{value}</b><small>{html.escape(sub)}</small></div>'
        for name, value, sub in tiles) + "</div>"


def funnel_html(statuses: dict) -> str:
    """New → Researched → Contacted → Meeting set as arrows; Not now set apart."""
    total = sum(statuses.values()) or 1
    stages = "".join(
        f'<div class="abb-chev{" abb-win" if status == "Meeting set" else ""}"><b>{statuses.get(status, 0)}</b>'
        f"<span>{html.escape(status)}</span><small>{statuses.get(status, 0) / total:.0%}</small></div>"
        for status in STATUSES if status != "Not now")
    parked = statuses.get("Not now", 0)
    return CSS + (f'<div class="abb-chevrons">{stages}<div class="abb-parked"><b>{parked}</b>'
                  f"<span>Not now</span><small>{parked / total:.0%}</small></div></div>")


def show_dashboard(numbers: dict) -> None:
    """Number tiles, a status funnel, the top accounts and ranked lists, all from one $facet result."""
    statuses = {r["status"]: r["count"] for r in numbers["by_status"]}
    st.html(kpis_html(numbers["totals"], statuses))
    st.html('<div class="abb-kicker">Pipeline by status</div>')
    st.html(funnel_html(statuses))

    top_col, industry_col = st.columns(2)
    with top_col.container(border=True, height="stretch"):
        st.markdown("**Top accounts to call**")
        for n, r in enumerate(numbers["top_accounts"], start=1):
            with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                st.markdown(f":gray[{n}]", width=16)
                st.button(r["company"], key=f"dash_open_{r['domain']}", type="tertiary",
                          on_click=open_brief, args=(r["domain"],), help=r.get("top_signal"))
                st.space("stretch")
                st.markdown(badge(f"{r['score']}/100", score_colour(r["score"])), width="content")
        if not numbers["top_accounts"]:
            st.caption("Nothing yet.")
        st.caption("Click a company to open its brief.")
    industries = [(r["industry"], f"{r['avg_score']} avg · {r['count']} acct{'s' if r['count'] != 1 else ''}",
                   r["avg_score"] / 100) for r in numbers["by_industry"]]
    industry_col.html(CSS + bar_card("Avg score by industry", industries))

    st.html('<div class="abb-kicker">What the research found</div>')
    st.html(CSS + '<div class="abb-grid">'
            + bar_card("Signals found", counted(numbers["signal_types"], "signal"))
            + bar_card("Where the evidence came from", counted(numbers["by_source"], "source"))
            + bar_card("Technologies seen", counted(numbers["technologies"], "technology"))
            + "</div>")
    st.caption("Every number on this page comes from one MongoDB query: a `$facet` aggregation "
               "that runs all the groupings in a single trip to the database.")
    with st.expander("See the MongoDB query behind this page"):
        st.code(json.dumps(DASHBOARD_PIPELINE, indent=2), language="json")


STATUS_DOTS = {"New": "⚪", "Researched": "🔵", "Contacted": "🟡", "Meeting set": "🟢", "Not now": "⚫"}


def save_status(domain: str, company: str, key: str) -> None:
    """Status picker callback: save straight to MongoDB."""
    try:
        set_status(accounts(), domain, st.session_state[key])
        st.session_state.saved_message = f"Saved: {company} is now {st.session_state[key]}"
    except Exception as exc:
        st.session_state.saved_message = friendly_error(exc)


def page_pipeline() -> None:
    try:
        rows = pipeline_rows(accounts())
    except Exception as exc:
        st.error(friendly_error(exc))
        return
    if not rows:
        st.info("No accounts yet. Build one on the New account page.")
        return
    if "saved_message" in st.session_state:
        st.toast(st.session_state.pop("saved_message"))
    st.caption("Ranked by score with a MongoDB aggregation pipeline. Click a company to open its brief; "
               "a status change is saved straight away.")
    names = account_labels(rows)
    with st.container(border=True, key="ptable", gap=None):
        with st.container(horizontal=True, vertical_alignment="center", gap="small", key="prow_head"):
            st.markdown("#", width=24)
            with st.container(key="pco_head", width=190):
                st.markdown("Company")
            st.markdown("Score", width=64)
            with st.container(key="psig_head"):
                st.markdown("Top signal")
            with st.container(key="pst_head", width=160):
                st.markdown("Status")
            with st.container(key="pdate_head", width=64):
                st.markdown("Updated")
        for n, r in enumerate(rows, start=1):
            with st.container(horizontal=True, vertical_alignment="center", gap="small", key=f"prow_{n}"):
                st.markdown(f"**{n}**", width=24)
                with st.container(key=f"pco_{n}", width=190):
                    st.button(names[r["domain"]], key=f"open_{r['domain']}", type="tertiary",
                              on_click=open_brief, args=(r["domain"],))
                st.markdown(badge(str(r["score"]), score_colour(r["score"])), width=64)
                with st.container(key=f"psig_{n}"):
                    st.markdown(md(r["top_signal"]))
                # The key includes the saved status, so a fresh picker appears after each change.
                key = f"status_{r['domain']}_{r['status']}"
                with st.container(key=f"pst_{n}", width=160):
                    st.selectbox("Status", STATUSES, index=STATUSES.index(r["status"]) if r["status"] in STATUSES else 0,
                                 key=key, format_func=lambda s: f"{STATUS_DOTS.get(s, '')} {s}",
                                 label_visibility="collapsed", filter_mode=None,
                                 on_change=save_status, args=(r["domain"], r["company"], key))
                with st.container(key=f"pdate_{n}", width=64):
                    st.caption(f"{r['updated_at']:%d %b}" if r.get("updated_at") else "-")

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
    rows = sorted(rows, key=lambda r: (r["company"].lower(), r["domain"]))
    names, scores = account_labels(rows), {r["domain"]: r["score"] for r in rows}
    domains = [r["domain"] for r in rows]
    selected = st.session_state.get("selected")
    domain = st.selectbox(f"Account ({len(domains)}, A-Z)", domains,
                          index=domains.index(selected) if selected in domains else 0,
                          format_func=lambda d: f"{names[d]} · {scores[d]}/100", filter_mode="contains",
                          placeholder="Type to search accounts", icon=":material/search:",
                          help="Click and start typing a company name to search.")
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


IN_30_SECONDS = [
    ("Read", "Public website pages, job ads, news and GitHub for one company."),
    ("Verify", "Gemini lists buying signals; any quote not word for word on its page is dropped."),
    ("Score", "0-100 from what each signal is, where it came from and how old it is."),
    ("Write", "The closest MongoDB customer story, then a call opener, email and questions."),
]


def in_30_seconds_html() -> str:
    tiles = "".join(f'<div class="abb-tile"><div class="abb-num">{n}</div><b>{html.escape(title)}</b>'
                    f"<span>{html.escape(text)}</span></div>"
                    for n, (title, text) in enumerate(IN_30_SECONDS, start=1))
    return CSS + f'<div class="abb-steps">{tiles}</div>'


ARCHITECTURE_SOURCES = ["Company website", "Job boards", "GDELT news", "GitHub"]
# (title, what the app does, what Atlas does) per stage of one brief.
ARCHITECTURE = [
    ("Read", "Crawls up to 30 public pages (robots.txt respected), plus job ads, news and GitHub",
     ["page_cache · TTL 24 h", "page_chunks · Voyage AI embeddings"]),
    ("Verify", "Gemini reads the passages Atlas picked and lists buying signals; quotes not on the page are dropped",
     ["$vectorSearch + keywords → 15 best passages"]),
    ("Score and match", "Score v2: points × source × age, capped at 100",
     ["stories · $rankFusion (vector + Atlas Search)"]),
    ("Write and save", "Gemini writes the opener, email and questions; pages show the results",
     ["accounts · atomic upsert, last 5 versions", "Aggregation + $facet → Pipeline, Dashboard"]),
]


def architecture_html() -> str:
    """Sources, then one card per stage: the app's work on top, the Atlas work in green below."""
    sources = "".join(f"<span>{html.escape(name)}</span>" for name in ARCHITECTURE_SOURCES)
    stages = []
    for n, (title, app_work, atlas_work) in enumerate(ARCHITECTURE, start=1):
        if n > 1:
            stages.append('<div class="abb-arch-arrow">→</div>')
        chips = "".join(f"<code>{html.escape(item)}</code>" for item in atlas_work)
        stages.append(
            f'<div class="abb-arch-stage"><div class="abb-arch-title"><i>{n}</i>{html.escape(title)}</div>'
            f'<div class="abb-arch-app"><small>App</small>{html.escape(app_work)}</div>'
            f'<div class="abb-arch-atlas"><small>MongoDB Atlas</small>{chips}</div></div>')
    return CSS + (
        f'<div class="abb-arch"><div class="abb-arch-sources"><small>Public web</small>{sources}</div>'
        f'<div class="abb-arch-down">↓</div><div class="abb-arch-flow">{"".join(stages)}</div>'
        '<div class="abb-arch-legend"><span><i class="abb-key-app"></i>Streamlit app (Python) + Gemini</span>'
        '<span><i class="abb-key-atlas"></i>MongoDB Atlas, free M0</span></div></div>')


SCORING_POINTS = [("AI initiative", 25), ("Legacy database", 20), ("Hiring engineers", 20),
                  ("Competitor database", 15), ("Modernisation", 15), ("Uses MongoDB (expansion)", 15),
                  ("Scale or real time", 10)]
SCORING_SOURCE = [("Job ad or GitHub", 1.0), ("News", 0.9), ("Pasted link", 0.8), ("Website", 0.7)]
SCORING_AGE = [("Up to 3 months", 1.0), ("Up to 6 months", 0.75), ("Up to a year", 0.5), ("Older", 0.25),
               ("No date", 0.8)]
SCORING_BONUSES = [("+5", "5 or more open technical roles"), ("+10", "20 or more open technical roles (instead of +5)"),
                   ("+10", "Headquarters in EMEA")]
SCORING_EXAMPLE = [("AI initiative in a 1-month-old job ad", 25, 1.0, 1.0),
                   ("The same claim on an undated website page", 25, 0.7, 0.8)]


def scoring_html() -> str:
    terms = [("Points", "what the signal is", "10-25"), ("Source", "where it came from", "× 0.7-1.0"),
             ("Age", "how old it is", "× 0.25-1.0")]
    formula = '<i>×</i>'.join(
        f'<div class="abb-term"><small>{html.escape(label)}</small><b>{html.escape(name)}</b>'
        f"<span>{html.escape(rng)}</span></div>" for name, label, rng in terms)
    formula += ('<i>=</i><div class="abb-term abb-term-out"><small>one line</small><b>Line score</b>'
                "<span>rounded</span></div>")
    cards = (bar_card("1 · Points per signal type", [(n, str(p), p / 25) for n, p in SCORING_POINTS])
             + bar_card("2 · × Source strength", [(n, f"× {m}", m) for n, m in SCORING_SOURCE])
             + bar_card("3 · × Age of the evidence", [(n, f"× {m}", m) for n, m in SCORING_AGE]))
    bonuses = "".join(f'<span class="abb-chip"><b>{html.escape(p)}</b> {html.escape(t)}</span>'
                      for p, t in SCORING_BONUSES)
    example = "".join(
        f"<tr><td>{html.escape(what)}</td><td class='abb-n'>{points}</td><td class='abb-n'>× {source}</td>"
        f"<td class='abb-n'>× {age}</td><td class='abb-n'><b>{round(points * source * age)}</b></td></tr>"
        for what, points, source, age in SCORING_EXAMPLE)
    return CSS + (
        f'<div class="abb-formula">{formula}</div>'
        '<div class="abb-note">Each signal type counts <b>once</b>, using its strongest evidence. The lines are '
        "added up, bonuses are added, and the total is capped at <b>100</b>.</div>"
        f'<div class="abb-grid">{cards}</div>'
        f'<div class="abb-kicker">Bonuses</div><div class="abb-chips">{bonuses}</div>'
        '<div class="abb-kicker">Worked example</div>'
        '<table class="abb-table"><thead><tr><th>Evidence</th><th class="abb-n">Points</th>'
        '<th class="abb-n">Source</th><th class="abb-n">Age</th><th class="abb-n">Score</th></tr></thead>'
        f"<tbody>{example}</tbody></table>")


# (group, feature, where it's used, why it helps) for the table on How it works.
MONGODB_FEATURES = [
    ("Search and AI", "Atlas Vector Search", "Picks the 15 best page passages and the closest customer story",
     "Finds matches by meaning, next to the data"),
    ("Search and AI", "Automated embedding (Voyage AI)", "Atlas turns stories and passages into vectors",
     "No embedding code or extra AI service in the app"),
    ("Search and AI", "Atlas Search + $rankFusion", "Mixes keyword and meaning ranks for the story match",
     "A story strong on both wins"),
    ("Data and queries", "Document model", "One document per account: signals, score maths, brief, history",
     "One read shows a whole brief; new fields need no migration"),
    ("Data and queries", "Aggregation pipeline", "Ranks the Pipeline page", "Sorting and shaping run in the database"),
    ("Data and queries", "$facet", "Computes every Dashboard number", "One query, one round trip"),
    ("Data and queries", "Atomic upsert", "Saves the new brief and archives the old one in one write",
     "Nothing lost if two people rebuild at once"),
    ("Running safely", "TTL indexes", "Delete cached pages and chunks after 24 hours",
     "Fast re-runs; MongoDB cleans up by itself"),
    ("Running safely", "Schema validation", "Rejects bad data, e.g. a score of 150", "Flexible, with guardrails"),
    ("Running safely", "Unique indexes", "One document per company and per story",
     "Re-running updates instead of duplicating"),
    ("Running safely", "Least-privilege user", "The app can only read and write adr_brief", "Safe by default"),
    ("Running safely", "Atlas M0", "Hosts the whole adr_brief database", "Fully managed and free"),
]


def features_html() -> str:
    rows, group = [], None
    for g, feature, where, why in MONGODB_FEATURES:
        if g != group:
            group = g
            rows.append(f'<tr class="abb-group"><td colspan="3">{html.escape(g)}</td></tr>')
        rows.append(f"<tr><td><b>{html.escape(feature)}</b></td><td>{html.escape(where)}</td>"
                    f'<td class="abb-why">{html.escape(why)}</td></tr>')
    return CSS + ('<table class="abb-table abb-stack"><thead><tr><th>Feature</th><th>Where it\'s used</th>'
                  f'<th>Why</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')


def page_how_it_works() -> None:
    st.subheader("In 30 seconds", anchor=False)
    st.html(in_30_seconds_html())
    st.caption("Public pages only: robots.txt is respected; no logins, LinkedIn or paywalls. "
               "Everything is saved in MongoDB Atlas.")

    st.subheader("How scoring works", anchor=False)
    st.html(scoring_html())
    st.caption("Fresh, hard evidence ranks an account higher. The Evidence tab of every brief shows each "
               "line's maths and a link to its source.")

    st.subheader("Architecture", anchor=False)
    st.caption("What happens to one brief, stage by stage. White is the app; green is MongoDB Atlas.")
    st.html(architecture_html())

    st.subheader("MongoDB features used, and why", anchor=False)
    st.html(features_html())


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
