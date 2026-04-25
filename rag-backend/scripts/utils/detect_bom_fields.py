#!/usr/bin/env python3
import csv
from pathlib import Path
from collections import Counter

ROOT_PATH = Path(__file__).parent.parent
CSV_PATH = ROOT_PATH / "es-export" / "merged_by_key" / "datamart_sekolah_merged_by_npsn.csv"


def clean_key(key: str) -> str:
    if not key:
        return key
    return (
        key.replace("\ufeff", "")
           .replace('"', "")
           .replace("'", "")
           .strip()
    )


def detect_bom_fields():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader)

    results = []
    counter = Counter()

    for h in headers:
        cleaned = clean_key(h)

        has_bom = "\ufeff" in h
        changed = h != cleaned

        if has_bom or changed:
            results.append({
                "raw": repr(h),
                "cleaned": cleaned,
                "has_bom": has_bom,
                "changed": changed,
            })
            counter["problematic"] += 1
        else:
            counter["clean"] += 1

    print("=" * 80)
    print(" DETEKSI HEADER CSV (BOM / KOTOR )")
    print("=" * 80)
    print(f" Total fields   : {len(headers)}")
    print(f" Clean fields   : {counter['clean']}")
    print(f" Problem fields: {counter['problematic']}")
    print("-" * 80)

    for r in results:
        print(f"RAW     : {r['raw']}")
        print(f"CLEANED : {r['cleaned']}")
        print(f"BOM     : {r['has_bom']} | CHANGED: {r['changed']}")
        print("-" * 80)


if __name__ == "__main__":
    detect_bom_fields()
