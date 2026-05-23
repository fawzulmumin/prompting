# services/core/aggregation_core.py
import os
import logging
from typing import Dict, Any, List, Optional
from elasticsearch import AsyncElasticsearch
from elasticsearch import ApiError, TransportError
 
logger = logging.getLogger(__name__)

# ── Provinsi variants (ES menyimpan dengan titik-titik singkatan khusus) ──
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
        # ES menyimpan sebagai "prov. d.i. yogyakarta"; alias "yogyakarta" / "prov. yogyakarta" juga dicoba
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

# ── Akreditasi alias — map nilai LLM ke nilai ES aktual ──────────────────────
# ES menyimpan sekolah tidak terakreditasi sebagai "Tidak Terakreditasi" (bukan "TT")
_AKRED_ES_ALIAS: Dict[str, str] = {
    "TT":     "Tidak Terakreditasi",
    "BELUM":  "Tidak Terakreditasi",
    "-":      "Tidak Terakreditasi",
}

# ── Metric fields yang ada di idx_sekolah_profile (BUKAN idx_sekolah_qc) ────
_METRIC_NEEDS_PROFILE: frozenset = frozenset({
    "siswa", "siswa_l", "siswa_p",
    "guru", "guru_pns", "guru_honor", "guru_sertifikasi", "guru_s1",
    "tendik",
    "guru_p3k", "guru_l", "guru_p_gender", "total_komputer",
    "siswa_baru", "siswa_mengulang", "siswa_lulus",
})

# ── Distribusi fields yang HANYA ada di idx_sekolah_profile ─────────────────
# akses_internet / waktu_penyelenggaraan ada di kedua index.
# sumber_air hanya dari idx_prompting_agent → hanya di profile index.
# sumber_listrik: ada di qc index tapi coverage sangat rendah (8-9 docs per provinsi);
# profile index memiliki coverage jauh lebih baik.
_DISTRIBUSI_NEEDS_PROFILE: frozenset = frozenset({
    "sumber_air",
    "sumber_listrik",
    "status_kepemilikan",
    "partisipasi_bos",
})

# ── Numeric filter ES fields yang ada di idx_sekolah_profile (bukan QC) ─────
# Dipakai untuk memaksa profile index ketika numeric_filters menyertakan field ini.
_PROFILE_NUMERIC_FIELDS: frozenset = frozenset({
    "siswa_total", "siswa_l", "siswa_p",
    "guru_total", "guru_pns", "guru_honor", "guru_sertifikasi",
    "guru_p3k", "guru_l", "guru_p",
    "tendik_l", "tendik_p",
    "total_komputer", "ruang_kelas",
    "toilet_siswa_laki", "toilet_siswa_perempuan",
})

# ── Metric target mapping ────────────────────────────────────────────────────
_METRIC_MAP: Dict[str, str] = {
    "siswa":            "siswa_total",
    "siswa_l":          "siswa_l",
    "siswa_p":          "siswa_p",
    "guru":             "guru_total",
    "guru_pns":         "guru_pns",
    "guru_honor":       "guru_honor",
    "guru_sertifikasi": "guru_sertifikasi",
    "guru_s1":          "guru_s1_keatas",
    "tendik":           None,   # script sum tendik_l + tendik_p
    "guru_p3k":         "guru_p3k",
    "guru_l":           "guru_l",
    "guru_p_gender":    "guru_p",
    "total_komputer":   "total_komputer",
    "siswa_baru":       None,   # script sum siswa_baru_l + siswa_baru_p
    "siswa_mengulang":  None,   # script sum siswa_mengulang_l + siswa_mengulang_p
    "siswa_lulus":      None,   # script sum siswa_lulus_l + siswa_lulus_p
}

# ── Script-based metrics: pair of (field_l, field_p) to sum ──────────────────
_SCRIPT_PAIRS: Dict[str, tuple] = {
    "tendik":        ("tendik_l",           "tendik_p"),
    "siswa_baru":    ("siswa_baru_l",        "siswa_baru_p"),
    "siswa_mengulang": ("siswa_mengulang_l", "siswa_mengulang_p"),
    "siswa_lulus":   ("siswa_lulus_l",       "siswa_lulus_p"),
}

def build_metric_aggs(metric_target: Optional[str]) -> Dict[str, Any]:
    """Bangun ES aggregation clause untuk metric yang diminta."""
    if not metric_target or metric_target == "sekolah":
        return {"main_metric": {"value_count": {"field": "npsn"}}}

    es_field = _METRIC_MAP.get(metric_target)
    if es_field:
        return {"main_metric": {"sum": {"field": es_field}}}

    if metric_target in _SCRIPT_PAIRS:
        f1, f2 = _SCRIPT_PAIRS[metric_target]
        script = (
            f"doc['{f1}'].size() > 0 && doc['{f2}'].size() > 0 "
            f"? doc['{f1}'].value + doc['{f2}'].value : 0"
        )
        return {"main_metric": {"sum": {"script": {"source": script}}}}

    # Fallback ke count sekolah
    return {"main_metric": {"value_count": {"field": "npsn"}}}


