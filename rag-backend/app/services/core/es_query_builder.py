# services/core/es_query_builder.py
"""
Centralized ES query builder.
Input: CognitiveIntent + es_filters dari _build_es_filters()
Output: dict ES query siap kirim
"""

import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


def _build_provinsi_filter(provinsi_value: str) -> dict:
    """Build ES filter untuk field provinsi, dengan handling khusus Jakarta dan Yogyakarta."""
    v = provinsi_value.lower()
    if "jakarta" in v:
        return {
            "bool": {
                "should": [
                    {"term": {"provinsi": "prov. d.k.i. jakarta"}},
                    {"term": {"provinsi": "prov. dki jakarta"}},
                    {"match": {"provinsi.text": "jakarta"}},
                ],
                "minimum_should_match": 1,
            }
        }
    if "yogyakarta" in v:
        return {
            "bool": {
                "should": [
                    {"term": {"provinsi": "prov. d.i. yogyakarta"}},
                    {"term": {"provinsi": "prov. yogyakarta"}},
                ],
                "minimum_should_match": 1,
            }
        }
    return {"term": {"provinsi": provinsi_value}}


def build_wilayah_filter(intent) -> List[Dict]:
    """
    Bangun filter wilayah bertingkat dari intent.
    Gunakan field .keyword untuk exact match yang efisien.

    Jika intent.wilayah.ambiguous=True, build should query dari semua
    candidates (Phase 14 — implementasi TODO Phase 3).
    """
    filters = []
    wil = intent.wilayah

    # ── Ambiguous: build should query ke semua kandidat ──────────────────
    if getattr(wil, "ambiguous", False) and getattr(wil, "candidates", []):
        should_clauses = []
        for candidate in wil.candidates:
            cand_filters = []
            desa_val = candidate.get("desa_kelurahan") or candidate.get("desa")
            if desa_val:
                cand_filters.append({"term": {"desa_kelurahan": desa_val.lower()}})
            if candidate.get("kecamatan"):
                cand_filters.append({"term": {"kecamatan": candidate["kecamatan"].lower()}})
            if candidate.get("kabupaten"):
                cand_filters.append({"term": {"kabupaten": candidate["kabupaten"].lower()}})
            if candidate.get("provinsi"):
                cand_filters.append(_build_provinsi_filter(candidate["provinsi"].lower()))
            if cand_filters:
                if len(cand_filters) == 1:
                    should_clauses.append(cand_filters[0])
                else:
                    should_clauses.append({"bool": {"filter": cand_filters}})

        if should_clauses:
            filters.append({
                "bool": {
                    "should": should_clauses,
                    "minimum_should_match": 1,
                }
            })
            logger.debug(
                "[WILAYAH_FILTER] ambiguous=True, %d candidates → should query",
                len(should_clauses),
            )
            return filters

    # ── Non-ambiguous: exact match per level (hierarki desa→kecamatan→kabupaten→provinsi) ──
    if wil.desa:
        filters.append({"bool": {"should": [
            {"term": {"desa_kelurahan": wil.desa.lower()}},
            {"term": {"desa_kelurahan": f"kel. {wil.desa.lower()}"}},
        ], "minimum_should_match": 1}})
    elif wil.kecamatan:
        filters.append({"bool": {"should": [
            {"term": {"kecamatan": wil.kecamatan.lower()}},
            {"term": {"kecamatan": f"kec. {wil.kecamatan.lower()}"}},
        ], "minimum_should_match": 1}})
    elif wil.kabupaten:
        filters.append({"term": {"kabupaten": wil.kabupaten.lower()}})
    elif wil.provinsi:
        filters.append(_build_provinsi_filter(wil.provinsi.lower()))
    # scope nasional → tidak ada filter wilayah

    return filters


