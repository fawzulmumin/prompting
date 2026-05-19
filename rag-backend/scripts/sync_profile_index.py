#!/usr/bin/env python3
"""
sync_profile_index.py — Sync idx_sekolah_profile dari idx_sekolah_qc + idx_prompting_agent

ALUR:
  1. Scroll semua dokumen dari idx_sekolah_qc (batch)
  2. mget ke idx_prompting_agent per batch (lookup by npsn)
  3. Merge field QC + field numerik agent
  4. Bulk index ke idx_sekolah_profile (_id = npsn)
  5. Checkpoint per batch untuk resume

PENGGUNAAN:
  python scripts/sync_profile_index.py             # full sync
  python scripts/sync_profile_index.py --full      # sama dengan di atas
  python scripts/sync_profile_index.py --npsn 70001798   # sync 1 sekolah
  python scripts/sync_profile_index.py --dry-run   # print tanpa kirim ke ES
  python scripts/sync_profile_index.py --resume    # lanjut dari checkpoint
  python scripts/sync_profile_index.py --batch-size 200
"""

import os
import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk

# ─────────────────────────────────────────────────────────────────────────────
# Path setup
# ─────────────────────────────────────────────────────────────────────────────
ROOT_PATH = Path(__file__).parent.parent
sys.path.append(str(ROOT_PATH))

import app.config as config  # noqa: F401 — load .env via config.py

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
ES_NODE     = os.getenv("ELASTICSEARCH_NODE")
ES_USER     = os.getenv("ELASTICSEARCH_USERNAME")
ES_PASS     = os.getenv("ELASTICSEARCH_PASSWORD")
QC_INDEX    = os.getenv("ELASTICSEARCH_INDEX",         "idx_sekolah_qc")
AGENT_INDEX = os.getenv("ELASTICSEARCH_AGENT_INDEX",   "idx_prompting_agent")
PROFILE_INDEX = os.getenv("ELASTICSEARCH_PROFILE_INDEX", "idx_sekolah_profile")

MAPPING_PATH    = ROOT_PATH / "data" / "idx_sekolah_profile_mapping.json"
CHECKPOINT_PATH = Path(__file__).parent / ".sync_checkpoint.json"

# Field numerik yang diambil dari idx_prompting_agent
AGENT_FIELDS: List[str] = [
    "siswa_total", "siswa_l", "siswa_p",
    "siswa_baru_l", "siswa_baru_p",
    "siswa_lulus_l", "siswa_lulus_p",
    "siswa_mengulang_l", "siswa_mengulang_p",
    "guru_total", "guru_l", "guru_p",
    "guru_pns", "guru_p3k", "guru_honor",
    "guru_sertifikasi", "guru_s1_keatas",
    "tendik_l", "tendik_p",
    "tipe_jamban", "kecukupan_air", "minum_siswa",
    "sumber_air",
    "toilet_siswa_laki", "toilet_siswa_perempuan",
    "total_komputer",
    "semester_id", "tahun", "nilai_akhir",
    "penyelenggara", "sk_penetapan", "last_update_sekolah",
]

# Field yang diambil dari idx_sekolah_qc (whitelist eksplisit — tidak include content/content_vector)
QC_FIELDS: List[str] = [
    "npsn", "sekolah_id", "nama", "bentuk_pendidikan", "status_sekolah",
    "keaktifan", "alamat_jalan", "desa_kelurahan", "kecamatan", "kabupaten",
    "provinsi", "kode_wilayah", "kode_pos", "wilayah_key",
    "lintang", "bujur", "lokasi",
    "nomor_telepon", "email", "website",
    "status_kepemilikan", "yayasan", "partisipasi_bos",
    "akreditasi", "waktu_penyelenggaraan", "sertifikasi_iso",
    "sk_pendirian_sekolah", "tanggal_sk_pendirian",
    "sk_izin_operasional", "tanggal_sk_izin_operasional",
    "wilayah_terpencil", "wilayah_perbatasan",
    "sumber_listrik", "akses_internet",
    "qc", "qc_score",
]


