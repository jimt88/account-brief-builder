"""Offline tests for scoring, domain keys and brief checks (no network, no Gemini).

Run from the project folder:  python -m pytest -q
"""

from brief.accounts import PIPELINE
from brief.build import (MAX_HISTORY, Email, Persona, SalesBrief, check_brief, domain_of,
                         save_account, score_account)
from brief.industries import IndustryGroup
from brief.signals import CompanyProfile, Signal, SignalType as T

URL = "https://example.com/about"


def profile(types, hq="Ireland"):
    signals = [Signal(type=t, quote=f"Quote number {i} about our plans for the year.", source_url=URL)
               for i, t in enumerate(types)]
    return CompanyProfile(company="Example", what_they_do="Payments software.", industry="Fintech",
                          industry_group=IndustryGroup.FINANCIAL, hq_country=hq,
                          situation="Modernising.", signals=signals)


def test_score_adds_up_and_each_type_counts_once():
    score, breakdown = score_account(profile([T.AI_INITIATIVE, T.AI_INITIATIVE, T.MODERNISATION]))
    assert score == 25 + 15 + 10 == sum(line["points"] for line in breakdown)
    assert [line["points"] for line in breakdown] == [25, 15, 10]


def test_score_is_capped_at_100():
    all_types = [T.AI_INITIATIVE, T.HIRING_ENGINEERS, T.LEGACY_DATABASE, T.MODERNISATION, T.SCALE_OR_REAL_TIME]
    score, breakdown = score_account(profile(all_types))
    assert sum(line["points"] for line in breakdown) == 100 and score == 100


def test_no_emea_points_without_a_stated_emea_hq():
    assert score_account(profile([T.OTHER], hq=""))[0] == 0
    assert score_account(profile([T.OTHER], hq="United States"))[0] == 0
    assert score_account(profile([T.NEW_MARKET_OR_PRODUCT], hq="Germany"))[0] == 10


def test_domain_is_the_account_key():
    assert domain_of("https://www.Example.com/about") == "example.com"
    assert domain_of("example.ie") == "example.ie"


STORY = {"company": "Acme Bank", "industry": "Banking", "hq_country": "France",
         "summary": "Acme cut release times by 50%.", "outcomes": [], "source_url": "https://x"}


def brief(**changes):
    fields = dict(
        why_now=["Building an AI assistant [1].", "Modernising core systems [2]."],
        personas=[Persona(side="IT", role="CTO", why="Owns the platform."),
                  Persona(side="Business", role="COO", why="Owns the outcome.")],
        value_hook="Ship AI features faster on one data platform.",
        cold_call_opener="Hi, this is [Your name] from MongoDB. Acme Bank cut release times by 50%. Open to a chat?",
        email=Email(subject="AI assistant", body="Hi, saw your AI plans. Acme cut release times by 50%. 20 minutes?"),
        discovery_questions=["How do you store model data today?", "What slows releases down?"],
        matched_story_why="Similar bank modernising.",
        signals_used=[1, 2],
    )
    fields.update(changes)
    return SalesBrief(**fields)


def test_good_brief_has_no_warnings():
    assert check_brief(brief(), profile([T.AI_INITIATIVE, T.MODERNISATION]), STORY) == []


def test_vendor_claim_without_a_quote_is_flagged():
    warnings = check_brief(brief(value_hook="Move off Oracle to MongoDB Atlas."),
                           profile([T.AI_INITIATIVE, T.MODERNISATION]), STORY)
    assert any('"oracle"' in w for w in warnings)


def test_invented_number_long_hook_and_bad_citation_are_flagged():
    warnings = check_brief(
        brief(value_hook=" ".join(["word"] * 30), why_now=["Growing fast.", "Hiring [9]."],
              email=Email(subject="Hi", body="We grew 75% last year.")),
        profile([T.AI_INITIATIVE, T.MODERNISATION]), STORY)
    joined = " ".join(warnings)
    assert "Value hook is 30 words" in joined and '"75"' in joined and joined.count("doesn't cite") == 2


def test_csv_export_has_salesforce_columns():
    import csv
    import io
    from brief.accounts import pipeline_csv

    rows = [{"company": "Example", "website": "example.ie", "domain": "example.ie", "status": "Contacted",
             "score": 45, "top_signal": "AI initiative signal", "description": "Payments.", "story": "HYPE"}]
    [header, row] = list(csv.reader(io.StringIO(pipeline_csv(rows))))
    assert header == ["Company", "Website", "Lead Status", "Description"]
    assert row[:3] == ["Example", "example.ie", "Contacted"] and "Score 45/100" in row[3] and "HYPE" in row[3]


def test_persona_and_story_why_are_checked_too():
    p = profile([T.AI_INITIATIVE, T.MODERNISATION])
    b = brief(personas=[Persona(side="IT", role="Head of Oracle DBAs", why="Runs the platform."),
                        Persona(side="Business", role="COO", why="Owns the outcome.")],
              matched_story_why="Acme grew 75% after moving.")
    joined = " ".join(check_brief(b, p, STORY))
    assert '"oracle"' in joined and '"75"' in joined


def test_all_african_countries_count_as_emea():
    for country in ["Gabon", "Sierra Leone", "Sudan", "Democratic Republic of the Congo", "Kenya"]:
        assert score_account(profile([T.OTHER], hq=country))[0] == 10, country


def test_csv_cells_cannot_become_formulas():
    from brief.accounts import safe_cell
    assert safe_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and safe_cell("Acme") == "Acme"


class RecordingAccounts:
    """Stands in for the accounts collection and remembers the update it was sent."""

    def update_one(self, query, update, upsert=False):
        self.query, self.update, self.upsert = query, update, upsert

    def find_one(self, query):
        return {}


def test_save_is_one_pipeline_update_that_caps_history():
    accounts = RecordingAccounts()
    brief = SalesBrief(why_now=[], personas=[], value_hook="h", cold_call_opener="o",
                       email=Email(subject="s", body="b"), discovery_questions=[],
                       matched_story_why="w", signals_used=[])
    story = {"company": "Acme", "source_url": "https://x", "summary": "s", "score": 0.8}
    save_account(accounts, "https://example.com", profile([T.AI_INITIATIVE]), 35, [], story, brief, [], [])
    assert accounts.query == {"domain": "example.com"} and accounts.upsert
    assert isinstance(accounts.update, list)  # a pipeline update, so it stays atomic
    history = accounts.update[0]["$set"]["history"]
    assert history["$slice"][1] == -MAX_HISTORY == -5
    assert "$concatArrays" in str(history["$slice"][0])


def test_breakdown_lines_name_their_signal_type():
    _, breakdown = score_account(profile([T.MODERNISATION, T.AI_INITIATIVE]))
    assert [line["type"] for line in breakdown] == ["ai_initiative", "modernisation", "emea_hq"]


def test_top_quote_follows_the_top_signal_type():
    top_quote = PIPELINE[-1]["$project"]["top_quote"]["$let"]
    cond = top_quote["vars"]["matching"]["$filter"]["cond"]
    assert cond == {"$eq": ["$$this.type", {"$first": "$score_breakdown.type"}]}
    # Falls back to the first quote, then to "".
    assert top_quote["in"]["$ifNull"][1:] == [{"$first": "$signals.quote"}, ""]
