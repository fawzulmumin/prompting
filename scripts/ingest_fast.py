#!/usr/bin/env python3
"""
ingest_fast.py — Ingest 2 fase terpisah untuk memaksimalkan GPU + minimasi waktu tunggu jaringan.
 
MASALAH KONDISI SAAT INI:
  GPU embed (51s) → nganggur nunggu bulk (65s) → GPU embed lagi → ...
  GPU idle 56% dari total waktu karena tunggu jaringan.
 
SOLUSI — Pipeline 2 fase:
  Fase 1 (fase_embed): GPU bekerja terus tanpa interupsi → simpan vector ke file .jsonl
  Fase 2 (fase_upload): Baca file, bulk upload ke ES → tanpa embedding sama sekali
 
ESTIMASI WAKTU:
  Kondisi saat ini : ~5-11 jam
  Dengan 2 fase    : ~2.4 jam total
 
PENGGUNAAN:
  # Fase 1 — jalankan dulu, biarkan GPU kerja penuh
  $env:PROD="production"; python scripts/ingest_fast.py --fase embed
 
  # Fase 2 — setelah fase 1 selesai
  $env:PROD="production"; python scripts/ingest_fast.py --fase upload
 
  # Atau langsung 2 fase sekaligus (sequential)
  $env:PROD="production"; python scripts/ingest_fast.py --fase all
 
  # Skip NPSN yang sudah ada di ES (untuk missing ingest)
  $env:PROD="production"; python scripts/ingest_fast.py --fase all --skip-existing
"""
 
import os
import sys
import csv
import json
import logging
import argparse
import time
from pathlib import Path
from datetime import datetime
from typing import Set, List, Dict, Any, Optional, Iterator
 
import torch
from tqdm import tqdm
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk as es_bulk
 
ROOT_PATH = Path(__file__).parent.parent
sys.path.append(str(ROOT_PATH))
 
import app.config as config  # noqa: F401
 
from scripts.ingest import (
    clean_key,
    build_derived_siswa,
    build_derived_guru,
    _build_content,
    build_document,
    _require_env,
    _safe_int,
    EMBED_DIM,
    CSV_PATH,
)
 
# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
EMBED_BATCH_SIZE  = 512          # 2x dari default → GPU lebih penuh
UPLOAD_BATCH_SIZE = 1000          # kecil agar bulk < 20 detik, hindari connection reset
VECTOR_FILE       = ROOT_PATH / "scripts" / "vectors_cache.jsonl"
EMBED_CHECKPOINT  = ROOT_PATH / "scripts" / "checkpoint_embed.json"
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
 
 
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
 
def _load_embed_checkpoint() -> int:
    """Return baris terakhir yang sudah di-embed."""
    if EMBED_CHECKPOINT.exists():
        with open(EMBED_CHECKPOINT) as f:
            cp = json.load(f)
            row = cp.get("last_row", 0)
            logger.info("📍 Resume embed dari baris %s", f"{row:,}")
            return row
    return 0
 
 
def _save_embed_checkpoint(row: int) -> None:
    with open(EMBED_CHECKPOINT, "w") as f:
        json.dump({"last_row": row}, f)
 
 
def fetch_existing_npsns(es: Elasticsearch, index_name: str) -> Set[str]:
    """Ambil semua NPSN yang sudah ada di ES via composite agg."""
    logger.info("📥 Mengambil NPSN yang sudah ada di ES...")
    existing: Set[str] = set()
    after_key = None
 
    while True:
        body: Dict = {
            "size": 0,
            "aggs": {
                "npsns": {
                    "composite": {
                        "size": 10000,
                        "sources": [{"npsn": {"terms": {"field": "npsn"}}}],
                    }
                }
            },
        }
        if after_key:
            body["aggs"]["npsns"]["composite"]["after"] = after_key
 
        res       = es.options(request_timeout=60).search(index=index_name, body=body)
        agg       = res.get("aggregations", {}).get("npsns", {})
        buckets   = agg.get("buckets", [])
        after_key = agg.get("after_key")
 
        for b in buckets:
            npsn = b.get("key", {}).get("npsn")
            if npsn:
                existing.add(str(npsn))
 
        if not after_key or not buckets:
            break
 
    logger.info("✅ NPSN sudah di ES: %s", f"{len(existing):,}")
    return existing
 
 