def build_distribusi_aggs(field_name: str, size: int = 15) -> Dict[str, Any]:
    """Bangun ES terms aggregation untuk distribusi field keyword."""
    return {"distribusi": {"terms": {"field": field_name, "size": size}}}

def build_qc_aggs() -> Dict[str, Any]:
    """
    Bangun ES aggregation untuk kualitas data (QC).
    Digunakan ketika intent.qc_masalah_only=True atau query eksplisit tentang kualitas data.

    qc_score adalah object biasa (bukan nested).
    qc_1..qc_13 ada di dalam plain object "qc" — query langsung tanpa nested wrapper.
    """
    # Sub-aggs per kolom QC: hitung berapa dokumen dengan nilai bermasalah (> 1)
    kolom_aggs: Dict[str, Any] = {}
    for i in range(1, 14):
        kolom_aggs[f"qc_{i}_masalah"] = {
            "filter": {"range": {f"qc.qc_{i}": {"gt": 1}}}
        }

    return {
        "avg_masalah": {
            "avg": {"field": "qc_score.total_masalah"}
        },
        "sekolah_bermasalah": {
            "filter": {"range": {"qc_score.total_masalah": {"gt": 0}}}
        },
        "distribusi_skor": {
            "histogram": {"field": "qc_score.persen_bersih", "interval": 10}
        },
        **kolom_aggs,
    }


async def execute_aggregation_query(
    es_client: AsyncElasticsearch,
    entities: Dict[str, Any]
) -> Optional[int]:
 
    logger.info(f"EXTRACTED ENTITIES FINAL: {entities}")
 
    index = os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")
 
    filters = build_filters(entities)
 
    # ── Gunakan count API, bukan search ──────────────────────────────────────
    # count API jauh lebih cepat untuk pure counting karena tidak perlu
    # fetch/score dokumen — langsung return angka dari inverted index.
    # Sebelumnya pakai _search dengan track_total_hits:True yang harus
    # scan semua dokumen untuk dataset besar (contoh: provinsi 85K+ docs).
    count_body = {
        "query": {
            "bool": {
                "filter": filters
            }
        }
    }
 
    logger.debug("AGG COUNT FILTERS: %s", filters)
 
    try:
        response = await es_client.count(
            index=index,
            body=count_body,
            request_timeout=10
        )
 
        return response.get("count", 0)
 
    except (ApiError, TransportError):
        logger.exception("Aggregation count failed")
        return None
 
