"""Small helpers shared by everything that calls Gemini."""

import re
import time

from google.genai import errors

TEXT_MODEL = "gemini-3.5-flash-lite"
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768  # Must match the Vector Search index.


RETRY_WAITS = [10, 20]  # Seconds. Short, so a live demo isn't left hanging.


class GeminiEmptyAnswer(Exception):
    """Gemini replied, but without the structured answer we asked for."""


def parsed_or_raise(response):
    """The structured answer from a Gemini response, or GeminiEmptyAnswer."""
    if response.parsed is None:
        raise GeminiEmptyAnswer("Gemini returned an empty or incomplete answer.")
    return response.parsed


def call_with_retry(func, *args, **kwargs):
    """Retry Gemini calls that hit the free-tier rate limit (HTTP 429) or a 5xx."""
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            return func(*args, **kwargs)
        except errors.APIError as exc:
            if exc.code in (429, 500, 503) and attempt < len(RETRY_WAITS):
                wait = RETRY_WAITS[attempt]
                print(f"    Gemini busy ({exc.code}); waiting {wait}s and retrying...")
                time.sleep(wait)
            else:
                raise


def normalise(text: str) -> str:
    """Make quote matching ignore curly quotes, dashes, case and extra spaces."""
    replacements = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u00a0": " "}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip().lower()
