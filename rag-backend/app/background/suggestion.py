# PRODUCTION FINAL suggestion.py
import os
import re
import logging
import asyncio
from typing import Dict, Any, List, Optional, Set
from datetime import datetime
from hashlib import sha256

from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import ConnectionError, TransportError

from app.utils.cache import set_cache, get_cache

logger = logging.getLogger(__name__)

# =========================================================
# Elasticsearch Client (Async Singleton + Circuit Breaker)
# =========================================================
_es_client: Optional[AsyncElasticsearch] = None
_es_available: bool = True

async def get_es_client() -> Optional[AsyncElasticsearch]:
    global _es_client, _es_available

    if not _es_available:
        return None

    if _es_client is None:
        try:
            logger.info("[Suggestion] Initializing Elasticsearch client...")
            _es_client = AsyncElasticsearch(
                hosts=[os.getenv("ELASTICSEARCH_NODE")],
                basic_auth=(
                    os.getenv("ELASTICSEARCH_USERNAME"),
                    os.getenv("ELASTICSEARCH_PASSWORD")
                ),
                max_retries=3,
                retry_on_timeout=True,
                request_timeout=20
            )

            await _es_client.ping()
            logger.info("[Suggestion] Elasticsearch connected ✓")

        except Exception as e:
            _es_available = False
            logger.error(f"[Suggestion] Elasticsearch disabled (circuit breaker): {e}")
            _es_client = None
            return None

    return _es_client

async def close_suggestion_client():
    global _es_client, _es_available
    if _es_client:
        await _es_client.aclose()
        _es_client = None
        _es_available = False

# =========================================================
# Text Utilities
# =========================================================
TITLE_PREFIX_PATTERNS = [
    r'^FAQ:\s*', r'^Internal Policy:\s*', r'^Knowledge Management:\s*',
    r'^SD:\s*', r'^SMP:\s*', r'^SMA:\s*', r'^SMK:\s*',
    r'^Pertanyaan:\s*', r'^Q:\s*', r'^Question:\s*',
    r'^Topik:\s*', r'^Topic:\s*'
]

STOPWORDS = {
    'yang','dan','di','dari','untuk','dengan','pada','ke','ini','itu','adalah','ada',
    'tidak','bisa','bagaimana','apa','siapa','kapan','dimana','mengapa','berapa',
    'apakah','atau','juga','saja','sudah','belum','akan','telah','sedang','harus',
    'boleh','bisakah','bolehkah','mengenai','tentang','kenapa'
}

QUESTION_PREFIX = [
    r'^apa\s+', r'^bagaimana\s+', r'^mengapa\s+', r'^kapan\s+', r'^siapa\s+',
    r'^di mana\s+', r'^berapa\s+', r'^apakah\s+', r'^bisakah\s+', r'^bolehkah\s+'
]


def clean_title(title: str) -> str:
    if not title:
        return "Topik terkait"

    cleaned = title.strip()
    for pattern in TITLE_PREFIX_PATTERNS:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)

    cleaned = cleaned.strip()
    return cleaned if len(cleaned) >= 3 else title.strip()


def normalize_query(query: str) -> str:
    if not query:
        return ""

    q = query.lower().strip()
    q = re.sub(r'\s+', ' ', q)

    for p in QUESTION_PREFIX:
        q = re.sub(p, '', q)

    return q.strip()


def extract_keywords(query: str) -> List[str]:
    words = re.findall(r'\b[a-zA-Z0-9]{3,}\b', query.lower())
    return list(dict.fromkeys([w for w in words if w not in STOPWORDS]))


def is_too_similar(a: str, b: str, threshold: float = 0.82) -> bool:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= threshold


