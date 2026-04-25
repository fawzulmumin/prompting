#!/usr/bin/env python3
"""
ingest_qc.py — Merge CSV datamart.sekolah + qc.sekolah → ingest ke idx_sekolah_qc
 
ALUR:
  1. Baca CSV datamart_sekolah_export.csv  (dari SSMS export)
  2. Baca CSV qc_sekolah.csv               (diberikan langsung sebagai CSV)
  3. Merge berdasarkan sekolah_id
  4. Build dokumen ES:
     - field profil dari datamart
     - nested object qc dari qc.sekolah
     - qc_score (total_masalah, persen_bersih) dihitung otomatis
     - field content (teks naratif Opsi B: profil + ringkasan masalah QC)
     - content_vector dikosongkan (diisi oleh rekan yang handle embedding)
     - wilayah_key builder
     - geo_point (lokasi) dari lintang + bujur
  5. Bulk ingest ke Elasticsearch
  6. Checkpoint per batch agar bisa resume jika terputus
 
PENGGUNAAN:
  # Jalankan fresh
  python scripts/ingest_qc.py
 
  # Resume dari checkpoint jika terputus
  python scripts/ingest_qc.py --resume
 
  # Dry run: print 5 dokumen pertama tanpa kirim ke ES
  python scripts/ingest_qc.py --dry-run
 
STRUKTUR FOLDER YANG DIHARAPKAN:
  rag-backend/
  ├── scripts/
  │   ├── ingest_qc.py          ← file ini
  │   └── es-export/
  │       ├── datamart_sekolah_export.csv   ← dari SSMS
  │       └── qc_sekolah.csv               ← CSV qc yang diberikan
  └── data/
      └── mapping.json
 
CATATAN UNTUK REKAN EMBEDDING:
  - Field `content` sudah terisi teks naratif gabungan profil + ringkasan masalah QC
  - Field `content_vector` sengaja dikosongkan (tidak di-set)
  - Untuk mengisi vector: baca content tiap dokumen, embed dengan all-MiniLM-L6-v2,
    lalu update via ES _update API:
      POST /idx_sekolah_qc/_update/<npsn>
      { "doc": { "content_vector": [...384 floats...] } }
  - Atau gunakan ingest_fast.py yang sudah ada sebagai referensi pola embed+upload
"""
 
import os
import sys
import csv
import json
import re
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List
 
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
 
# ─────────────────────────────────────────────────────────────────────────────
# Path setup
# ─────────────────────────────────────────────────────────────────────────────
ROOT_PATH = Path(__file__).parent.parent
sys.path.append(str(ROOT_PATH))
 
import app.config as config  # noqa: F401 — load env vars dari .env
 
# ─────────────────────────────────────────────────────────────────────────────
# Konstanta path file input
# ─────────────────────────────────────────────────────────────────────────────
EXPORT_DIR      = ROOT_PATH / "scripts" / "es-export"
DATAMART_CSV    = EXPORT_DIR / "datamart_sekolah_export.csv"
QC_CSV          = EXPORT_DIR / "qc_sekolah.csv"
CHECKPOINT_PATH = Path(__file__).parent / "checkpoint_qc.json"
 
BATCH_SIZE      = 2000   # dokumen per bulk request
 
# QC: jumlah kolom yang digunakan sebagai indikator (qc_1 s/d qc_15)
QC_TOTAL_COLS   = 15
 
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
# Label deskriptif tiap kode QC — digunakan untuk field content (Opsi B)
# ─────────────────────────────────────────────────────────────────────────────
QC_LABELS: Dict[str, Dict[int, str]] = {
    "qc_1":  {2: "duplikat sekolah (nama+jenjang+status+wilayah sama)"},
    "qc_2":  {2: "NPSN ganda", 3: "format NPSN tidak valid", 4: "NPSN kosong"},
    "qc_3":  {2: "nama mengandung karakter spesial", 3: "nama terlalu pendek (<5 huruf)", 4: "nama kosong"},
    "qc_4":  {2: "alamat mengandung karakter spesial", 3: "alamat terlalu pendek (<5 huruf)", 4: "alamat kosong"},
    "qc_5":  {2: "kode wilayah tidak aktif di referensi", 3: "format kode wilayah tidak valid", 4: "kode wilayah kosong"},
    "qc_6":  {2: "koordinat lintang kosong"},
    "qc_7":  {2: "koordinat bujur kosong"},
    "qc_8":  {2: "SK izin operasional terisi tapi file belum diunggah", 3: "SK izin operasional terlalu pendek", 4: "SK izin operasional kosong"},
    "qc_9":  {2: "tanggal SK izin operasional kosong"},
    "qc_10": {2: "SK pendirian terlalu pendek (<5 huruf)", 3: "SK pendirian kosong"},
    "qc_11": {2: "tanggal SK pendirian kosong"},
    "qc_12": {2: "foto sekolah belum diunggah"},
    "qc_13": {2: "yayasan terdaftar tapi tidak aktif", 3: "yayasan tidak terdaftar"},
    "qc_14": {},  # belum digunakan
    "qc_15": {},  # belum digunakan
}
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
 
