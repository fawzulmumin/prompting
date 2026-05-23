# app/services/core/cognitive_router.py
"""
CognitiveRouter v2 — orchestrator untuk 3-tier intent classification.

Arsitektur setelah P17-B refactor:
  Tier 1: tier1_pattern.py    (regex, <5ms)
  Tier 2: tier2_rule.py       (scoring, <20ms)
  Tier 3: tier3_llm.py        (LLMClassifier, DeepSeek)

Public API tidak berubah — semua import dari file lain tetap valid.
"""
import re
import logging
import asyncio
import hashlib
from typing import Optional, Dict, Any

from app.providers.deepseek import call_deepseek
from app.utils.tiered_cache import TieredCache
from app.utils.prompt_loader import load_prompt

# Re-export semua nama publik — file lain tidak perlu ganti import
from app.services.core.intent_models import (   # noqa: F401
    WilayahEntities,
    CognitiveIntent,
    VALID_MODULES,
    VALID_SCOPES,
    QueryRoute,
)
from app.services.core.keyword_fallback import (  # noqa: F401
    _keyword_fallback,
    wilayah_hint,
    nama_sekolah_hint,
)
from app.classification.tier3_llm import LLMClassifier

logger = logging.getLogger(__name__)

_intent_cache   = TieredCache(maxsize=512)
_llm_classifier = LLMClassifier()

_CACHE_TTL  = 86_400   # 24 jam
_LLM_TIMEOUT = 65.0

_SYSTEM_PROMPT: str = load_prompt("intent_classifier")

# Re-export _parse_llm_output untuk backward compat — tests import langsung dari sini
def _parse_llm_output(raw: str, query: str = "") -> Optional[CognitiveIntent]:
    return _llm_classifier.parse_llm_output(raw, query=query)


# ── Cache key helpers ────────────────────────────────────────────────────────

_TYPO_MAP = {
    r"\bbrapa\b":     "berapa",
    r"\bskla[h]?\b":  "sekolah",
    r"\bseklah\b":    "sekolah",
    r"\bsekoah\b":    "sekolah",
    r"\bsekolha\b":   "sekolah",
    r"\bnegri\b":     "negeri",
    r"\bnegrei\b":    "negeri",
    r"\bjmlh\b":      "jumlah",
    r"\bjmlah\b":     "jumlah",
    r"\btampilan\b":  "tampilkan",
    r"\bdafatra\b":   "daftar",
    r"\bdafrtar\b":   "daftar",
    r"\bpropinsi\b":  "provinsi",
    r"\bkabupetan\b": "kabupaten",
}


def _canonical_query(q: str) -> str:
    q = q.lower().strip()
    q = re.sub(r"\s+", " ", q)
    for pattern, replacement in _TYPO_MAP.items():
        q = re.sub(pattern, replacement, q, flags=re.IGNORECASE)
    return q


def _cache_key(q: str) -> str:
    h = hashlib.md5(_canonical_query(q).encode()).hexdigest()
    return f"cognitive_router:v6:{h}"


# ── Main orchestrator ────────────────────────────────────────────────────────