# ---------------------------------------------------------------------------
# FASE 1 — Embedding → simpan ke file .jsonl
# ---------------------------------------------------------------------------
 
def fase_embed(skip_npsns: Set[str] = None) -> None:
    """
    Baca CSV, hitung vector untuk setiap baris yang belum ada,
    simpan hasilnya ke VECTOR_FILE sebagai .jsonl.
 
    Format tiap baris di file:
      {"npsn": "...", "content": "...", "doc": {...}, "vector": [...384 floats...]}
    """
    if skip_npsns is None:
        skip_npsns = set()
 
    # Load model
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    logger.info("🧠 Loading embedding model on %s", device)
    from sentence_transformers import SentenceTransformer
    model_path = ROOT_PATH / "models" / "all-MiniLM-L6-v2"
    model_id   = str(model_path) if model_path.exists() else "all-MiniLM-L6-v2"
    embedder   = SentenceTransformer(model_id, device=device)
 
    # Resume dari checkpoint
    resume_row = _load_embed_checkpoint()
 
    # Hitung total yang perlu di-embed
    logger.info("🔍 Menghitung baris yang perlu di-embed...")
    total_todo = 0
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, raw_row in enumerate(reader):
            if i < resume_row:
                continue
            row  = {clean_key(k): v for k, v in raw_row.items()}
            npsn = str(row.get("npsn", "")).strip()
            if npsn and npsn.lower() not in {"null", "none", "0", ""} \
                    and npsn not in skip_npsns:
                total_todo += 1
 
    logger.info("📊 Perlu di-embed: %s baris", f"{total_todo:,}")
    logger.info("=" * 70)
    logger.info("FASE 1 — EMBEDDING (GPU bekerja penuh, tanpa tunggu jaringan)")
    logger.info("=" * 70)
 
    start      = datetime.now()
    batch_rows = []   # list of (row, content, npsn)
    current_row = 0
    done_count  = 0
 
    # Buka file output dalam mode append jika resume
    open_mode = "a" if resume_row > 0 and VECTOR_FILE.exists() else "w"
 
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f_csv, \
         open(VECTOR_FILE, open_mode, encoding="utf-8") as f_out, \
         tqdm(total=total_todo, desc="Embedding") as pbar:
 
        reader = csv.DictReader(f_csv)
 
        for i, raw_row in enumerate(reader):
            current_row = i
 
            # Skip baris yang sudah diproses sebelumnya
            if i < resume_row:
                continue
 
            row  = {clean_key(k): v for k, v in raw_row.items()}
            npsn = str(row.get("npsn", "")).strip()
 
            if not npsn or npsn.lower() in {"null", "none", "0", ""} \
                    or npsn in skip_npsns:
                continue
 
            # Build content & doc (tanpa vector dulu)
            derived_siswa = build_derived_siswa(row)
            derived_guru  = build_derived_guru(row)
            content       = _build_content(row, derived_siswa, derived_guru)
 
            batch_rows.append((row, content, npsn))
 
            if len(batch_rows) >= EMBED_BATCH_SIZE:
                # Embed semua dalam batch
                texts   = [c for _, c, _ in batch_rows]
                vectors = embedder.encode(
                    texts,
                    batch_size=EMBED_BATCH_SIZE,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                ).tolist()
 
                # Tulis ke file
                for (row_data, content_str, npsn_val), vec in zip(batch_rows, vectors):
                    if not vec or len(vec) != EMBED_DIM:
                        continue
                    record = {
                        "npsn":    npsn_val,
                        "content": content_str,
                        "row":     {
                            k: v for k, v in row_data.items()
                            if v and str(v).strip() not in {"", "null", "none", "-"}
                        },
                        "vector":  vec,
                    }
                    f_out.write(json.dumps(record, ensure_ascii=False) + "\n")
 
                done_count += len(batch_rows)
                pbar.update(len(batch_rows))
                batch_rows.clear()
 
                # Simpan checkpoint setiap batch
                _save_embed_checkpoint(current_row)
 
                # Flush ke disk secara periodik
                if done_count % (EMBED_BATCH_SIZE * 10) == 0:
                    f_out.flush()
 
        # Sisa batch
        if batch_rows:
            texts   = [c for _, c, _ in batch_rows]
            vectors = embedder.encode(
                texts,
                normalize_embeddings=True,
                show_progress_bar=False,
            ).tolist()
            for (row_data, content_str, npsn_val), vec in zip(batch_rows, vectors):
                if not vec or len(vec) != EMBED_DIM:
                    continue
                record = {
                    "npsn":    npsn_val,
                    "content": content_str,
                    "row":     {k: v for k, v in row_data.items()
                                if v and str(v).strip() not in {"", "null", "none", "-"}},
                    "vector":  vec,
                }
                f_out.write(json.dumps(record, ensure_ascii=False) + "\n")
            done_count += len(batch_rows)
            pbar.update(len(batch_rows))
 
    elapsed = (datetime.now() - start).total_seconds()
    file_mb = VECTOR_FILE.stat().st_size / 1024 / 1024
 
    # Hapus checkpoint embed — fase 1 selesai
    EMBED_CHECKPOINT.unlink(missing_ok=True)
 
    logger.info("=" * 70)
    logger.info("FASE 1 SELESAI")
    logger.info("  Vectors tersimpan : %s dokumen", f"{done_count:,}")
    logger.info("  File output       : %s (%.1f MB)", VECTOR_FILE, file_mb)
    logger.info("  Durasi            : %.1f menit", elapsed / 60)
    logger.info("=" * 70)
    logger.info("▶️  Jalankan fase upload: python scripts/ingest_fast.py --fase upload")
 
 