def build_qc_filter(intent) -> List[Dict]:
    """
    Bangun filter QC dari intent.

    Mapping field intent → ES:
      qc_masalah_only=True  → range: qc_score.total_masalah gte 1
      qc_field=N            → range: qc.qc_N gte 2  (1=OK, >=2 berarti bermasalah)
      qc_min_masalah=N      → range: qc_score.total_masalah gte N
                               (jika qc_masalah_only juga True, ambil nilai terbesar)

    qc_score.total_masalah dan qc.qc_N adalah plain object (bukan nested),
    sehingga cukup pakai range query biasa tanpa nested query.
    """
    filters = []

    # 1. qc_masalah_only: sekolah yang punya setidaknya 1 masalah data
    has_masalah_only = getattr(intent, "qc_masalah_only", False)
    qc_min = getattr(intent, "qc_min_masalah", None)

    # Tentukan threshold total_masalah (ambil nilai terbesar antara keduanya)
    total_masalah_gte = None
    if has_masalah_only:
        total_masalah_gte = 1
    if qc_min and isinstance(qc_min, int) and qc_min > 0:
        total_masalah_gte = max(total_masalah_gte or 0, qc_min)

    if total_masalah_gte is not None:
        filters.append({
            "range": {
                "qc_score.total_masalah": {"gte": total_masalah_gte}
            }
        })

    # 2. qc_field: filter pada kolom QC spesifik yang bermasalah (nilai >= 2)
    qc_field = getattr(intent, "qc_field", None)
    if qc_field and isinstance(qc_field, int) and 1 <= qc_field <= 13:
        filters.append({
            "range": {
                f"qc.qc_{qc_field}": {"gte": 2}
            }
        })

    # 3. qc_persen_min / qc_persen_max: filter range persen_bersih
    persen_min = getattr(intent, "qc_persen_min", None)
    persen_max = getattr(intent, "qc_persen_max", None)
    # Hanya proses jika nilai berupa angka nyata (guard vs MagicMock)
    _persen_min_valid = isinstance(persen_min, (int, float)) and persen_min is not False
    _persen_max_valid = isinstance(persen_max, (int, float)) and persen_max is not False
    if _persen_min_valid or _persen_max_valid:
        range_clause = {}
        if _persen_min_valid:
            range_clause["gte"] = float(persen_min)
        if _persen_max_valid:
            range_clause["lte"] = float(persen_max)
        filters.append({"range": {"qc_score.persen_bersih": range_clause}})

    # 4. qc_field_list: multiple QC field filters sekaligus
    qc_field_list = getattr(intent, "qc_field_list", None) or []
    for qf in qc_field_list:
        if isinstance(qf, int) and 1 <= qf <= 13:
            filters.append({"range": {f"qc.qc_{qf}": {"gte": 2}}})

    return filters


# Module-level cache for semester_id (set by _refresh_semester_id, read by _latest_semester_id)
_cached_semester_id = None


def _latest_semester_id():
    """
    Return the latest semester_id from the in-memory cache.
    Returns None when no semester_id has been fetched yet (graceful fallback).
    The field 'semester_id' is not yet in the current ES index mapping — function
    is a forward-compatible stub. When the field exists, call _refresh_semester_id(es)
    periodically to populate the cache.
    """
    return _cached_semester_id


async def _refresh_semester_id(es_client) -> None:
    """Fetch the latest semester_id from ES and update the module cache."""
    import os as _os
    global _cached_semester_id
    index = _os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")
    try:
        resp = await es_client.search(
            index=index,
            body={"size": 0, "aggs": {"max_semester": {"max": {"field": "semester_id"}}}},
            request_timeout=5,
        )
        val = resp.get("aggregations", {}).get("max_semester", {}).get("value")
        if val is not None:
            _cached_semester_id = val
    except Exception:
        pass


