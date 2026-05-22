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

# ── Province alias map ────────────────────────────────────────────────────────
# Maps bare alias/shorthand → canonical ES-ready province value (lowercase with
# "prov." prefix so build_filters/_build_provinsi_filter works out-of-the-box).
# Priority: if input matches here (and has no explicit kab/kec prefix), return
# scope=provinsi immediately — before LocationResolver sees it as a kabupaten.
PROVINCE_ALIAS_MAP: Dict[str, str] = {
    "jakarta":         "prov. dki jakarta",
    "yogyakarta":      "prov. d.i. yogyakarta",
    "jogja":           "prov. d.i. yogyakarta",
    "jogjakarta":      "prov. d.i. yogyakarta",
    "aceh":            "prov. aceh",
    "papua":           "prov. papua",
    "kalbar":          "prov. kalimantan barat",
    "kaltim":          "prov. kalimantan timur",
    "kalteng":         "prov. kalimantan tengah",
    "kalsel":          "prov. kalimantan selatan",
    "sulsel":          "prov. sulawesi selatan",
    "sulut":           "prov. sulawesi utara",
    "sulteng":         "prov. sulawesi tengah",
    "sultra":          "prov. sulawesi tenggara",
    "sumut":           "prov. sumatera utara",
    "sumsel":          "prov. sumatera selatan",
    "sumbar":          "prov. sumatera barat",
    "jabar":           "prov. jawa barat",
    "jateng":          "prov. jawa tengah",
    "jatim":           "prov. jawa timur",
    "ntb":             "prov. nusa tenggara barat",
    "ntt":             "prov. nusa tenggara timur",
    "malut":           "prov. maluku utara",
    "papua barat":     "prov. papua barat",
    "kepri":           "prov. kepulauan riau",
    "babel":           "prov. kepulauan bangka belitung",
    "diy":             "prov. d.i. yogyakarta",
    "dki":             "prov. dki jakarta",
    "dki jakarta":     "prov. dki jakarta",
    # Full Kalimantan names — LocationResolver sometimes fails to resolve these
    # from full-name queries like "total komputer di sekolah Kalimantan Barat"
    "kalimantan barat":   "prov. kalimantan barat",
    "kalimantan timur":   "prov. kalimantan timur",
    "kalimantan tengah":  "prov. kalimantan tengah",
    "kalimantan selatan": "prov. kalimantan selatan",
    "kalimantan utara":   "prov. kalimantan utara",
}

# Prefixes that signal the user explicitly chose kabupaten/kecamatan level —
# skip province-alias check when any of these are present.
_KAB_KEC_PREFIXES = ("kota ", "kab.", "kabupaten ", "kec.", "kecamatan ")


def _is_explicit_kab_kec(text: str) -> bool:
    low = text.lower().strip()
    return any(low.startswith(p) for p in _KAB_KEC_PREFIXES)


# ── Singleton resolver ────────────────────────────────────────────────────────
_resolver = LocationResolver()


def _get_resolver() -> LocationResolver:
    return _resolver


# ── Noise-word strip ──────────────────────────────────────────────────────────
_NOISE_PATTERN = re.compile(
    r"\b(ada|ada berapa|jumlah|berapa|total|daftar|tampilkan|sekolah|sd|smp|sma|smk|mi|mts|ma|"
    r"tk|paud|slb|pkbm|di|dari|dalam|wilayah|negeri|swasta|"
    r"bandingkan|perbandingan|vs|versus|dan|dengan|akreditasi|a|b|c|"
    r"guru|siswa|murid|tendik|komputer|listrik|pln|internet|"
    r"yang|tidak|punya|memiliki|belum|tanpa)\b",
    re.IGNORECASE,
)

# Pulau names that map to multi-province filter in aggregation_core.PULAU_PROVINSI_MAP
_PULAU_NAMES: frozenset = frozenset({
    "jawa", "sumatera", "kalimantan", "sulawesi", "papua",
    "bali", "maluku", "nusa tenggara", "indonesia timur",
})

_VS_PATTERN = re.compile(r"\s+(?:vs\.?|versus)\s+", re.IGNORECASE)


def _extract_location_tokens(query: str) -> str:
    cleaned = re.sub(r"[?!.,;:]", "", query)   # strip trailing/inline punctuation first
    cleaned = _NOISE_PATTERN.sub(" ", cleaned)
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
        kab = r.kabupaten
        # Location cache stores "kota jakarta X" (from minority ES docs without "adm."),
        # but the main ES corpus uses "kota adm. jakarta X". Normalize here.
        if kab.lower().startswith("kota jakarta "):
            kab = "kota adm. jakarta " + kab[len("kota jakarta "):]
        ent["kabupaten"] = kab
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

    # ── Pulau scope fast-path ─────────────────────────────────────────────────
    # If loc_text (after noise stripping) is a known pulau name, return
    # pulau entity so aggregation_core expands it to multi-province filter.
    _loc_lower = loc_text.lower().strip()
    if _loc_lower in _PULAU_NAMES:
        logger.debug("[LR_ADAPTER] pulau scope: '%s'", _loc_lower)
        _pulau_ent: Dict[str, Any] = {"pulau": _loc_lower}
        if status_sekolah:
            _pulau_ent["status_sekolah"] = status_sekolah
        return build_result(
            status=ResultStatus.SUCCESS,
            provider="LOCATION_RESOLVER_ADAPTER",
            data={"entities": _pulau_ent},
            metadata={"method": "pulau_scope"},
            confidence=1.0,
        )

    # ── Province alias fast-path (before LocationResolver) ───────────────────
    # If the bare location token is a known province alias and has no explicit
    # kab/kec prefix, resolve directly to province scope (confidence=1.0).
    if not _is_explicit_kab_kec(_loc_lower):
        # Strip prefix administratif sebelum alias lookup
        _stripped = re.sub(r'^(provinsi|prov\.?)\s+', '', _loc_lower).strip()
        _prov_canonical = PROVINCE_ALIAS_MAP.get(_stripped) or PROVINCE_ALIAS_MAP.get(_loc_lower)
        if _prov_canonical:
            logger.debug("[LR_ADAPTER] province alias: '%s' → '%s'", _loc_lower, _prov_canonical)
            _ent: Dict[str, Any] = {"provinsi": _prov_canonical}
            if status_sekolah:
                _ent["status_sekolah"] = status_sekolah
            return build_result(
                status=ResultStatus.SUCCESS,
                provider="LOCATION_RESOLVER_ADAPTER",
                data={"entities": _ent},
                metadata={"method": "province_alias", "status_sekolah": status_sekolah},
                confidence=1.0,
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

_LOCATION_FIELDS = {"provinsi", "kabupaten", "kecamatan", "desa_kelurahan", "pulau"}


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
        # Province alias fast-path for each VS part
        loc_lower = loc_text.lower().strip()
        if not _is_explicit_kab_kec(loc_lower):
            # Strip prefix administratif sebelum alias lookup
            _stripped_part = re.sub(r'^(provinsi|prov\.?)\s+', '', loc_lower).strip()
            prov_canonical = PROVINCE_ALIAS_MAP.get(_stripped_part) or PROVINCE_ALIAS_MAP.get(loc_lower)
            if prov_canonical:
                result.append({"provinsi": prov_canonical})
                continue
        wilayah = _make_wilayah(loc_text)
        resolved = _get_resolver().resolve(wilayah)
        if resolved.confidence > 0.0 and not resolved.ambiguous:
            ent = _resolved_to_entities(resolved)
            if ent:
                result.append(ent)
    return result
