#!/usr/bin/env python3
import os
import sys
import json
import csv
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any

import torch
from tqdm import tqdm
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from sentence_transformers import SentenceTransformer

# -------------------------------------------------------------------
# Path & Env
# -------------------------------------------------------------------
ROOT_PATH = Path(__file__).parent.parent
sys.path.append(str(ROOT_PATH))

import app.config as config  # noqa: F401  # load env vars

CSV_PATH = ROOT_PATH / "es-export" / "clean" / "datamart_sekolah_merged_by_npsn_clean.csv"
CHECKPOINT_PATH = Path(__file__).parent / "checkpoint.json"

BATCH_SIZE = 5000
EMBED_BATCH_SIZE = 256
EMBED_DIM = 384

# -------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

def clean_key(key: str) -> str:
    """
    Membersihkan nama kolom dari:
    - UTF-8 BOM
    - tanda kutip
    - spasi berlebih
    """
    if not key:
        return key

    return (
        key.replace("\ufeff", "")
           .replace('"', "")
           .replace("'", "")
           .strip()
    )

# -------------------------------------------------------------------
# Ingestor
# -------------------------------------------------------------------
class Ingestor:
    def __init__(self, resume: bool = False):
        self.es = Elasticsearch(
            hosts=[os.getenv("ELASTICSEARCH_NODE")],
            basic_auth=(
                os.getenv("ELASTICSEARCH_USERNAME"),
                os.getenv("ELASTICSEARCH_PASSWORD"),
            ),
            request_timeout=300,
        )

        self.index_name = os.getenv("ELASTICSEARCH_INDEX", "idx_prompting_agent")
        self.checkpoint = {"last_row": 0} if not resume else self._load_checkpoint()

        self.embedder: SentenceTransformer | None = None
        self.start_time: datetime | None = None

    # ------------------------------------------------------------------
    # Checkpoint
    # ------------------------------------------------------------------
    def _load_checkpoint(self) -> Dict[str, int]:
        if CHECKPOINT_PATH.exists():
            with open(CHECKPOINT_PATH) as f:
                cp = json.load(f)
                logger.info(f"📍 Resume dari baris {cp['last_row']:,}")
                return cp
        return {"last_row": 0}

    def _save_checkpoint(self, row: int) -> None:
        with open(CHECKPOINT_PATH, "w") as f:
            json.dump({"last_row": row}, f)

        if row % 10_000 == 0:
            logger.info(f" Checkpoint disimpan di baris {row:,}")

    # ------------------------------------------------------------------
    # CSV Utils
    # ------------------------------------------------------------------
    def _count_csv_rows(self) -> int:
        logger.info(f" Menghitung total baris CSV...")
        with open(CSV_PATH, "rb") as f:
            return sum(1 for _ in f) - 1  # minus header

    # ------------------------------------------------------------------
    # Content Builder
    # ------------------------------------------------------------------
    def _generate_content(self, row: Dict[str, Any]) -> str:
        """
        Menghasilkan JSON string bersih untuk embedding:
        - key sudah dinormalisasi
        - value kosong / nol / null dibuang
        - tanpa field teknis
        """

        SKIP_FIELDS = {
            "content_vector",
            "lokasi",
            "metadata"
        }

        clean_row = {
            clean_key(k): v
            for k, v in row.items()
            if k not in SKIP_FIELDS
            and v
            and str(v).lower() not in {"null", "none", "-", "", "0"}
        }

        return json.dumps(
            clean_row,
            ensure_ascii=False,
            separators=(",", ":")
        )


    def _build_document(self, row, content, vector):
        doc_id = str(row.get("npsn", "")).strip()

        if not doc_id or doc_id.lower() in {"null", "none", "0"}:
            return None

        ALLOWED_FIELDS = {
            "npsn",
            "nama",
            "provinsi",
            "kabupaten",
            "kecamatan",
            "desa_kelurahan",
            "tahun",
            "status_sekolah",
            "akreditasi",
            "bentuk_pendidikan",
        }

        # ----------------------------
        # SAFE FIELD FILTERING
        # ----------------------------
        filtered_row = {
            k: row.get(k)
            for k in ALLOWED_FIELDS
            if row.get(k) is not None and str(row.get(k)).strip() != ""
        }

        # ----------------------------
        # SAFE LOWER HELPER
        # ----------------------------
        def _safe_lower(val):
            if not val:
                return ""
            return str(val).strip().lower()

        # ----------------------------
        # TYPE PATCH: tahun → int
        # ----------------------------
        if "tahun" in filtered_row:
            try:
                filtered_row["tahun"] = int(filtered_row["tahun"])
            except (TypeError, ValueError):
                del filtered_row["tahun"]

        # ----------------------------
        # GEO PATCH (STRICT)
        # ----------------------------
        lokasi = None
        lat_raw = row.get("lintang")
        lon_raw = row.get("bujur")

        try:
            lat = float(lat_raw)
            lon = float(lon_raw)

            if -90 <= lat <= 90 and -180 <= lon <= 180:
                lokasi = {"lat": lat, "lon": lon}
        except (TypeError, ValueError):
            pass

        if lokasi:
            filtered_row["lintang"] = lat
            filtered_row["bujur"] = lon
            filtered_row["lokasi"] = lokasi

        # ----------------------------
        # WILAYAH KEY BUILDER
        # ----------------------------
        prov = _safe_lower(filtered_row.get("provinsi"))
        kab = _safe_lower(filtered_row.get("kabupaten"))
        kec = _safe_lower(filtered_row.get("kecamatan"))
        desa = _safe_lower(filtered_row.get("desa_kelurahan"))

        if prov and kab and kec and desa:
            filtered_row["wilayah_key"] = f"{prov}|{kab}|{kec}|{desa}"

        # ----------------------------
        # VECTOR VALIDATION
        # ----------------------------
        if not vector or not isinstance(vector, list):
            logger.warning(f"Missing vector for npsn={doc_id}")
            return None

        # ----------------------------
        # CONTENT SAFETY
        # ----------------------------
        if not isinstance(content, str):
            content = str(content)

        return {
            "_index": self.index_name,
            "_id": doc_id,
            "_source": {
                **filtered_row,
                "content": content,
                "content_vector": vector,
            },
        }




    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------
    def _batch_embed(self, texts: List[str]) -> List[List[float]]:
        if self.embedder is None:
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            logger.info(f"🧠 Loading embedding model on {device}")
            self.embedder = SentenceTransformer("all-MiniLM-L6-v2", device=device)

        return self.embedder.encode(
            texts,
            batch_size=EMBED_BATCH_SIZE,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).tolist()

    # ------------------------------------------------------------------
    # Batch Processor
    # ------------------------------------------------------------------
    def _process_batch(self, batch: List[tuple]) -> Dict[str, int]:
        texts = [c for _, c in batch]
        vectors = self._batch_embed(texts)

        actions = []
        for (row, content), vec in zip(batch, vectors):
            if not vec or len(vec) != EMBED_DIM:
                continue

            doc = self._build_document(row, content, vec)
            if doc:
                actions.append(doc)


        if not actions:
            return {"success": 0, "failed": len(batch)}

        for attempt in range(3):
            try:
                success, errors = bulk(
                    self.es,
                    actions,
                    raise_on_error=False,
                    refresh=False,
                    request_timeout=120,
                )

                if errors:
                    logger.error(f"{len(errors)} documents failed in this batch")
                    
                    # RETRY FAILED DOCUMENTS
                    retry_actions = []
                    for err in errors:
                        if "index" in err:
                            retry_actions.append(err["index"]["data"])

                    if retry_actions:
                        logger.info("Retrying failed documents...")
                        bulk(self.es, retry_actions)

                return {"success": success, "failed": len(errors)}
            except Exception as e:
                logger.warning(f" Retry {attempt+1}/3: {e}")
                if attempt == 2:
                    return {"success": 0, "failed": len(actions)}

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run(self) -> None:
        total_rows = self._count_csv_rows()

        logger.info("=" * 70)
        logger.info(f" STARTING INGESTION | {total_rows:,} rows")
        logger.info(f" Index: {self.index_name}")
        logger.info("=" * 70)

        self.start_time = datetime.now()
        success = failed = current_row = 0
        batch: List[tuple] = []

        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            for _ in range(self.checkpoint["last_row"]):
                next(reader, None)
                current_row += 1

            for raw_row in tqdm(reader, total=total_rows, initial=current_row, desc="Ingesting"):
                    current_row += 1
                    row = {clean_key(k): v for k, v in raw_row.items()}
                    batch.append((row, self._generate_content(row)))

                    if len(batch) >= BATCH_SIZE:
                        r = self._process_batch(batch)
                        success += r["success"]
                        failed += r["failed"]
                        batch.clear()
                        if r["failed"] == 0:
                            self._save_checkpoint(current_row)
                        else:
                            logger.warning("Checkpoint NOT saved due to failures")


        if batch:
            r = self._process_batch(batch)
            success += r["success"]
            failed += r["failed"]

        self.es.indices.refresh(index=self.index_name)
        CHECKPOINT_PATH.unlink(missing_ok=True)
        count = self.es.count(index=self.index_name)["count"]
        logger.info(f"Final index count: {count}")

        elapsed = (datetime.now() - self.start_time).total_seconds()
        logger.info("=" * 70)
        logger.info(" INGESTION COMPLETED")
        logger.info(f" Success: {success:,}")
        logger.info(f" Failed:  {failed:,}")
        logger.info(f" Time:    {elapsed/60:.1f} minutes")
        logger.info("=" * 70)


# -------------------------------------------------------------------
# Entrypoint
# -------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    try:
        Ingestor(resume=args.resume).run()
    except KeyboardInterrupt:
        logger.warning(" Dihentikan oleh user")