async def execute_nested_aggregation_query(
    es_client: AsyncElasticsearch,
    entities: Dict[str, Any],
    metric_target: Optional[str] = None,
    distribusi_field: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Jalankan count + breakdown jenjang & status dalam SATU query ES.
    Digunakan untuk level kabupaten dan provinsi agar narasi lebih kaya.
    
    Return:
        {
            "total": int,
            "breakdown_jenjang": {"SD": 1234, "SMP": 567, ...},
            "breakdown_status":  {"NEGERI": 800, "SWASTA": 1001, ...}
        }
        atau None jika gagal.
    """
    # Pilih index: siswa/guru/tendik hanya ada di idx_sekolah_profile;
    # sumber_air juga hanya ada di profile index.
    _use_profile_index = (
        metric_target in _METRIC_NEEDS_PROFILE
        or distribusi_field in _DISTRIBUSI_NEEDS_PROFILE
    )
    # Cek numeric_filters: jika filter menggunakan field dari profile index,
    # paksa route ke profile meski metric_target=None (count sekolah).
    if not _use_profile_index:
        _numeric_filters = entities.get("numeric_filters", [])
        if _numeric_filters:
            from app.services.core.es_query_builder import _NUMERIC_FIELD_MAP
            _filter_es_fields = {
                _NUMERIC_FIELD_MAP.get(
                    f.field if hasattr(f, "field") else (f.get("field", "") if isinstance(f, dict) else ""),
                    f.field if hasattr(f, "field") else (f.get("field", "") if isinstance(f, dict) else ""),
                )
                for f in _numeric_filters
            }
            if _filter_es_fields & _PROFILE_NUMERIC_FIELDS:
                _use_profile_index = True
                logger.info(
                    "[AGG_CORE] numeric_filter uses profile fields %s → force index=profile",
                    _filter_es_fields & _PROFILE_NUMERIC_FIELDS,
                )
    index = (
        os.getenv("ELASTICSEARCH_PROFILE_INDEX", "idx_sekolah_profile")
        if _use_profile_index
        else os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")
    )
    filters = build_filters(entities)
    _akreditasi = entities.get("akreditasi")
    jenjang_multi = entities.get("jenjang_multi")

    # Metric aggs — dinamis berdasarkan target
    metric_aggs = build_metric_aggs(metric_target)
    logger.info("[AGG_CORE] METRIC AGG INJECTED: metric_target=%r agg=%s index=%s",
                metric_target, metric_aggs, index)

    body = {
        "size": 0,
        "query": {"bool": {"filter": filters}},
        "aggs": {
            **metric_aggs,
            "by_jenjang": {
                "terms": {
                    "field": "bentuk_pendidikan",
                    "size": 20,
                    "order": {"_count": "desc"}
                }
            },
            "by_status": {
                "terms": {"field": "status_sekolah", "size": 5}
            },
            **(build_distribusi_aggs(distribusi_field) if distribusi_field else {}),
        },
        "track_total_hits": True
    }

    # Tambah sub-agg metric ke by_jenjang ketika metric bukan sekolah,
    # agar breakdown jenjang menampilkan sum metric (bukan doc_count sekolah).
    if metric_target and metric_target != "sekolah":
        body["aggs"]["by_jenjang"]["aggs"] = {
            "metric_val": list(metric_aggs.values())[0]
        }

    logger.debug("NESTED AGG FILTERS: %s", filters)
    # ── Dynamic aggregation: multi-jenjang + akreditasi ─────────────────
    if _akreditasi and jenjang_multi:

        for jenjang_item in jenjang_multi:
            body["aggs"][f"filtered_{jenjang_item}"] = {
                "filter": {
                    "bool": {
                        "must": [
                            {"term": {"bentuk_pendidikan": jenjang_item}}
                            # akreditasi sudah dari base filter
                        ]
                    }
                }
            }

        logger.debug(
            "[AGG_CORE] multi-jenjang aggs added: %s",
            jenjang_multi
        )
    try:
        response = await es_client.search(
            index=index,
            body=body,
            request_timeout=10
        )
 
        # Document count (always needed for breakdown context)
        total_raw = response.get("hits", {}).get("total", {})
        total_doc = total_raw.get("value", 0) if isinstance(total_raw, dict) else total_raw

        # Metric value — use main_metric agg if metric_target != sekolah
        aggs = response.get("aggregations", {})
        _main = aggs.get("main_metric", {})
        main_metric_val = _main.get("value")
        logger.info("[AGG_CORE] MAIN METRIC RESULT: metric_target=%r main=%s total_doc=%s",
                    metric_target, _main, total_doc)
        is_numeric = metric_target and metric_target != "sekolah"
        # Bug-1 diagnostic: jika sum == doc_count untuk field numerik di profile index,
        # kemungkinan field berisi flag (0/1) bukan count aktual.
        if (is_numeric and _use_profile_index and main_metric_val is not None
                and total_doc > 0 and int(main_metric_val) == total_doc):
            logger.warning(
                "[AGG_CORE] SUSPICIOUS: sum(%s)=%.0f == doc_count=%d — "
                "field mungkin flag bukan count aktual. Verifikasi di ES.",
                metric_target, main_metric_val, total_doc
            )
        if (
            is_numeric
            and not _use_profile_index  # profile index seharusnya punya data
            and (main_metric_val is None or main_metric_val == 0)
            and total_doc > 0
        ):
            # Field tidak ada di qc index — graceful fallback ke school count
            logger.warning(
                "[AGG_CORE] metric_target=%s returned 0 dari qc index; "
                "fallback ke doc_count=%d", metric_target, total_doc
            )
            effective_total = total_doc
            metric_target = None  # Treat as school count to avoid misleading labels
        else:
            effective_total = int(main_metric_val or 0) if is_numeric else total_doc

        # Breakdown jenjang — gunakan sum metric jika numeric, doc_count jika sekolah
        jenjang_buckets = aggs.get("by_jenjang", {}).get("buckets", [])
        breakdown_jenjang = {
            b["key"]: (
                int(b.get("metric_val", {}).get("value", 0) or 0)
                if is_numeric else b["doc_count"]
            )
            for b in jenjang_buckets
        }

        # Breakdown status
        status_buckets = aggs.get("by_status", {}).get("buckets", [])
        breakdown_status = {b["key"]: b["doc_count"] for b in status_buckets}

        # Distribusi field jika diminta
        distribusi_result = None
        if distribusi_field:
            dist_buckets = aggs.get("distribusi", {}).get("buckets", [])
            dist_total = sum(b["doc_count"] for b in dist_buckets)
            distribusi_result = [
                {
                    "key": b["key"],
                    "count": b["doc_count"],
                    "pct": round(b["doc_count"] / dist_total * 100, 1) if dist_total > 0 else 0,
                }
                for b in dist_buckets
            ]

        return {
            "total":             effective_total,
            "total_doc":         total_doc,
            "metric_target":     metric_target or "sekolah",
            "breakdown_jenjang": breakdown_jenjang,
            "breakdown_status":  breakdown_status,
            "distribusi":        distribusi_result,
            "distribusi_field":  distribusi_field,
        }
 
    except (ApiError, TransportError):
        logger.exception("Nested aggregation failed, falling back to count")
        return None
 
PULAU_PROVINSI_MAP: Dict[str, List[str]] = {
    "jawa": [
        "prov. dki jakarta", "prov. jawa barat", "prov. jawa tengah",
        "prov. jawa timur", "prov. banten", "prov. d.i. yogyakarta",
    ],
    "sumatera": [
        "prov. aceh", "prov. sumatera utara", "prov. sumatera barat",
        "prov. riau", "prov. kepulauan riau", "prov. jambi",
        "prov. sumatera selatan", "prov. bengkulu", "prov. lampung",
        "prov. kepulauan bangka belitung",
    ],
    "kalimantan": [
        "prov. kalimantan barat", "prov. kalimantan tengah",
        "prov. kalimantan selatan", "prov. kalimantan timur",
        "prov. kalimantan utara",
    ],
    "sulawesi": [
        "prov. sulawesi utara", "prov. gorontalo", "prov. sulawesi tengah",
        "prov. sulawesi barat", "prov. sulawesi selatan", "prov. sulawesi tenggara",
    ],
    "papua": [
        "prov. papua", "prov. papua barat", "prov. papua selatan",
        "prov. papua tengah", "prov. papua pegunungan", "prov. papua barat daya",
    ],
    "maluku": ["prov. maluku", "prov. maluku utara"],
    "nusa tenggara": ["prov. nusa tenggara barat", "prov. nusa tenggara timur"],
    "bali": ["prov. bali"],
    "indonesia timur": [
        "prov. kalimantan timur", "prov. kalimantan utara", "prov. sulawesi utara",
        "prov. sulawesi tengah", "prov. sulawesi selatan", "prov. sulawesi tenggara",
        "prov. sulawesi barat", "prov. gorontalo", "prov. maluku", "prov. maluku utara",
        "prov. papua", "prov. papua barat", "prov. nusa tenggara timur",
        "prov. nusa tenggara barat", "prov. bali",
    ],
}


def build_filters(entities: Dict[str, Any]) -> List[Dict[str, Any]]:
    filters = []

    # Key-key internal yang TIDAK boleh jadi term filter langsung di generic loop
    _SKIP_KEYS = {
        "hierarchy_warning",
        "_akreditasi_values",
        "akreditasi_gte",
        "akreditasi_exact",
        "akreditasi_mode",
        "akreditasi",
        "bentuk_pendidikan",
        "_area_group",
        "_kecamatan_list",
        # Desa ambiguity metadata — ditangani secara eksplisit di bawah
        "_desa_ambiguous",
        "_desa_candidates",
        # Field QC — ditangani secara eksplisit di bawah
        "qc_masalah_only",
        "qc_field",
        "qc_min_masalah",
        "qc_persen_min",
        "qc_persen_max",
        "qc_field_list",
        # Pulau — ditangani secara eksplisit di bawah
        "pulau",
        "provinsi_list",
        # Numeric range filters — ditangani secara eksplisit di bawah
        "numeric_filters",
    }

    # ── Pulau → expand ke terms filter multi-provinsi ─────────────────────────
    _pulau = entities.get("pulau")
    if _pulau and _pulau in PULAU_PROVINSI_MAP:
        filters.append({"terms": {"provinsi": PULAU_PROVINSI_MAP[_pulau]}})
        logger.debug("[AGG_CORE] pulau filter: %s → %d provinsi", _pulau, len(PULAU_PROVINSI_MAP[_pulau]))
    elif entities.get("provinsi_list"):
        filters.append({"terms": {"provinsi": entities["provinsi_list"]}})
        logger.debug("[AGG_CORE] provinsi_list filter: %d provinsi", len(entities["provinsi_list"]))

    # Jika ada area_group (mis. purwokerto → list kecamatan),
    # tambahkan terms filter untuk kecamatan
    kec_list = entities.get("_kecamatan_list")
    if kec_list and isinstance(kec_list, list):
        filters.append({"terms": {"kecamatan": kec_list}})

    # ── Desa ambiguity: gunakan OR atas wilayah_key semua kandidat ──────
    _desa_ambiguous   = entities.get("_desa_ambiguous", False)
    _desa_candidates  = entities.get("_desa_candidates", [])
    if _desa_ambiguous and _desa_candidates:
        desa_name = (entities.get("desa_kelurahan") or "").lower()
        wilayah_terms = []
        for candidate in _desa_candidates:
            prov = (candidate.get("provinsi") or "").lower()
            kab  = (candidate.get("kabupaten") or "").lower()
            kec  = (candidate.get("kecamatan") or "").lower()
            if prov and kab and kec and desa_name:
                wilayah_terms.append(f"{prov}|{kab}|{kec}|{desa_name}")
        if len(wilayah_terms) == 1:
            filters.append({"term": {"wilayah_key": wilayah_terms[0]}})
        elif len(wilayah_terms) > 1:
            filters.append({"bool": {
                "should": [{"term": {"wilayah_key": wk}} for wk in wilayah_terms],
                "minimum_should_match": 1,
            }})
        logger.debug("[FILTERS] desa_ambiguous OR wilayah_key: %s", wilayah_terms)

    for field, value in entities.items():

        if field in _SKIP_KEYS:
            continue
        # Skip desa_kelurahan when handled by ambiguous wilayah_key filter
        if field == "desa_kelurahan" and _desa_ambiguous:
            continue

        # MULTI VALUE (PAUD / multi-jenjang)
        if field == "bentuk_pendidikan_in":
            filters.append({"terms": {"bentuk_pendidikan": value}})

        elif field == "provinsi":
            filters.append(_build_provinsi_filter(value))
        # default: term filter untuk string/bool/int
        else:
            filters.append({"term": {field: value}})
    
    # ── Filter akreditasi (IMPROVED VERSION) ─────────────────────────
    _akreditasi      = entities.get("akreditasi")
    _akreditasi_mode = entities.get("akreditasi_mode", "exact")

    if _akreditasi:
        _AKREDITASI_ORDER = ["A", "B", "C", "TT"]

        if _akreditasi_mode == "gte":
            try:
                _idx = _AKREDITASI_ORDER.index(_akreditasi.upper())
                _valid_grades = _AKREDITASI_ORDER[:_idx + 1]
            except ValueError:
                _valid_grades = [_akreditasi.upper()]

            filters.append({"terms": {"akreditasi": _valid_grades}})
            logger.debug("[AGG_CORE] akreditasi GTE filter: %s", _valid_grades)

        elif _akreditasi_mode == "exclude":
            filters.append({
                "bool": {
                    "must_not": [
                        {"term": {"akreditasi": _akreditasi.upper()}}
                    ]
                }
            })

        else:  # exact
            _akred_val = _AKRED_ES_ALIAS.get(_akreditasi.upper(), _akreditasi.upper())
            filters.append({
                "term": {"akreditasi": _akred_val}
            })
            logger.debug("[AGG_CORE] akreditasi exact filter: %s → %s", _akreditasi.upper(), _akred_val)
    
    # Akreditasi filter — gunakan terms (multiple values) atau term (single)
    elif "akreditasi_gte" in entities and entities["akreditasi_gte"]:
        filters.append({"terms": {"akreditasi": entities["akreditasi_gte"]}})
        logger.debug("AGG akreditasi_gte filter: %s", entities["akreditasi_gte"])
    elif "akreditasi_exact" in entities and entities["akreditasi_exact"]:
        filters.append({"term": {"akreditasi": entities["akreditasi_exact"]}})
        logger.debug("AGG akreditasi_exact filter: %s", entities["akreditasi_exact"])
    # Legacy fallback: _akreditasi_values dari implementasi sebelumnya
    elif "_akreditasi_values" in entities and entities["_akreditasi_values"]:
        filters.append({"terms": {"akreditasi": entities["_akreditasi_values"]}})
        logger.debug("AGG _akreditasi_values (legacy) filter: %s", entities["_akreditasi_values"])
        
    # ── Filter jenjang (bentuk_pendidikan) ──────────────────────────────────────
    _bentuk = entities.get("bentuk_pendidikan")
    _bentuk_in = entities.get("bentuk_pendidikan_in")
    if _bentuk_in:
        filters.append({"terms": {"bentuk_pendidikan": _bentuk_in}})
    elif _bentuk:
        filters.append({"term": {"bentuk_pendidikan": _bentuk}})

    # ── Filter keaktifan ─────────────────────────────────────────────────────
    if "keaktifan" in entities and entities["keaktifan"] is not None:
        filters.append({"term": {"keaktifan": entities["keaktifan"]}})
        logger.debug("[AGG_CORE] keaktifan filter: %s", entities["keaktifan"])

    # ── Filter wilayah_terpencil ─────────────────────────────────────────────
    if "wilayah_terpencil" in entities and entities["wilayah_terpencil"] is not None:
        filters.append({"term": {"wilayah_terpencil": entities["wilayah_terpencil"]}})
        logger.debug("[AGG_CORE] wilayah_terpencil filter: %s", entities["wilayah_terpencil"])

    # ── Filter wilayah_perbatasan ────────────────────────────────────────────
    if "wilayah_perbatasan" in entities and entities["wilayah_perbatasan"] is not None:
        filters.append({"term": {"wilayah_perbatasan": entities["wilayah_perbatasan"]}})
        logger.debug("[AGG_CORE] wilayah_perbatasan filter: %s", entities["wilayah_perbatasan"])

    # ── Filter QC: hanya sekolah dengan data bermasalah ─────────────────────
    if entities.get("qc_masalah_only"):
        filters.append({"range": {"qc_score.total_masalah": {"gt": 0}}})
        logger.debug("[AGG_CORE] qc_masalah_only filter applied")

    if entities.get("qc_min_masalah") is not None:
        filters.append({"range": {"qc_score.total_masalah": {"gte": int(entities["qc_min_masalah"])}}})
        logger.debug("[AGG_CORE] qc_min_masalah filter: %s", entities["qc_min_masalah"])

    # ── Filter QC spesifik per kolom (qc_field / qc_field_list) ─────────────
    _qc_field = entities.get("qc_field")
    if _qc_field is not None and isinstance(_qc_field, int) and 1 <= _qc_field <= 13:
        filters.append({"range": {f"qc.qc_{_qc_field}": {"gte": 2}}})
        logger.debug("[AGG_CORE] qc_field filter: qc_%d >= 2", _qc_field)

    for _qf in (entities.get("qc_field_list") or []):
        if isinstance(_qf, int) and 1 <= _qf <= 13:
            filters.append({"range": {f"qc.qc_{_qf}": {"gte": 2}}})
            logger.debug("[AGG_CORE] qc_field_list item filter: qc_%d >= 2", _qf)

    # ── Filter persen_bersih (qc_persen_min / qc_persen_max) ────────────────
    _persen_min = entities.get("qc_persen_min")
    _persen_max = entities.get("qc_persen_max")
    _persen_min_valid = isinstance(_persen_min, (int, float)) and _persen_min is not False
    _persen_max_valid = isinstance(_persen_max, (int, float)) and _persen_max is not False
    if _persen_min_valid or _persen_max_valid:
        range_clause: dict = {}
        if _persen_min_valid:
            range_clause["gte"] = float(_persen_min)
        if _persen_max_valid:
            range_clause["lte"] = float(_persen_max)
        filters.append({"range": {"qc_score.persen_bersih": range_clause}})
        logger.debug("[AGG_CORE] persen_bersih filter: %s", range_clause)

    # ── Numeric range filters (siswa, guru, dll.) ────────────────────────────
    _numeric_filters = entities.get("numeric_filters", [])
    if _numeric_filters:
        from app.services.core.es_query_builder import build_numeric_filters
        range_clauses = build_numeric_filters(_numeric_filters)
        if range_clauses:
            filters.extend(range_clauses)
            logger.info("[NUMERIC_FILTER] Applied %d range filter(s)", len(range_clauses))

    return filters
 
async def get_entity_doc_count(es_client, entity: Dict[str, str]) -> int:
 
    must_filters = []
 
    for field, value in entity.items():
        must_filters.append({
            "term": {
                field: value
            }                                                                                                                                                           
        })
 
    query = {
        "query": {
            "bool": {
                "must": must_filters
            }
        },
        "size": 0
    }
 
    index = os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")
    try:
        response = await es_client.search(index=index, body=query)
        return response["hits"]["total"]["value"]
    except Exception as e:
        logger.warning("[AGG_CORE] get_entity_doc_count failed: %s", e)
        return 0
 
 
async def get_entity_doc_counts_batch(
    es_client,
    entities_list: list,
    index: str = "idx_sekolah_qc"
) -> list:
    """
    Batch version of get_entity_doc_count using ES msearch.
    Sends all queries in a single HTTP request instead of N sequential requests.
 
    Args:
        entities_list: list of entity dicts, e.g.
            [{"kabupaten": "kab. kuningan"}, {"kecamatan": "kec. kuningan"}, ...]
    Returns:
        list of int (doc counts), same order as input
    """
    if not entities_list:
        return []
 
    # Build msearch body: alternating header + query lines
    msearch_body = []
    for entity in entities_list:
        must_filters = [{"term": {field: value}} for field, value in entity.items()]
        msearch_body.append({"index": index})
        msearch_body.append({
            "query": {"bool": {"must": must_filters}},
            "size": 0
        })
 
    try:
        response = await es_client.msearch(body=msearch_body)
        counts = []
        for resp in response.get("responses", []):
            if "error" in resp:
                counts.append(0)
            else:
                counts.append(resp.get("hits", {}).get("total", {}).get("value", 0))
        return counts
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("[MSEARCH] Batch doc count failed: %s", e)
        # Fallback: return zeros (caller should handle gracefully)
        return [0] * len(entities_list)
 
def calculate_confidence(total: int, entities: Dict[str, Any]) -> float:
    """
    Confidence scoring for aggregation queries.
    """
 
    if total <= 0:
        return 0.0
 
    score = 0.5  # base
 
    # Level specificity boost
    if "desa_kelurahan" in entities:
        score += 0.25
    elif "kecamatan" in entities:
        score += 0.20
    elif "kabupaten" in entities:
        score += 0.15
    elif "provinsi" in entities:
        score += 0.10
 
    # Year filter boost
    if "tahun" in entities:
        score += 0.10
 
    # Status filter boost
    if "status_sekolah" in entities:
        score += 0.05
    
    # Penalize very small totals
    if total < 5:
        score -= 0.15
 
    # Penalize if hierarchy warning exists
    if "hierarchy_warning" in entities:
        score -= 0.15
        
    return round(min(max(score, 0.0), 1.0), 2)
 
async def execute_global_ranking_query(
    es_client: AsyncElasticsearch,
    group_field: str,
    size: int = 5,
    jenjang_filter: Optional[str] = None,
    provinsi_filter: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:

    index = os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")

    filters = []
    if jenjang_filter:
        filters.append({"term": {"bentuk_pendidikan": jenjang_filter.upper()}})
    if provinsi_filter:
        filters.append({"term": {"provinsi": provinsi_filter.lower()}})

    body: Dict[str, Any] = {
        "size": 0,
        "aggs": {
            "group_by_field": {
                "terms": {
                    "field": group_field,
                    "size": size,
                    "order": {"_count": "desc"}
                }
            }
        }
    }
    if filters:
        body["query"] = {"bool": {"filter": filters}}

    try:
        response = await es_client.search(
            index=index,
            body=body,
            request_timeout=10
        )

        buckets = (
            response.get("aggregations", {})
            .get("group_by_field", {})
            .get("buckets", [])
        )

        return buckets

    except (ApiError, TransportError):
        logger.exception("Global ranking aggregation failed")
        return None
 
def normalize_entity_hierarchy(entity: Dict[str, Any]) -> Dict[str, Any]:
 
    geo_fields = ["desa_kelurahan", "kecamatan", "kabupaten", "provinsi"]
 
    geo_entity = {}
 
    if "desa_kelurahan" in entity:
        # Selalu sertakan desa_kelurahan
        geo_entity["desa_kelurahan"] = entity["desa_kelurahan"]
 
        # TAMBAHAN: jika ada kecamatan sebagai konteks spesifik,
        # sertakan juga untuk query yang lebih akurat.
        # Contoh: cibunar di kec. parungpanjang ≠ cibunar di kec. balapulang
        if "kecamatan" in entity:
            geo_entity["kecamatan"] = entity["kecamatan"]
 
    elif "kecamatan" in entity:
        geo_entity["kecamatan"] = entity["kecamatan"]
 
    elif "kabupaten" in entity:
        geo_entity["kabupaten"] = entity["kabupaten"]
 
    elif "provinsi" in entity:
        geo_entity["provinsi"] = entity["provinsi"]
 
    extra_filters = {
        k: v
        for k, v in entity.items()
        if k not in geo_fields
    }
 
    normalized = {**geo_entity, **extra_filters}
 
    normalized_fixed = {}
 
    for k, v in normalized.items():
        if not isinstance(v, str):
            normalized_fixed[k] = v
            continue
        if k in geo_fields:
            normalized_fixed[k] = v.lower()
        elif k == "status_sekolah":
            normalized_fixed[k] = v.upper()
        else:
            normalized_fixed[k] = v
 
    return normalized_fixed
 
async def execute_drilldown_query(
    es_client: AsyncElasticsearch,
    entities: Dict[str, Any],
    drilldown_field: str,
    size: int = 3,
    metric_target: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Ambil top-N wilayah di level bawahnya dalam satu query ES.

    Jika metric_target diberikan (misal "siswa"), "count" berisi sum metric
    (jumlah siswa), bukan doc_count (jumlah sekolah).

    Return:
        [{"name": "kab. bogor", "count": 8234}, ...]
    """
    _is_numeric = metric_target and metric_target != "sekolah"
    _use_profile = metric_target in _METRIC_NEEDS_PROFILE
    index = (
        os.getenv("ELASTICSEARCH_PROFILE_INDEX", "idx_sekolah_profile")
        if _use_profile
        else os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")
    )
    filters = build_filters(entities)

    drilldown_agg: Dict[str, Any] = {
        "terms": {
            "field": drilldown_field,
            "size": size,
            "order": {"_count": "desc"},
        }
    }
    if _is_numeric:
        _agg_spec = list(build_metric_aggs(metric_target).values())[0]
        drilldown_agg["aggs"] = {"metric_val": _agg_spec}

    body = {
        "size": 0,
        "query": {"bool": {"filter": filters}},
        "aggs": {"drilldown": drilldown_agg},
    }

    try:
        response = await es_client.search(
            index=index,
            body=body,
            request_timeout=10,
        )
        buckets = (
            response.get("aggregations", {})
            .get("drilldown", {})
            .get("buckets", [])
        )
        return [
            {
                "name": b["key"],
                "count": (
                    int(b.get("metric_val", {}).get("value", 0) or 0)
                    if _is_numeric
                    else b["doc_count"]
                ),
            }
            for b in buckets
        ]
    except (ApiError, TransportError):
        logger.exception("Drilldown query failed")
        return []

async def handle_qc_percentage_query(
    intent,
    es_client: AsyncElasticsearch,
    query: str = "",
) -> Dict[str, Any]:
    """
    Hitung persentase sekolah bersih / bermasalah dari ES.

    Menjalankan dua query concurrent:
      1. Total semua sekolah (dengan filter wilayah+jenjang, tanpa filter QC)
      2. Total sekolah bersih (total_masalah == 0)
    Kemudian menghitung proporsinya.
    """
    import asyncio as _asyncio
    from app.services.core.es_query_builder import extract_filters_from_intent, build_es_query
    from app.services.core.result_model import ResultStatus

    index = os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")

    w = getattr(intent, "wilayah", None)
    if w and getattr(w, "kabupaten", None):
        wilayah_label = w.kabupaten.title()
    elif w and getattr(w, "provinsi", None):
        wilayah_label = w.provinsi.title()
    else:
        wilayah_label = "seluruh Indonesia"

    es_filters = extract_filters_from_intent(intent)

    try:
        q_total = build_es_query(intent, es_filters, size=0)
        q_total["aggs"] = {"total": {"value_count": {"field": "npsn"}}}

        q_bersih = build_es_query(intent, es_filters, size=0)
        q_bersih["query"]["bool"].setdefault("filter", [])
        q_bersih["query"]["bool"]["filter"].append(
            {"term": {"qc_score.total_masalah": 0}}
        )
        q_bersih["aggs"] = {"total": {"value_count": {"field": "npsn"}}}

        r_total, r_bersih = await _asyncio.gather(
            es_client.search(index=index, body=q_total, request_timeout=10),
            es_client.search(index=index, body=q_bersih, request_timeout=10),
        )

        total = r_total.get("aggregations", {}).get("total", {}).get("value", 0)
        bersih = r_bersih.get("aggregations", {}).get("total", {}).get("value", 0)
        bermasalah = total - bersih

        persen_bersih = round((bersih / total) * 100, 2) if total > 0 else 0.0
        persen_bermasalah = round((bermasalah / total) * 100, 2) if total > 0 else 0.0

        return {
            "status": ResultStatus.SUCCESS,
            "provider": "ELASTICSEARCH_QC_PERCENTAGE",
            "data": {
                "qc_percentage": True,
                "total_sekolah": total,
                "total_bersih": bersih,
                "total_bermasalah": bermasalah,
                "persen_bersih": persen_bersih,
                "persen_bermasalah": persen_bermasalah,
                "wilayah_label": wilayah_label,
            },
            "confidence": 0.95,
            "answer_confident": True,
        }
    except Exception as exc:
        logger.exception("[AGG_QC_PCT] Error: %s", exc)
        return {
            "status": ResultStatus.VALIDATION_ERROR,
            "provider": "SYSTEM_VALIDATION",
            "data": {"message": "Gagal menghitung persentase kualitas data."},
            "confidence": 0,
            "answer_confident": False,
        }


async def execute_multi_aggregation_query(
    es_client: AsyncElasticsearch,
    entities_list: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
 
    results = []
 
    for entities in entities_list:
 
        normalized = normalize_entity_hierarchy(entities)
 
        total = await execute_aggregation_query(
            es_client,
            normalized
        )
 
        if total is None:
            total = 0
 
        results.append({
            "entities": entities,
            "total": total
        })
 
    return results