def build_es_query(
    intent,
    es_filters: Dict[str, Any],
    size: int = 10,
    from_: int = 0,
    query_vector: Optional[List[float]] = None,
) -> Dict:
    """
    Builder ES query utama (optimized: filter vs scoring dipisah)
    """
    hard_filters = []
    must_clauses = []

    # ── Wilayah → HARD FILTER ──
    if es_filters.get("scope") != "nasional":
        hard_filters.extend(build_wilayah_filter(intent))

    # ── Akreditasi → HARD FILTER ──
    if "akreditasi_exact" in es_filters:
        hard_filters.append({"term": {"akreditasi": es_filters["akreditasi_exact"]}})
    elif "akreditasi_in" in es_filters:
        hard_filters.append({"terms": {"akreditasi": es_filters["akreditasi_in"]}})
    elif "akreditasi_exclude" in es_filters:
        hard_filters.append({
            "bool": {
                "must_not": [{"term": {"akreditasi": es_filters["akreditasi_exclude"]}}]
            }
        })

    # ── Status Sekolah → HARD FILTER ──
    if "status_sekolah" in es_filters:
        raw = es_filters["status_sekolah"]  # "NEGERI" atau "SWASTA"
        variants = {
            "NEGERI":  ["NEGERI", "Negeri"],
            "SWASTA":  ["SWASTA", "Swasta", "Luar Negeri"],
        }
        terms = variants.get(raw.upper(), [raw])
        hard_filters.append({"terms": {"status_sekolah": terms}})

    # ── Jenjang → HARD FILTER ──
    if "bentuk_pendidikan_in" in es_filters:
        hard_filters.append({"terms": {"bentuk_pendidikan": es_filters["bentuk_pendidikan_in"]}})
    elif "bentuk_pendidikan" in es_filters:
        hard_filters.append({"term": {"bentuk_pendidikan": es_filters["bentuk_pendidikan"]}})

    # ── Keaktifan → HARD FILTER ──
    if getattr(intent, "keaktifan", None):
        hard_filters.append({"term": {"keaktifan": intent.keaktifan}})

    # ── Wilayah Terpencil → HARD FILTER ──
    if getattr(intent, "wilayah_terpencil", None) is not None:
        hard_filters.append({"term": {"wilayah_terpencil": intent.wilayah_terpencil}})

    # ── Wilayah Perbatasan → HARD FILTER ──
    if getattr(intent, "wilayah_perbatasan", None) is not None:
        hard_filters.append({"term": {"wilayah_perbatasan": intent.wilayah_perbatasan}})

    # ── QC Filter ──
    qc_filters = build_qc_filter(intent)
    hard_filters.extend(qc_filters)

    # ── Nama Sekolah → SCORING ──
    if intent.nama_sekolah:
        must_clauses.append({
            "multi_match": {
                "query": intent.nama_sekolah,
                "fields": ["nama^3", "nama.keyword^5", "nama.autocomplete", "nama.shingle^2"],
                "type": "best_fields",
                "fuzziness": "AUTO",
            }
        })

    # ── NPSN → HARD FILTER ──
    if intent.npsn:
        hard_filters.append({"term": {"npsn": intent.npsn}})

    bool_query = {
        "bool": {
            "must": must_clauses if must_clauses else [{"match_all": {}}],
            "filter": hard_filters,
        }
    }

    if query_vector and from_ == 0:
        return {
            "sub_searches": [
                {"query": bool_query},
                {"knn": {
                    "field": "content_vector",
                    "query_vector": query_vector,
                    "k": max(10, size),
                    "num_candidates": max(100, size * 10),
                    **({"filter": hard_filters} if hard_filters else {}),
                }},
            ],
            "rank": {"rrf": {"window_size": 50, "rank_constant": 20}},
            "size": size,
            "_source": {"excludes": ["content_vector"]},
        }

    return {
        "query": bool_query,
        "size": size,
        "from": from_,
        "_source": {"excludes": ["content_vector"]},
    }


def build_aggregation_query(intent_or_filters, es_filters: Dict = None) -> Dict:
    """
    Mendukung dua mode:
      - Mode baru:  build_aggregation_query(intent, es_filters)
      - Mode lama:  build_aggregation_query(normalized_dict)   ← backward compat
    """
    if es_filters is None and isinstance(intent_or_filters, dict):
        # Panggilan lama dari aggregation_handler
        legacy = intent_or_filters
        must = []
        if "bentuk_pendidikan" in legacy:
            must.append({"term": {"bentuk_pendidikan": legacy["bentuk_pendidikan"]}})
        if "bentuk_pendidikan_in" in legacy:
            must.append({"terms": {"bentuk_pendidikan": legacy["bentuk_pendidikan_in"]}})
        if "status_sekolah" in legacy:
            must.append({"term": {"status_sekolah": legacy["status_sekolah"]}})
        if "akreditasi_exact" in legacy:
            must.append({"term": {"akreditasi": legacy["akreditasi_exact"]}})
        if "akreditasi_gte" in legacy:
            must.append({"terms": {"akreditasi": legacy["akreditasi_gte"]}})
        if "akreditasi_in" in legacy:
            must.append({"terms": {"akreditasi": legacy["akreditasi_in"]}})
        return {
            "query": {"bool": {"must": must if must else [{"match_all": {}}]}},
            "size": 0,
            "_source": False,
            "aggs": {
                "total": {"value_count": {"field": "npsn"}},
                "by_jenjang": {"terms": {"field": "bentuk_pendidikan", "size": 30}},
                "by_status": {"terms": {"field": "status_sekolah", "size": 5}},
                "by_akreditasi": {"terms": {"field": "akreditasi", "size": 10}},
            }
        }
    # Mode baru
    base = build_es_query(intent_or_filters, es_filters, size=0)
    base["aggs"] = {
        "total": {"value_count": {"field": "npsn"}},
        "by_jenjang": {"terms": {"field": "bentuk_pendidikan", "size": 30}},
        "by_status": {"terms": {"field": "status_sekolah", "size": 5}},
        "by_akreditasi": {"terms": {"field": "akreditasi", "size": 10}},
    }
    return base


