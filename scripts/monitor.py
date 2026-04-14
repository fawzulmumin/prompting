import os
import time
import sys
from pathlib import Path
from datetime import datetime

# Setup path agar bisa baca config
root_path = Path(__file__).parent.parent
sys.path.append(str(root_path))

import app.config as config
from elasticsearch import Elasticsearch # pyright: ignore[reportMissingImports]

def monitor():
    es = Elasticsearch(
        hosts=[os.getenv("ELASTICSEARCH_NODE", "http://localhost:9200")],
        basic_auth=(
            os.getenv("ELASTICSEARCH_USERNAME"),
            os.getenv("ELASTICSEARCH_PASSWORD")
        )
    )

    index_name = os.getenv("ELASTICSEARCH_INDEX", "idx_prompting_agent")
    total_goal = int(os.getenv("TOTAL_GOAL", "0"))

    if total_goal <= 0:
        print(" TOTAL_GOAL belum diset atau tidak valid")
        return

    print(f"Monitoring Ingestion for index: {index_name}")
    print(f"Goal: {total_goal:,} documents\n")

    start_time = time.time()

    try:
        while True:
            res = es.count(index=index_name)
            current_count = res["count"]

            elapsed_total = time.time() - start_time

            docs_per_sec = current_count / elapsed_total if elapsed_total > 0 else 0
            remaining_docs = max(total_goal - current_count, 0)

            eta_seconds = remaining_docs / docs_per_sec if docs_per_sec > 0 else 0
            eta_hours = eta_seconds / 3600

            progress = min((current_count / total_goal) * 100, 100)

            sys.stdout.write(
                f"\r Progress: {progress:.2f}% | "
                f"Docs: {current_count:,}/{total_goal:,} | "
                f"Speed: {docs_per_sec:.1f} doc/s | "
                f"ETA: {eta_hours:.2f} hours"
            )
            sys.stdout.flush()

            time.sleep(5)

    except KeyboardInterrupt:
        print("\n Monitoring stopped.")


if __name__ == "__main__":
    monitor()