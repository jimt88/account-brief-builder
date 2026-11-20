"""Turn technical errors into one friendly sentence for the app.

Each case is something that can go wrong in a live demo, with what to do about it.
"""

import requests
from google.genai import errors as gemini_errors
from pymongo import errors as mongo_errors

from brief.gemini_utils import GeminiEmptyAnswer


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, gemini_errors.APIError):
        if exc.code == 429:
            return ("Gemini's free tier is at its limit (too many requests this minute, or today's "
                    "quota is used up). Wait a minute and try again; the daily quota resets overnight.")
        if exc.code in (500, 502, 503, 504):
            return "Gemini is temporarily overloaded. Please try again in a minute."
        if exc.code in (400, 401, 403):
            return "The Gemini API key was rejected. Check GEMINI_API_KEY in the app's secrets."
        return f"Gemini returned an error ({exc.code}). Please try again."
    if isinstance(exc, GeminiEmptyAnswer):
        return "Gemini gave an incomplete answer. Please click Build brief again."
    if isinstance(exc, mongo_errors.OperationFailure) and exc.code in (8000, 18):
        return ("MongoDB Atlas rejected the username or password. Check MONGODB_URI in the app's "
                "secrets (the password must replace <db_password>).")
    if isinstance(exc, (mongo_errors.ServerSelectionTimeoutError, mongo_errors.ConfigurationError)):
        return ("Couldn't reach MongoDB Atlas. Check MONGODB_URI in the app's secrets and that Atlas "
                "Network Access allows this app (0.0.0.0/0 for Streamlit Cloud).")
    if isinstance(exc, mongo_errors.PyMongoError):
        return "MongoDB returned an error. Please try again in a moment."
    if isinstance(exc, requests.Timeout):
        return "The website was too slow to respond. Try again, or paste a faster page in Extra links."
    if isinstance(exc, requests.RequestException):
        return "Couldn't download the website. Check the address, or try the company's corporate site."
    return f"Something unexpected went wrong ({type(exc).__name__}). Please try again."
