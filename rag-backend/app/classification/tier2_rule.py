"""
app/classification/tier2_rule.py
==================================
Tier 2: Rule-based scoring classifier.

Handles ~20% more queries that Tier 1 deferred.
No LLM, uses additive scoring with diminishing returns.
Target latency: <20ms.

Jika tidak confident (confidence < CONFIDENCE_THRESHOLD) -> return None,
biarkan Tier 3 (LLM) handle.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.60


# ---------------------------------------------------------------------------
# Entity token sets
# ---------------------------------------------------------------------------

_JENJANG_TOKENS: frozenset[str] = frozenset({
    "sd", "sdn", "mi", "min", "tk", "ra", "kb", "tpa", "sps", "paud",
    "smp", "smpn", "mts", "mtsn",
    "sma", "sman", "ma", "man", "mak",
    "smk", "smkn", "slb", "pkbm", "skb", "lkp",
})

_STATUS_TOKENS: frozenset[str] = frozenset({
    "negeri", "swasta",
})

_AKREDITASI_TOKENS: frozenset[str] = frozenset({
    "akreditasi", "akred", "terakreditasi",
})

_LOCATION_TOKENS: frozenset[str] = frozenset({
    "provinsi", "kabupaten", "kota", "kecamatan", "desa", "kelurahan",
    "prov", "kab", "kec",
    "jakarta", "bandung", "surabaya", "semarang", "yogyakarta", "medan",
    "makassar", "palembang", "bekasi", "depok", "tangerang", "bogor",
    "jawa", "kalimantan", "sulawesi", "sumatera", "papua",
    "aceh", "bali", "lombok", "maluku", "riau", "lampung",
    "bengkulu", "jambi", "kepri", "babel",
})


# ---------------------------------------------------------------------------
# Signal definitions: (module, weight, pattern_str)
# ---------------------------------------------------------------------------

_SIGNAL_DEFS: list[tuple[str, float, str]] = [
    # Aggregation
    ("aggregation", 0.65, r"\b(berapa|brapa|ada berapa|jumlah|total|banyaknya)\b"),
    ("aggregation", 0.45, r"\b(ranking|peringkat|terbanyak|tersedikit|paling banyak|urutan|rangking)\b"),
    ("aggregation", 0.40, r"\b(distribusi|breakdown|komposisi|sebaran|persentase|proporsi)\b"),
    ("aggregation", 0.35, r"\b(per\s+(provinsi|kabupaten|kecamatan|desa|kota|wilayah|jenjang))\b"),
    ("aggregation", 0.30, r"\b(statistik|rekapitulasi|rekap)\b"),
    # Distribusi infrastruktur — "tidak punya listrik" / "tanpa internet" → aggregation
    ("aggregation", 0.65, r"\b(tidak\s+(punya|ada|memiliki)|tanpa|belum\s+(ada|punya))\s+(listrik|internet|air\s+bersih|pln)\b"),
    # Keyword infrastruktur saja (moderate) — "listrik apa", "pakai listrik PLN"
    ("aggregation", 0.50, r"\b(sumber\s+listrik|sumber\s+air|akses\s+internet|pakai\s+listrik|jenis\s+listrik)\b"),
    # "jenis sekolah" → breakdown jenjang (Phase 21-B1)
    ("aggregation", 0.60, r"\b(jenis\s+sekolah|tipe\s+sekolah|macam[- ]macam\s+sekolah|jenjang\s+apa\s+saja|ada\s+jenis\s+apa)\b"),
    # Multi-metric dalam satu query → aggregation (Phase 21-A)
    ("aggregation", 0.55, r"\bguru\s+(pns|asn|honor\w*|pppk|p3k|laki|perempuan)\s+(dan|dengan)\s+guru\b"),
    ("aggregation", 0.55, r"\bsiswa\s+(laki|perempuan|baru|lulus|mengulang)\s+(dan|dengan)\s+siswa\b"),
    # Numeric threshold → aggregation (Phase 23A)
    ("aggregation", 0.70, r"\b(lebih dari|kurang dari|di atas|di bawah|minimal|maksimal|setidaknya|paling banyak|paling sedikit)\s+\d+"),
    ("aggregation", 0.65, r"\b\d+\s*(siswa|guru|murid|peserta didik|kelas|ruangan|toilet|komputer)\b.*\b(lebih|kurang|atas|bawah)\b"),
    # Sekolah dengan kondisi numerik → list (pengguna ingin lihat daftar sekolah)
    ("list", 0.70, r"\b(sekolah|sd|smp|sma|smk)\b.*\b(dengan|yang punya|yang memiliki)\b.*\b\d+\b.*\b(siswa|guru|kelas)\b"),

    # List
    ("list", 0.65, r"\b(daftar|tampilkan|tunjukkan|lihat|show|sebutkan|list)\b"),
    ("list", 0.50, r"^(cari|temukan|find|mana saja)\b"),
    ("list", 0.45, r"\b(ada\s+(sekolah|sd|smp|sma|smk)\s+apa|sekolah\s+apa\s+saja)\b"),
    ("list", 0.35, r"\b(rekomendasi|rekomen|suggest|pilihan)\b"),

    # Profile
    ("profile", 0.70, r"\bnpsn\s*\d{8}\b|\b\d{8}\b"),
    ("profile", 0.55, r"\b(profil|profile|info|informasi|detail|kontak|alamat|telepon|website)\b"),
    ("profile", 0.50, r"\b(sdn|sd negeri|smpn|smp negeri|sman|sma negeri|smkn|smk negeri|man|mtsn|min)\s+\d+\b"),
    ("profile", 0.40, r"\b(visi|misi|sejarah|kepala sekolah)\b"),

    # Wilayah terpencil / perbatasan
    ("aggregation", 0.72, r"\b(terpencil|wilayah\s+terpencil|daerah\s+terpencil|3[Tt]|terdepan|tertinggal)\b"),
    ("list",        0.72, r"\b(terpencil|wilayah\s+terpencil|daerah\s+terpencil|3[Tt])\b"),
    ("aggregation", 0.70, r"\b(perbatasan|wilayah\s+perbatasan|daerah\s+perbatasan|perbatasan\s+negara)\b"),
    ("list",        0.70, r"\b(perbatasan|wilayah\s+perbatasan|daerah\s+perbatasan)\b"),

    # BOS (Bantuan Operasional Sekolah) filter
    ("aggregation", 0.75, r"\b(tidak\s+(dapat|menerima|ikut|peserta)\s+bos|bukan\s+peserta\s+bos|tanpa\s+bos|non.?bos)\b"),
    ("aggregation", 0.72, r"\b(peserta\s+bos|dapat\s+bos|menerima\s+bos|ikut\s+bos|penerima\s+bos)\b"),
    ("list",        0.72, r"\b(tidak\s+(dapat|menerima|ikut)\s+bos|bukan\s+peserta\s+bos)\b"),
    ("list",        0.70, r"\b(peserta\s+bos|dapat\s+bos|penerima\s+bos)\b"),

    # Comparison
    ("comparison", 0.65, r"\b(vs|versus)\b"),
    ("comparison", 0.65, r"\b(bandingkan|perbandingan|dibandingkan|compare)\b"),
    ("comparison", 0.55, r"\b(mana yang lebih|mana yang paling|lebih banyak|lebih sedikit)\b"),
    ("comparison", 0.45, r"\b(selisih|perbedaan|dibanding)\b"),

    # Geo
    ("geo", 0.65, r"\b(terdekat|paling dekat|dekat dengan|sekitar|radius)\b"),
    ("geo", 0.60, r"\b(koordinat|latitude|longitude|lat\s*[\-\d]|lon\s*[\-\d])\b"),
    ("geo", 0.50, r"\b(dari|dekat)\s+(stasiun|terminal|bandara|mall|rumah sakit|kantor|masjid|gereja|monas|bundaran)\b"),
    ("geo", 0.40, r"\b(jarak|kilometer|km)\b"),
]

# Compiled signal map
_SIGNAL_MAP: list[tuple[str, float, re.Pattern]] = [
    (module, weight, re.compile(pattern, re.IGNORECASE | re.DOTALL))
    for module, weight, pattern in _SIGNAL_DEFS
]

# Extra patterns for entity detection and bonuses
_MULTI_LOC_PATTERN = re.compile(
    r"\bdi\s+\w[\w\s]{1,20}?\s+dan\s+\w[\w\s]{1,20}?\b",
    re.IGNORECASE,
)
_JENJANG_PATTERN = re.compile(
    r"\b(sd|sdn|smp|smpn|sma|sman|smk|smkn|mi|min|mts|mtsn|man|ma|mak|tk|ra|slb|pkbm|skb)\b",
    re.IGNORECASE,
)
_STATUS_PATTERN = re.compile(
    r"\b(negeri|swasta)\b",
    re.IGNORECASE,
)
_NAMED_SCHOOL_PATTERN = re.compile(
    r"\b(sdn|sd negeri|smpn|smp negeri|sman|sma negeri|smkn|smk negeri|man|mtsn|min)\s+\d+\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class RuleResult:
    module: Optional[str]
    confidence: float
    signals: list[tuple[str, float]]   # (signal_desc, weight_contributed)
    duration_ms: float
    entity_hints: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------
class RuleClassifier:
    """
    Tier 2: rule-based scoring classifier.

    Usage:
        clf = RuleClassifier()
        result = clf.classify("sekolah di bandung akreditasi A")
        if result.module:
            # confident
        else:
            # defer to Tier 3 (LLM)
    """

    def classify(self, query: str) -> RuleResult:
        t0 = time.perf_counter()
        query_clean = query.strip()

        module_scores: dict[str, float] = {}
        fired_signals: list[tuple[str, float]] = []

        # --- Collect signals with additive diminishing returns ---
        for module, weight, compiled in _SIGNAL_MAP:
            if compiled.search(query_clean):
                prev = module_scores.get(module, 0.0)
                added = weight * (1.0 - prev * 0.5)
                module_scores[module] = min(prev + added, 0.98)
                fired_signals.append((f"{module}:{compiled.pattern[:40]}", weight))

        # --- Entity hints ---
        entity_hints: dict = {}

        jenjang_match = _JENJANG_PATTERN.findall(query_clean)
        if jenjang_match:
            entity_hints["jenjang"] = list(set(j.upper() for j in jenjang_match))

        status_match = _STATUS_PATTERN.search(query_clean)
        if status_match:
            entity_hints["status"] = status_match.group(0).upper()

        # --- Named school → profile bonus ---
        if _NAMED_SCHOOL_PATTERN.search(query_clean):
            prev = module_scores.get("profile", 0.0)
            bonus = 0.30 * (1.0 - prev * 0.5)
            module_scores["profile"] = min(prev + bonus, 0.98)
            fired_signals.append(("profile:named_school_bonus", 0.30))

        # --- Jenjang token → small boost to top list/aggregation/geo module ---
        if entity_hints.get("jenjang") and module_scores:
            top_module = max(module_scores, key=lambda m: module_scores[m])
            if top_module in ("list", "aggregation", "geo"):
                prev = module_scores[top_module]
                bonus = 0.10 * (1.0 - prev * 0.5)
                module_scores[top_module] = min(prev + bonus, 0.98)
                fired_signals.append((f"{top_module}:jenjang_bonus", 0.10))

        # --- Multi-location "di X dan Y" → comparison bonus ---
        if _MULTI_LOC_PATTERN.search(query_clean):
            if "aggregation" in module_scores or "list" in module_scores:
                comp_prev = module_scores.get("comparison", 0.0)
                if comp_prev < 0.50:
                    module_scores["comparison"] = min(comp_prev + 0.25, 0.98)
                    fired_signals.append(("comparison:multi_loc_bonus", 0.25))

        duration_ms = (time.perf_counter() - t0) * 1000

        if not module_scores:
            return RuleResult(
                module=None,
                confidence=0.0,
                signals=[],
                duration_ms=duration_ms,
                entity_hints=entity_hints,
            )

        # --- Semantic priority: comparison beats aggregation ---
        # Only override when comparison has a direct signal (>= 0.45), not just a bonus.
        priority_applied = False
        if "comparison" in module_scores and "aggregation" in module_scores:
            if module_scores["comparison"] >= 0.45:
                best_module = "comparison"
                best_conf = module_scores["comparison"]
                priority_applied = True

        if not priority_applied:
            best_module = max(module_scores, key=lambda m: module_scores[m])
            best_conf = module_scores[best_module]

            # Conflict: top 2 close (gap <= 0.10) → reduce confidence
            sorted_scores = sorted(module_scores.values(), reverse=True)
            if len(sorted_scores) >= 2:
                gap = sorted_scores[0] - sorted_scores[1]
                if gap <= 0.10:
                    best_conf = best_conf * 0.75

        if best_conf < CONFIDENCE_THRESHOLD:
            return RuleResult(
                module=None,
                confidence=best_conf,
                signals=fired_signals,
                duration_ms=duration_ms,
                entity_hints=entity_hints,
            )

        logger.debug(
            "[TIER2] query=%r module=%s conf=%.2f dur=%.1fms",
            query_clean[:60], best_module, best_conf, duration_ms,
        )

        return RuleResult(
            module=best_module,
            confidence=best_conf,
            signals=fired_signals,
            duration_ms=duration_ms,
            entity_hints=entity_hints,
        )

    def explain(self, query: str) -> str:
        """Debug helper: tampilkan semua signal yang fired."""
        result = self.classify(query)
        lines = [
            f"Query   : {query!r}",
            f"Module  : {result.module}",
            f"Conf    : {result.confidence:.2f}",
            f"Duration: {result.duration_ms:.2f}ms",
            f"Signals : {result.signals}",
            f"Entities: {result.entity_hints}",
        ]
        return "\n".join(lines)


# Singleton
_default_classifier = RuleClassifier()


def classify_rule(query: str) -> RuleResult:
    """Convenience function menggunakan default classifier."""
    return _default_classifier.classify(query)