def clean_key(key: str) -> str:
    """Bersihkan nama kolom dari BOM, kutip, dan spasi berlebih."""
    if not key:
        return key
    return key.replace("\ufeff", "").replace('"', "").replace("'", "").strip()
 
 
NOISE_VALUES = {
    "null", "none", "", "-", "n/a", "na", "tidak ada", "belum ada",
    "kosong", "0", "00", "000", "#n/a", "–", "—", "tad", ".",
}

def safe_str(val: Any) -> str:
    if val is None:
        return None
    s = str(val).strip()
    return None if s.lower() in NOISE_VALUES else s
 
def safe_date(val: Any) -> Optional[str]:
    """Normalisasi format tanggal SSMS (titik) ke format standar ES (titik dua).
    Input : '2021-09-08 19.54.31.287'  atau  '2021-09-08'
    Output: '2021-09-08 19:54:31'      atau  '2021-09-08'
    """
    s = safe_str(val)
    if not s:
        return None
    # Ganti titik pemisah waktu → titik dua, buang milidetik
    # Pattern: yyyy-MM-dd HH.mm.ss.SSS  atau  yyyy-MM-dd HH.mm.ss   
    m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2})\.(\d{2})\.(\d{2})", s)
    if m:
        return f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}"
    # Format sudah benar atau hanya tanggal
    return s[:10] if len(s) >= 10 else None

def safe_float(val: Any) -> Optional[float]:
    """Parse float, kembalikan None jika gagal atau nol."""
    try:
        f = float(val)
        return None if f == 0.0 else f
    except (TypeError, ValueError):
        return None
 
 
def safe_bool(val: Any) -> Optional[bool]:
    """Parse 1/0/'1'/'0' jadi bool, kembalikan None jika tidak dikenali."""
    if val is None:
        return None
    s = str(val).strip()
    if s in {"1", "true", "True"}:
        return True
    if s in {"0", "false", "False"}:
        return False
    return None
 
 