# ---------------------------------------------------------------------------
# FASE 2 — Upload dari file ke ES (tanpa embedding)
# ---------------------------------------------------------------------------
 
def _read_vector_file(skip_npsns: Set[str]) -> Iterator[Dict]:
    """Generator: baca VECTOR_FILE baris per baris, skip yang sudah ada."""
    if not VECTOR_FILE.exists():
        raise FileNotFoundError(
            f"Vector file tidak ditemukan: {VECTOR_FILE}\n"
            "Jalankan fase embed dulu: python scripts/ingest_fast.py --fase embed"
        )
 
    with open(VECTOR_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                npsn   = record.get("npsn", "")
                if npsn and npsn not in skip_npsns:
                    yield record
            except json.JSONDecodeError:
                continue
 
 
def fase_upload(
    es: Elasticsearch,
    index_name: str,
    skip_npsns: Set[str],
) -> None:
    """
    Baca VECTOR_FILE, build dokumen ES, bulk upload.
    Tidak ada embedding — murni network I/O ke ES.
    """
    if not VECTOR_FILE.exists():
        raise FileNotFoundError(
            f"Vector file tidak ditemukan: {VECTOR_FILE}\n"
            "Jalankan fase embed dulu."
        )
 
    # Hitung total baris di file untuk progress bar
    total_lines = sum(1 for _ in open(VECTOR_FILE, encoding="utf-8") if _.strip())
    already_done = len(skip_npsns)
    todo = max(total_lines - already_done, 0)
 
    logger.info("=" * 70)
    logger.info("FASE 2 — UPLOAD ke ES (tanpa embedding)")
    logger.info("  Total di file   : %s", f"{total_lines:,}")
    logger.info("  Sudah di ES     : %s", f"{already_done:,}")
    logger.info("  Perlu diupload  : %s", f"{todo:,}")
    logger.info("=" * 70)
 
    start   = datetime.now()
    success = failed = 0
    batch: List[Dict] = []
    done_npsns: Set[str] = set()
 
    with tqdm(total=todo, desc="Uploading") as pbar:
        for record in _read_vector_file(skip_npsns):
            npsn    = record["npsn"]
            content = record["content"]
            vector  = record["vector"]
            row     = record["row"]
 
            doc = build_document(row, content, vector, index_name)
            if doc:
                batch.append(doc)
                done_npsns.add(npsn)
 
            if len(batch) >= UPLOAD_BATCH_SIZE:
                r = _bulk_with_retry(es, batch)
                success += r["success"]
                failed  += r["failed"]
                pbar.update(len(batch))
                batch.clear()
 
        # Sisa batch
        if batch:
            r = _bulk_with_retry(es, batch)
            success += r["success"]
            failed  += r["failed"]
            pbar.update(len(batch))
 
    # Refresh index
    es.indices.refresh(index=index_name)
    count   = es.count(index=index_name)["count"]
    elapsed = (datetime.now() - start).total_seconds()
 
    logger.info("=" * 70)
    logger.info("FASE 2 SELESAI")
    logger.info("  ES doc count total : %s", f"{count:,}")
    logger.info("  Berhasil diupload  : %s", f"{success:,}")
    logger.info("  Gagal              : %s", f"{failed:,}")
    logger.info("  Durasi             : %.1f menit", elapsed / 60)
    logger.info("=" * 70)
 
    if failed == 0:
        # Hapus vector file jika semua berhasil
        logger.info("🧹 Menghapus vector cache file...")
        VECTOR_FILE.unlink(missing_ok=True)
        logger.info("🎉 Index lengkap! Total: %s dokumen.", f"{count:,}")
    else:
        logger.warning(
            "⚠️  %s dokumen masih gagal. Jalankan ulang --fase upload untuk retry.",
            f"{failed:,}",
        )
 
 
def _bulk_with_retry(es: Elasticsearch, actions: List[Dict], max_retry: int = 5) -> Dict[str, int]:
    """Bulk upload dengan retry dan backoff."""
    for attempt in range(max_retry):
        try:
            success, errors = es_bulk(
                es.options(request_timeout=90),
                actions,
                raise_on_error=False,
                refresh=False,
                chunk_size=1000,   # sangat kecil → bulk selesai < 10 detik
            )
 
            if errors:
                failed_ids = {
                    (e.get("index") or e.get("create") or {}).get("_id")
                    for e in errors
                    if (e.get("index") or e.get("create") or {}).get("_id")
                }
                retry = [a for a in actions if a.get("_id") in failed_ids]
                if retry:
                    logger.debug("  Retrying %d docs...", len(retry))
                    es_bulk(
                        es.options(request_timeout=90),
                        retry,
                        raise_on_error=False,
                        refresh=False,
                    )
 
            return {"success": success, "failed": len(errors)}
 
        except Exception as exc:
            wait = 2 ** attempt   # exponential backoff: 1s, 2s, 4s, 8s, 16s
            logger.warning("  Attempt %d/%d gagal (%s), retry dalam %ds...",
                           attempt + 1, max_retry, exc, wait)
            time.sleep(wait)
 
    return {"success": 0, "failed": len(actions)}
 
 
# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
 
def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest cepat 2 fase: embed → upload")
    parser.add_argument(
        "--fase",
        choices=["embed", "upload", "all"],
        required=True,
        help=(
            "embed  = Fase 1: hitung vectors, simpan ke file (GPU intensif)\n"
            "upload = Fase 2: upload dari file ke ES (network intensif)\n"
            "all    = Jalankan keduanya secara berurutan"
        ),
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip NPSN yang sudah ada di ES (untuk mode missing ingest)",
    )
    args = parser.parse_args()
 
    env = _require_env("ELASTICSEARCH_NODE", "ELASTICSEARCH_USERNAME", "ELASTICSEARCH_PASSWORD")
    index_name = os.getenv("ELASTICSEARCH_INDEX", "idx_prompting_agent")
 
    es = Elasticsearch(
        hosts=[env["ELASTICSEARCH_NODE"]],
        basic_auth=(env["ELASTICSEARCH_USERNAME"], env["ELASTICSEARCH_PASSWORD"]),
        request_timeout=300,
        http_compress=True,
        connections_per_node=3,
        retry_on_timeout=True,
        max_retries=5,
    )
 
    # Ambil existing NPSN jika diperlukan
    skip_npsns: Set[str] = set()
    if args.skip_existing:
        skip_npsns = fetch_existing_npsns(es, index_name)
 
    if args.fase in ("embed", "all"):
        fase_embed(skip_npsns=skip_npsns)
 
    if args.fase in ("upload", "all"):
        # Untuk upload, selalu ambil existing terbaru dari ES
        existing = fetch_existing_npsns(es, index_name)
        fase_upload(es, index_name, skip_npsns=existing)
 
 
if __name__ == "__main__":
    try:
        main()
    except (EnvironmentError, FileNotFoundError) as exc:
        logger.error("❌ %s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("⛔ Dihentikan. Jalankan ulang untuk lanjut dari checkpoint.")
        sys.exit(130)
