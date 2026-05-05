# location_service.py

import os
import logging
import json
from typing import Dict, List
 
from elasticsearch import AsyncElasticsearch
 
logger = logging.getLogger(__name__)
 
# =========================================================
# CONSTANTS
# =========================================================
 
LOCATION_FIELDS = (
    "provinsi",
    "kabupaten",
    "kecamatan",
    "desa_kelurahan",
)
 
PREFIXES = (
    "prov. ", "kab. ", "kabupaten ", "kota ",
    "kec. ", "kecamatan ", "desa ", "kelurahan ",
)

_FIELD_TERM_SIZE = {
    "provinsi": 50,
    "kabupaten": 600,
    "kecamatan": 8000,
    "desa_kelurahan": 90000,
}
 
CACHE_FILE = "cache/location_hierarchy_cache.json"
 
# =========================================================
# GLOBAL CACHE
#
# LOCATION_INDEX:
#   { level: { nama_bersih: raw_value } }
#
# LOCATION_HIERARCHY:
#   { level: { nama_lower: [ {kecamatan, kabupaten, provinsi}, ... ] } }
#   Nilai adalah LIST agar nama yang sama di banyak wilayah tersimpan semua.
# =========================================================
 
LOCATION_INDEX: Dict[str, Dict[str, str]] = {}
LOCATION_HIERARCHY: Dict[str, Dict[str, List[Dict]]] = {}
 
# Tetap export untuk backward compat
PROVINCE_LOOKUP: Dict[str, str] = {}
KABUPATEN_LOOKUP: Dict[str, str] = {}
KECAMATAN_LOOKUP: Dict[str, str] = {}
DESA_LOOKUP: Dict[str, str] = {}
 
 
# =========================================================
# LOAD LOCATION INDEX (nama → raw value, untuk fallback detect)
# =========================================================
 
async def load_location_mapping_from_es(
    es_client: AsyncElasticsearch,
    field: str,
) -> Dict[str, str]:
 
    index = os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")
    term_size = _FIELD_TERM_SIZE.get(field, 20000)

    try:
        res = await es_client.search(
            index=index,
            size=0,
            body={"aggs": {"list": {"terms": {"field": field, "size": term_size}}}},
            request_timeout=30,
        )
    except Exception as e:
        logger.error("[LOCATION] Failed loading field=%s: %s", field, e)
        return {}

    buckets = res.get("aggregations", {}).get("list", {}).get("buckets", [])
    if len(buckets) >= term_size:
        logger.warning(
            "[LOCATION] field=%s hit term size limit (%d) — data mungkin terpotong",
            field, term_size,
        )
    lookup: Dict[str, str] = {}
 
    for bucket in buckets:
        raw = bucket.get("key")
        if not raw:
            continue
        norm = raw.lower().strip()
        lookup[norm] = raw
        lookup[norm.replace(" ", "")] = raw
        for prefix in PREFIXES:
            if norm.startswith(prefix):
                stripped = norm[len(prefix):].strip()
                lookup[stripped] = raw
                lookup[stripped.replace(" ", "")] = raw
 
    logger.info("[LOCATION] Loaded %d mappings for %s", len(lookup), field)
    return lookup
 
 
# =========================================================
# LOAD LOCATION HIERARCHY
# Menggunakan composite aggregation — jauh lebih cepat dari
# async_scan (menit vs jam untuk 560K+ dokumen).
# =========================================================
 