def safe_qc_byte(val: Any) -> Optional[int]:
    """Parse nilai QC jadi int 0–15, kembalikan None jika gagal."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
 
 
def load_qc_lookup(qc_csv_path: Path) -> Dict[str, Dict[str, Any]]:
    """
    Baca qc_sekolah.csv dan buat lookup dict berdasarkan sekolah_id.
    Return: { sekolah_id: { qc_1: x, qc_2: x, ..., qc_15: x } }
    """
    logger.info(f"📂 Membaca QC CSV: {qc_csv_path}")
    lookup: Dict[str, Dict[str, Any]] = {}
 
    with open(qc_csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {clean_key(k): v for k, v in row.items()}
            sid = safe_str(row.get("sekolah_id"))
            if not sid:
                continue
            lookup[sid.lower()] = {
                f"qc_{i}": safe_qc_byte(row.get(f"qc_{i}"))
                for i in range(1, QC_TOTAL_COLS + 1)
            }
 
    logger.info(f"✅ QC lookup selesai: {len(lookup):,} sekolah")
    return lookup
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Content Builder (Opsi B: profil + ringkasan masalah QC)
# ─────────────────────────────────────────────────────────────────────────────
 
def build_content(profil: Dict[str, Any], qc_vals: Dict[str, Any]) -> str:
    """
    Bangun teks naratif untuk field `content` yang akan di-embed oleh rekan.
 
    Format:
      { profil sekolah sebagai JSON } Masalah data: [daftar masalah QC jika ada]
 
    Contoh output:
      {"nama":"SDN 01 CONTOH","bentuk_pendidikan":"SD","provinsi":"Prov. Jawa Barat",
       "kabupaten":"Kab. Bandung","status_sekolah":"NEGERI","akreditasi":"A",
       "keaktifan":"Aktif","alamat_jalan":"Jl. Raya No.1","yayasan":null}
      Masalah data: koordinat lintang kosong, foto sekolah belum diunggah
    """
    PROFIL_FIELDS = [
        "nama", "bentuk_pendidikan", "provinsi", "kabupaten", "kecamatan",
        "desa_kelurahan", "status_sekolah", "akreditasi", "keaktifan",
        "alamat_jalan", "yayasan", "sumber_listrik", "akses_internet",
        "waktu_penyelenggaraan", "status_kepemilikan",
    ]
 
    profil_clean = {
        k: profil.get(k)
        for k in PROFIL_FIELDS
        if profil.get(k) is not None
    }
 
    # Kumpulkan deskripsi masalah QC
    masalah: List[str] = []
    for col, code_labels in QC_LABELS.items():
        val = qc_vals.get(col)
        if val is not None and val != 1 and val in code_labels:
            masalah.append(code_labels[val])
    
    PROFIL_FIELDS = [
        "nama", "bentuk_pendidikan", "provinsi", "kabupaten", "kecamatan",
        "desa_kelurahan", "status_sekolah", "akreditasi", "keaktifan",
        "alamat_jalan", "yayasan", "sumber_listrik", "akses_internet",
        "waktu_penyelenggaraan", "status_kepemilikan",
    ]

    profil_clean = {
        k: safe_str(profil.get(k))
        for k in PROFIL_FIELDS
    }
    
    parts = []
    if profil_clean.get("nama"):
        parts.append(profil_clean["nama"])
    if profil_clean.get("bentuk_pendidikan"):
        parts.append(profil_clean["bentuk_pendidikan"])
    if profil_clean.get("status_sekolah"):
        status_up = profil_clean["status_sekolah"].upper().strip()
        if status_up == "NEGERI":
            jenis = "negeri"
        elif status_up == "SWASTA":
            jenis = "swasta"
        else:
            jenis = profil_clean["status_sekolah"].lower()  
        parts.append(f"sekolah {jenis}")
    if profil_clean.get("akreditasi"):
        parts.append(f"akreditasi {profil_clean['akreditasi']}")
    if profil_clean.get("keaktifan"):
        parts.append(f"status {profil_clean['keaktifan'].lower()}")

    lokasi_parts = []
    if profil_clean.get("desa_kelurahan"):
        lokasi_parts.append(profil_clean["desa_kelurahan"])
    if profil_clean.get("kecamatan"):
        lokasi_parts.append(profil_clean["kecamatan"])
    if profil_clean.get("kabupaten"):
        lokasi_parts.append(profil_clean["kabupaten"])
    if profil_clean.get("provinsi"):
        lokasi_parts.append(profil_clean["provinsi"])
    if lokasi_parts:
        parts.append("di " + ", ".join(lokasi_parts))

    if profil_clean.get("alamat_jalan"):
        parts.append(f"alamat {profil_clean['alamat_jalan']}")
    if profil_clean.get("yayasan"):
        parts.append(f"yayasan {profil_clean['yayasan']}")
    if profil_clean.get("sumber_listrik"):
        parts.append(f"listrik {profil_clean['sumber_listrik'].lower()}")
    if profil_clean.get("akses_internet"):
        parts.append(f"internet {profil_clean['akses_internet'].lower()}")

    narasi = ". ".join(parts)

    if masalah:
        return f"{narasi}. Masalah data: {', '.join(masalah)}."
    else:
        return f"{narasi}. Data lengkap."
 
 
# ─────────────────────────────────────────────────────────────────────────────
# QC Score Calculator
# ─────────────────────────────────────────────────────────────────────────────
 
def calc_qc_score(qc_vals: Dict[str, Any]) -> Dict[str, Any]:
    """
    Hitung total_masalah dan persen_bersih dari nilai QC.
 
    Hanya menghitung qc_1 s/d qc_13 (13 indikator aktif).
    qc_14 dan qc_15 dikecualikan karena belum digunakan (selalu 0),
    sehingga tidak menimbulkan masalah palsu pada setiap sekolah.
 
    Contoh sample data sebelumnya:
      qc_14=0, qc_15=0 → keduanya dihitung masalah → score -2 tidak akurat
    Setelah fix:
      qc_14 dan qc_15 diabaikan dari score → hasil akurat
    """
    QC_ACTIVE_COLS = 13  # qc_1 s/d qc_13
 
    total = 0
    for i in range(1, QC_ACTIVE_COLS + 1):
        val = qc_vals.get(f"qc_{i}")
        if val is not None and val != 1:
            total += 1
 
    persen_bersih = round(((QC_ACTIVE_COLS - total) / QC_ACTIVE_COLS) * 100, 2)
    return {"total_masalah": total, "persen_bersih": persen_bersih}
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Document Builder
# ─────────────────────────────────────────────────────────────────────────────
 
def build_document(
    row: Dict[str, Any],
    qc_vals: Dict[str, Any],
    index_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Build satu dokumen ES dari satu baris datamart + data QC-nya.
    Return None jika NPSN tidak valid (dokumen ini akan di-skip).
    """
    doc_id = safe_str(row.get("npsn"))
    if not doc_id or doc_id.lower() in {"null", "none", "0"}:
        return None
 
    sekolah_id = safe_str(row.get("sekolah_id"))
 
    # ── Geo ──────────────────────────────────────────────────────────────────
    lat = safe_float(row.get("lintang"))
    lon = safe_float(row.get("bujur"))
    lokasi = None
    if lat is not None and lon is not None:
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            lokasi = {"lat": lat, "lon": lon}
        else:
            lat = lon = None  # koordinat di luar range, buang
 
    # ── Wilayah Key ───────────────────────────────────────────────────────────
    def _sl(v):
        s = safe_str(v)
        return s.lower() if s else None
 
    prov = _sl(row.get("provinsi"))
    kab  = _sl(row.get("kabupaten"))
    kec  = _sl(row.get("kecamatan"))
    desa = _sl(row.get("desa_kelurahan"))
    wilayah_key = f"{prov}|{kab}|{kec}|{desa}" if all([prov, kab, kec, desa]) else None
 
    # ── Boolean Fields ────────────────────────────────────────────────────────
    wilayah_terpencil  = safe_bool(row.get("wilayah_terpencil"))
    wilayah_perbatasan = safe_bool(row.get("wilayah_perbatasan"))
    partisipasi_bos    = safe_bool(row.get("partisipasi_bos"))
 
    # ── QC Object ─────────────────────────────────────────────────────────────
    qc_obj = {
        f"qc_{i}": qc_vals.get(f"qc_{i}")
        for i in range(1, QC_TOTAL_COLS + 1)
    }
    # Buang key yang None agar tidak ada null di object qc
    qc_obj = {k: v for k, v in qc_obj.items() if v is not None}
 
    qc_score = calc_qc_score(qc_vals)
    content  = build_content(row, qc_vals)
 
    # ── Assemble Source ───────────────────────────────────────────────────────
    source: Dict[str, Any] = {}
 
    def _set(field: str, val: Any):
        """Set field ke source hanya jika val tidak None."""
        if val is not None:
            source[field] = val
 
    _set("sekolah_id",                  sekolah_id)
    _set("npsn",                        doc_id)
    _set("nama",                        safe_str(row.get("nama")))
    _set("bentuk_pendidikan",           safe_str(row.get("bentuk_pendidikan")))
    _set("status_sekolah",              safe_str(row.get("status_sekolah")))
    _set("keaktifan",                   safe_str(row.get("keaktifan")))
    _set("alamat_jalan",                safe_str(row.get("alamat_jalan")))
    _set("desa_kelurahan",              safe_str(row.get("desa_kelurahan")))
    _set("kecamatan",                   safe_str(row.get("kecamatan")))
    _set("kabupaten",                   safe_str(row.get("kabupaten")))
    _set("provinsi",                    safe_str(row.get("provinsi")))
    _set("kode_wilayah",                safe_str(row.get("kode_wilayah")))
    _set("kode_pos",                    safe_str(row.get("kode_pos")))
    _set("wilayah_key",                 wilayah_key)
    if lat is not None: _set("lintang", lat)
    if lon is not None: _set("bujur",   lon)
    _set("lokasi",                      lokasi)
    _set("nomor_telepon",               safe_str(row.get("nomor_telepon")))
    _set("email",                       safe_str(row.get("email")))
    _set("website",                     safe_str(row.get("website")))
    _set("status_kepemilikan",          safe_str(row.get("status_kepemilikan")))
    _set("yayasan",                     safe_str(row.get("yayasan")))
    _set("akreditasi",                  safe_str(row.get("akreditasi")))
    _set("sk_pendirian_sekolah",        safe_str(row.get("sk_pendirian_sekolah")))
    _set("tanggal_sk_pendirian",        safe_date(row.get("tanggal_sk_pendirian")))
    _set("sk_izin_operasional",         safe_str(row.get("sk_izin_operasional")))
    _set("tanggal_sk_izin_operasional", safe_date(row.get("tanggal_sk_izin_operasional")))
    _set("sumber_listrik",              safe_str(row.get("sumber_listrik")))
    _set("akses_internet",              safe_str(row.get("akses_internet")))
    _set("waktu_penyelenggaraan",       safe_str(row.get("waktu_penyelenggaraan")))
    _set("sertifikasi_iso",             safe_str(row.get("sertifikasi_iso")))
    if wilayah_terpencil  is not None: source["wilayah_terpencil"]  = wilayah_terpencil
    if wilayah_perbatasan is not None: source["wilayah_perbatasan"] = wilayah_perbatasan
    if partisipasi_bos    is not None: source["partisipasi_bos"]    = partisipasi_bos
 
    if qc_obj:
        source["qc"]       = qc_obj
    source["qc_score"]     = qc_score
    source["content"]      = content
    # content_vector sengaja tidak diset di sini — diisi oleh rekan embedding
 
    return {
        "_index":  index_name,
        "_id":     doc_id,
        "_source": source,
    }
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint
# ─────────────────────────────────────────────────────────────────────────────
 
