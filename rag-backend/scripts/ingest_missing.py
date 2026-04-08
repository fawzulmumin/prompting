#!/usr/bin/env python3
"""
ingest_missing.py — Ingest HANYA dokumen yang belum ada di index ES.
 
Strategi:
  1. Ambil semua NPSN yang sudah ada di ES via scroll/composite agg (cepat).
  2. Scan CSV, skip baris yang NPSN-nya sudah ada.
  3. Ingest hanya baris yang hilang.
 
Kapan dipakai:
  - Setelah ingest utama selesai tapi ada dokumen gagal (misal: 95K failed).
  - Tidak perlu reset index — data yang sudah ada tetap aman.
 
Jalankan:
  $env:PROD="production"; python scripts/ingest_missing.py
"""
 
import os
import sys
import csv
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import Set, List, Dict, Any, Optional
 
import torch
from tqdm import tqdm
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
 
ROOT_PATH = Path(__file__).parent.parent
sys.path.append(str(ROOT_PATH))
 
import app.config as config  # noqa: F401
 
# ---------------------------------------------------------------------------
# Re-use semua helper dari ingest.py
# ---------------------------------------------------------------------------
from scripts.ingest import (
    clean_key,
    build_derived_siswa,
    build_derived_guru,
    _build_content,
    build_document,
    _require_env,
    EMBED_DIM,
    EMBED_BATCH_SIZE,
    CSV_PATH,
    INT_FIELDS,
    FLOAT_FIELDS,
    IDENTITY_FIELDS,
)
 
# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BATCH_SIZE      = 500     # kecil agar tidak kena connection reset
MISSING_CHECKPOINT = Path(__file__).parent / "checkpoint_missing.json"
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
 
 
# ---------------------------------------------------------------------------
# Step 1: Ambil semua NPSN yang sudah ada di ES
# ---------------------------------------------------------------------------
 
def fetch_existing_npsns(es: Elasticsearch, index_name: str) -> Set[str]:
    """
    Gunakan composite aggregation untuk ambil semua NPSN unik di index.
    Lebih efisien dari scroll untuk data besar.
    """
    logger.info("📥 Mengambil NPSN yang sudah ada di ES...")
    existing: Set[str] = set()
    after_key = None
    page = 0
 
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
 
        try:
            res = es.options(request_timeout=60).search(index=index_name, body=body)
        except Exception as exc:
            logger.error("Gagal fetch NPSN page=%d: %s", page, exc)
            break
 
        agg     = res.get("aggregations", {}).get("npsns", {})
        buckets = agg.get("buckets", [])
        after_key = agg.get("after_key")
 
        for b in buckets:
            npsn = b.get("key", {}).get("npsn")
            if npsn:
                existing.add(str(npsn))
 
        page += 1
        if page % 5 == 0:
            logger.info("  ...fetch page %d, total NPSN sejauh ini: %s", page, f"{len(existing):,}")
 
        if not after_key or not buckets:
            break
 
    logger.info("✅ Total NPSN sudah di ES: %s", f"{len(existing):,}")
    return existing
 
 
# ---------------------------------------------------------------------------
# Step 2: Hitung dulu berapa yang missing (dry-run cepat)
# ---------------------------------------------------------------------------
 
def count_missing(existing_npsns: Set[str]) -> int:
    missing = 0
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            row = {clean_key(k): v for k, v in raw_row.items()}
            npsn = str(row.get("npsn", "")).strip()
            if npsn and npsn.lower() not in {"null", "none", "0", ""} \
                    and npsn not in existing_npsns:
                missing += 1
    return missing
 
 
# ---------------------------------------------------------------------------
# Checkpoint untuk missing ingest
# ---------------------------------------------------------------------------
 
def _load_missing_checkpoint() -> Set[str]:
    """Load set NPSN yang sudah berhasil di-ingest di run sebelumnya."""
    if MISSING_CHECKPOINT.exists():
        with open(MISSING_CHECKPOINT) as f:
            data = json.load(f)
            done = set(data.get("done_npsns", []))
            logger.info("📍 Resume: %s NPSN sudah selesai dari run sebelumnya.", f"{len(done):,}")
            return done
    return set()
 
 
