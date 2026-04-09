# confidence_engine.py

from typing import Dict
from app.services.entity_service import LOCATION_HINT_WORDS


def resolution_score(metadata: dict) -> float:
    if metadata.get("explicit_single_match"):
        return 1.0
    if metadata.get("auto_disambiguated"):
        return 0.85
    if metadata.get("auto_resolved_by_priority"):
        return 0.75
    # ── TAMBAH: cross-level match adalah resolusi yang reliable ──────────────
    if metadata.get("auto_resolved_cross_level"):
        return 0.80
    # ─────────────────────────────────────────────────────────────────────────
    if metadata.get("explicit_multi_match"):
        return 0.7
    if metadata.get("fuzzy_match"):
        return 0.6
    return 0.5


def ambiguity_score(resolution_status: str) -> float:
    if resolution_status == "AMBIGUOUS":
        return 0.0
    if resolution_status in ["MULTI_MATCH", "PARTIAL"]:
        return 0.6
    return 1.0


def data_strength_score(total_hits: int) -> float:
    if total_hits == 0:
        return 0.0
    if total_hits < 5:
        return 0.4
    if total_hits < 20:
        return 0.6
    if total_hits < 100:
        return 0.8
    return 1.0


def query_clarity_score(query: str, resolution_metadata: dict | None = None) -> float:
    resolution_metadata = resolution_metadata or {}
    q = query.lower()

    for word in LOCATION_HINT_WORDS:
        if word in q:
            return 1.0

    if resolution_metadata.get("explicit_single_match"):
        return 0.95
    if resolution_metadata.get("auto_resolved_cross_level"):   # ← TAMBAH
        return 0.90
    if resolution_metadata.get("fuzzy_match"):
        return 0.8
    if resolution_metadata.get("auto_disambiguated"):
        return 0.85

    return 0.6


def compute_aggregation_confidence(
    query: str,
    resolution_status: str,
    resolution_metadata: Dict,
    total_hits: int,
    ranking_gap: float = 0.0
) -> float:
    r = resolution_score(resolution_metadata or {})
    a = ambiguity_score(resolution_status)
    d = data_strength_score(total_hits)
    q = query_clarity_score(query, resolution_metadata or {})

    # ── ranking_gap hanya relevan untuk ranking queries ──────────────────────
    # Untuk aggregation biasa (berapa sekolah di X), gap selalu 0.0
    # sehingga selalu dapat 0.3 — ini tidak adil.
    # Solusi: jika gap == 0.0 (bukan ranking query), pakai data_strength
    # sebagai proxy untuk komponen ini instead.
    if ranking_gap == 0.0:
        # Bukan ranking query — gunakan proxy dari data quality
        g = min(d, 1.0)  # data_strength sudah cukup representatif
    else:
        g = ranking_gap_score(ranking_gap)
    # ─────────────────────────────────────────────────────────────────────────

    confidence = (
        0.30 * r +
        0.20 * a +
        0.20 * d +
        0.15 * q +
        0.15 * g
    )

    return round(confidence, 3)

def compute_rag_confidence(
    llm_score: float,
    retrieval_score: float,
    num_docs: int
) -> float:

    # normalisasi llm_score (0–100 → 0–1)
    llm = min(max(llm_score / 100, 0), 1)

    # retrieval score sudah 0–1
    retrieval = min(max(retrieval_score, 0), 1)

    # data strength ringan
    if num_docs == 0:
        data = 0.0
    elif num_docs < 3:
        data = 0.5
    else:
        data = 1.0

    confidence = (
        0.5 * llm +
        0.3 * retrieval +
        0.2 * data
    )

    return round(confidence, 3)

def compute_comparison_confidence(
    entities_count: int,
    total_hits: int
) -> float:

    if entities_count < 2:
        return 0.0

    if total_hits == 0:
        return 0.0

    base = 0.7 + min(total_hits / 200, 0.2)

    if total_hits > 100:
        base += 0.1

    return round(min(base, 1.0), 3)

def ranking_gap_score(gap: float) -> float:
    if gap > 0.5:
        return 1.0
    if gap > 0.25:
        return 0.8
    if gap > 0.1:
        return 0.6
    return 0.3
