# school_name_normalizer.py

import re
from typing import Optional

NUMBER_WORDS = {
    "satu": "1",
    "dua": "2",
    "tiga": "3",
    "empat": "4",
    "lima": "5",
    "enam": "6",
    "tujuh": "7",
    "delapan": "8",
    "sembilan": "9",
    "sepuluh": "10"
}

SCHOOL_PREFIX_PATTERNS = [
    r"\bsdn\b",
    r"\bsd\s+negeri\b",
    r"\bsd\s+n\b",

    r"\bsmpn\b",
    r"\bsmp\s+negeri\b",
    r"\bsmp\s+n\b",

    r"\bsman\b",
    r"\bsma\s+negeri\b",
    r"\bsma\s+n\b",

    r"\bsmkn\b",
    r"\bsmk\s+negeri\b",
    r"\bsmk\s+n\b",
]


def _normalize_number_words(text: str) -> str:
    for word, number in NUMBER_WORDS.items():
        text = re.sub(rf"\b{word}\b", number, text)
    return text


def _normalize_school_prefix(text: str):

    replacements = {
        r"\bsdn\b": "sd negeri",
        r"\bsd\s+negeri\b": "sd negeri",
        r"\bsd\s+n\b": "sd negeri",

        r"\bsmpn\b": "smp negeri",
        r"\bsmp\s+negeri\b": "smp negeri",
        r"\bsmp\s+n\b": "smp negeri",

        r"\bsman\b": "sma negeri",
        r"\bsma\s+negeri\b": "sma negeri",
        r"\bsma\s+n\b": "sma negeri",

        r"\bsmkn\b": "smk negeri",
        r"\bsmk\s+negeri\b": "smk negeri",
        r"\bsmk\s+n\b": "smk negeri",
    }

    for pattern, repl in replacements.items():
        text = re.sub(pattern, repl, text)

    return text


def normalize_school_name(query: str) -> str:
    """
    Normalize school name variations.

    Example:
    smpn 1 bandung -> smp 1 bandung
    sma negeri satu bandung -> sma 1 bandung
    """

    if not query:
        return query

    text = query.lower().strip()

    text = _normalize_number_words(text)

    text = _normalize_school_prefix(text)

    text = re.sub(r"\s+", " ", text)

    return text