def build_geo_query(
    intent,
    es_filters: Dict,
    lat: float,
    lon: float,
    radius_km: float = 5.0,
    size: int = 10,
) -> Dict:
    """
    Builder untuk geo query (distance dari posisi user).
    """
    must_filters = []

    # Jenjang
    if "bentuk_pendidikan_in" in es_filters:
        must_filters.append({"terms": {"bentuk_pendidikan": es_filters["bentuk_pendidikan_in"]}})
    elif "bentuk_pendidikan" in es_filters:
        must_filters.append({"term": {"bentuk_pendidikan": es_filters["bentuk_pendidikan"]}})

    if "status_sekolah" in es_filters:
        must_filters.append({"term": {"status_sekolah": es_filters["status_sekolah"]}})

    query = {
        "query": {
            "bool": {
                "must": must_filters if must_filters else [{"match_all": {}}],
                "filter": [{
                    "geo_distance": {
                        "distance": f"{radius_km}km",
                        "lokasi": {"lat": lat, "lon": lon}
                    }
                }]
            }
        },
        "sort": [{
            "_geo_distance": {
                "lokasi": {"lat": lat, "lon": lon},
                "order": "asc",
                "unit": "km"
            }
        }],
        "size": size,
        "_source": {"excludes": ["content_vector"]},
        "script_fields": {
            "distance_km": {
                "script": {
                    "source": f"doc['lokasi'].arcDistance({lat}, {lon}) / 1000",
                    "lang": "painless"
                }
            }
        }
    }
    return query

def build_disambiguation_query(
    nama_sekolah: str,
    wilayah_entities,  # WilayahEntities
    npsn: str = None,
) -> dict:
    """
    Query untuk profile handler yang menangani nama sekolah ambigu.
    
    Strategi bertingkat:
    1. Jika ada NPSN → exact match, tidak perlu disambiguasi
    2. Jika ada kecamatan → filter 4 level (paling presisi)  
    3. Jika hanya kabupaten → filter 3 level + boost nama
    4. Jika hanya provinsi → filter 2 level + fallback ke top-score
    5. Tanpa wilayah → multi_match dengan min_score tinggi
    
    Ini MENCEGAH "SDN 1 Serpong" di Tangsel muncul saat user
    bertanya tentang "SDN 1 Serpong" di Bogor.
    """
    
    if npsn:
        # NPSN unik secara nasional → zero ambiguity
        return {"query": {"term": {"npsn": npsn}}, "size": 1}
    
    must = []
    should = []
    filter_clauses = []
    
    # Nama sekolah — multi-field dengan booster
    must.append({
        "multi_match": {
            "query": nama_sekolah,
            "fields": [
                "nama.keyword^10",   # exact match tertinggi
                "nama^5",            # analyzed (stopword removed)
                "nama.autocomplete^2"  # edge ngram
            ],
            "type": "best_fields",
            "fuzziness": "1",        # toleransi 1 typo
            "minimum_should_match": "75%"
        }
    })
    
    # Filter wilayah secara hierarkis (presisi makin tinggi makin banyak level)
    wil = wilayah_entities
    
    if wil.desa:
        if wil.kabupaten:
            filter_clauses.append({"term": {"kabupaten": wil.kabupaten}})
        if wil.kecamatan:
            filter_clauses.append({"term": {"kecamatan": wil.kecamatan}})
        filter_clauses.append({"term": {"desa_kelurahan": wil.desa}})
    elif wil.kecamatan:
        filter_clauses.extend([
            {"term": {"kecamatan": wil.kecamatan}},
        ])
        if wil.kabupaten:
            filter_clauses.append({"term": {"kabupaten": wil.kabupaten}})
    elif wil.kabupaten:
        filter_clauses.append({"term": {"kabupaten": wil.kabupaten}})
        # Boost: provinsi jika ada (tidak filter ketat, tapi boost ranking)
        if wil.provinsi:
            should.append({"term": {"provinsi": wil.provinsi}})
    elif wil.provinsi:
        filter_clauses.append({"term": {"provinsi": wil.provinsi}})
    
    query = {
        "query": {
            "bool": {
                "must": must,
                "should": should,
                "filter": filter_clauses,
                "minimum_should_match": 0
            }
        },
        "size": 5,          # ambil 5 kandidat untuk disambiguation
        "min_score": 3.0,   # tolak hasil yang tidak relevan
        "_source": [        # hemat transfer — hanya field yang diperlukan
            "npsn", "nama", "kabupaten", "kecamatan", "desa_kelurahan",
            "provinsi", "akreditasi", "status_sekolah", "bentuk_pendidikan",
            "wilayah_display", "alamat_jalan", "lintang", "bujur"
        ]
    }
    
    return query


