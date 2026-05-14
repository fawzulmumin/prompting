"""
app/classification/tier1_pattern.py
=====================================
Tier 1: Pattern-based fast-path classifier.

Handles ~50% of queries using regex + keyword matching.
No LLM, no async, no external calls. Target latency: <5ms.

Jika tidak confident (confidence < CONFIDENCE_THRESHOLD) → return None,
biarkan Tier 2 (rule) atau Tier 3 (LLM) handle.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.70  # minimum untuk commit ke satu intent


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Trigger words per intent — order matters (more specific first)
_PATTERNS: list[tuple[str, str, float]] = [
    # (pattern_name, regex, base_confidence)

    # --- OUT OF CONTEXT (high confidence — jelas bukan sekolah) ---
    ("oot_politic",     r"\b(presiden|gubernur|bupati|walikota|dprd|pilkada|pemilu|pilpres)\b", 0.95),
    ("oot_creative",    r"\b(puisi|cerpen|essay|karangan|lagu|novel|cerita fiksi)\b", 0.95),
    ("oot_price",       r"\b(harga|beli|jual|toko|marketplace|tokopedia|shopee|lazada)\b", 0.95),
    ("oot_medical",     r"\b(dokter|rumah sakit|penyakit|obat|resep|apotek)\b", 0.92),
    ("oot_weather",     r"\b(cuaca|hujan|prakiraan|bmkg|suhu hari ini)\b", 0.92),
    ("oot_recipe",      r"\b(resep masakan|cara memasak|bahan makanan|bumbu)\b", 0.92),

    # --- PROFILE (nama sekolah spesifik atau NPSN) ---
    ("profile_npsn",    r"\bnpsn\s*\d{8}\b|\b\d{8}\b", 0.95),
    ("profile_nama",    r"\b(sdn|sd negeri|smpn|smp negeri|sman|sma negeri|smkn|smk negeri|"
                        r"man|mts negeri|min)\s+\d+\b", 0.90),
    ("profile_info",    r"\b(profil|profile|info|informasi|kontak|alamat|telepon)\b.{0,30}"
                        r"\b(sd|smp|sma|smk|mi|mts|ma|slb|tk)\b", 0.80),

    # --- GEO / NEARBY ---
    ("geo_terdekat",    r"\b(terdekat|paling dekat|dekat dengan|sekitar|radius)\b", 0.90),
    ("geo_koordinat",   r"\b(koordinat|latitude|longitude|lat\s*[\-\d]|lon\s*[\-\d])\b", 0.95),
    ("geo_landmark",    r"\b(dari|dekat)\s+\b(stasiun|terminal|bandara|mall|rumah sakit|"
                        r"kantor|masjid|gereja|monas|bundaran)\b", 0.85),

    # --- COMPARISON ---
    ("comp_vs",         r"\b(vs|versus)\b", 0.90),
    ("comp_bandingkan", r"\b(bandingkan|perbandingan|dibandingkan|compare)\b", 0.90),
    ("comp_mana_lebih", r"\b(mana yang lebih|mana yang paling|lebih banyak|lebih sedikit)\b", 0.85),
    ("comp_multi_loc",  r"\b(di\s+\w[\w\s]{1,20}?\s+dan\s+\w[\w\s]{1,20}?\s*(berapa|jumlah))\b", 0.75),

    # --- AGGREGATION (count/jumlah tanpa "daftar"/"tampilkan") ---
    ("agg_berapa",      r"^(berapa|brapa|ada berapa|jumlah|total)\b(?!.*\b(daftar|tampilkan|list|sebutkan)\b)", 0.85),
    ("agg_count_kw",    r"\b(berapa|brapa|jumlah|total|banyak)\b.{0,40}"
                        r"\b(sekolah|sd|smp|sma|smk|mi|mts|ma|tk|slb|pkbm)\b", 0.80),
    ("agg_ranking",     r"\b(ranking|peringkat|terbanyak|tersedikit|paling banyak|urutan)\b", 0.82),
    ("agg_breakdown",   r"\b(breakdown|distribusi|komposisi|per (provinsi|kabupaten|kecamatan))\b", 0.85),

    # --- LIST (daftar/tampilkan/cari) ---
    ("list_daftar",     r"\b(daftar|tampilkan|tunjukkan|lihat|show|sebutkan|list)\b", 0.82),
    ("list_cari",       r"^(cari|temukan|find|mana saja)\b", 0.80),
    ("list_ada_apa",    r"\b(ada (sekolah|sd|smp|sma|smk) apa|sekolah apa saja)\b", 0.85),
]

# Compile semua pattern sekali saat module load
_COMPILED: list[tuple[str, re.Pattern, float]] = [
    (name, re.compile(pattern, re.IGNORECASE | re.DOTALL), conf)
    for name, pattern, conf in _PATTERNS
]

# Mapping pattern_name prefix → module
_PREFIX_TO_MODULE: dict[str, str] = {
    "oot_": "out_of_context",
    "profile_": "profile",
    "geo_": "geo",
    "comp_": "comparison",
    "agg_": "aggregation",
    "list_": "list",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class PatternResult:
    module: Optional[str]         # None = tidak confident
    confidence: float
    matched_pattern: str          # nama pattern yang trigger, "" jika tidak ada
    duration_ms: float
    all_matches: list[str] = field(default_factory=list)  # semua pattern yang match


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------
class PatternClassifier:
    """
    Tier 1: regex-based fast-path classifier.

    Usage:
        clf = PatternClassifier()
        result = clf.classify("berapa SD di bandung")
        if result.module:
            # confident → use result.module
        else:
            # tidak confident → serahkan ke Tier 2
    """

    def classify(self, query: str) -> PatternResult:
        t0 = time.perf_counter()
        query_clean = query.strip()

        matches: list[tuple[str, float]] = []  # (pattern_name, confidence)

        for name, compiled, base_conf in _COMPILED:
            if compiled.search(query_clean):
                matches.append((name, base_conf))

        duration_ms = (time.perf_counter() - t0) * 1000

        if not matches:
            return PatternResult(
                module=None,
                confidence=0.0,
                matched_pattern="",
                duration_ms=duration_ms,
                all_matches=[],
            )

        # Resolve module dari setiap match
        module_scores: dict[str, float] = {}
        for name, conf in matches:
            module = self._pattern_to_module(name)
            if module:
                # Jika module sama match beberapa kali → ambil max confidence
                module_scores[module] = max(module_scores.get(module, 0.0), conf)

        if not module_scores:
            return PatternResult(
                module=None,
                confidence=0.0,
                matched_pattern=matches[0][0] if matches else "",
                duration_ms=duration_ms,
                all_matches=[m[0] for m in matches],
            )

        # Ambil module dengan score tertinggi
        best_module = max(module_scores, key=lambda m: module_scores[m])
        best_conf = module_scores[best_module]

        # Semantic priority: comparison menang dari aggregation
        # (mirrors cognitive_router ATURAN 3: comparison SELALU menang jika ada trigger kata comparison)
        priority_applied = False
        if "comparison" in module_scores and "aggregation" in module_scores:
            best_module = "comparison"
            best_conf = module_scores["comparison"]
            priority_applied = True

        if not priority_applied:
            # Multi-intent detection: jika 2 modul berbeda sama-sama kuat (>= 0.80),
            # pattern classifier tidak cukup untuk decide → defer ke Tier 2/3.
            strong_modules = {m: s for m, s in module_scores.items() if s >= 0.80}
            if len(strong_modules) >= 2:
                return PatternResult(
                    module=None,
                    confidence=0.0,
                    matched_pattern="multi_intent_conflict",
                    duration_ms=duration_ms,
                    all_matches=[m[0] for m in matches],
                )

            # Conflict detection: jika ada 2+ module dengan score berdekatan (gap <= 0.05)
            sorted_scores = sorted(module_scores.values(), reverse=True)
            if len(sorted_scores) >= 2:
                gap = sorted_scores[0] - sorted_scores[1]
                if gap <= 0.05:
                    # Conflict — turunkan confidence, biarkan tier berikutnya handle
                    best_conf = best_conf * 0.6

        # Tidak cukup confident → return None
        if best_conf < CONFIDENCE_THRESHOLD:
            return PatternResult(
                module=None,
                confidence=best_conf,
                matched_pattern=matches[0][0],
                duration_ms=duration_ms,
                all_matches=[m[0] for m in matches],
            )

        best_pattern = next(
            (name for name, conf in matches if self._pattern_to_module(name) == best_module),
            matches[0][0],
        )

        logger.debug(
            "[TIER1] query=%r module=%s conf=%.2f pattern=%s dur=%.1fms",
            query_clean[:60], best_module, best_conf, best_pattern, duration_ms,
        )

        return PatternResult(
            module=best_module,
            confidence=best_conf,
            matched_pattern=best_pattern,
            duration_ms=duration_ms,
            all_matches=[m[0] for m in matches],
        )

    def _pattern_to_module(self, pattern_name: str) -> Optional[str]:
        for prefix, module in _PREFIX_TO_MODULE.items():
            if pattern_name.startswith(prefix):
                return module
        return None

    def explain(self, query: str) -> str:
        """Debug helper: tampilkan semua pattern yang match."""
        result = self.classify(query)
        lines = [
            f"Query   : {query!r}",
            f"Module  : {result.module}",
            f"Conf    : {result.confidence:.2f}",
            f"Pattern : {result.matched_pattern}",
            f"Duration: {result.duration_ms:.2f}ms",
            f"All hits: {result.all_matches}",
        ]
        return "\n".join(lines)


# Singleton
_default_classifier = PatternClassifier()


def classify_pattern(query: str) -> PatternResult:
    """Convenience function menggunakan default classifier."""
    return _default_classifier.classify(query)