# ─────────────────────────────────────────────────────────────────────────────
# ES client
# ─────────────────────────────────────────────────────────────────────────────
def get_es_client() -> Elasticsearch:
    return Elasticsearch(
        hosts=[ES_NODE],
        basic_auth=(ES_USER, ES_PASS),
        request_timeout=60,
        max_retries=3,
        retry_on_timeout=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Index management
# ─────────────────────────────────────────────────────────────────────────────
def ensure_index(es: Elasticsearch) -> None:
    if es.indices.exists(index=PROFILE_INDEX):
        logger.info("[SYNC] Index '%s' already exists — skipping creation", PROFILE_INDEX)
        return

    if not MAPPING_PATH.exists():
        raise FileNotFoundError(f"Mapping file not found: {MAPPING_PATH}")

    with open(MAPPING_PATH, encoding="utf-8") as f:
        mapping = json.load(f)

    es.indices.create(index=PROFILE_INDEX, body=mapping)
    logger.info("[SYNC] Created index '%s'", PROFILE_INDEX)


# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint
# ─────────────────────────────────────────────────────────────────────────────
def load_checkpoint() -> Dict[str, Any]:
    if CHECKPOINT_PATH.exists():
        with open(CHECKPOINT_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_checkpoint(scroll_id: str, processed: int) -> None:
    data = {
        "last_scroll_id": scroll_id,
        "processed": processed,
        "last_batch_at": datetime.utcnow().isoformat() + "Z",
    }
    with open(CHECKPOINT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def clear_checkpoint() -> None:
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        logger.info("[SYNC] Checkpoint cleared")


# ─────────────────────────────────────────────────────────────────────────────
# Agent data fetchers (search-based, bukan mget — karena _id di agent bukan NPSN)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_agent_batch(
    es: Elasticsearch,
    npsns: List[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Ambil data numerik terbaru dari idx_prompting_agent per batch NPSN.

    FIX v2: Ganti collapse+size approach dengan search tanpa collapse,
    lalu deduplicate manual per npsn (ambil tahun+semester terbaru).
    collapse+size tidak menjamin return N unique NPSN karena collapse
    bekerja setelah scoring, bukan sebagai pre-filter.
    """
    if not npsns:
        return {}
    try:
        resp = es.search(
            index=AGENT_INDEX,
            body={
                "size": len(npsns) * 5,  # buffer: max 5 semester per sekolah
                "query": {"bool": {"filter": [{"terms": {"npsn": npsns}}]}},
                "sort": [
                    {"tahun": {"order": "desc"}},
                    {"semester_id": {"order": "desc"}},
                ],
                "_source": AGENT_FIELDS + ["npsn"],
            },
        )

        # Deduplicate: untuk setiap npsn, ambil yang pertama (tahun+semester terbaru)
        result: Dict[str, Dict[str, Any]] = {}
        for hit in resp.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            npsn = src.get("npsn")
            if npsn and npsn not in result:
                result[npsn] = {k: v for k, v in src.items() if k in AGENT_FIELDS}

        missing = set(npsns) - set(result.keys())
        if missing:
            logger.debug(
                "[SYNC] %d NPSN tidak ditemukan di agent (sekolah baru/belum ada data): %s...",
                len(missing),
                list(missing)[:3],
            )

        return result

    except Exception as exc:
        logger.warning("[SYNC] fetch_agent_batch error: %s", exc)
        return {}


def fetch_agent_single(es: Elasticsearch, npsn: str) -> Optional[Dict[str, Any]]:
    """Ambil data numerik terbaru dari idx_prompting_agent untuk satu NPSN."""
    try:
        resp = es.search(
            index=AGENT_INDEX,
            body={
                "size": 1,
                "query": {"term": {"npsn": npsn}},
                "sort": [
                    {"tahun": {"order": "desc"}},
                    {"semester_id": {"order": "desc"}},
                ],
                "_source": AGENT_FIELDS,
            },
        )
        hits = resp.get("hits", {}).get("hits", [])
        return hits[0].get("_source", {}) if hits else None
    except Exception as exc:
        logger.warning("[SYNC] fetch_agent_single error untuk %s: %s", npsn, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Document merge
# ─────────────────────────────────────────────────────────────────────────────
def merge_doc(qc_doc: Dict[str, Any], agent_source: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Gabungkan field QC (whitelist) + field numerik dari agent."""
    qc_source = qc_doc.get("_source", {})

    doc: Dict[str, Any] = {}
    for field in QC_FIELDS:
        val = qc_source.get(field)
        if val is not None:
            doc[field] = val

    agent = agent_source or {}
    for field in AGENT_FIELDS:
        val = agent.get(field)
        if val is not None:
            doc[field] = val

    doc["_meta_sync"] = {
        "synced_at": datetime.utcnow().isoformat() + "Z",
        "source": "idx_sekolah_qc+idx_prompting_agent",
    }

    # Hapus key dengan value None agar tidak konflik dengan dynamic: strict
    return {k: v for k, v in doc.items() if v is not None}


# ─────────────────────────────────────────────────────────────────────────────
# Batch processing
# ─────────────────────────────────────────────────────────────────────────────
def process_batch(
    es: Elasticsearch,
    qc_docs: List[Dict[str, Any]],
    dry_run: bool,
) -> int:
    # Bangun lookup npsn → qc_doc
    npsn_to_doc: Dict[str, Dict[str, Any]] = {}
    for doc in qc_docs:
        npsn = doc.get("_source", {}).get("npsn")
        if npsn:
            npsn_to_doc[npsn] = doc

    if not npsn_to_doc:
        return 0

    npsns = list(npsn_to_doc.keys())

    # FIXED: search+terms+collapse bukan mget (karena _id di agent bukan NPSN)
    agent_lookup = fetch_agent_batch(es, npsns)
    logger.info(
        "[SYNC] agent_lookup: %d/%d NPSN ditemukan di agent",
        len(agent_lookup), len(npsns),
    )

    # Merge + bulk
    actions = []
    for npsn, qc_doc in npsn_to_doc.items():
        doc = merge_doc(qc_doc, agent_lookup.get(npsn))

        if dry_run:
            preview = json.dumps({"_id": npsn, **doc}, ensure_ascii=False)
            print(preview[:3000])
            print("---")
            continue

        actions.append({
            "_index": PROFILE_INDEX,
            "_id": npsn,
            "_op_type": "index",
            "_source": doc,
        })

    if dry_run:
        return len(npsn_to_doc)

    if actions:
        success, errors = bulk(es, actions, raise_on_error=False)
        if errors:
            logger.warning("[SYNC] %d bulk errors in batch", len(errors))
        return success

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Single NPSN sync
# ─────────────────────────────────────────────────────────────────────────────
def sync_single(es: Elasticsearch, npsn: str, dry_run: bool) -> None:
    resp = es.search(
        index=QC_INDEX,
        body={"query": {"term": {"npsn": npsn}}, "size": 1},
    )
    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        logger.error("[SYNC] NPSN %s tidak ditemukan di %s", npsn, QC_INDEX)
        return

    agent_source = fetch_agent_single(es, npsn)
    doc = merge_doc(hits[0], agent_source)

    if dry_run:
        print(json.dumps({"_id": npsn, **doc}, indent=2, ensure_ascii=False))
        return

    es.index(index=PROFILE_INDEX, id=npsn, body=doc)
    logger.info("[SYNC] Indexed NPSN %s (agent_data=%s)", npsn, agent_source is not None)


# ─────────────────────────────────────────────────────────────────────────────
# Full sync (scroll)
# ─────────────────────────────────────────────────────────────────────────────
def full_sync(
    es: Elasticsearch,
    batch_size: int,
    dry_run: bool,
    resume: bool,
) -> None:
    checkpoint = load_checkpoint() if resume else {}
    processed = checkpoint.get("processed", 0)

    resp = es.search(
        index=QC_INDEX,
        scroll="5m",
        size=batch_size,
        body={"query": {"match_all": {}}},
    )
    scroll_id: str = resp["_scroll_id"]

    total_raw = resp["hits"]["total"]
    total = total_raw["value"] if isinstance(total_raw, dict) else total_raw
    logger.info("[SYNC] Total dokumen di %s: %d", QC_INDEX, total)

    batch_num = 0
    try:
        while True:
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                break

            batch_num += 1
            synced = process_batch(es, hits, dry_run)
            processed += synced

            if not dry_run:
                save_checkpoint(scroll_id, processed)

            logger.info(
                "[SYNC] Batch %d | +%d | Total: %d / %d",
                batch_num, synced, processed, total,
            )

            resp = es.scroll(scroll_id=scroll_id, scroll="5m")
            scroll_id = resp["_scroll_id"]

    finally:
        try:
            es.clear_scroll(scroll_id=scroll_id)
        except Exception:
            pass

    logger.info("[SYNC] Selesai. Total diproses: %d", processed)
    if not dry_run:
        clear_checkpoint()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync idx_sekolah_profile dari idx_sekolah_qc + idx_prompting_agent"
    )
    parser.add_argument("--full",       action="store_true", help="Full sync (default jika tidak ada flag lain)")
    parser.add_argument("--npsn",       type=str,            help="Sync satu sekolah berdasarkan NPSN")
    parser.add_argument("--dry-run",    action="store_true", help="Print dokumen tanpa kirim ke ES")
    parser.add_argument("--resume",     action="store_true", help="Lanjut dari checkpoint terakhir")
    parser.add_argument("--batch-size", type=int, default=500, metavar="N", help="Ukuran batch scroll (default: 500)")
    args = parser.parse_args()

    es = get_es_client()

    try:
        es.ping()
        logger.info("[SYNC] Elasticsearch terhubung: %s", ES_NODE)
    except Exception as exc:
        logger.error("[SYNC] Tidak bisa terhubung ke Elasticsearch: %s", exc)
        sys.exit(1)

    if not args.dry_run:
        ensure_index(es)

    if args.npsn:
        sync_single(es, args.npsn, dry_run=args.dry_run)
    else:
        full_sync(
            es,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            resume=args.resume,
        )


if __name__ == "__main__":
    main()