def _save_missing_checkpoint(done_npsns: Set[str]) -> None:
    with open(MISSING_CHECKPOINT, "w") as f:
        json.dump({"done_npsns": list(done_npsns)}, f)
 
 
# ---------------------------------------------------------------------------
# Ingestor Missing
# ---------------------------------------------------------------------------
 
class MissingIngestor:
    def __init__(self):
        env = _require_env(
            "ELASTICSEARCH_NODE",
            "ELASTICSEARCH_USERNAME",
            "ELASTICSEARCH_PASSWORD",
        )
        self.es = Elasticsearch(
            hosts=[env["ELASTICSEARCH_NODE"]],
            basic_auth=(env["ELASTICSEARCH_USERNAME"], env["ELASTICSEARCH_PASSWORD"]),
            request_timeout=300,
            http_compress=True,
            connections_per_node=3,
            retry_on_timeout=True,
            max_retries=5,
        )
        self.index_name = os.getenv("ELASTICSEARCH_INDEX", "idx_prompting_agent")
        self.embedder   = None
        self.start_time: Optional[datetime] = None
 
    def _ensure_embedder(self):
        if self.embedder is None:
            from sentence_transformers import SentenceTransformer
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            logger.info("🧠 Loading embedding model on %s", device)
            model_path = ROOT_PATH / "models" / "all-MiniLM-L6-v2"
            model_id   = str(model_path) if model_path.exists() else "all-MiniLM-L6-v2"
            self.embedder = SentenceTransformer(model_id, device=device)
 
    def _batch_embed(self, texts: List[str]) -> List[List[float]]:
        self._ensure_embedder()
        return self.embedder.encode(
            texts,
            batch_size=EMBED_BATCH_SIZE,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).tolist()
 
    def _process_batch(self, batch: List[tuple]) -> Dict[str, int]:
        texts   = [c for _, c in batch]
        vectors = self._batch_embed(texts)
 
        actions: List[Dict] = []
        for (row, content), vec in zip(batch, vectors):
            doc = build_document(row, content, vec, self.index_name)
            if doc:
                actions.append(doc)
 
        if not actions:
            return {"success": 0, "failed": len(batch)}
 
        for attempt in range(5):   # lebih banyak retry untuk missing ingest
            try:
                success, errors = bulk(
                    self.es.options(request_timeout=120),
                    actions,
                    raise_on_error=False,
                    refresh=False,
                    chunk_size=200,   # chunk kecil agar tidak timeout
                )
 
                if errors:
                    failed_ids = set()
                    for err in errors:
                        err_info = err.get("index") or err.get("create") or {}
                        if err_info.get("_id"):
                            failed_ids.add(err_info["_id"])
                            logger.debug(
                                "  FAILED id=%s reason=%s",
                                err_info.get("_id"),
                                err_info.get("error", {}).get("reason", "unknown"),
                            )
 
                    retry_actions = [a for a in actions if a.get("_id") in failed_ids]
                    if retry_actions:
                        logger.info("  Retrying %d dokumen...", len(retry_actions))
                        bulk(
                            self.es.options(request_timeout=120),
                            retry_actions,
                            raise_on_error=False,
                            refresh=False,
                        )
 
                return {"success": success, "failed": len(errors)}
 
            except Exception as exc:
                logger.warning("  Attempt %d/5 gagal: %s", attempt + 1, exc)
                if attempt == 4:
                    return {"success": 0, "failed": len(actions)}
 
        return {"success": 0, "failed": len(actions)}
 
    def run(self) -> None:
        if not CSV_PATH.exists():
            raise FileNotFoundError(f"CSV tidak ditemukan: {CSV_PATH}")
 
        # --- Ambil NPSN yang sudah ada di ES ---
        existing_npsns   = fetch_existing_npsns(self.es, self.index_name)
        # --- Load checkpoint run sebelumnya (jika ada) ---
        done_this_run    = _load_missing_checkpoint()
        # Gabungkan: skip yang sudah di ES + yang sudah dicoba di run ini
        skip_npsns       = existing_npsns | done_this_run
 
        # --- Hitung missing ---
        logger.info("🔍 Menghitung dokumen yang hilang (dry-run)...")
        missing_count = count_missing(skip_npsns)
        logger.info("📊 Dokumen hilang: %s dari %s total ES", f"{missing_count:,}", f"{len(existing_npsns):,}")
 
        if missing_count == 0:
            logger.info("✅ Tidak ada dokumen yang hilang. Index sudah lengkap!")
            MISSING_CHECKPOINT.unlink(missing_ok=True)
            return
 
        logger.info("=" * 70)
        logger.info("STARTING MISSING INGEST | target: %s dokumen", f"{missing_count:,}")
        logger.info("=" * 70)
 
        self.start_time = datetime.now()
        success = failed = 0
        batch: List[tuple] = []
        done_batch_npsns: Set[str] = set()
 
        with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
 
            with tqdm(total=missing_count, desc="Missing ingest") as pbar:
                for raw_row in reader:
                    row  = {clean_key(k): v for k, v in raw_row.items()}
                    npsn = str(row.get("npsn", "")).strip()
 
                    # Skip yang sudah ada
                    if not npsn or npsn.lower() in {"null", "none", "0", ""} \
                            or npsn in skip_npsns:
                        continue
 
                    # Build content
                    derived_siswa = build_derived_siswa(row)
                    derived_guru  = build_derived_guru(row)
                    content       = _build_content(row, derived_siswa, derived_guru)
 
                    batch.append((row, content))
                    done_batch_npsns.add(npsn)
 
                    if len(batch) >= BATCH_SIZE:
                        r = self._process_batch(batch)
                        success += r["success"]
                        failed  += r["failed"]
                        pbar.update(len(batch))
 
                        # Tandai sebagai done agar bisa resume
                        skip_npsns.update(done_batch_npsns)
                        done_this_run.update(done_batch_npsns)
                        _save_missing_checkpoint(done_this_run)
 
                        batch.clear()
                        done_batch_npsns.clear()
 
                        if r["failed"] > 0:
                            logger.warning("  %d gagal di batch ini.", r["failed"])
 
        # Sisa batch
        if batch:
            r = self._process_batch(batch)
            success += r["success"]
            failed  += r["failed"]
 
        # Refresh & cleanup
        self.es.indices.refresh(index=self.index_name)
        count   = self.es.count(index=self.index_name)["count"]
        elapsed = (datetime.now() - self.start_time).total_seconds()
 
        # Hapus checkpoint jika semua berhasil
        if failed == 0:
            MISSING_CHECKPOINT.unlink(missing_ok=True)
            logger.info("✅ Checkpoint dihapus — semua dokumen berhasil.")
 
        logger.info("=" * 70)
        logger.info("MISSING INGEST COMPLETED")
        logger.info("  ES doc count total : %s", f"{count:,}")
        logger.info("  Berhasil di-ingest : %s", f"{success:,}")
        logger.info("  Masih gagal        : %s", f"{failed:,}")
        logger.info("  Durasi             : %.1f menit", elapsed / 60)
        logger.info("=" * 70)
 
        if failed > 0:
            logger.warning(
                "⚠️  Masih ada %s dokumen gagal. "
                "Jalankan kembali script ini untuk retry.",
                f"{failed:,}",
            )
        else:
            logger.info("🎉 Index lengkap! Total: %s dokumen.", f"{count:,}")
 
 
# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
 
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest dokumen yang hilang dari index ES")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Hanya hitung berapa dokumen yang hilang tanpa ingest.",
    )
    args = parser.parse_args()
 
    try:
        ingestor = MissingIngestor()
 
        if args.dry_run:
            existing = fetch_existing_npsns(ingestor.es, ingestor.index_name)
            missing  = count_missing(existing)
            logger.info("DRY RUN — Dokumen hilang: %s", f"{missing:,}")
        else:
            ingestor.run()
 
    except EnvironmentError as exc:
        logger.error("❌ Konfigurasi error: %s", exc)
        sys.exit(1)
    except FileNotFoundError as exc:
        logger.error("❌ %s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("⛔ Dihentikan. Checkpoint tersimpan — jalankan lagi untuk lanjut.")
        sys.exit(130)
