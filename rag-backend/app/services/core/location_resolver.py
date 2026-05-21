"""
app/services/core/location_resolver.py
=========================================
LocationResolver: bridge between raw LLM wilayah output and
canonical ES keyword filter values.

5-step resolution chain per level:
  1. Exact match      (confidence=1.0)
  2. Prefix-stripped  (confidence=0.95)
  3. Hierarchy enrich (confidence=0.9, fills higher levels from lower)
  4. Fuzzy match      (confidence=0.7, single-word only, len>=5)
  5. Unresolved       (confidence=0.0, return raw value)

Ambiguity:
  - Same kecamatan/desa name in multiple kabupaten -> ambiguous=True
  - Disambiguated via context (existing kabupaten/kecamatan) when possible
  - If still ambiguous after disambiguation -> confidence capped at 0.5
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Ordered longest-first so longer prefixes are stripped before shorter ones
_STRIP_PREFIXES = [
    "kalimantan ",     # guard: province prefix that looks like strippable prefix
    "provinsi ",
    "kabupaten ",
    "kecamatan ",
    "kelurahan ",
    "prov. ",
    "kab. ",
    "kec. ",
    "kel. ",
    "kota ",
    "desa ",
]

_LEVEL_TO_HIERARCHY_KEY = {
    "desa_kelurahan": "desa_kelurahan",
    "kecamatan":      "kecamatan",
    "kabupaten":      "kabupaten",
    "provinsi":       None,  # top level, no hierarchy above
}


@dataclass
class ResolvedWilayah:
    """Canonical ES values after resolution, plus transparency metadata."""
    provinsi:   Optional[str] = None
    kabupaten:  Optional[str] = None
    kecamatan:  Optional[str] = None
    desa:       Optional[str] = None
    confidence: float = 1.0
    method:     str   = "exact"   # "exact"|"prefix_stripped"|"hierarchy_enrich"|"fuzzy"|"unresolved"
    ambiguous:  bool  = False
    candidates: list  = field(default_factory=list)


def _strip_location_prefix(name: str) -> str:
    """Strip common Indonesian location prefixes, return stripped name."""
    lower = name.lower()
    for prefix in _STRIP_PREFIXES:
        if lower.startswith(prefix):
            return name[len(prefix):].strip()
    return name


class LocationResolver:
    """
    Resolves WilayahEntities raw names to canonical ES keyword values.

    Safe to instantiate without ES client — reads only from global
    LOCATION_INDEX / LOCATION_HIERARCHY populated at startup.
    Returns raw LLM values gracefully when index is not yet loaded.

    Usage:
        resolver = LocationResolver()
        resolved = resolver.resolve(intent.wilayah)
        intent.wilayah = WilayahEntities(
            provinsi=resolved.provinsi,
            kabupaten=resolved.kabupaten,
            ...
        )
    """

    def resolve(self, wilayah) -> ResolvedWilayah:
        """
        Resolve WilayahEntities to canonical ES values.

        Processes most-specific level first (desa -> kecamatan -> kabupaten -> provinsi),
        then enriches higher levels from LOCATION_HIERARCHY automatically.
        """
        if wilayah is None:
            return ResolvedWilayah()

        # Graceful passthrough when index not yet populated (e.g. cold startup)
        try:
            from app.services.location_service import LOCATION_INDEX, LOCATION_HIERARCHY
        except ImportError:
            logger.debug("[LocationResolver] Cannot import location_service")
            return self._passthrough(wilayah, reason="import_error")

        if not LOCATION_INDEX:
            logger.debug("[LocationResolver] LOCATION_INDEX empty, using raw values")
            return self._passthrough(wilayah, reason="index_empty")

        result = ResolvedWilayah()
        worst_method = "exact"
        worst_conf   = 1.0
        any_ambiguous = False
        all_candidates: list = []

        # ── Process desa ──────────────────────────────────────────────────
        if wilayah.desa:
            canonical, method, conf, ambiguous, cands = self._resolve_single(
                wilayah.desa,
                "desa_kelurahan",
                LOCATION_INDEX,
                LOCATION_HIERARCHY,
                context_kabupaten=wilayah.kabupaten,
                context_kecamatan=wilayah.kecamatan,
            )
            # Graceful fallback: if desa is unresolved but kecamatan is available,
            # drop the unresolved desa so kecamatan scope is used instead.
            if method == "unresolved" and wilayah.kecamatan:
                logger.info(
                    "[LocationResolver] desa '%s' unresolved — dropping to kecamatan '%s' scope",
                    wilayah.desa, wilayah.kecamatan,
                )
            else:
                result.desa = canonical
                worst_method, worst_conf = self._degrade(worst_method, worst_conf, method, conf)
                if ambiguous:
                    any_ambiguous = True
                    all_candidates = cands
                    worst_conf = min(worst_conf, 0.5)
                # Enrich kecamatan/kabupaten/provinsi — pass kecamatan context for disambiguation
                if canonical:
                    self._enrich_from_desa(
                        canonical, result, LOCATION_HIERARCHY,
                        context_kecamatan=wilayah.kecamatan,
                    )

        # ── Process kecamatan (only if not already enriched from desa) ────
        if wilayah.kecamatan and result.kecamatan is None:
            canonical, method, conf, ambiguous, cands = self._resolve_single(
                wilayah.kecamatan,
                "kecamatan",
                LOCATION_INDEX,
                LOCATION_HIERARCHY,
                context_kabupaten=wilayah.kabupaten or result.kabupaten,
            )
            # Graceful fallback: if kecamatan is unresolved but kabupaten is available,
            # drop the unresolved kecamatan so kabupaten scope is used instead.
            if method == "unresolved" and (wilayah.kabupaten or result.kabupaten):
                logger.info(
                    "[LocationResolver] kecamatan '%s' unresolved — dropping to kabupaten scope",
                    wilayah.kecamatan,
                )
            else:
                result.kecamatan = canonical
                worst_method, worst_conf = self._degrade(worst_method, worst_conf, method, conf)
                if ambiguous:
                    any_ambiguous = True
                    all_candidates = cands
                    worst_conf = min(worst_conf, 0.5)
                if canonical and result.kabupaten is None:
                    self._enrich_from_kecamatan(
                        canonical, result, LOCATION_HIERARCHY,
                        context_kabupaten=wilayah.kabupaten,
                    )

        # ── Process kabupaten (only if not already enriched) ──────────────
        if wilayah.kabupaten and result.kabupaten is None:
            canonical, method, conf, ambiguous, cands = self._resolve_single(
                wilayah.kabupaten,
                "kabupaten",
                LOCATION_INDEX,
                LOCATION_HIERARCHY,
            )
            result.kabupaten = canonical
            worst_method, worst_conf = self._degrade(worst_method, worst_conf, method, conf)
            if ambiguous:
                any_ambiguous = True
                all_candidates = cands
                worst_conf = min(worst_conf, 0.5)
            if canonical and result.provinsi is None:
                self._enrich_from_kabupaten(canonical, result, LOCATION_HIERARCHY)

        # ── Process provinsi (only if not already enriched) ───────────────
        if wilayah.provinsi and result.provinsi is None:
            canonical, method, conf, ambiguous, cands = self._resolve_single(
                wilayah.provinsi,
                "provinsi",
                LOCATION_INDEX,
                LOCATION_HIERARCHY,
            )
            result.provinsi = canonical
            worst_method, worst_conf = self._degrade(worst_method, worst_conf, method, conf)

        result.confidence = worst_conf
        result.method     = worst_method
        result.ambiguous  = any_ambiguous
        result.candidates = all_candidates
        return result

    # ──────────────────────────────────────────────────────────────────────
    # Internal resolution
    # ──────────────────────────────────────────────────────────────────────

    def _resolve_single(
        self,
        name: str,
        level: str,
        index: dict,
        hierarchy: dict,
        context_kabupaten: Optional[str] = None,
        context_kecamatan: Optional[str] = None,
    ) -> tuple:
        """
        Resolve one name at one level.
        Returns (canonical, method, confidence, ambiguous, candidates).
        """
        if not name:
            return None, "exact", 1.0, False, []

        lv_index = index.get(level, {})
        key = name.lower().strip()

        # 1. Exact match
        if key in lv_index:
            canonical = lv_index[key]
            ambig, cands = self._check_ambiguity(
                canonical, level, hierarchy, context_kabupaten, context_kecamatan
            )
            return canonical, "exact", 1.0, ambig, cands

        # 2. Prefix-stripped match
        stripped = _strip_location_prefix(key)
        if stripped and stripped != key and stripped in lv_index:
            canonical = lv_index[stripped]
            ambig, cands = self._check_ambiguity(
                canonical, level, hierarchy, context_kabupaten, context_kecamatan
            )
            return canonical, "prefix_stripped", 0.95, ambig, cands

        # Also try stripping prefix from original (mixed-case) and lowercase again
        stripped_raw = _strip_location_prefix(name.strip()).lower()
        if stripped_raw and stripped_raw != key and stripped_raw in lv_index:
            canonical = lv_index[stripped_raw]
            ambig, cands = self._check_ambiguity(
                canonical, level, hierarchy, context_kabupaten, context_kecamatan
            )
            return canonical, "prefix_stripped", 0.95, ambig, cands

        # 3. Fuzzy match — single word only, length >= 5
        if len(key) >= 5 and " " not in key:
            try:
                from app.utils.location_utils import fuzzy_match_location
                fuzzy_hit = fuzzy_match_location(key, levels=[level])
                if fuzzy_hit:
                    matched_name, _ = fuzzy_hit
                    if matched_name in lv_index:
                        canonical = lv_index[matched_name]
                        ambig, cands = self._check_ambiguity(
                            matched_name, level, hierarchy,
                            context_kabupaten, context_kecamatan,
                        )
                        return canonical, "fuzzy", 0.7, ambig, cands
            except ImportError:
                pass

        # 4. Unresolved
        return key, "unresolved", 0.0, False, []

    def _check_ambiguity(
        self,
        name: str,
        level: str,
        hierarchy: dict,
        context_kabupaten: Optional[str] = None,
        context_kecamatan: Optional[str] = None,
    ) -> tuple[bool, list]:
        """
        Detect and attempt to disambiguate a name.
        Returns (still_ambiguous, candidates).
        """
        if level not in ("kecamatan", "desa_kelurahan"):
            return False, []

        hier_key = name.lower()
        entries = hierarchy.get(level, {}).get(hier_key, [])

        if len(entries) <= 1:
            return False, []

        candidates = [dict(e, name=name) for e in entries]

        # Disambiguate via kabupaten context
        if context_kabupaten:
            ctx = context_kabupaten.lower()
            filtered = [
                e for e in entries
                if ctx in e.get("kabupaten", "").lower()
            ]
            if len(filtered) == 1:
                return False, []
            if filtered:
                entries = filtered
                candidates = [dict(e, name=name) for e in filtered]

        # Disambiguate via kecamatan context (for desa level)
        if context_kecamatan and level == "desa_kelurahan":
            ctx = context_kecamatan.lower()
            filtered = [
                e for e in entries
                if ctx in e.get("kecamatan", "").lower()
            ]
            if len(filtered) == 1:
                logger.debug(
                    "[LocationResolver] desa '%s' disambiguated by kecamatan '%s'",
                    name, context_kecamatan,
                )
                return False, []
            if filtered:
                candidates = [dict(e, name=name) for e in filtered]

        return True, candidates[:5]

    # ──────────────────────────────────────────────────────────────────────
    # Hierarchy enrichment helpers
    # ──────────────────────────────────────────────────────────────────────

    def _enrich_from_desa(
        self,
        desa_canonical: str,
        result: ResolvedWilayah,
        hierarchy: dict,
        context_kecamatan: Optional[str] = None,
    ):
        key = desa_canonical.lower()
        entries = hierarchy.get("desa_kelurahan", {}).get(key, [])
        if not entries:
            return
        chosen = entries[0]
        # Disambiguate by kecamatan context when multiple entries share the same desa name
        if context_kecamatan and len(entries) > 1:
            ctx = context_kecamatan.lower()
            for e in entries:
                if ctx in e.get("kecamatan", "").lower():
                    chosen = e
                    logger.debug(
                        "[LocationResolver] desa '%s' enriched via kecamatan context '%s'",
                        desa_canonical, context_kecamatan,
                    )
                    break
        if result.kecamatan is None and chosen.get("kecamatan"):
            result.kecamatan = chosen["kecamatan"]
        if result.kabupaten is None and chosen.get("kabupaten"):
            result.kabupaten = chosen["kabupaten"]
        if result.provinsi is None and chosen.get("provinsi"):
            result.provinsi = chosen["provinsi"]

    def _enrich_from_kecamatan(
        self,
        kec_canonical: str,
        result: ResolvedWilayah,
        hierarchy: dict,
        context_kabupaten: Optional[str] = None,
    ):
        key = kec_canonical.lower()
        entries = hierarchy.get("kecamatan", {}).get(key, [])
        if not entries:
            return
        chosen = entries[0]
        if context_kabupaten and len(entries) > 1:
            ctx = context_kabupaten.lower()
            for e in entries:
                if ctx in e.get("kabupaten", "").lower():
                    chosen = e
                    break
        if result.kabupaten is None and chosen.get("kabupaten"):
            result.kabupaten = chosen["kabupaten"]
        if result.provinsi is None and chosen.get("provinsi"):
            result.provinsi = chosen["provinsi"]

    def _enrich_from_kabupaten(self, kab_canonical: str, result: ResolvedWilayah, hierarchy: dict):
        key = kab_canonical.lower()
        entries = hierarchy.get("kabupaten", {}).get(key, [])
        if entries and result.provinsi is None:
            e = entries[0]
            if e.get("provinsi"):
                result.provinsi = e["provinsi"]

    # ──────────────────────────────────────────────────────────────────────
    # Utility helpers
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _degrade(
        cur_method: str, cur_conf: float,
        new_method: str, new_conf: float,
    ) -> tuple[str, float]:
        """Keep the worst (lowest confidence) method/confidence seen so far."""
        if new_conf < cur_conf:
            return new_method, new_conf
        return cur_method, cur_conf

    @staticmethod
    def _passthrough(wilayah, reason: str = "") -> ResolvedWilayah:
        """Return raw LLM values unchanged when index is unavailable."""
        logger.debug("[LocationResolver] passthrough reason=%s", reason)
        return ResolvedWilayah(
            provinsi=getattr(wilayah, "provinsi", None),
            kabupaten=getattr(wilayah, "kabupaten", None),
            kecamatan=getattr(wilayah, "kecamatan", None),
            desa=getattr(wilayah, "desa", None),
            confidence=0.5,
            method="unresolved",
        )


# Singleton — safe to import at module level
_default_resolver = LocationResolver()


def resolve_wilayah(wilayah) -> ResolvedWilayah:
    """Convenience function using the default singleton resolver."""
    return _default_resolver.resolve(wilayah)


def strip_loc_prefix(value: str) -> str:
    """Strip standard Indonesian location prefixes and 'adm.' from a place name."""
    value = re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", value)
    value = re.sub(r"^adm\.\s*", "", value)
    return value.strip()
