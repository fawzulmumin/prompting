"""
Sort utilities untuk list school handler.
Dipindah dari list_school_handler.py (P17-A refactor).
"""
from typing import Dict, List, Optional

_SORT_QUALITY_KEYWORDS = {
    "terbaik", "unggulan", "favorit", "terfavorit", "top", "berkualitas",
    "terakreditasi a", "akreditasi a", "terbaik akreditasi",
    "data terbersih", "paling bersih", "kualitas data terbaik",
}
_SORT_MASALAH_KEYWORDS = {
    "paling bermasalah", "data paling kotor", "paling banyak masalah",
    "masalah terbanyak", "kualitas data terburuk",
}

_AKREDITASI_ORDER_SCRIPT = (
    "def v = doc.containsKey('akreditasi') && doc['akreditasi'].size() > 0"
    " ? doc['akreditasi'].value : 'Z';"
    "if (v == 'A') { return 1; }"
    "if (v == 'B') { return 2; }"
    "if (v == 'C') { return 3; }"
    "return 4;"
)


def detect_sort_mode(query: str, intent=None) -> str:
    if intent is not None:
        sort_by = getattr(intent, "sort_by", None)
        sort_order = getattr(intent, "sort_order", None)
        if sort_by == "total_masalah" and sort_order == "desc":
            return "bermasalah"
        if sort_by == "total_masalah" and sort_order == "asc":
            return "quality"
        if sort_by == "persen_bersih" and sort_order == "asc":
            return "bermasalah"
        if sort_by == "persen_bersih" and sort_order == "desc":
            return "quality"
    q = query.lower()
    if any(kw in q for kw in _SORT_MASALAH_KEYWORDS):
        return "bermasalah"
    if any(kw in q for kw in _SORT_QUALITY_KEYWORDS):
        return "quality"
    return "default"


def build_sort_clause(sort_mode: str, intent=None) -> List[Dict]:
    if intent is not None:
        sort_by = getattr(intent, "sort_by", None)
        sort_order = getattr(intent, "sort_order", None)
        if sort_by == "persen_bersih" and sort_order in ("asc", "desc"):
            return [
                {"qc_score.persen_bersih": {"order": sort_order, "missing": "_last"}},
                {"_score": {"order": "desc"}},
            ]
        if sort_by == "total_masalah" and sort_order in ("asc", "desc"):
            return [
                {"qc_score.total_masalah": {"order": sort_order, "missing": "_last"}},
                {"_score": {"order": "desc"}},
            ]
    if sort_mode == "quality":
        return [
            {"qc_score.persen_bersih": {"order": "desc", "missing": "_last"}},
            {"_script": {"type": "number",
                "script": {"lang": "painless", "source": _AKREDITASI_ORDER_SCRIPT},
                "order": "asc"}},
            {"_score": {"order": "desc"}},
        ]
    if sort_mode == "bermasalah":
        return [
            {"qc_score.total_masalah": {"order": "desc", "missing": "_last"}},
            {"_score": {"order": "desc"}},
        ]
    return [{"_score": {"order": "desc"}}]
