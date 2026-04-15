import os
import sys
import time
import logging
from pathlib import Path

# ── Setup path & config ──
sys.path.append(str(Path(".")))
import app.config as config  # pastikan config.py kamu ada

from elasticsearch import Elasticsearch

# ── Logging setup ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# ── Environment ──
INDEX_NAME = "idx_prompting_agent"
MAX_RETRIES = 6
BASE_DELAY = 5  # detik

# ── Init Elasticsearch ──
es = Elasticsearch(
    hosts=[os.getenv("ELASTICSEARCH_NODE")],
    basic_auth=(
        os.getenv("ELASTICSEARCH_USERNAME"),
        os.getenv("ELASTICSEARCH_PASSWORD")
    ),
    request_timeout=15,
)

def check_index_exists():
    try:
        exists = es.indices.exists(index=INDEX_NAME)
        if not exists:
            logger.warning(f"Index '{INDEX_NAME}' belum ada!")
        return exists
    except Exception as e:
        logger.error(f"Gagal cek index: {e}")
        return False


def check_cluster_health():
    for i in range(MAX_RETRIES):
        try:
            health = es.cluster.health(index=INDEX_NAME)
            shards = es.cat.shards(
                index=INDEX_NAME,
                v=True,
                h="shard,prirep,state,node"
            )

            status = health["status"]
            unassigned = health["unassigned_shards"]
            nodes = health["number_of_nodes"]

            logger.info(
                f"[{i * BASE_DELAY}s] Status={status.upper()} | "
                f"Unassigned={unassigned} | Nodes={nodes}"
            )

            print("\n=== SHARDS INFO ===")
            print(shards)

            # kondisi aman
            if status in ("green", "yellow"):
                logger.info("Cluster aman — lanjut ke proses berikutnya ")
                return True

            #  kondisi bahaya
            if i == MAX_RETRIES - 1 and status == "red":
                logger.error("Cluster RED terlalu lama — abort!")
                return False

        except Exception as e:
            logger.warning(f"Percobaan ke-{i+1} gagal: {e}")

        # ⏳ exponential backoff
        delay = BASE_DELAY * (i + 1)
        logger.info(f"Tunggu {delay} detik sebelum retry...")
        time.sleep(delay)

    return False


def main():
    logger.info(" Memulai pengecekan Elasticsearch...")

    if not check_index_exists():
        logger.warning("Lanjut tetap, tapi pastikan index dibuat sebelum indexing.")

    healthy = check_cluster_health()

    if healthy:
        logger.info(" Sistem siap digunakan (indexing / querying)")
    else:
        logger.error(" Sistem belum siap — periksa cluster terlebih dahulu")


if __name__ == "__main__":
    main()
