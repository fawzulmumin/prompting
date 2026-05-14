"""
app/services/core/location_resolver_adapter.py
================================================
Drop-in replacement for entity_service.resolve_entities() that uses
LocationResolver (Phase 3) instead of the legacy find_all_location_matches stack.

Called only from aggregation_handler and comparison_handler.
Return dict structure is identical to resolve_entities():
    {
        "status":     ResultStatus.*,
        "provider":   str,
        "data":       {"entities": dict | list, ...},
        "reason":     None | str,
        "metadata":   dict,
        "confidence": float | None,
    }
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Any, Dict, Optional

from elasticsearch import AsyncElasticsearch

from app.services.core.location_resolver import LocationResolver, ResolvedWilayah
from app.services.core.result_model import ResultStatus, build_result

logger = logging.getLogger(__name__)

# ── Singleton resolver ────────────────────────────────────────────────────────
_resolver = LocationResolver()


def _get_resolver() -> LocationResolver:
    return _resolver


# ── Noise-word strip ──────────────────────────────────────────────────────────
_NOISE_PATTERN = re.compile(
    r"\b(jumlah|berapa|total|daftar|tampilkan|sekolah|sd|smp|sma|smk|mi|mts|ma|"
    r"tk|paud|slb|pkbm|di|dari|dalam|wilayah|negeri|swasta|"
    r"bandingkan|perbandingan|vs|versus|dan|dengan|akreditasi|a|b|c)\b",
    re.IGNORECASE,
)

_VS_PATTERN = re.compile(r"\s+(?:vs\.?|versus)\s+", re.IGNORECASE)


def _extract_location_tokens(query: str) -> str:
    cleaned = _NOISE_PATTERN.sub(" ", query)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _make_wilayah(loc_text: str) -> SimpleNamespace:
    """Wrap a raw location string into an object LocationResolver.resolve() can consume."""
    low = loc_text.lower().strip()
    w = SimpleNamespace(provinsi=None, kabupaten=None, kecamatan=None, desa=None)
    if low.startswith(("prov", "provinsi")):
        w.provinsi = loc_text
    elif low.startswith(("kab", "kabupaten", "kota")):
        w.kabupaten = loc_text
    elif low.startswith(("kec", "kecamatan")):
        w.kecamatan = loc_text
    elif low.startswith(("desa", "kelurahan", "kel")):
        w.desa = loc_text
    else:
        w.kabupaten = loc_text
    return w


def _resolved_to_entities(r: ResolvedWilayah) -> Dict[str, Any]:
    """Convert ResolvedWilayah → flat entities dict for use in ES query builders."""
    ent: Dict[str, Any] = {}
    if r.provinsi:
        ent["provinsi"] = r.provinsi
    if r.kabupaten:
        ent["kabupaten"] = r.kabupaten
    if r.kecamatan:
        ent["kecamatan"] = r.kecamatan
    if r.desa:
        ent["desa"] = r.desa
    return ent


def _detect_status_sekolah(query: str) -> Optional[str]:
    if re.search(r"\bnegeri\b", query, re.IGNORECASE):
        return "NEGERI"
    if re.search(r"\bswasta\b", query, re.IGNORECASE):
        return "SWASTA"
    return None


# ── Public API ────────────────────────────────────────────────────────────────

async def resolve_location_from_query(
    query: str,
    es_client: AsyncElasticsearch,  # kept for API parity; not used internally
) -> Dict[str, Any]:
    """
    Resolve location entities from a natural-language query.

    Drop-in replacement for entity_service.resolve_entities().
    Internally uses LocationResolver instead of the legacy matcher stack.
    """
    status_sekolah = _detect_status_sekolah(query)

    # ── VS / multi-location split ─────────────────────────────────────────────
    if _VS_PATTERN.search(query):
        parts = _VS_PATTERN.split(query)
        entities_list = _resolve_parts(parts)
        if len(entities_list) >= 2:
            return build_result(
                status=ResultStatus.SUCCESS,
                provider="LOCATION_RESOLVER_ADAPTER",
                data={"entities": entities_list, "is_multi": True},
                metadata={"vs_split": True, "status_sekolah": status_sekolah},
                confidence=0.9,
            )

    # ── Single location ───────────────────────────────────────────────────────
    loc_text = _extract_location_tokens(query)
    if not loc_text:
        return build_result(
            status=ResultStatus.NOT_FOUND,
            provider="LOCATION_RESOLVER_ADAPTER",
            metadata={"reason": "no_location_tokens"},
            confidence=0.0,
        )

    wilayah = _make_wilayah(loc_text)
    resolved = _get_resolver().resolve(wilayah)

    logger.debug(
        "[LR_ADAPTER] query=%s | loc=%s | method=%s | conf=%.2f | ambiguous=%s",
        query[:60], loc_text[:40], resolved.method, resolved.confidence, resolved.ambiguous,
    )

    # ── AMBIGUOUS ──────────────────────────────────────────────────────────────
    if resolved.ambiguous:
        return build_result(
            status=ResultStatus.AMBIGUOUS,
            provider="LOCATION_RESOLVER_ADAPTER",
            data={"candidates": resolved.candidates},
            metadata={"method": resolved.method, "status_sekolah": status_sekolah},
            confidence=resolved.confidence,
        )

    # ── NOT_FOUND ─────────────────────────────────────────────────────────────
    if resolved.confidence == 0.0 and resolved.method == "unresolved":
        return build_result(
            status=ResultStatus.NOT_FOUND,
            provider="LOCATION_RESOLVER_ADAPTER",
            metadata={"method": resolved.method, "status_sekolah": status_sekolah},
            confidence=0.0,
        )

    # ── SUCCESS ───────────────────────────────────────────────────────────────
    entities = _resolved_to_entities(resolved)
    if status_sekolah:
        entities["status_sekolah"] = status_sekolah

    return build_result(
        status=ResultStatus.SUCCESS,
        provider="LOCATION_RESOLVER_ADAPTER",
        data={"entities": entities},
        metadata={"method": resolved.method, "status_sekolah": status_sekolah},
        confidence=resolved.confidence,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

# ── Validator helpers (migrated from entity_service.py Phase 9) ───────────────

_LOCATION_FIELDS = {"provinsi", "kabupaten", "kecamatan", "desa_kelurahan"}


def validate_location_presence(entities: Dict[str, Any]) -> bool:
    """Return True if entities contains at least one location field."""
    if not entities:
        return False
    return bool(_LOCATION_FIELDS & set(entities.keys()))


def _detect_location_level_from_query(query: str) -> Optional[str]:
    q = re.sub(r"[^\w\s]", " ", query.lower()).strip()
    if re.search(r"\bprovinsi\b", q):
        return "provinsi"
    if re.search(r"\b(kabupaten|kab\.)\b", q):
        return "kabupaten"
    if re.search(r"\bkota\b", q):
        return "kota"
    if re.search(r"\b(kecamatan|kec\.)\b", q):
        return "kecamatan"
    if re.search(r"\b(desa|kelurahan)\b", q):
        return "desa_kelurahan"
    return None


def validate_hierarchy_consistency(query: str, entities: dict) -> bool:
    """Return True if the entities dict is consistent with the level requested in query."""
    requested = _detect_location_level_from_query(query)
    if not requested:
        return True
    if requested == "kota":
        return "kabupaten" in entities and entities["kabupaten"].startswith("kota")
    return requested in entities


def validate_hierarchy_integrity(entities: Dict[str, Any]) -> Dict[str, Any]:
    """Return build_result SUCCESS/VALIDATION_ERROR depending on hierarchy completeness."""
    if not entities:
        return build_result(status=ResultStatus.SUCCESS)
    if "kecamatan" in entities and "kabupaten" not in entities:
        return build_result(
            status=ResultStatus.VALIDATION_ERROR,
            metadata={"warning": None},
            reason="HIERARCHY_INCOMPLETE",
        )
    if "desa_kelurahan" in entities and "kecamatan" not in entities:
        return build_result(
            status=ResultStatus.VALIDATION_ERROR,
            reason="HIERARCHY_INCOMPLETE",
            metadata={"missing_parent": "kecamatan", "level": "desa_kelurahan"},
        )
    return build_result(status=ResultStatus.SUCCESS, metadata={"hierarchy_warning": None})


# ── Internal helpers ──────────────────────────────────────────────────────────

def _resolve_parts(parts: list) -> list:
    """Resolve each part of a VS query; return list of entity dicts (only successes)."""
    result = []
    for part in parts:
        loc_text = _extract_location_tokens(part.strip())
        if not loc_text:
            continue
        wilayah = _make_wilayah(loc_text)
        resolved = _get_resolver().resolve(wilayah)
        if resolved.confidence > 0.0 and not resolved.ambiguous:
            ent = _resolved_to_entities(resolved)
            if ent:
                result.append(ent)
    return result