# =========================================================
# Core Suggestion Engine
# =========================================================
async def generate_suggestions(
    payload: Dict[str, Any],
    max_suggestions: int = 5,
    ttl: int = 3600
) -> bool:

    if not payload or not payload.get("query"):
        return False

    query = payload["query"]
    normalized = normalize_query(query)

    # Cache key (hash-safe)
    qhash = sha256(query.encode()).hexdigest()[:16]
    date_key = datetime.utcnow().strftime("%Y%m%d")
    cache_key = f"suggestions:{qhash}:{date_key}"

    # Already cached
    cached = await get_cache(cache_key)
    if cached:
        return True

    # Skip ES path — idx_sekolah_qc bukan index FAQ,
    # langsung ke contextual fallback
    suggestions: List[Dict[str, Any]] = []

    # ---------------- Fallback path ----------------
    if not suggestions:
        q_lower = query.lower()
        keywords = extract_keywords(query)
        lokasi = next((w for w in keywords if len(w) > 4), None)
        lokasi_str = lokasi.capitalize() if lokasi else "wilayah ini"

        if any(k in q_lower for k in ("qc", "masalah", "koordinat", "error", "salah", "tidak valid")):
            fallback_titles = [
                f"Sekolah mana di {lokasi_str} yang koordinatnya kosong?",
                f"Berapa sekolah di {lokasi_str} yang datanya tidak lengkap?",
                "Bagaimana cara memeriksa NPSN sekolah yang tidak valid?",
                "Apa saja kriteria QC data sekolah yang gagal?",
            ]
        elif any(k in q_lower for k in ("jumlah", "berapa", "total", "statistik", "banyak")):
            fallback_titles = [
                f"Berapa total sekolah negeri di {lokasi_str}?",
                f"Apa distribusi jenjang sekolah di {lokasi_str}?",
                f"Berapa sekolah swasta aktif di {lokasi_str}?",
                "Bagaimana perbandingan sekolah SD, SMP, SMA di Indonesia?",
            ]
        elif any(k in q_lower for k in ("daftar", "list", "tampilkan", "sebutkan", "cari")):
            fallback_titles = [
                f"Daftar sekolah negeri di {lokasi_str}",
                f"Sekolah SMA terbaik di {lokasi_str}",
                f"Sekolah yang memiliki akreditasi A di {lokasi_str}",
                "Bagaimana cara mencari sekolah berdasarkan NPSN?",
            ]
        else:
            fallback_titles = [
                f"Apa informasi terbaru tentang sekolah di {lokasi_str}?",
                "Bagaimana cara mendaftar ke sekolah negeri?",
                "Apa perbedaan sekolah negeri dan swasta?",
                "Berapa total sekolah yang terdaftar di Indonesia?",
            ]

        for title in fallback_titles[:max_suggestions]:
            suggestions.append({"title": title, "score": 0.3, "source": "fallback"})

    if not suggestions:
        return False

    titles = [s["title"] for s in suggestions]

    await set_cache(cache_key, {
        "suggestions": titles,
        "timestamp": datetime.utcnow().isoformat(),
        "query": query,
        "count": len(titles),
        "source": suggestions[0].get("source")
    }, ttl)

    return True


# =========================================================
# Public API
# =========================================================
async def get_suggestions(query: str, max_suggestions: int = 5) -> Dict[str, Any]:

    qhash = sha256(query.encode()).hexdigest()[:16]
    date_key = datetime.utcnow().strftime("%Y%m%d")
    cache_key = f"suggestions:{qhash}:{date_key}"

    cached = await get_cache(cache_key)

    if cached:
        return {
            "suggestions": cached.get("suggestions", [])[:max_suggestions],
            "from_cache": True,
            "timestamp": cached.get("timestamp"),
            "source": cached.get("source", "cache")
        }

    payload = {"query": query}
    success = await generate_suggestions(payload, max_suggestions=max_suggestions)

    if success:
        cached = await get_cache(cache_key)
        if cached:
            return {
                "suggestions": cached.get("suggestions", [])[:max_suggestions],
                "from_cache": False,
                "timestamp": cached.get("timestamp"),
                "source": cached.get("source", "generated")
            }

    return {
        "suggestions": [],
        "from_cache": False,
        "error": "Suggestion generation failed"
    }


# =========================================================
# Healthcheck / Test
# =========================================================
if __name__ == "__main__":
    async def test():
        q = "Bagaimana cara mendaftar sekolah?"
        res = await get_suggestions(q, 5)
        print(res)

    asyncio.run(test())