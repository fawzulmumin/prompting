# retrieval_retry_handler.py
"""
Self-correction untuk retrieval: ketika initial search menghasilkan terlalu sedikit
atau rendah relevansi, expand scope secara bertahap sebelum menyerah.

Strategi:
  "strict"   → hasil awal dengan filter lokasi penuh
  "expanded" → hapus kecamatan, pertahankan kabupaten/provinsi
  "keyword_only" → hapus semua filter lokasi, gunakan text-only search

Log setiap attempt ke low_score_events.csv dengan tag [RETRY].
"""

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_LOG_DIR  = Path(__file__).parent.parent / "logs"
_LOG_FILE = _LOG_DIR / "low_score_events.csv"

# Maximum ES retry attempts per request (expanded + keyword_only).
# Guards against any future loop extension accidentally causing unbounded retries.
_MAX_RETRY_ATTEMPTS = 2


def _append_retry_log(
    query: str,
    attempt: str,
    score_before: float,
    score_after: float,
    hits_count: int,
) -> None:
    """Tulis satu baris [RETRY] ke low_score_events.csv (synchronous, ringan)."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().isoformat(),
                query.replace("\n", " ")[:200],
                f"[RETRY:{attempt}]",
                "retrieval_retry",
                hits_count,
                "",
                "",
                f"score_before={score_before:.3f} score_after={score_after:.3f}",
                "FALSE", "", "FALSE", "",
            ])
    except Exception:
        logger.debug("[RETRY] Failed to write retry log", exc_info=True)


def _build_expanded_filter(loc_filter: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Attempt 1: hapus kecamatan dari filter agar cakupan lebih luas.
    Jika filter hanya berisi kecamatan, return None (nasional scope).
    """
    expanded = {k: v for k, v in loc_filter.items() if k != "kecamatan"}
    return expanded if expanded else None


async def retry_with_expanded_query(
    es_client: Any,
    query: str,
    original_loc_filter: Optional[Dict[str, Any]],
    size: int = 5,
    score_before: float = 0.0,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Coba ulang pencarian dengan scope yang lebih luas.

    Returns:
        (hits, was_retried)
        - hits: hasil search terbaik yang ditemukan
        - was_retried: True jika retry berhasil menemukan hasil lebih baik
    """
    from app.services.search.search_service import (
        perform_search,
        compute_context_sufficiency_score,
    )

    if not original_loc_filter:
        return [], False

    attempts = 0

    # ── Attempt 1: expand (hapus kecamatan) ──────────────────────────
    expanded = _build_expanded_filter(original_loc_filter)
    if expanded != original_loc_filter and attempts < _MAX_RETRY_ATTEMPTS:
        attempts += 1
        hits_expanded = await perform_search(
            es_client=es_client,
            query_text=query,
            size=size,
            filter_level=None,
            location_filter=expanded or None,
        )
        score_expanded = compute_context_sufficiency_score(hits_expanded)
        _append_retry_log(query, "expanded", score_before, score_expanded, len(hits_expanded))
        logger.info(
            "[RETRY] expanded scope: %d hits, score %.3f → %.3f",
            len(hits_expanded), score_before, score_expanded,
        )
        if score_expanded >= 0.3:
            return hits_expanded, True

    # ── Attempt 2: keyword_only (hapus semua filter lokasi) ───────────
    if attempts < _MAX_RETRY_ATTEMPTS:
        attempts += 1
        hits_keyword = await perform_search(
            es_client=es_client,
            query_text=query,
            size=size,
            filter_level=None,
            location_filter=None,
        )
        score_keyword = compute_context_sufficiency_score(hits_keyword)
        _append_retry_log(query, "keyword_only", score_before, score_keyword, len(hits_keyword))
        logger.info(
            "[RETRY] keyword_only: %d hits, score %.3f → %.3f",
            len(hits_keyword), score_before, score_keyword,
        )
        if score_keyword >= 0.3:
            return hits_keyword, True

    return [], False