async def load_location_hierarchy(es_client: AsyncElasticsearch):
    """
    Build LOCATION_HIERARCHY via ES composite aggregation.
    Composite agg memproses kombinasi unik di sisi ES server,
    bukan iterasi dokumen satu per satu.
    Estimasi waktu: 2-5 menit (vs 4+ jam dengan async_scan).
    """
    global LOCATION_HIERARCHY
 
    index = os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")
 
    LOCATION_HIERARCHY = {
        "desa_kelurahan": {},
        "kecamatan":      {},
        "kabupaten":      {},
    }
 
    logger.info("[LOCATION] Building hierarchy via composite aggregation...")
 
    after_key       = None
    page            = 0
    total_combos    = 0
 
    while True:
        agg_body: Dict = {
            "size": 0,
            "aggs": {
                "combo": {
                    "composite": {
                        "size": 1000,
                        "sources": [
                            {"desa": {"terms": {"field": "desa_kelurahan", "missing_bucket": True}}},
                            {"kec":  {"terms": {"field": "kecamatan",      "missing_bucket": True}}},
                            {"kab":  {"terms": {"field": "kabupaten",      "missing_bucket": True}}},
                            {"prov": {"terms": {"field": "provinsi",       "missing_bucket": True}}},
                        ],
                    }
                }
            },
        }
 
        if after_key:
            agg_body["aggs"]["combo"]["composite"]["after"] = after_key
 
        try:
            res = await es_client.search(
                index=index,
                body=agg_body,
                request_timeout=60,
            )
        except Exception as e:
            logger.error("[LOCATION] Composite agg failed page=%d: %s", page, e)
            break
 
        agg_result = res.get("aggregations", {}).get("combo", {})
        buckets    = agg_result.get("buckets", [])
        after_key  = agg_result.get("after_key")
 
        if not buckets:
            break
 
        for bucket in buckets:
            kv   = bucket.get("key", {})
            desa = kv.get("desa") or ""
            kec  = kv.get("kec")  or ""
            kab  = kv.get("kab")  or ""
            prov = kv.get("prov") or ""
 
            # ── Desa ───────────────────────────────────────────
            if desa:
                dkey     = desa.lower()
                entry    = {"kecamatan": kec, "kabupaten": kab, "provinsi": prov}
                existing = LOCATION_HIERARCHY["desa_kelurahan"].get(dkey, [])
                combo    = f"{kec}|{kab}"
                if not any(
                    f"{e.get('kecamatan')}|{e.get('kabupaten')}" == combo
                    for e in existing
                ):
                    existing.append(entry)
                LOCATION_HIERARCHY["desa_kelurahan"][dkey] = existing
 
            # ── Kecamatan ──────────────────────────────────────
            if kec:
                kkey     = kec.lower()
                entry    = {"kabupaten": kab, "provinsi": prov}
                existing = LOCATION_HIERARCHY["kecamatan"].get(kkey, [])
                combo    = f"{kab}|{prov}"
                if not any(
                    f"{e.get('kabupaten')}|{e.get('provinsi')}" == combo
                    for e in existing
                ):
                    existing.append(entry)
                LOCATION_HIERARCHY["kecamatan"][kkey] = existing
 
            # ── Kabupaten ──────────────────────────────────────
            if kab:
                kabkey   = kab.lower()
                entry    = {"provinsi": prov}
                existing = LOCATION_HIERARCHY["kabupaten"].get(kabkey, [])
                if not any(e.get("provinsi") == prov for e in existing):
                    existing.append(entry)
                LOCATION_HIERARCHY["kabupaten"][kabkey] = existing
 
            total_combos += 1
 
        page += 1
        if page % 10 == 0:
            logger.info(
                "[LOCATION] Hierarchy progress: page=%d combos=%d desa=%d",
                page, total_combos,
                len(LOCATION_HIERARCHY["desa_kelurahan"]),
            )
 
        if not after_key:
            break
 
    logger.info(
        "[LOCATION] Hierarchy built: desa=%d kec=%d kab=%d (from %d combos)",
        len(LOCATION_HIERARCHY["desa_kelurahan"]),
        len(LOCATION_HIERARCHY["kecamatan"]),
        len(LOCATION_HIERARCHY["kabupaten"]),
        total_combos,
    )
 
 
# =========================================================
# INITIALIZE — load cache ATAU rebuild dari ES + simpan cache
# =========================================================
 
