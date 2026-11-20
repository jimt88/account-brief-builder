"""Every live-demo failure gets a friendly sentence, not a stack trace."""

import requests
from google.genai import errors as gemini_errors
from pymongo import errors as mongo_errors

from brief.errors import friendly_error
from brief.gemini_utils import GeminiEmptyAnswer


def gemini_error(code):
    return gemini_errors.APIError(code, {"error": {"message": "x", "status": "X"}})


def test_gemini_rate_limit():
    assert "free tier" in friendly_error(gemini_error(429))


def test_gemini_overloaded_and_bad_key():
    assert "overloaded" in friendly_error(gemini_error(503))
    assert "GEMINI_API_KEY" in friendly_error(gemini_error(403))


def test_gemini_empty_answer():
    assert "again" in friendly_error(GeminiEmptyAnswer())


def test_mongo_bad_password_and_unreachable():
    assert "password" in friendly_error(mongo_errors.OperationFailure("bad auth", code=8000))
    assert "Network Access" in friendly_error(mongo_errors.ServerSelectionTimeoutError("timeout"))


def test_slow_website():
    assert "too slow" in friendly_error(requests.Timeout())


def test_unknown_error_does_not_leak_details():
    message = friendly_error(RuntimeError("secret-ish internal detail"))
    assert "secret-ish" not in message and "RuntimeError" in message
