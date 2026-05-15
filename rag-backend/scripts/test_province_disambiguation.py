"""
scripts/test_province_disambiguation.py
Validasi runtime untuk province alias vs kota/kabupaten disambiguation.
Jalankan dari root rag-backend:
    python scripts/test_province_disambiguation.py
"""
import asyncio
import sys
sys.path.insert(0, '.')

async def main():
    from unittest.mock import AsyncMock
    from app.services.core.location_resolver_adapter import resolve_location_from_query
    from app.services.core.result_model import ResultStatus

    mock_es = AsyncMock()

    test_cases = [
        # (query, expected_scope, expected_val_substring)
        # Must → provinsi
        ("ada berapa SD di jakarta?",        "provinsi", "dki jakarta"),
        ("ada berapa SD di yogyakarta?",     "provinsi", "yogyakarta"),   # canonical: "prov. d.i. yogyakarta"
        ("daftar SMA di jogja",              "provinsi", "yogyakarta"),   # canonical: "prov. d.i. yogyakarta"
        ("sekolah di jatim",                 "provinsi", "jawa timur"),
        ("SD di aceh",                       "provinsi", "aceh"),
        # Must → kabupaten (explicit prefix present)
        ("SD di kota yogyakarta",            "kabupaten", "kota yogyakarta"),
        ("SMP di jakarta selatan",           "kabupaten", "jakarta selatan"),
        ("sekolah di jakarta pusat",         "kabupaten", "jakarta pusat"),
    ]

    print(f"\n{'Query':<45} {'Exp.scope':<12} {'Got entities':<45} {'OK?'}")
    print("-" * 110)

    all_ok = True
    for query, exp_scope, exp_val in test_cases:
        result = await resolve_location_from_query(query, mock_es)
        entities = result.get("data", {}).get("entities", {})

        # entities can be dict (single) or list (multi)
        if isinstance(entities, list):
            entities = entities[0] if entities else {}

        got_prov = entities.get("provinsi", "")
        got_kab  = entities.get("kabupaten", "")

        if exp_scope == "provinsi":
            got_scope = "provinsi" if got_prov else ("kabupaten" if got_kab else "—")
            got_val   = got_prov or got_kab or "—"
        else:
            got_scope = "kabupaten" if got_kab else ("provinsi" if got_prov else "—")
            got_val   = got_kab or got_prov or "—"

        ok = (got_scope == exp_scope) and (exp_val.lower() in str(got_val).lower())
        mark = "OK" if ok else "FAIL"
        if not ok:
            all_ok = False

        print(f"{query:<45} {exp_scope:<12} {str(entities):<45} {mark}")

    print()
    if all_ok:
        print("All checks passed.")
    else:
        print("Some checks FAILED — see rows marked FAIL above.")
    print()

asyncio.run(main())