async def initialize_location_index(es_client: AsyncElasticsearch):
    """
    1. Coba load dari cache file (instan).
    2. Jika tidak ada / corrupt → rebuild dari ES → simpan cache baru.
    """
    global LOCATION_INDEX
    global LOCATION_HIERARCHY
 
    # ── Coba load dari cache ──────────────────────────────────────
    if os.path.exists(CACHE_FILE):
        try:
            logger.warning("[LOCATION] Loading from cache file...")
 
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
 
            if not cached.get("index"):
                raise ValueError("Cache index empty")
 
            LOCATION_INDEX.clear()
            LOCATION_INDEX.update(cached.get("index", {}))
 
            raw_hier = cached.get("hierarchy", {})
            LOCATION_HIERARCHY.clear()
            for level, entries in raw_hier.items():
                LOCATION_HIERARCHY[level] = {}
                for key, val in entries.items():
                    if isinstance(val, list):
                        LOCATION_HIERARCHY[level][key] = val
                    elif isinstance(val, dict):
                        # Format lama: wrap ke list
                        LOCATION_HIERARCHY[level][key] = [val]
                    else:
                        LOCATION_HIERARCHY[level][key] = []
 
            logger.warning(
                "[LOCATION] Cache loaded OK. index=%s hierarchy=%s",
                {k: len(v) for k, v in LOCATION_INDEX.items()},
                {k: len(v) for k, v in LOCATION_HIERARCHY.items()},
            )
            return  # sukses, tidak perlu rebuild
 
        except Exception as e:
            logger.error("[LOCATION] Cache invalid, will rebuild: %s", e)
            # Lanjut ke rebuild di bawah
 
    # ── Rebuild dari ES (cache tidak ada atau corrupt) ────────────
    logger.warning(
        "[LOCATION] No valid cache found. Rebuilding from ES "
        "(estimasi 2-5 menit)..."
    )
 
    # Build LOCATION_INDEX
    for field in LOCATION_FIELDS:
        lookup = await load_location_mapping_from_es(es_client, field)
        LOCATION_INDEX[field] = lookup
        if not lookup:
            logger.error("[LOCATION] CRITICAL: field %s returned empty", field)
 
    # Build LOCATION_HIERARCHY (composite agg, jauh lebih cepat)
    await load_location_hierarchy(es_client)
 
    # ── Simpan cache baru ─────────────────────────────────────────
    try:
        cache_data = {
            "index":     LOCATION_INDEX,
            "hierarchy": LOCATION_HIERARCHY,
        }
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False)
 
        size_mb = os.path.getsize(CACHE_FILE) / 1024 / 1024
        logger.warning(
            "[LOCATION] Cache saved to '%s' (%.1f MB). "
            "Restart berikutnya akan load instan.",
            CACHE_FILE, size_mb,
        )
    except Exception as e:
        logger.error("[LOCATION] Failed to save cache: %s", e)
 
    logger.info(
        "[LOCATION] Init complete. index=%s hierarchy=%s",
        {k: len(v) for k, v in LOCATION_INDEX.items()},
        {k: len(v) for k, v in LOCATION_HIERARCHY.items()},
    )
 
 
# =========================================================
# ENRICH HIERARCHY
# =========================================================
 
def enrich_hierarchy_from_index(entities: dict) -> dict:
    """Enrich entities dengan hierarki wilayah dari LOCATION_HIERARCHY."""
    if not entities:
        return entities
 
    result = dict(entities)
 
    if "desa_kelurahan" in result:
        key     = result["desa_kelurahan"].lower()
        entries = LOCATION_HIERARCHY.get("desa_kelurahan", {}).get(key, [])
        if entries:
            if len(entries) > 1:
                # Ambiguous: desa name exists in multiple kecamatan/kabupaten.
                # Flag it and store all candidates; still fill hierarchy from
                # entries[0] as a display fallback so callers don't break.
                result["_desa_ambiguous"] = True
                result["_desa_candidates"] = entries
            p = entries[0]
            result.setdefault("kecamatan", p.get("kecamatan"))
            result.setdefault("kabupaten", p.get("kabupaten"))
            result.setdefault("provinsi",  p.get("provinsi"))
 
    elif "kecamatan" in result:
        key     = result["kecamatan"].lower()
        entries = LOCATION_HIERARCHY.get("kecamatan", {}).get(key, [])
        if entries:
            p = entries[0]
            result.setdefault("kabupaten", p.get("kabupaten"))
            result.setdefault("provinsi",  p.get("provinsi"))
 
    elif "kabupaten" in result:
        key     = result["kabupaten"].lower()
        entries = LOCATION_HIERARCHY.get("kabupaten", {}).get(key, [])
        if entries:
            p = entries[0]
            result.setdefault("provinsi", p.get("provinsi"))
 
    return result
