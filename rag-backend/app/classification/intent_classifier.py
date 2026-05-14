"""
app/classification/intent_classifier.py
=========================================
Multi-tier Intent Classifier.

Dispatch chain:
  Tier 1 (Pattern, <5ms)   -> confident? commit
       | defer
  Tier 2 (Rule, <20ms)     -> confident? commit
       | defer
  Tier 3 (LLM, 5-65s)     -> always commits (existing cognitive_router)

Usage:
    classifier = IntentClassifier()
    result = await classifier.classify("berapa SD di bandung")
    print(result.module, result.tier_used, result.confidence)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal, Optional, Any

from app.classification.tier1_pattern import PatternClassifier
from app.classification.tier2_rule import RuleClassifier

logger = logging.getLogger(__name__)

TierUsed = Literal["tier1_pattern", "tier2_rule", "tier3_llm"]


@dataclass
class ClassificationResult:
    """Unified result dari semua tier."""
    module: str
    confidence: float
    tier_used: TierUsed
    duration_ms: float
    tier_durations: dict = field(default_factory=dict)
    fallback_chain: list = field(default_factory=list)
    raw_result: Optional[Any] = None
    secondary_module: Optional[str] = None
    extracted_entities: Optional[dict] = None


class IntentClassifier:
    """
    Multi-tier dispatcher: Pattern -> Rule -> LLM.

    Setiap tier bisa COMMIT (return modul) atau DEFER (pass ke berikutnya).
    Tier 3 (LLM) selalu commit.
    """

    def __init__(self):
        self._t1 = PatternClassifier()
        self._t2 = RuleClassifier()
        self._t3 = None  # lazy-loaded

    def _get_t3(self):
        if self._t3 is None:
            from app.services.core import cognitive_router as cr
            self._t3 = cr
        return self._t3

    async def classify(self, query: str) -> ClassificationResult:
        """Classify query melalui dispatch chain. Async karena Tier 3 (LLM) async."""
        t_start = time.perf_counter()
        fallback_chain: list[str] = []
        tier_durations: dict[str, float] = {}

        # ── Tier 1: Pattern ──────────────────────────────────────────────
        t1_start = time.perf_counter()
        r1 = self._t1.classify(query)
        tier_durations["tier1"] = (time.perf_counter() - t1_start) * 1000

        if r1.module is not None:
            total_ms = (time.perf_counter() - t_start) * 1000
            logger.info(
                "[CLASSIFIER] tier=1 module=%s conf=%.2f dur=%.1fms query=%r",
                r1.module, r1.confidence, total_ms, query[:60],
            )
            return ClassificationResult(
                module=r1.module,
                confidence=r1.confidence,
                tier_used="tier1_pattern",
                duration_ms=total_ms,
                tier_durations=tier_durations,
                fallback_chain=fallback_chain,
                raw_result=r1,
            )

        fallback_chain.append(f"tier1_defer(conf={r1.confidence:.2f})")

        # ── Tier 2: Rule ─────────────────────────────────────────────────
        t2_start = time.perf_counter()
        r2 = self._t2.classify(query)
        tier_durations["tier2"] = (time.perf_counter() - t2_start) * 1000

        if r2.module is not None:
            total_ms = (time.perf_counter() - t_start) * 1000
            logger.info(
                "[CLASSIFIER] tier=2 module=%s conf=%.2f dur=%.1fms query=%r",
                r2.module, r2.confidence, total_ms, query[:60],
            )
            return ClassificationResult(
                module=r2.module,
                confidence=r2.confidence,
                tier_used="tier2_rule",
                duration_ms=total_ms,
                tier_durations=tier_durations,
                fallback_chain=fallback_chain,
                raw_result=r2,
            )

        fallback_chain.append(f"tier2_defer(conf={r2.confidence:.2f})")

        # ── Tier 3: LLM (cognitive_router) ───────────────────────────────
        logger.info(
            "[CLASSIFIER] escalate_to_llm query=%r chain=%s",
            query[:60], fallback_chain,
        )

        t3_start = time.perf_counter()
        cr = self._get_t3()
        llm_result = await self._call_tier3(cr, query)
        tier_durations["tier3"] = (time.perf_counter() - t3_start) * 1000

        total_ms = (time.perf_counter() - t_start) * 1000
        fallback_chain.append(f"tier3_llm(dur={tier_durations['tier3']:.0f}ms)")

        module, confidence, secondary, entities = self._parse_llm_result(llm_result)

        logger.info(
            "[CLASSIFIER] tier=3 module=%s conf=%.2f dur=%.1fms query=%r",
            module, confidence, total_ms, query[:60],
        )

        return ClassificationResult(
            module=module,
            confidence=confidence,
            tier_used="tier3_llm",
            duration_ms=total_ms,
            tier_durations=tier_durations,
            fallback_chain=fallback_chain,
            raw_result=llm_result,
            secondary_module=secondary,
            extracted_entities=entities,
        )

    async def _call_tier3(self, cr, query: str):
        """Call cognitive_router. Wrapper isolates us from interface changes."""
        try:
            return await cr.classify_intent(query)
        except Exception as e:
            logger.error("[CLASSIFIER] tier3 error: %s", e)
            return self._make_fallback_result()

    def _parse_llm_result(self, result) -> tuple[str, float, Optional[str], Optional[dict]]:
        """
        Parse CognitiveIntent (or any object) into (module, confidence, secondary, entities).
        CognitiveIntent has no confidence field — LLM always commits at 0.90.
        """
        if result is None:
            return "out_of_context", 0.50, None, None

        # Dict shape
        if isinstance(result, dict):
            module = result.get("module") or result.get("primary") or "out_of_context"
            secondary_raw = result.get("secondary")
            entities = result.get("entities") or result.get("extracted")
            secondary = secondary_raw[0] if isinstance(secondary_raw, list) and secondary_raw else secondary_raw
            return str(module), 0.90, secondary, entities

        # CognitiveIntent / dataclass shape
        module = str(getattr(result, "module", None) or "out_of_context")
        secondary_raw = getattr(result, "secondary", None)
        secondary = secondary_raw[0] if isinstance(secondary_raw, list) and secondary_raw else secondary_raw

        # Build entity dict from CognitiveIntent fields
        entities: dict = {}
        for field_name in ("jenjang", "status", "akreditasi", "nama_sekolah", "npsn", "scope"):
            val = getattr(result, field_name, None)
            if val is not None:
                entities[field_name] = val
        wilayah = getattr(result, "wilayah", None)
        if wilayah is not None:
            entities["wilayah"] = wilayah

        return module, 0.90, secondary if secondary else None, entities if entities else None

    def _make_fallback_result(self):
        """Fallback object jika Tier 3 gagal total."""
        class _Fallback:
            module = "out_of_context"
            secondary = []
            scope = "nasional"
            jenjang = status = akreditasi = nama_sekolah = npsn = None
            wilayah = None
        return _Fallback()

    def classify_sync(self, query: str) -> ClassificationResult:
        """
        Sync-only: Tier 1 + Tier 2. Does NOT call LLM.
        Useful for testing, cache key generation, and fast pre-checks.
        Returns module="unknown" when both tiers defer.
        """
        t_start = time.perf_counter()

        r1 = self._t1.classify(query)
        if r1.module is not None:
            return ClassificationResult(
                module=r1.module,
                confidence=r1.confidence,
                tier_used="tier1_pattern",
                duration_ms=(time.perf_counter() - t_start) * 1000,
                raw_result=r1,
            )

        r2 = self._t2.classify(query)
        module = r2.module or "unknown"
        return ClassificationResult(
            module=module,
            confidence=r2.confidence,
            tier_used="tier2_rule",
            duration_ms=(time.perf_counter() - t_start) * 1000,
            fallback_chain=["tier1_defer"],
            raw_result=r2,
        )


# Singleton
_default_classifier = IntentClassifier()


async def classify_intent(query: str) -> ClassificationResult:
    """Convenience async function — full dispatch chain including LLM."""
    return await _default_classifier.classify(query)


def classify_intent_sync(query: str) -> ClassificationResult:
    """Convenience sync function — Tier 1+2 only, no LLM."""
    return _default_classifier.classify_sync(query)
