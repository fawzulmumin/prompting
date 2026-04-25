# search_service.py 

import os
import hashlib
import logging
import math
import asyncio as _asyncio
from typing import Optional, List, Dict, Any
 
from elasticsearch import AsyncElasticsearch
from elasticsearch import ApiError, TransportError
 
from app.services.embedding_service import get_embedding
from app.utils.cache import get_cache, set_cache
 
logger = logging.getLogger(__name__)
 
LOCATION_FIELDS = (
    "provinsi",
    "kabupaten",
    "kecamatan",
    "desa_kelurahan"
)
 
async def perform_search(
    es_client: AsyncElasticsearch,
    query_text: str,
    size: int = 5,
    filter_level: Optional[str] = None,
    location_filter: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Hybrid search dengan Reciprocal Rank Fusion (RRF).
    Otomatis fallback ke script_score jika ES < 8.8 atau RRF tidak tersedia.

    RRF menggabungkan:
      - BM25  (lexical match pada field `content`)
      - KNN   (semantic similarity via `content_vector`)
    tanpa masalah skala skor yang tidak seimbang.
    """
    try:
        vector = await _get_or_create_embedding(query_text)
        if not vector:
            logger.warning("[SEARCH] Empty embedding vector")
            return []

        index = os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")

        # ── Coba RRF dulu (ES 8.8+) ──────────────────────────────────────────
        try:
            # ===============================
            # DETECT QUERY MODE
            # ===============================
            def _is_explicit_school_name(q: str) -> bool:
                import re
                return bool(re.search(
                    r'\b(sd|mi|smp|mts|sma|smk|ma|sman|smpn|sdn|smkn|man|mtsn|min)\s*\d',
                    q.lower()
                ))

            query_mode = "keyword" if _is_explicit_school_name(query_text) else "semantic"

            # ===============================
            # BUILD QUERY
            # ===============================
            es_query = _build_es_query(
                query_text=query_text,
                vector=vector,
                size=size,
                filter_level=filter_level,
                query_mode=query_mode,
                location_filter=location_filter,   
            )
            response = await es_client.search(index=index, body=es_query)
            hits = response.get("hits", {}).get("hits", [])
            logger.debug("[SEARCH] RRF hybrid: returned %d hits", len(hits))
            return hits

        except (ApiError, TransportError) as rrf_err:
            # RRF gagal (ES versi lama atau feature flag belum aktif)
            # → fallback ke script_score dengan normalisasi manual
            logger.warning(
                "[SEARCH] RRF unavailable (%s), falling back to script_score", rrf_err
            )
            es_query_fb = _build_es_query_fallback(
                query_text=query_text,
                vector=vector,
                size=size,
                filter_level=filter_level,
                location_filter=location_filter,
            )
            response = await es_client.search(index=index, body=es_query_fb)
            hits = response.get("hits", {}).get("hits", [])
            logger.debug("[SEARCH] Fallback script_score: returned %d hits", len(hits))
            return hits

    except (ApiError, TransportError) as e:
        logger.exception("[SEARCH] Elasticsearch error: %s", e)
        return []

    except Exception as e:
        logger.exception("[SEARCH] Unexpected error: %s", e)
        return []
 
async def fuzzy_search_all_locations(es_client: AsyncElasticsearch, query: str):
 
    index = os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")
 
    body = {
        "size": 5,
        "query": {
            "multi_match": {
                "query": query,
                # "fields": [f"{field}.text" for field in LOCATION_FIELDS],
                "fields": [f"{field}.text" for field in LOCATION_FIELDS],
                "fuzziness": "AUTO"
            }
        }
    }
 
    try:
        return await es_client.search(index=index, body=body)
    except Exception as e:
        logger.warning("[SEARCH] fuzzy_search_all_locations failed: %s", e)
        return {}

# =========================================================
# SCHOOL COUNT BY LOCATION
# =========================================================
 
async def count_schools_by_location(
    es_client: AsyncElasticsearch,
    level: str,
    value: str
) -> int:
    """
    Count number of schools by exact location match.
    Used for comparison queries.
    """
 
    try:
        index = os.getenv("ELASTICSEARCH_INDEX")
 
        query = {
            "query": {
                "term": {
                    level: {
                        "value": value
                    }
                }
            }
        }
 
        res = await es_client.count(
            index=index,
            body=query
        )
 
        return res.get("count", 0)
 
    except (ApiError, TransportError) as e:
        logger.exception(
            "[COUNT_SCHOOLS] Elasticsearch error level=%s value=%s",
            level,
            value
        )
        return 0
 
    except Exception:
        logger.exception(
            "[COUNT_SCHOOLS] Unexpected error level=%s value=%s",
            level,
            value
        )
        return 0
 
async def _get_or_create_embedding(text: str) -> List[float]:
    cache_key = f"embed:{hashlib.md5(text.encode()).hexdigest()}"
 
    try:
        cached = await get_cache(cache_key)
        if cached:
            return cached
    except Exception:
        logger.warning("[SEARCH] Cache read failed")
 
    vector = await get_embedding(text)
 
    try:
        await set_cache(cache_key, vector, ttl=3600)
    except Exception:
        logger.warning("[SEARCH] Cache write failed")
 
    return vector
 
def _build_es_query(query_text, vector, size, filter_level, query_mode="semantic", location_filter=None):
    """
    Hybrid search menggunakan Reciprocal Rank Fusion (RRF).

    query_mode:
      - "semantic" → default, untuk query umum/deskriptif
      - "keyword"  → untuk nama sekolah eksplisit (boost BM25)

    RRF menggabungkan:
      - BM25 (lexical match)
      - KNN  (semantic similarity)

    Tidak bergantung pada skala skor → lebih stabil.
    """

    # Adaptive tuning berdasarkan mode query
    rank_constant = 20 if query_mode == "keyword" else 60
    window_size = max(size * 10, 100)

    query = {
        "size": size,
        "_source": {
            "excludes": ["content_vector"]
        },
        "sub_searches": [
            # BM25 — lexical
            {
                "query": {
                    "match": {
                        "content": {
                            "query": query_text,
                            "operator": "or",
                            "minimum_should_match": "60%",  # dinaikkan (lebih presisi)
                        }
                    }
                }
            },
            # KNN — semantic
            {
                "knn": {
                    "field": "content_vector",
                    "query_vector": vector,
                    "num_candidates": max(size * 15, 75),  # dinaikkan (recall lebih baik)
                }
            },
        ],
        "rank": {
            "rrf": {
                "window_size": window_size,
                "rank_constant": rank_constant,
            }
        },
    }
    
    if location_filter:
        _terms = [
            {"term": {field: value}}
            for field, value in location_filter.items()
            if value
        ]
        if _terms:
            query["post_filter"] = {
                "bool": {"filter": _terms}
            }

    return query


def _build_es_query_fallback(query_text, vector, size, filter_level, location_filter=None):
    """
    Fallback untuk ES < 8.8 yang belum mendukung RRF sub_searches.
    Menggunakan script_score dengan normalisasi manual:
      - BM25 dinormalisasi ke [0,1] via sigmoid sebelum dijumlah
      - Cosine similarity tetap [0,1] secara alami
    Total skor = 0.5 * norm_bm25 + 0.5 * cosine
    """
    query = {
        "size": size,
        "_source": {
            "excludes": ["content_vector"]
        },
        "query": {
            "script_score": {
                "query": {
                    "match": {
                        "content": {
                            "query": query_text,
                            "operator": "or",
                            "minimum_should_match": "50%",
                        }
                    }
                },
                "script": {
                    # Sigmoid normalisasi BM25: 1/(1+e^(-0.1*(bm25-10)))
                    # Cosine similarity: nilai asli sudah [0,1]
                    # Bobot sama (0.5:0.5) — sesuaikan jika BM25 lebih diutamakan
                    "source": """
                        double cosine = cosineSimilarity(params.query_vector, 'content_vector');
                        double bm25   = _score;
                        double norm   = 1.0 / (1.0 + Math.exp(-0.1 * (bm25 - 10.0)));
                        return 0.5 * norm + 0.5 * cosine;
                    """,
                    "params": {
                        "query_vector": vector
                    }
                }
            }
        }
    }

    if location_filter:
        _terms = [
            {"term": {field: value}}
            for field, value in location_filter.items()
            if value
        ]
        if _terms:
            query["post_filter"] = {
                "bool": {"filter": _terms}
            }

    return query
 
 
# =========================================================
# GEO SEARCH — Item #18
# Field 'lokasi' (geo_point) tersedia di mapping tapi belum digunakan.
# Fungsi ini mendeteksi intent geo dan menjalankan geo_distance filter.
# =========================================================
 
GEO_KEYWORDS = (
    "terdekat", "dekat", "dekat sini", "radius", "km dari",
    "sekitar sini", "sekolah di sekitar", "nearby",
    # typo variants yang umum
    "deket", "dket", "dkeat", "terdeket",
)
 
 
 
# =========================================================
# NOMINATIM GEOCODING — konversi nama tempat → koordinat
# Menggunakan OpenStreetMap Nominatim (gratis, tanpa API key)
# Rate limit: 1 request/detik → kita cache hasilnya 30 hari
# =========================================================
 
import httpx as _httpx
 
# Landmark populer Indonesia — hardcoded untuk respons instan
# tanpa rate limit Nominatim
_LANDMARK_CACHE: Dict[str, tuple] = {
    # Jakarta
    "monas":              (-6.1754, 106.8272),
    "monumen nasional":   (-6.1754, 106.8272),
    "bundaran hi":        (-6.1944, 106.8229),
    "gelora bung karno":  (-6.2183, 106.8023),
    "gbk":                (-6.2183, 106.8023),
    "thamrin":            (-6.1930, 106.8230),
    "sudirman":           (-6.2088, 106.8175),
    "kuningan":           (-6.2298, 106.8310),
    "blok m":             (-6.2446, 106.7988),
    "tanah abang":        (-6.1866, 106.8144),
    "senen":              (-6.1764, 106.8451),
    "kemayoran":          (-6.1574, 106.8520),
    "pluit":              (-6.1164, 106.7943),
    "kelapa gading":      (-6.1576, 106.9006),
    "pondok indah":       (-6.2659, 106.7847),
    "fatmawati":          (-6.2910, 106.7984),
    # Stasiun kereta
    "stasiun gambir":     (-6.1764, 106.8302),
    "gambir":             (-6.1764, 106.8302),
    "stasiun senen":      (-6.1764, 106.8451),
    "stasiun pasar senen":(-6.1764, 106.8451),
    "stasiun manggarai":  (-6.2146, 106.8524),
    "manggarai":          (-6.2146, 106.8524),
    "stasiun tanah abang":(-6.1869, 106.8145),
    "stasiun bekasi":     (-6.2354, 107.0027),
    "stasiun bogor":      (-6.5967, 106.7980),
    "stasiun depok":      (-6.3918, 106.8234),
    "stasiun serpong":    (-6.3184, 106.6654),
    "stasiun tangerang":  (-6.1782, 106.6271),
    # Bandara
    "soetta":             (-6.1256, 106.6559),
    "soekarno hatta":     (-6.1256, 106.6559),
    "bandara soetta":     (-6.1256, 106.6559),
    "bandara soekarno hatta": (-6.1256, 106.6559),
    "halim":              (-6.2664, 106.8907),
    "bandara halim":      (-6.2664, 106.8907),
    "bandung":            (-6.9175, 107.6191),
    "surabaya":           (-7.2575, 112.7521),
    "yogyakarta":         (-7.7956, 110.3695),
    "semarang":           (-6.9932, 110.4203),
    "medan":              (3.5896, 98.6731),
    "makassar":           (-5.1477, 119.4327),
    "denpasar":           (-8.6705, 115.2126),
    "palembang":          (-2.9761, 104.7754),
    "balikpapan":         (-1.2379, 116.8529),
    # Yogyakarta
    "tugu jogja":         (-7.7829, 110.3668),
    "tugu yogyakarta":    (-7.7829, 110.3668),
    "malioboro":          (-7.7928, 110.3659),
    "kraton yogyakarta":  (-7.8053, 110.3642),
    "borobudur":          (-7.6079, 110.2038),
    "prambanan":          (-7.7520, 110.4914),
    # Wisata populer
    "ancol":              (-6.1256, 106.8454),
    "taman mini":         (-6.3024, 106.8952),
    "ragunan":            (-6.3122, 106.8207),
    "senayan":            (-6.2183, 106.8023),
    # Kota-kota besar
    "bali":               (-8.4095, 115.1889),
    "lombok":             (-8.6500, 116.3240),
    "manado":             (-1.4748, 124.8421),
    "pekanbaru":          (-0.5071, 101.4478),
    "pontianak":          (-0.0263, 109.3425),
    "samarinda":          (-0.5016, 117.1537),
    "jayapura":           (-2.5916, 140.6690),
    "kupang":             (-10.1772, 123.6070),
}
 
 
async def geocode_place_name(place_name: str) -> Optional[tuple]:
    """
    Konversi nama tempat → (lat, lon) menggunakan:
    1. Hardcoded landmark populer (instan, no rate limit)
    2. Redis cache (TTL 30 hari)
    3. Nominatim API (fallback, 1 req/detik)
 
    Returns: (lat, lon) atau None jika tidak ditemukan
    """
    if not place_name or len(place_name.strip()) < 3:
        return None
 
    name_clean = place_name.lower().strip()
 
    # Step 1: Hardcoded landmark — instan
    for landmark, coords in _LANDMARK_CACHE.items():
        if landmark in name_clean or name_clean in landmark:
            logger.info("[GEO] Landmark match: '%s' → %s", place_name, coords)
            return coords
 
    # Step 2: Redis cache
    cache_key = f"geocode:nominatim:{hashlib.md5(name_clean.encode()).hexdigest()}"
    try:
        cached = await get_cache(cache_key)
        if cached:
            logger.info("[GEO] Nominatim cache hit: '%s' → %s", place_name, cached)
            return tuple(cached)
    except Exception:
        pass
 
    # Step 3: Nominatim API
    try:
        params = {
            "q": f"{place_name}, Indonesia",
            "format": "json",
            "limit": 1,
            "countrycodes": "id",
        }
        headers = {
            # Nominatim requires User-Agent identification
            "User-Agent": "elasticsearch-kb-school-search/1.0"
        }
        async with _httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            results = resp.json()
 
        if results:
            lat = float(results[0]["lat"])
            lon = float(results[0]["lon"])
            coords = (lat, lon)
            logger.info(
                "[GEO] Nominatim result: '%s' → lat=%.4f lon=%.4f display='%s'",
                place_name, lat, lon, results[0].get("display_name", "")[:60]
            )
            # Cache 30 hari
            await set_cache(cache_key, list(coords), ttl=86400 * 30)
            return coords
        else:
            logger.info("[GEO] Nominatim: no result for '%s'", place_name)
            return None
 
    except Exception as e:
        logger.warning("[GEO] Nominatim error for '%s': %s", place_name, e)
        return None
 
 
def extract_place_from_query(query: str) -> Optional[str]:
    """
    Ekstrak nama tempat dari query geo.
    Contoh: "sekolah terdekat dari monas" → "monas"
    "sekolah dalam radius 2 km dari soekarno hatta" → "soekarno hatta"
    """
    import re as _re
    q = query.lower().strip()

    # Strip trailing noise yang tidak mengandung nama tempat
    q = _re.sub(r'\s*,\s*dan\s+apakah.*$', '', q, flags=_re.IGNORECASE).strip()
    q = _re.sub(r'\s+dalam\s+radius\s+[\d.]+\s*km?\s*$', '', q, flags=_re.IGNORECASE).strip()

    _noise = _re.compile(
        r'\b(sekolah|sd|smp|sma|smk|tk|paud|negeri|swasta|'
        r'terdekat|km|meter|dan|yang|'
        r'apakah|statusnya|atau|ada|mana|sini|saya|aku|koordinat)\b',
        _re.IGNORECASE
    )

    patterns = [
        r'(?:dalam\s+)?radius\s+[\d.]+\s*km?\s+dari\s+(.+)$',
        r'dari\s+(.+)$',
        r'(?:dekat|deket|dket|sekitar|di sekitar)\s+(.+)$',
        r'(?:terdekat|nearby)\s+(?:dari\s+)?(.+)$',
    ]
    for pat in patterns:
        m = _re.search(pat, q)
        if m:
            place = m.group(1).strip()
            place = _re.sub(r'^dari\s+', '', place).strip()
            if _re.search(r'-?\d{1,3}\.\d+', place):
                return None
            place = _noise.sub('', place).strip()
            place = _re.sub(r'\s+', ' ', place).strip()
            place = place.rstrip('.,?! ').strip()
            if len(place) >= 3:
                return place
    return None
 
 
 
def detect_geo_query(query: str) -> bool:
    """Return True jika query mengandung intent lokasi geografis."""
    q = query.lower()
    return any(kw in q for kw in GEO_KEYWORDS)
 
 
async def perform_geo_search(
    es_client: AsyncElasticsearch,
    lat: float,
    lon: float,
    radius_km: float = 5.0,
    bentuk: Optional[str] = None,
    status: Optional[str] = None,
    akreditasi: Optional[str] = None,
    size: int = 10,
) -> Dict[str, Any]:
    """
    Cari sekolah dalam radius tertentu dari koordinat user.
    Menjalankan dua query paralel: hits terdekat + aggregation status/jenjang.

    Returns:
        Dict dengan keys: hits, total_in_radius, breakdown_status, breakdown_jenjang
    """
    import asyncio as _asyncio

    index = os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")

    filters = [
        {
            "geo_distance": {
                "distance": f"{radius_km}km",
                "lokasi": {"lat": lat, "lon": lon}
            }
        }
    ]

    if bentuk:
        filters.append({"term": {"bentuk_pendidikan": bentuk}})
    if status:
        filters.append({"term": {"status_sekolah": status.upper()}})
    if akreditasi:
        filters.append({"term": {"akreditasi": akreditasi.upper()}})

    # ── Query 1: hits terdekat ────────────────────────────────────────────────
    body_hits = {
        "size": size,
        "track_total_hits": True,
    
        "_source": [
            "nama",
            "npsn",
            "bentuk_pendidikan",
            "status_sekolah",
            "akreditasi",
            "kabupaten",
            "kecamatan",
            "siswa_total",   
            "provinsi",
            "desa_kelurahan",
            "lintang",
            "bujur"
        ],

        "query": {"bool": {"filter": filters}},
        "sort": [
            {
                "_geo_distance": {
                    "lokasi": {"lat": lat, "lon": lon},
                    "order": "asc",
                    "unit": "km",
                    "distance_type": "arc"
                }
            }
        ],
        "collapse": {"field": "npsn"}
    }

    # ── Query 2: aggregation status & jenjang dari SEMUA dalam radius ─────────
    # Tidak pakai collapse agar count akurat per status/jenjang
    body_agg = {
        "size": 0,
        "query": {"bool": {"filter": filters}},
        "aggs": {
            "status_breakdown": {
                "terms": {"field": "status_sekolah", "size": 5}
            },
            "jenjang_breakdown": {
                "terms": {"field": "bentuk_pendidikan", "size": 10}
            }
        }
    }

    try:
        # Jalankan kedua query secara paralel
        resp_hits, resp_agg = await _asyncio.gather(
            es_client.search(index=index, body=body_hits, request_timeout=10),
            es_client.search(index=index, body=body_agg, request_timeout=10),
            return_exceptions=True
        )

        # ── Parse hits ───────────────────────────────────────────────────────
        if isinstance(resp_hits, Exception):
            logger.error("[GEO_SEARCH] hits query failed: %s", resp_hits)
            return {"hits": [], "total_in_radius": 0, "breakdown_status": {}, "breakdown_jenjang": {}}

        hits      = resp_hits.get("hits", {}).get("hits", [])
        total     = resp_hits["hits"]["total"]
        total_val = total["value"] if isinstance(total, dict) else total

        results = []
        for h in hits:
            sort_val = h.get("sort", [None])[0]
            try:
                jarak_km = round(float(sort_val), 2) if sort_val is not None else None
            except (TypeError, ValueError):
                jarak_km = None

            src = h["_source"]

            results.append({
                "_source": src,
                "_score":  h.get("_score"),
                "jarak_km": jarak_km,
            })

        # ── Parse aggregation ─────────────────────────────────────────────────
        breakdown_status  = {}
        breakdown_jenjang = {}

        if not isinstance(resp_agg, Exception):
            aggs = resp_agg.get("aggregations", {})
            for bucket in aggs.get("status_breakdown", {}).get("buckets", []):
                breakdown_status[bucket["key"].upper()] = bucket["doc_count"]
            for bucket in aggs.get("jenjang_breakdown", {}).get("buckets", []):
                breakdown_jenjang[bucket["key"]] = bucket["doc_count"]
        else:
            logger.warning("[GEO_SEARCH] agg query failed: %s", resp_agg)

        logger.info(
            "[GEO_SEARCH] lat=%.4f lon=%.4f radius=%skm total=%s returned=%s "
            "status_breakdown=%s",
            lat, lon, radius_km, total_val, len(results), breakdown_status
        )

        return {
            "hits":              results,
            "total_in_radius":   total_val,
            "breakdown_status":  breakdown_status,
            "breakdown_jenjang": breakdown_jenjang,
        }

    except (ApiError, TransportError) as e:
        logger.exception("[GEO_SEARCH] ES error: %s", e)
        return {"hits": [], "total_in_radius": 0, "breakdown_status": {}, "breakdown_jenjang": {}}
    except Exception as e:
        logger.exception("[GEO_SEARCH] Unexpected error: %s", e)
        return {"hits": [], "total_in_radius": 0, "breakdown_status": {}, "breakdown_jenjang": {}}
 
def compute_context_sufficiency_score(
    hits: List[Dict[str, Any]]
) -> float:
    """
    More robust scoring:
    - Uses max_score as primary signal
    - Uses average score as stability factor
    - Applies sigmoid normalization
    - Avoids binary 0/1 collapse
    """
 
    if not hits:
        return 0.0
 
    scores = [
        h.get("_score", 0.0)
        for h in hits
        if h.get("_score") is not None
    ]
 
    if not scores:
        return 0.0
 
    max_score = max(scores)
    avg_score = sum(scores) / len(scores)
 
    # Hard floor threshold
    if max_score < 0.2:
        return 0.0
 
    # Combine signals
    combined = (0.7 * max_score) + (0.3 * avg_score)
 
    # Sigmoid normalization
    normalized = 1 / (1 + math.exp(-0.1 * (combined - 10)))
    if len(scores) < 2:
        normalized *= 0.7
 
    return round(normalized, 2)