def handle_ambiguous_results(hits: list, intent) -> dict:
    """
    Jika ES mengembalikan >1 hasil untuk nama sekolah yang sama,
    pilih yang paling relevan atau minta user disambiguasi.
    
    Return:
      {"type": "single",   "doc": {...}}          → langsung ke template/narrator
      {"type": "ambiguous","candidates": [...]}   → minta user pilih
      {"type": "not_found","suggestion": "..."}  → tidak ditemukan
    """
    if not hits:
        return {"type": "not_found", "suggestion": None}
    
    if len(hits) == 1:
        return {"type": "single", "doc": hits[0]["_source"]}
    
    # Cek apakah semua hit adalah sekolah yang BENAR-BENAR berbeda
    # (bukan duplikat data dari sumber berbeda)
    npsns = [h["_source"].get("npsn") for h in hits if h["_source"].get("npsn")]
    if len(set(npsns)) == 1:
        # NPSN sama = data duplikat → ambil yang score tertinggi
        return {"type": "single", "doc": hits[0]["_source"]}
    
    # Multiple sekolah berbeda → butuh disambiguasi
    candidates = []
    for hit in hits[:3]:  # max 3 kandidat
        src = hit["_source"]
        candidates.append({
            "npsn":     src.get("npsn"),
            "nama":     src.get("nama"),
            "lokasi":   src.get("wilayah_display", ""),
            "jenjang":  src.get("bentuk_pendidikan", ""),
            "status":   src.get("status_sekolah", ""),
            "akreditasi": src.get("akreditasi", "-"),
            "score":    round(hit["_score"], 2),
        })
    
    return {"type": "ambiguous", "candidates": candidates}

