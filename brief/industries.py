"""A short, fixed list of industry groups.

Stories and target companies both get one of these labels, so Atlas Vector
Search can pre-filter by industry ("only show me retail stories"). Free-text
industry names from Gemini vary too much ("Finance" vs "Financial Services")
to filter on directly.
"""

from enum import Enum


class IndustryGroup(str, Enum):
    FINANCIAL = "Financial Services & Insurance"
    RETAIL = "Retail & Consumer"
    MANUFACTURING = "Manufacturing & Automotive"
    ENERGY = "Energy & Utilities"
    TELECOMS = "Telecommunications"
    PUBLIC = "Public Sector"
    HEALTHCARE = "Healthcare & Life Sciences"
    TECHNOLOGY = "Technology & Software"
    TRANSPORT = "Transport, Logistics & Travel"
    MEDIA = "Media, Gaming & Entertainment"
    OTHER = "Other"


INDUSTRY_GROUPS = [g.value for g in IndustryGroup]