async def classify_and_extract(
    query:     str,
    use_cache: bool  = True,
    timeout:   float = _LLM_TIMEOUT,
) -> CognitiveIntent:
    """
    Entry point utama.
    Returns CognitiveIntent dengan module + semua entitas terekstrak.
    """
    ck = _cache_key(query)

    # Cache read
    if use_cache:
        try:
            cached = await _intent_cache.get(ck)
            if cached:
                cached_copy = dict(cached)
                wil_data = cached_copy.pop("wilayah", {})
                cached_copy.setdefault("sort_by", None)
                cached_copy.setdefault("sort_order", None)
                cached_copy.setdefault("qc_persen_min", None)
                cached_copy.setdefault("qc_persen_max", None)
                cached_copy.setdefault("qc_field_list", [])
                cached_copy.setdefault("qc_persen_query", False)
                cached_copy.setdefault("infrastruktur_filter", None)
                cached_copy.setdefault("distribusi_field", None)
                cached_copy.setdefault("metric_target_list", [])
                cached_copy.setdefault("numeric_filters", [])
                wil_data.setdefault("pulau", None)
                intent = CognitiveIntent(**cached_copy)
                intent.wilayah = WilayahEntities(**wil_data)
                return intent
        except Exception:
            pass

    # LLM call (Tier 3)
    intent: Optional[CognitiveIntent] = None
    try:
        prompt = _SYSTEM_PROMPT + "\n\nQuery: " + query
        raw = await asyncio.wait_for(
            call_deepseek(prompt, max_tokens=500, temperature=0.0),
            timeout,
        )
        intent = _llm_classifier.parse_llm_output(raw, query=query)
        if intent is not None:
            logger.info(
                "[CognitiveRouter] Parsed intent: module=%s secondary=%s scope=%s "
                "wilayah={prov=%s, kab=%s, kec=%s, desa=%s} "
                "jenjang=%s status=%s sub_queries_keys=%s",
                intent.module, intent.secondary, intent.scope,
                intent.wilayah.provinsi, intent.wilayah.kabupaten,
                intent.wilayah.kecamatan, intent.wilayah.desa,
                intent.jenjang, intent.status,
                list(intent.sub_queries.keys()),
            )
    except Exception as e:
        logger.warning(
            "[CognitiveRouter] LLM call failed: %s: %s",
            type(e).__name__, e or "(no message)",
        )

    # Keyword fallback (jika LLM gagal)
    if intent is None:
        logger.info("[CognitiveRouter] Using keyword fallback for: %r", query[:60])
        intent = _keyword_fallback(query)

    # Location resolution
    try:
        from app.services.core.location_resolver import resolve_wilayah
        resolved = resolve_wilayah(intent.wilayah)
        intent.wilayah = WilayahEntities(
            provinsi=resolved.provinsi,
            kabupaten=resolved.kabupaten,
            kecamatan=resolved.kecamatan,
            desa=resolved.desa,
            landmark=intent.wilayah.landmark,
            ambiguous=resolved.ambiguous,
            candidates=list(resolved.candidates) if resolved.candidates else [],
        )
        if resolved.method in ("fuzzy", "unresolved") or resolved.ambiguous:
            logger.warning(
                "[LocationResolver] method=%s confidence=%.2f ambiguous=%s "
                "candidates=%s | wilayah=%s",
                resolved.method, resolved.confidence, resolved.ambiguous,
                resolved.candidates[:3], intent.wilayah,
            )
    except Exception as _lr_exc:
        logger.warning("[LocationResolver] skipped: %s", _lr_exc)

    # ── Validate dependencies inject_from_result ─────────────────────────────
    # Guard: jika LLM menginvent key yang tidak ada di return value handler,
    # re-generate dependencies dari keyword_fallback yang sudah terbukti benar.
    _VALID_RESULT_KEYS   = {"top_location", "top_jenjang", "top_level"}
    _VALID_INJECT_FIELDS = {"provinsi", "kabupaten", "kecamatan", "desa", "jenjang"}
    _deps = getattr(intent, "dependencies", {}) or {}
    _deps_invalid = False
    for _dep_module, _dep_cfg in _deps.items():
        _inject_map = (_dep_cfg or {}).get("inject_from_result", {})
        for _intent_field, _result_key in _inject_map.items():
            if _result_key not in _VALID_RESULT_KEYS or _intent_field not in _VALID_INJECT_FIELDS:
                logger.warning(
                    "[CognitiveRouter] invalid inject_from_result: "
                    "field=%r key=%r — will re-generate from keyword_fallback",
                    _intent_field, _result_key,
                )
                _deps_invalid = True
                break

    if _deps_invalid:
        _fb_intent = _keyword_fallback(query)
        if _fb_intent.dependencies:
            intent.dependencies = _fb_intent.dependencies
            for _dep_mod in _fb_intent.dependencies:
                if _dep_mod not in intent.secondary:
                    intent.secondary.append(_dep_mod)
            logger.info(
                "[CognitiveRouter] dependencies overridden from keyword_fallback: %s",
                intent.dependencies,
            )
        else:
            intent.dependencies = {}
            logger.info("[CognitiveRouter] dependencies cleared (no valid fallback)")
    # ─────────────────────────────────────────────────────────────────────────

    # Cache write
    if use_cache:
        try:
            cache_data = {**intent.__dict__}
            cache_data["wilayah"] = intent.wilayah.__dict__
            await _intent_cache.set(ck, cache_data, ttl_l1=300, ttl_l2=1800)
        except Exception:
            pass

    return intent


async def classify_intent(
    query:     str,
    use_cache: bool  = True,
    timeout:   float = _LLM_TIMEOUT,
) -> CognitiveIntent:
    """Public alias untuk classify_and_extract — kompatibel dengan semua import lama."""
    return await classify_and_extract(query, use_cache=use_cache, timeout=timeout)


def is_education_query(query: str) -> bool:
    """Shim — rule-based, sinkron, tanpa LLM."""
    from app.services.core.intent_classifier import (
        classify_intent_rule as _rule_classify,
        IntentType,
    )
    result = _rule_classify(query)
    return result != IntentType.OUT_OF_CONTEXT