_JENJANG_NORMALIZE: dict = {
    # Alias → nilai exact di ES (case-sensitive sesuai bucket)
    "pesantren":         "Pondok Pesantren",
    "pondok":            "Pondok Pesantren",
    "pondok pesantren":  "Pondok Pesantren",
    "universitas":       "Universitas",
    "univ":              "Universitas",
    "institut":          "Institut",
    "politeknik":        "Politeknik",
    "poli":              "Politeknik",
    "akademi":           "Akademi",
    "sekolah tinggi":    "Sekolah Tinggi",
    "pkbm":              "PKBM",
    "skb":               "SKB",
    "kursus":            "Kursus",
    "kb":                "KB",
    "ra":                "RA",
    "sps":               "SPS",
    "tpa":               "TPA",
    "paudq":             "PAUDQ",
    "slb":               "SLB",
    # Jenjang umum (sudah benar di LLM tapi jaga-jaga)
    "sd": "SD", "mi": "MI", "smp": "SMP", "mts": "MTs",
    "sma": "SMA", "smk": "SMK", "ma": "MA", "tk": "TK",
    "slb":               "SLB",
    "luar biasa":        "SLB",
    "akademik":          "Akademik",
    "spk":               "SPK SMA",
    "spk sd":            "SPK SD",
    "spk smp":           "SPK SMP",
    "spk sma":           "SPK SMA",
    "spk tk":            "SPK TK",
    "spk kb":            "SPK KB",
    "smtk":              "SMTK",
    "smptk":             "SMPTK",
    "spm ulya":          "SPM Ulya",
    "spm wustha":        "SPM Wustha",
    "spm ula":           "SPM Ula",
    "pratama widyalaya": "Pratama Widyalaya",
    "sdtk":              "SDTK",
    "taman seminari":    "Taman Seminari",
    "seminari":          "Taman Seminari",
    "smak":              "SMAK",
    "akademi komunitas": "Akademi Komunitas",
    "mak":               "MAK",
    "widyalaya":         "Pratama Widyalaya",
}

def normalize_jenjang(raw: str) -> str:
    """Normalisasi nilai jenjang dari LLM/user ke nilai exact ES."""
    if not raw:
        return raw
    key = raw.strip().lower()

    return _JENJANG_NORMALIZE.get(key, raw)  # fallback: pakai nilai asli

def extract_filters_from_intent(intent) -> Dict[str, Any]:
    """
    Konversi CognitiveIntent → dict filter kompatibel dengan handler lama.
    Wrapper backward-compat agar handler yang belum direfactor penuh tetap bisa berjalan.
    Mengembalikan {} jika intent adalah None.
    """
    if intent is None:
        return {}

    filters: Dict[str, Any] = {}

    # ── Jenjang (SUPPORT MULTI) ─────────────────────────────
    if getattr(intent, "jenjang_list", None):
        filters["bentuk_pendidikan_in"] = [normalize_jenjang(j) for j in intent.jenjang_list]

    elif getattr(intent, "jenjang", None):
        _PAUD_EXPAND = ["TK", "KB", "RA", "SPS", "TPA", "PAUDQ", "SPK TK", "SPK KB", "SDTK"]
        if intent.jenjang.upper() == "PAUD":
            filters["bentuk_pendidikan_in"] = _PAUD_EXPAND
        else:
            filters["bentuk_pendidikan"] = normalize_jenjang(intent.jenjang)

    if getattr(intent, "status", None):
        filters["status_sekolah"] = intent.status

    if getattr(intent, "akreditasi_mode", None) == "in" and getattr(intent, "akreditasi_list", None):
        filters["akreditasi_in"] = [g.upper() for g in intent.akreditasi_list]
    elif getattr(intent, "akreditasi", None):
        mode = getattr(intent, "akreditasi_mode", None)
        if mode == "gte":
            grades = ["A", "B", "C"]
            if intent.akreditasi in grades:
                idx = grades.index(intent.akreditasi)
                filters["akreditasi_gte"] = grades[:idx + 1]
        elif mode == "exclude":
            filters["akreditasi_exclude"] = intent.akreditasi
        elif intent.akreditasi in ("TT", "BELUM"):
            filters["akreditasi_in"] = ["TT", "Belum", "belum terakreditasi", "-"]
        else:
            filters["akreditasi_exact"] = intent.akreditasi

    if getattr(intent, "yayasan", None):
        filters["penyelenggara"] = intent.yayasan

    # ── QC Filters ──────────────────────────────────────────────────────────
    if getattr(intent, "qc_masalah_only", False):
        filters["qc_masalah_only"] = True

    if getattr(intent, "qc_min_masalah", None) is not None:
        filters["qc_min_masalah"] = intent.qc_min_masalah

    if getattr(intent, "qc_field", None) is not None:
        filters["qc_field"] = intent.qc_field

    if getattr(intent, "qc_field_list", None):
        filters["qc_field_list"] = list(intent.qc_field_list)

    if getattr(intent, "qc_persen_min", None) is not None:
        filters["qc_persen_min"] = float(intent.qc_persen_min)

    if getattr(intent, "qc_persen_max", None) is not None:
        filters["qc_persen_max"] = float(intent.qc_persen_max)

    return filters

