# services/core/es_query_builder.py 
"""
Centralized ES query builder.
Input: CognitiveIntent + es_filters dari _build_es_filters()
Output: dict ES query siap kirim
"""

from typing import Dict, Any, List, Optional

def build_wilayah_filter(intent) -> List[Dict]:
    """
    Bangun filter wilayah bertingkat dari intent.
    Gunakan field .keyword untuk exact match yang efisien.
    """
    filters = []
    wil = intent.wilayah

    # Hierarki: desa > kecamatan > kabupaten > provinsi
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
        filters.append({"term": {"provinsi": wil.provinsi.lower()}})
    # scope nasional → tidak ada filter wilayah

    return filters


def build_es_query(
    intent,
    es_filters: Dict[str, Any],
    size: int = 10,
    from_: int = 0,
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

    # ── Jumlah Siswa (Range) → HARD FILTER ──
    if getattr(intent, "siswa_min", None) or getattr(intent, "siswa_max", None):
        range_filter = {"range": {"siswa_total": {}}}
        if intent.siswa_min is not None:
            range_filter["range"]["siswa_total"]["gte"] = intent.siswa_min
        if intent.siswa_max:
            range_filter["range"]["siswa_total"]["lte"] = intent.siswa_max
        hard_filters.append(range_filter)
    
    # ── Penyelenggara → (opsional, bisa scoring atau filter) ──
    if "penyelenggara" in es_filters:
        must_clauses.append({
            "bool": {
                "should": [
                    {"match": {"penyelenggara": {"query": es_filters["penyelenggara"], "fuzziness": "AUTO"}}},
                    {"term": {"penyelenggara.keyword": es_filters["penyelenggara"].lower()}},
                ],
                "minimum_should_match": 1
            }
        })

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

    query = {
        "query": {
            "bool": {
                "must": must_clauses if must_clauses else [{"match_all": {}}],
                "filter": hard_filters,
            }
        },
        "size": size,
        "from": from_,
    }
    
    # ── Sorting ──
    if getattr(intent, "sort_by", None) == "siswa_total":
        query["sort"] = [
            {"siswa_total": {"order": intent.sort_order or "desc"}},
            {"_score": {"order": "desc"}}
        ]
        
    return query


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

    return filters