def load_checkpoint() -> int:
    if CHECKPOINT_PATH.exists():
        with open(CHECKPOINT_PATH) as f:
            cp = json.load(f)
            row = cp.get("last_row", 0)
            logger.info(f"📍 Resume dari baris {row:,}")
            return row
    return 0
 
 
def save_checkpoint(row: int) -> None:
    with open(CHECKPOINT_PATH, "w") as f:
        json.dump({"last_row": row}, f)
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Main Ingestor
# ─────────────────────────────────────────────────────────────────────────────
 
def run(resume: bool = False, dry_run: bool = False) -> None:
    # Validasi file input
    for path, label in [(DATAMART_CSV, "datamart CSV"), (QC_CSV, "QC CSV")]:
        if not path.exists():
            raise FileNotFoundError(
                f"❌ {label} tidak ditemukan di: {path}\n"
                f"   Pastikan file sudah ditempatkan di folder es-export/"
            )
 
    # Load QC lookup ke memori (ukurannya kecil, aman)
    qc_lookup = load_qc_lookup(QC_CSV)
 
    index_name = os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")
    start_row  = load_checkpoint() if resume else 0
 
    if dry_run:
        logger.info("🔍 DRY RUN mode — tidak ada data yang dikirim ke ES")
 
    # Hitung total baris CSV
    with open(DATAMART_CSV, "rb") as f:
        total_rows = sum(1 for _ in f) - 1
    logger.info(f"📊 Total baris datamart CSV: {total_rows:,}")
 
    if not dry_run:
        es = Elasticsearch(
            hosts=[os.getenv("ELASTICSEARCH_NODE")],
            basic_auth=(
                os.getenv("ELASTICSEARCH_USERNAME"),
                os.getenv("ELASTICSEARCH_PASSWORD"),
            ),
            request_timeout=300,
        )
        # Pastikan index ada
        if not es.indices.exists(index=index_name):
            raise RuntimeError(
                f"❌ Index '{index_name}' belum ada di ES.\n"
                f"   Jalankan init_es.py terlebih dahulu."
            )
        logger.info(f"✅ Terhubung ke ES | Index: {index_name}")
    else:
        es = None
 
    start_time = datetime.now()
    success = failed = skipped = current_row = 0
    batch: List[Dict] = []
 
    with open(DATAMART_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
 
        # Skip baris yang sudah diproses jika resume
        for _ in range(start_row):
            next(reader, None)
            current_row += 1
 
        for raw_row in reader:
            current_row += 1
            row = {clean_key(k): v for k, v in raw_row.items()}
 
            # Cari QC data berdasarkan sekolah_id
            sid = safe_str(row.get("sekolah_id"))
            qc_vals = qc_lookup.get(sid.lower(), {}) if sid else {}
 
            if not qc_vals:
                skipped += 1  # catat tapi tetap ingest tanpa QC
 
            doc = build_document(row, qc_vals, index_name)
            if doc is None:
                skipped += 1
                continue
 
            if dry_run:
                # Print sample 5 dokumen pertama
                if current_row <= 5:
                    print(json.dumps(doc["_source"], ensure_ascii=False, indent=2))
                continue
 
            batch.append(doc)
 
            if len(batch) >= BATCH_SIZE:
                s, errs = _bulk_send(es, batch)
                success += s
                failed  += errs
                batch.clear()
                if errs == 0:
                    save_checkpoint(current_row)
                else:
                    logger.warning(f"⚠️  {errs} dokumen gagal, checkpoint tidak disimpan")
 
                # Progress log setiap 10 batch
                if (current_row // BATCH_SIZE) % 10 == 0:
                    pct = current_row / total_rows * 100
                    elapsed = (datetime.now() - start_time).total_seconds()
                    logger.info(
                        f"Progress: {current_row:,}/{total_rows:,} ({pct:.1f}%) | "
                        f"✅ {success:,} | ❌ {failed:,} | ⏱ {elapsed/60:.1f}m"
                    )
 
    # Flush batch terakhir
    if batch and not dry_run:
        s, errs = _bulk_send(es, batch)
        success += s
        failed  += errs
 
    if not dry_run:
        es.indices.refresh(index=index_name)
        count = es.count(index=index_name)["count"]
        CHECKPOINT_PATH.unlink(missing_ok=True)
 
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info("=" * 65)
        logger.info("✅ INGEST SELESAI")
        logger.info(f"   Dokumen berhasil : {success:,}")
        logger.info(f"   Dokumen gagal    : {failed:,}")
        logger.info(f"   Di-skip (no NPSN): {skipped:,}")
        logger.info(f"   Total di index   : {count:,}")
        logger.info(f"   Waktu            : {elapsed/60:.1f} menit")
        logger.info("=" * 65)
        logger.info("")
        logger.info("📌 LANGKAH SELANJUTNYA UNTUK REKAN EMBEDDING:")
        logger.info("   Field `content` sudah terisi di setiap dokumen.")
        logger.info("   Gunakan ES scroll API untuk baca content, generate vector,")
        logger.info("   lalu update field `content_vector` via _update API.")
    else:
        logger.info(f"🔍 Dry run selesai. {current_row} baris dibaca.")
 
 
def _bulk_send(es: Elasticsearch, batch: List[Dict]) -> tuple:
    """Kirim batch ke ES, return (success_count, error_count)."""
    for attempt in range(3):
        try:
            success, errors = bulk(
                es,
                batch,
                raise_on_error=False,
                refresh=False,
                request_timeout=120,
            )
            return success, len(errors) if errors else 0
        except Exception as e:
            logger.warning(f"  Retry {attempt+1}/3: {e}")
            if attempt == 2:
                return 0, len(batch)
    return 0, len(batch)
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────────
 
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest datamart.sekolah + qc.sekolah ke Elasticsearch"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Lanjutkan dari checkpoint terakhir jika proses sebelumnya terputus",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print 5 dokumen pertama tanpa mengirim ke ES",
    )
    args = parser.parse_args()
 
    try:
        run(resume=args.resume, dry_run=args.dry_run)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("⛔ Dihentikan oleh user. Jalankan dengan --resume untuk melanjutkan.")
        sys.exit(0)