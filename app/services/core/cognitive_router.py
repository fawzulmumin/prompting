# services/core/cognitive_router.py
"""
CognitiveRouter v2 — Single LLM call untuk classify + extract entities.
Menggantikan:
  - llm_intent_classifier.py (klasifikasi)
  - smart_query_parser.py (ekstraksi entitas rule-based)
"""
 
import re
import json
import logging
import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum
 
from app.providers.deepseek import call_deepseek
from app.utils.cache import get_cache, set_cache
from app.services.core.oot_response_templates import get_oot_response
 
logger = logging.getLogger(__name__)
 
_CACHE_TTL = 86_400  # 24 jam
_LLM_TIMEOUT = 21.0   # detik
 
 
# ════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ════════════════════════════════════════════════════════════════
 
@dataclass
class WilayahEntities:
    provinsi:  Optional[str] = None
    kabupaten: Optional[str] = None
    kecamatan: Optional[str] = None
    desa:      Optional[str] = None
    landmark:  Optional[str] = None
 
@dataclass
class CognitiveIntent:
    # Routing
    module:    str = "out_of_context"
    secondary: List[str] = field(default_factory=list)
    scope:     str = "nasional"
 
    # Entities
    jenjang:          Optional[str] = None
    jenjang_list:     List[str] = field(default_factory=list)
    status:           Optional[str] = None
    akreditasi:       Optional[str] = None
    akreditasi_mode:  Optional[str] = None
    akreditasi_list:  List[str] = field(default_factory=list)
    
    nama_sekolah:     Optional[str] = None
    npsn:             Optional[str] = None
    wilayah:          WilayahEntities = field(default_factory=WilayahEntities)
    wilayah_bandingkan: Optional[str] = None
    yayasan:          Optional[str] = None
    koordinat_user:   bool = False
    radius_km:        Optional[float] = None
    siswa_min:    Optional[int]   = None   # filter "lebih dari X siswa"
    siswa_max:    Optional[int]   = None   # filter "kurang dari X siswa"
    sort_by:      Optional[str]   = None   # "siswa_total" | "guru_total" | "akreditasi"
    sort_order:   Optional[str]   = None   # "asc" | "desc"
    metric_target: Optional[str]  = None   # "sekolah"|"siswa"|"guru"|"guru_pns"|"guru_honor"|"guru_sertifikasi"|"tendik"
    # OOT
    is_oot:    bool = False
    oot_reason: Optional[str] = None
 
    # Debug
    raw_label: Optional[str] = None
    @property
    def primary(self) -> str:
        """Alias .module → kompatibel dengan IntentResult.primary di rag_server."""
        return self.module
 
    @primary.setter
    def primary(self, value: str) -> None:
        self.module = value
 
    @property
    def filters(self) -> dict:
        """Alias dict sederhana untuk akses filter — kompatibel dengan test & IntentResult.filters."""
        result = {}
        if self.jenjang:
            result["jenjang"] = self.jenjang
        if self.status:
            result["status"] = self.status
        if self.akreditasi:
            result["akreditasi"] = self.akreditasi
        if self.nama_sekolah:
            result["nama_sekolah"] = self.nama_sekolah
        if self.npsn:
            result["npsn"] = self.npsn
        if self.yayasan:
            result["yayasan"] = self.yayasan
        if self.radius_km is not None:
            result["radius_km"] = self.radius_km
        return result
 
VALID_MODULES = {
    "aggregation", "comparison", "profile", "list", "geo", "out_of_context"
}
 
VALID_SCOPES = {
    "nasional", "provinsi", "kabupaten", "kecamatan", "desa", "geo_radius"
}
 
class QueryRoute(str, Enum):
    AGGREGATION    = "aggregation"
    COMPARISON     = "comparison"
    PROFILE        = "profile"
    LIST           = "list"
    GEO            = "geo"
    OUT_OF_CONTEXT = "out_of_context"
 
# ════════════════════════════════════════════════════════════════
# SYSTEM PROMPT (import dari file terpisah untuk kemudahan editing)
# ════════════════════════════════════════════════════════════════
 
# Letakkan isi system prompt dari Bagian 1 di sini sebagai string
_SYSTEM_PROMPT = """
KAMU ADALAH INTENT CLASSIFIER DAN ENTITY EXTRACTOR untuk Sistem Informasi Satuan Pendidikan
Kemendikdasmen Indonesia. Sistem ini mengelola data sekolah (SD/SMP/SMA/SMK/MI/MTs/MA/TK/PAUD/SLB),
guru & tenaga pendidik (PTK), dan siswa dari seluruh Indonesia.
 
═══════════════════════════════════════════════════════════
OUTPUT: WAJIB JSON VALID. Tidak ada teks lain, tidak ada markdown, tidak ada ```json.
═══════════════════════════════════════════════════════════
 
SCHEMA OUTPUT:
{
  "module": "<label_utama>",
  "secondary": ["<label_pendukung>"],
  "scope": "<nasional|provinsi|kabupaten|kecamatan|desa|geo_radius>",
  "entities": {
    "jenjang": "<jenjang tunggal jika hanya satu jenis, atau null jika lebih dari satu>",
    "jenjang_list": "<array jenjang jika user minta lebih dari satu, contoh [\"SD\",\"SMP\"], atau [] jika hanya satu>",
    "status": "<NEGERI|SWASTA|null>",
    "akreditasi": "<A|B|C|TT|BELUM|null — jika tunggal atau gte/exclude>",
    "akreditasi_mode": "<exact|gte|exclude|in|null>",
    "akreditasi_list": "<array misal [\"A\",\"B\"] jika mode=in, atau [] jika tidak>",
    "siswa_min": "<angka minimum siswa atau null — untuk 'di atas X', 'lebih dari X'>",
    "siswa_max": "<angka maksimum siswa atau null — untuk 'di bawah X', 'kurang dari X'>",
    "sort_by":   "<siswa_total|guru_total|akreditasi|null>",
    "sort_order":"<asc|desc|null>",
    "metric_target": "<sekolah|siswa|guru|guru_pns|guru_honor|guru_sertifikasi|tendik|null>"
    "nama_sekolah": "<nama sekolah spesifik atau null>",
    "npsn": "<8 digit atau null>",
    "wilayah": {
      "provinsi": "<nama atau null>",
      "kabupaten": "<nama tanpa prefix kota/kab atau null>",
      "kecamatan": "<nama tanpa prefix kec atau null>",
      "desa": "<nama tanpa prefix desa/kel atau null>",
      "landmark": "<nama tempat rujukan atau null>"
    },
    "wilayah_bandingkan": "<nama wilayah kedua untuk comparison atau null>",
    "yayasan": "<nama yayasan/penyelenggara atau null>",
    "koordinat_user": false,
    "radius_km": "<angka atau null>"
  },
  "is_oot": false,
  "oot_reason": null
}
 
═══════════════════════════════════════════════════════════
LABEL MODULE (pilih tepat satu):
═══════════════════════════════════════════════════════════
  aggregation  → menghitung jumlah/total/ranking sekolah, guru, atau siswa
  comparison   → membandingkan DUA wilayah berbeda ATAU dua status (negeri vs swasta)
  profile      → detail/profil SATU sekolah spesifik (ada nama atau NPSN)
  list         → menampilkan daftar sekolah berdasarkan filter/area
  geo          → mencari sekolah dari POSISI USER (butuh GPS/koordinat user)
  out_of_context → pertanyaan di luar domain pendidikan Indonesia
 
═══════════════════════════════════════════════════════════
ATURAN MODULE — Urutan Prioritas:
═══════════════════════════════════════════════════════════
 
ATURAN 1 — OOT Gatekeeper (cek PERTAMA):
  Jika query tidak mengandung unsur APAPUN dari daftar berikut → is_oot: true
  Domain pendidikan mencakup: sekolah, siswa, guru/PTK, akreditasi, npsn, ppdb,
  zonasi, kurikulum, jenjang (SD/SMP/SMA/SMK/dll), tenaga pendidik, honorer,
  kepala sekolah, peserta didik, satuan pendidikan, dapodik, emis
  PENGECUALIAN: jika query campuran (OOT + pendidikan) → abaikan bagian OOT, proses bagian pendidikan
 
ATURAN 1B — LIST vs AGGREGATION (Prioritas setelah OOT):
  list JIKA query mengandung DUA atau lebih dari: 
    ["tampilkan", "daftar", "list", "sebutkan"] DAN ["urutkan", "ranking", "terbanyak", "terbesar"]
  BAHKAN JIKA ada kata "berapa" atau "total" — jika ada "urutkan", itu adalah permintaan LIST
  aggregation HANYA jika user minta angka/total/jumlah TANPA minta daftar individual
  
  Contoh:
  "tampilkan semua SMP ... urutkan dari terbanyak" → module: "list"  ← BUKAN aggregation
  "berapa total SMP ..." → module: "aggregation"

ATURAN 2 — GEO vs LIST:
  geo HANYA jika user menyebut: "dari sini", "posisi saya", "posisi ku", "koordinat",
  "radius X km", "dekat sini", "sekitar sini", "terdekat dari sini/rumah/saya"
  → Set koordinat_user: true dan radius_km jika disebutkan
  BUKAN geo jika: "terdekat dari [nama tempat/landmark]" → ini LIST + secondary: ["geo"]
  → Set landmark di wilayah.landmark
 
ATURAN 3 — COMPARISON vs AGGREGATION:
  comparison SELALU menang jika ada: bandingkan, vs, versus, dibanding, perbandingan,
  lebih banyak, mana yang lebih, mana lebih, perbedaan, selisih
  BAHKAN JIKA ada kata aggregation (jumlah/berapa/total) dalam query yang sama
  Isi wilayah_bandingkan untuk wilayah kedua

KASUS KHUSUS — COMPARISON + SPK:
  "perbandingan SPK SMA di Aceh dan Bali"
  → module: "comparison"
  → jenjang: "SPK SMA"
  → wilayah.provinsi: "aceh"
  → wilayah_bandingkan: "bali"
  Jangan salah menganggap "SPK" sebagai bagian dari wilayah.
  "SPK SMA" adalah jenjang, bukan nama tempat.
 
ATURAN 4 — PROFILE (sekolah vs vs wilayah):
  "SMAN 1 vs SMAN 2" (dua nama sekolah + vs) → profile
  "Bandung vs Bogor" (dua wilayah + vs) → comparison
  "alamat/profil/nomor telepon/website [nama sekolah]" → profile
  "apakah [nama sekolah] terakreditasi?" → profile
  NPSN (8 digit angka) selalu → profile
 
ATURAN 5 — SCOPE (selalu isi):
  nasional → tidak ada nama wilayah spesifik, atau "seluruh Indonesia", "nasional"
  provinsi → ada nama provinsi tanpa kab/kec
  kabupaten → ada nama kab/kota
  kecamatan → ada nama kecamatan
  desa → ada nama desa/kelurahan
  geo_radius → mode GPS user aktif
 
═══════════════════════════════════════════════════════════
ATURAN EKSTRAKSI ENTITAS — KRITIS:
═══════════════════════════════════════════════════════════
 
WILAYAH — Selalu strip prefix administratif:
  Input: "kabupaten tangerang" → kabupaten: "tangerang"
  Input: "kota bandung" → kabupaten: "bandung"
  Input: "kecamatan cisauk" → kecamatan: "cisauk"
  Input: "desa pagedangan" → desa: "pagedangan"
  Input: "tangsel" → kabupaten: "tangerang selatan"   (alias umum)
  Input: "jaksel" → kabupaten: "jakarta selatan"
  Input: "jakpus" → kabupaten: "jakarta pusat"
 
JENJANG — Expand singkatan:
  SMAN/SMPN/SDN/SMKN/MAN/MTSN/MIN → otomatis set status: "NEGERI" + jenjang yang tepat
  PAUD = umbrella (TK/KB/RA/SPS/TPA) → tetap tulis jenjang: "PAUD"
  SPK SD / SPK SMP / SPK SMA / SPK SMK / SPK TK:
  → tetap pertahankan prefix "SPK" (jangan dihapus/strip)
  → normalisasi ke format: "SPK <JENJANG>"
  Contoh:
    "spk sma" → jenjang: "SPK SMA"
    "sekolah spk smp" → jenjang: "SPK SMP"
  SPK TIDAK PERNAH menjadi bagian dari wilayah.
  Jika ada kata "SPK", selalu interpretasikan sebagai jenjang, bukan nama lokasi.
  Jenjang valid tambahan (gunakan persis seperti ini):
  "Kursus", "PKBM", "SKB"           → jenjang non-formal, gunakan apa adanya
  "Pondok Pesantren"                 → jenjang: "Pondok Pesantren"
  "SPK SD/SMP/SMA/SMK/TK/KB"       → tulis dengan spasi: "SPK SMA", "SPK TK", dst.
  "SMTK", "SMPTK", "SMAK"          → jenjang Kristen, gunakan apa adanya
  "SLB"                             → Sekolah Luar Biasa
  "Universitas", "Institut",
  "Politeknik", "Sekolah Tinggi"    → perguruan tinggi
STATUS:
  "negeri" / "sekolah negeri" / "sdn/smpn/sman/smkn" → status: "NEGERI"
  "swasta" / "sekolah swasta"                         → status: "SWASTA"
  Selalu output UPPERCASE untuk status. Jangan output "Negeri" atau "negeri".
AKREDITASI:
  "akreditasi A" / "terakreditasi A" → akreditasi: "A", mode: "exact"
  "akreditasi B ke atas" / "minimal akreditasi B" → akreditasi: "B", mode: "gte"
  "belum terakreditasi" / "tidak terakreditasi" / "akreditasi TT" → akreditasi: "TT", mode: "exact"
  "bukan akreditasi C" / "selain akreditasi C" → akreditasi: "C", mode: "exclude"
 
NAMA SEKOLAH — Ekstrak HANYA jika ada nama spesifik:
  Query: "profil SMAN 3 Bandung" → nama_sekolah: "SMAN 3 Bandung"
  Query: "SMK Pelita Harapan Tangerang" → nama_sekolah: "SMK Pelita Harapan"
  Query: "berapa SMK di Bandung" → nama_sekolah: null  (tidak ada nama spesifik)
 
YAYASAN/PENYELENGGARA:
  "milik yayasan Al-Azhar" → yayasan: "Al-Azhar"
  "sekolah penyelenggara Muhammadiyah" → yayasan: "Muhammadiyah"
  "sekolah Kristen" / "sekolah Katolik" → yayasan: "Kristen" / "Katolik"
  Jika tidak disebutkan → yayasan: null
  
FILTER NUMERIK:
  "di atas 500 orang" / "lebih dari 500 siswa" → siswa_min: 500
  "di bawah 200 siswa" / "kurang dari 200 orang" → siswa_max: 200
  "antara 300 dan 600 siswa" → siswa_min: 300, siswa_max: 600
 
SORTING:
  "urutkan dari terbanyak siswa" / "dari yang paling banyak siswanya" → sort_by: "siswa_total", sort_order: "desc"
  "urutkan dari paling sedikit" → sort_by: "siswa_total", sort_order: "asc"
  METRIC_TARGET — Apa yang ingin dihitung/dijumlah:
  "berapa siswa" / "jumlah murid" / "total peserta didik" / "jumlah siswa"
    → metric_target: "siswa"
  "berapa guru" / "jumlah PTK" / "jumlah tenaga pendidik" / "total guru"
    → metric_target: "guru"
  "guru PNS" / "jumlah guru PNS" / "PNS saja"
    → metric_target: "guru_pns"
  "guru honorer" / "guru honor" / "tenaga honor"
    → metric_target: "guru_honor"
  "guru bersertifikasi" / "guru sertifikasi"
    → metric_target: "guru_sertifikasi"
  "tenaga kependidikan" / "tendik"
    → metric_target: "tendik"
  "berapa sekolah" / "jumlah sekolah" / tidak ada target spesifik
    → metric_target: null   ← default, berarti hitung sekolah
  
  PENTING: metric_target HANYA diisi saat module=aggregation atau module=comparison.
  Untuk module=profile, metric_target selalu null.
═══════════════════════════════════════════════════════════
SCOPE NASIONAL — Aturan Deteksi:
═══════════════════════════════════════════════════════════
 
  scope: "nasional" JIKA:
  - Tidak ada nama wilayah apapun di query
  - Ada kata: "nasional", "seluruh indonesia", "se-indonesia", "indonesia"
  - Query: "berapa total SMK di Indonesia?" → nasional
  - Query: "berapa SMK akreditasi A?" (tanpa wilayah) → nasional
  scope bukan nasional jika ada nama kota/kab/prov/kec/desa apapun
  
═══════════════════════════════════════════════════════════
ATURAN KRITIS — NAMA SEKOLAH vs WILAYAH (ANTI-KONFUSI):
═══════════════════════════════════════════════════════════
 
PRIORITAS RESOLUSI ENTITAS — urutan wajib:
  1. Jika ada NPSN (8 digit) → nama_sekolah: null, npsn: <8digit>, wilayah dari konteks
  2. Jika ada pola [JENJANG][NOMOR] [NAMA KOTA] → nama_sekolah: fullname, wilayah: kota itu
     Contoh: "SMAN 3 Bandung" → nama_sekolah: "SMAN 3 Bandung", kabupaten: "bandung"
  3. Nama wilayah TIDAK BOLEH masuk ke nama_sekolah. nama_sekolah hanya berisi nama institusi.
 
JENJANG MULTI — jika query menyebut >1 jenjang:
  "sd, smp, sma di ..." → jenjang: null (tidak isi), jenjang_list: ["SD","SMP","SMA"]
  SCHEMA UPDATE: tambah field "jenjang_list": ["<jenjang1>","<jenjang2>"] ke entities
 
AKREDITASI — bedakan pola ini:
  "akreditasi A dan B" → akreditasi: "B", akreditasi_mode: "gte"  (A dan B = B ke atas)
  "akreditasi A atau B" → akreditasi: null, akreditasi_mode: "in", akreditasi_list: ["A","B"]
  "akreditasi A dan B"  → akreditasi: "B", akreditasi_mode: "gte", akreditasi_list: []
  "hanya akreditasi A"  → akreditasi: "A", akreditasi_mode: "exact", akreditasi_list: []
  "hanya akreditasi A" → akreditasi: "A", akreditasi_mode: "exact"
 
WILAYAH — JANGAN ekstrak kata benda umum sebagai wilayah:
  "bahan bangunan" → wilayah: null  (bangunan = kata benda, bukan nama desa)
  "sekolah di pantai" → wilayah: null  (pantai = kata benda umum)
  "sekolah di Pantai Indah Kapuk" → landmark: "Pantai Indah Kapuk"  (nama proper)
  Ciri nama wilayah valid: diawali huruf kapital ATAU didahului kata "di/dari/kec./kab./prov."
 
OOT SUPER-CEPAT — langsung is_oot: true tanpa analisis lebih jika query berisi:
  harga, berapa ribu, burger, makanan, minuman, resep, cuaca, presiden (non-edu context),
  saham, cryptocurrency, liga, bola, pertandingan, film, musik, hotel, wisata
  DAN tidak ada satupun sinyal pendidikan.
  
═══════════════════════════════════════════════════════════
ATURAN SECONDARY:
═══════════════════════════════════════════════════════════
  Isi secondary HANYA jika user EKSPLISIT minta dua hal ("tampilkan juga", "dan lihat juga")
  Maksimal 2 label secondary.
 
═══════════════════════════════════════════════════════════
FORMAT OOT:
═══════════════════════════════════════════════════════════
  Jika is_oot: true → isi oot_reason dengan kategori singkat:
  "politik", "ekonomi_keuangan", "hiburan", "kesehatan", "cuaca_bencana",
  "pertanian", "negara_asing", "definisi_umum"
 
═══════════════════════════════════════════════════════════
CONTOH:
═══════════════════════════════════════════════════════════
 
Query: "bandingkan jumlah SMK di Bandung dan Bogor, tampilkan juga daftarnya"
{"module":"comparison","secondary":["list"],"scope":"kabupaten","entities":{"jenjang":"SMK","status":null,"akreditasi":null,"akreditasi_mode":null,"nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":"bandung","kecamatan":null,"desa":null,"landmark":null},"wilayah_bandingkan":"bogor","yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}
 
Query: "profil SMAN 3 Bandung"
{"module":"profile","secondary":[],"scope":"kabupaten","entities":{"jenjang":"SMA","status":"NEGERI","akreditasi":null,"akreditasi_mode":null,"nama_sekolah":"SMAN 3 Bandung","npsn":null,"wilayah":{"provinsi":null,"kabupaten":"bandung","kecamatan":null,"desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}
 
Query: "sekolah dalam radius 2 km dari posisi saya"
{"module":"geo","secondary":[],"scope":"geo_radius","entities":{"jenjang":null,"status":null,"akreditasi":null,"akreditasi_mode":null,"nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":null,"kecamatan":null,"desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":true,"radius_km":2},"is_oot":false,"oot_reason":null}
 
Query: "daftar SMA terdekat dari Monas"
{"module":"list","secondary":["geo"],"scope":"kabupaten","entities":{"jenjang":"SMA","status":null,"akreditasi":null,"akreditasi_mode":null,"nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":null,"kecamatan":null,"desa":null,"landmark":"Monas"},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}
 
Query: "SMK milik yayasan Muhammadiyah di Surabaya belum terakreditasi"
{"module":"list","secondary":[],"scope":"kabupaten","entities":{"jenjang":"SMK","status":null,"akreditasi":"TT","akreditasi_mode":"exact","nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":"surabaya","kecamatan":null,"desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":"Muhammadiyah","koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}
 
Query: "berapa SMK di seluruh Indonesia akreditasi A?"
{"module":"aggregation","secondary":[],"scope":"nasional","entities":{"jenjang":"SMK","status":null,"akreditasi":"A","akreditasi_mode":"exact","nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":null,"kecamatan":null,"desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}
 
Query: "tampilkan semua smp negeri di kecamatan lowokwaru kota malang akreditasi a siswa di atas 500"
{"module":"list","secondary":[],"scope":"kecamatan","entities":{"jenjang":"SMP","status":"NEGERI","akreditasi":"A","akreditasi_mode":"exact","nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":"malang","kecamatan":"lowokwaru","desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}
 
Query: "berapa total sd smp sma smk di kabupaten sleman terakreditasi b ke atas dan persentasenya"
{"module":"aggregation","secondary":[],"scope":"kabupaten","entities":{"jenjang":null,"status":null,"akreditasi":"B","akreditasi_mode":"gte","nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":"sleman","kecamatan":null,"desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}
 
Query: "sekolah dengan akreditasi a dan b di kabupaten sleman ada berapa"
{"module":"aggregation","secondary":[],"scope":"kabupaten","entities":{"jenjang":null,"status":null,"akreditasi":"B","akreditasi_mode":"gte","nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":"sleman","kecamatan":null,"desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}
 
Query: "tampilkan semua smp negeri di kecamatan lowokwaru kota malang akreditasi a siswa di atas 500 urutkan dari terbanyak"
{"module":"list","secondary":[],"scope":"kecamatan","entities":{"jenjang":"SMP","status":"NEGERI","akreditasi":"A","akreditasi_mode":"exact","nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":"malang","kecamatan":"lowokwaru","desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}
 
Query: "berapa total sd smp sma smk di kabupaten sleman terakreditasi b ke atas dan persentasenya"
{"module":"aggregation","secondary":[],"scope":"kabupaten","entities":{"jenjang":null,"status":null,"akreditasi":"B","akreditasi_mode":"gte","nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":"sleman","kecamatan":null,"desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}
 
Query: "sekolah akreditasi a dan b di kabupaten sleman ada berapa"
{"module":"aggregation","secondary":[],"scope":"kabupaten","entities":{"jenjang":null,"status":null,"akreditasi":"B","akreditasi_mode":"gte","nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":"sleman","kecamatan":null,"desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}

Query: "berapa pondok pesantren di Jawa Timur"
{"module":"aggregation","secondary":[],"scope":"provinsi","entities":{"jenjang":"Pondok Pesantren","status":null,"akreditasi":null,"akreditasi_mode":null,"siswa_min":null,"siswa_max":null,"sort_by":null,"sort_order":null,"nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":"jawa timur","kabupaten":null,"kecamatan":null,"desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}

Query: "daftar PKBM di Bandung"
{"module":"list","secondary":[],"scope":"kabupaten","entities":{"jenjang":"PKBM","status":null,"akreditasi":null,"akreditasi_mode":null,"siswa_min":null,"siswa_max":null,"sort_by":null,"sort_order":null,"nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":"bandung","kecamatan":null,"desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}

Query: "berapa total sd smp sma smk di kabupaten sleman terakreditasi b ke atas"
{"module":"aggregation","secondary":[],"scope":"kabupaten","entities":{"jenjang":null,"jenjang_list":["SD","SMP","SMA","SMK"],"status":null,"akreditasi":"B","akreditasi_mode":"gte","siswa_min":null,"siswa_max":null,"sort_by":null,"sort_order":null,"nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":"sleman","kecamatan":null,"desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}

Query: "daftar sd dan smp negeri di kecamatan lowokwaru"
{"module":"list","secondary":[],"scope":"kecamatan","entities":{"jenjang":null,"jenjang_list":["SD","SMP"],"status":"NEGERI","akreditasi":null,"akreditasi_mode":null,"siswa_min":null,"siswa_max":null,"sort_by":null,"sort_order":null,"nama_sekolah":null,"npsn":null,"wilayah":{"provinsi":null,"kabupaten":null,"kecamatan":"lowokwaru","desa":null,"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,"radius_km":null},"is_oot":false,"oot_reason":null}

Balas HANYA dengan JSON valid. Tanpa markdown, tanpa penjelasan.
"""
 
 
# ════════════════════════════════════════════════════════════════
# KEYWORD FALLBACK (saat LLM tidak tersedia)
# ════════════════════════════════════════════════════════════════
 
_OOC_STRONG = {
    "presiden", "gubernur", "bupati", "walikota", "pilpres", "pilkada",
    "saham", "ihsg", "bitcoin", "crypto", "forex", "investasi",
    "film", "netflix", "spotify", "resep", "masakan", "traveling", "hotel",
    "cuaca", "gempa", "tsunami", "covid", "vaksin", "dokter", "rumah sakit",
    "israel", "palestina", "korea", "inggris", "amerika",
}
 
_SCHOOL_SIGNALS = {
    "sekolah", "pendidikan", "siswa", "guru", "kelas", "kurikulum",
    "sd", "smp", "sma", "smk", "mi", "mts", "ma", "tk", "paud", "slb",
    "sman", "smpn", "sdn", "smkn", "man", "mtsn", "min",
    "npsn", "akreditasi", "ppdb", "zonasi", "honorer",
}
 
_WILAYAH_ALIAS = {
    "tangsel": "tangerang selatan",
    "jaksel": "jakarta selatan",
    "jakbar": "jakarta barat",
    "jakpus": "jakarta pusat",
    "jakut": "jakarta utara",
    "jakti": "jakarta timur",
    "jogja": "yogyakarta",
    "solo": "surakarta",
}
 
_AKRED_RE = re.compile(
    r'\b(?:akreditasi|terakreditasi|berakreditasi|terkareditasi|'
    r'terakred|akred)\w*\s+([abc])\b',
    re.I
)
_JENJANG_RE = re.compile(
    r'\b(sd|smp|sma|smk|mi|mts|ma|tk|paud|kb|ra|sps|tpa|paudq|pkbm|skb|kursus|slb)\b'
    r'|(?:pondok\s+pesantren|pesantren|pondok|universitas|institut|politeknik|akademi|sekolah\s+tinggi)',
    re.I
)
_STATUS_RE  = re.compile(r'\b(negeri|swasta)\b', re.I)
_NPSN_RE    = re.compile(r'\b(\d{8})\b')
_YAYASAN_RE = re.compile(
    r'\b(?:milik|penyelenggara|yayasan|naungan)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b',
    re.I
)
_NAMED_SCHOOL_RE = re.compile(
    r'\b(sman|smpn|sdn|smkn|man|mtsn|min|tkn)\b', re.I
)
_NAMED_SCHOOL_MAP = {
    "sman": ("SMA", "NEGERI"), "smpn": ("SMP", "NEGERI"),
    "sdn":  ("SD",  "NEGERI"), "smkn": ("SMK", "NEGERI"),
    "man":  ("MA",  "NEGERI"), "mtsn": ("MTs", "NEGERI"),
    "min":  ("MI",  "NEGERI"), "tkn":  ("TK",  "NEGERI"),
}
 
def _keyword_fallback(query: str) -> CognitiveIntent:
    """
    Keyword fallback sederhana — digunakan saat LLM tidak tersedia.
    Tidak perlu se-komprehensif dulu karena LLM seharusnya handle 95%+ kasus.
    """
    q      = query.lower()
    tokens = set(q.split())
    import re as _re
 
    # Multi-jenjang detection
    _JENJANG_MAP = {
        "sd": "SD", "mi": "MI", "smp": "SMP", "mts": "MTs",
        "sma": "SMA", "smk": "SMK", "ma": "MA",
        "tk": "TK", "paud": "PAUD", "slb": "SLB",
        "kb": "KB", "ra": "RA", "sps": "SPS", "tpa": "TPA",
        "paudq": "PAUDQ", "pkbm": "PKBM", "skb": "SKB",
        "kursus": "Kursus",
        "pesantren": "Pondok Pesantren",
        "pondok pesantren": "Pondok Pesantren",
        "pondok": "Pondok Pesantren",
        "universitas": "Universitas",
        "institut": "Institut",
        "politeknik": "Politeknik",
        "akademi": "Akademi",
        "sekolah tinggi": "Sekolah Tinggi",
        # Prioritas tinggi
        "slb":              "SLB",
        "luar biasa":       "SLB",
        "akademik":         "Akademik",
        "spk":              "SPK SMA",   # default SPK tanpa suffix → SMA
        "spk sd":           "SPK SD",
        "spk smp":          "SPK SMP",
        "spk sma":          "SPK SMA",
        "spk smk":          "SPK SMK",
        "spk tk":           "SPK TK",
        "spk kb":           "SPK KB",
        "smtk":             "SMTK",
        "smptk":            "SMPTK",
        "spm ulya":         "SPM Ulya",
        "spm wustha":       "SPM Wustha",
        "spm ula":          "SPM Ula",
        "pratama widyalaya":"Pratama Widyalaya",
        # Prioritas medium
        "sdtk":             "SDTK",
        "taman seminari":   "Taman Seminari",
        "seminari":         "Taman Seminari",
        "smak":             "SMAK",
        "smag":             "SMAg.K",
        "akademi komunitas":"Akademi Komunitas",
        "nava dhammasekha": "Nava Dhammasekha",
        "pdf ulya":         "PDF Ulya",
        "pdf wustha":       "PDF Wustha",
        "pdf":              "PDF Ulya",   # ambiguous, default ulya
        "widyalaya":        "Pratama Widyalaya",  # default paling umum
        "mak":              "MAK",
    }
 
    def _match_jenjang_key(key: str, text: str) -> bool:
        """Handle single-word (\\b) dan multi-word (spasi) key."""
        if ' ' in key:
            # Multi-word: cukup substring setelah normalisasi spasi
            return key in text
        return bool(_re.search(r'\b' + _re.escape(key) + r'\b', text))

    _found_jenjang = [
        v for k, v in _JENJANG_MAP.items()
        if _match_jenjang_key(k, q)
    ]
 
    _jenjang_single = _found_jenjang[0] if len(_found_jenjang) == 1 else None
    _jenjang_multi  = _found_jenjang    if len(_found_jenjang) > 1 else []
    has_ooc    = any(w in q for w in _OOC_STRONG)
    has_school = (
        any(w in tokens for w in _SCHOOL_SIGNALS)
        or bool(re.search(r'\b(?:sd|smp|sma|smk|mi|mts|ma|tk)n?\b', q))
        or any(_match_jenjang_key(k, q) for k in _JENJANG_MAP)
    )
 
    if has_ooc and not has_school:
        return CognitiveIntent(module="out_of_context", is_oot=True)
 
    # Ekstrak entitas dasar (ENHANCED AKREDITASI)
    _akred = None
    _akred_mode = None
 
    # Pola GTE akreditasi (B ke atas, minimal B, dst)
    _akred_gte_pattern = _re.compile(
        r'\b(akreditasi|terakreditasi|berakreditasi)\s+([abc])\s+(ke\s+atas|keatas|dan\s+di\s+atas|minimal|minimum)',
        _re.IGNORECASE
    )
 
    # Pola "A dan B"
    _akred_ab_pattern = _re.compile(
        r'\bakreditasi\s+[ab]\s+dan\s+[ab]\b',
        _re.IGNORECASE
    )
 
    m_gte = _akred_gte_pattern.search(q)
    if m_gte:
        _akred = m_gte.group(2).upper()
        _akred_mode = "gte"
    elif _akred_ab_pattern.search(q):
        _akred = "B"  # Ambil yang paling rendah → include A & B
        _akred_mode = "gte"
    elif _re.search(r'\bakreditasi\s+([abc])\b', q):
        m = _re.search(r'\bakreditasi\s+([abc])\b', q)
        _akred = m.group(1).upper()
        _akred_mode = "exact"
 
    # fallback ke regex lama kalau belum kena
    if not _akred:
        m = _AKRED_RE.search(q)
        if m:
            _akred = m.group(1).upper()
            _akred_mode = "exact"
 
    akreditasi = _akred
 
    jenjang = status = None
 
    m = _NAMED_SCHOOL_RE.search(q)
    if m:
        jenjang, status = _NAMED_SCHOOL_MAP[m.group(1).lower()]
    else:
        # Gunakan hasil multi-jenjang detection
        if _jenjang_single:
            jenjang = _jenjang_single
        else:
            jenjang = None  # multi → kosongkan
 
        # Status tetap pakai regex lama
        m = _STATUS_RE.search(q)
        if m:
            status = m.group(1).upper()
 
    npsn = None
    m = _NPSN_RE.search(q)
    if m: npsn = m.group(1)
 
    yayasan = None
    m = _YAYASAN_RE.search(query)
    if m: yayasan = m.group(1).strip()
 
    # ── Deteksi secondary (eksplisit double-intent) ──────────────────────────
    _SECONDARY_LIST_SIGNALS = (
        "sebutkan juga", "tampilkan juga", "dan tampilkan", "tampilkan daftar",
        "daftar nama", "nama-namanya", "dan lihat juga", "cari juga",
        "lokasinya", "daftarnya", "dan daftar",
    )
    _SECONDARY_GEO_SIGNALS = (
        "terdekat dari", "terdekat di",
    )
    secondary = []
    for sig in _SECONDARY_LIST_SIGNALS:
        if sig in q:
            secondary.append("list")
            break
    for sig in _SECONDARY_GEO_SIGNALS:
        if sig in q and "dari sini" not in q and "posisi" not in q and "koordinat" not in q:
            if "secondary" not in locals() or "geo" not in secondary:
                secondary.append("geo")
            break
 
    # ── Routing utama ────────────────────────────────────────────────────────
    # Prioritas 1: nama sekolah spesifik (SMPN 5, SDN 02, ...) atau NPSN
    _has_named_school = bool(npsn or re.search(r'\b(?:sd|smp|sma|smk|mi|mts|ma|tk)n?\s*\d+\b', q))
 
    # Prioritas 2: query dengan nama sekolah non-numbered + kata profil/alamat/lokasi
    _PROFILE_SIGNALS = ("profil ", "profile ", "alamat ", "lokasi", "lokasinya", "di mana", "dimana", "terakreditasi", "akreditasi?")
    _has_profile_signal = any(sig in q for sig in _PROFILE_SIGNALS)
 
    # Dua nama sekolah bernomor + vs → profile (school vs school)
    _named_school_count = len(re.findall(r'\b(?:sd|smp|sma|smk|mi|mts|ma|tk)n?\s*\d+\b', q))
 
    if _has_named_school:
        module = "profile"
    elif _has_profile_signal and jenjang and nama_sekolah_hint(q):
        # Ada jenjang + sinyal profil + nama sekolah non-numbered → profile
        module = "profile"
    elif any(w in q for w in ("bandingkan", "versus", "dibanding", "perbandingan", "perbedaan", "selisih", "mana yang lebih", "mana lebih", "lebih banyak")):
        module = "comparison"
    elif "vs" in q.split():
        # "vs" standalone — cek apakah dua nama sekolah spesifik
        module = "comparison"
    elif any(w in q for w in ("berapa", "jumlah", "total")) or any(
        abbr in q for abbr in ("jmlh", "jml ", "jml.")
    ) or re.search(r'\bpaling\s+banyak\b', q) or re.search(r'\bbanyak\s+sekolah\b', q):
        module = "aggregation"
    elif any(w in q for w in ("terdekat dari sini", "dari sini", "posisi saya", "posisi saya sekarang", "koordinat_user")) or \
         re.search(r'\bradius\b', q) or \
         re.search(r'\bkoordinat\b', q):
        module = "geo"
    elif any(w in q for w in ("daftar", "list", "tampilkan", "cari", "apa saja")):
        module = "list"
    elif has_school:
        module = "list"
    else:
        return CognitiveIntent(module="out_of_context", is_oot=True)
 
    # Hapus secondary yang sama dengan primary
    secondary = [s for s in secondary if s != module]
 
    return CognitiveIntent(
        module=module,
        secondary=secondary,
        akreditasi=akreditasi,
        akreditasi_mode=_akred_mode,
        jenjang=jenjang,
        jenjang_list=_jenjang_multi,
        status=status,
        npsn=npsn,
        yayasan=yayasan,
    )
 
 
_GENERIC_AFTER_JENJANG = re.compile(
    r'\b(?:sd|smp|sma|smk|mi|mts|ma|tk)\s+'
    r'(?:negeri|swasta|akreditasi|dan|atau|di|yang|apa|ini|itu|dengan|dalam|untuk|dari|vs|versus)\b',
    re.IGNORECASE
)
 
def nama_sekolah_hint(q: str) -> bool:
    """Cek apakah ada indikasi nama sekolah non-numbered (misal 'SMK Pelita Harapan').
    Jenjang harus diikuti nama proper, bukan kata umum/filter.
    """
    # Jika jenjang hanya diikuti kata-kata generik, bukan nama sekolah
    if _GENERIC_AFTER_JENJANG.search(q):
        return False
    # Jenjang diikuti huruf (nama proper)
    return bool(re.search(
        r'\b(?:sd|smp|sma|smk|mi|mts|ma|tk)\s+[A-Za-z]',
        q, re.IGNORECASE
    ))
 
def _safe_float(v) -> Optional[float]:
    """Konversi nilai radius_km dari LLM output dengan aman."""
    if v is None or v in ("null", "", "None", "0"):
        return None
    try:
        result = float(v)
        return result if result > 0 else None
    except (ValueError, TypeError):
        return None
    
def _safe_int(v) -> Optional[int]:
    if v is None or v in ("null", "", "None"):
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None
# ════════════════════════════════════════════════════════════════
# LLM OUTPUT PARSER
# ════════════════════════════════════════════════════════════════

def _parse_llm_output(raw: str) -> Optional[CognitiveIntent]:
    if not raw:
        return None
    try:
        cleaned = re.sub(r'```(?:json)?\s*|\s*```', '', raw, flags=re.I).strip()
        m = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if not m:
            return None
        obj = json.loads(m.group(0))

        module = obj.get("module", "").lower()
        if module not in VALID_MODULES:
            return None

        _scope_raw = obj.get("scope") or "nasional"   # handle null → default
        scope = str(_scope_raw).lower().strip()
        if scope not in VALID_SCOPES:
            scope = "nasional"

        secondary = [
            s.lower() for s in (obj.get("secondary") or [])
            if isinstance(s, str) and s.lower() in VALID_MODULES
        ]

        ent = obj.get("entities", {}) or {}
        wil = ent.get("wilayah", {}) or {}

        def _norm(v):
            if v in (None, "null", "", "None"):
                return None
            return str(v).strip() or None

        # TAMBAHAN: ambil akreditasi_list dari LLM
        _akr_list = ent.get("akreditasi_list", [])
        if isinstance(_akr_list, list):
            akreditasi_list = [str(x).upper() for x in _akr_list if x]
        else:
            akreditasi_list = []

        return CognitiveIntent(
            module=module,
            secondary=secondary,
            scope=scope,
            jenjang=_norm(ent.get("jenjang")),
            jenjang_list=[
                j for j in (ent.get("jenjang_list") or [])
                if isinstance(j, str) and j.strip()
            ],
            status=_norm(ent.get("status")),
            akreditasi=_norm(ent.get("akreditasi")),
            akreditasi_mode=_norm(ent.get("akreditasi_mode")),

            # TAMBAHAN FIELD BARU
            akreditasi_list=akreditasi_list,

            nama_sekolah=_norm(ent.get("nama_sekolah")),
            npsn=_norm(ent.get("npsn")),
            wilayah=WilayahEntities(
                provinsi=_norm(wil.get("provinsi")),
                kabupaten=_norm(wil.get("kabupaten")),
                kecamatan=_norm(wil.get("kecamatan")),
                desa=_norm(wil.get("desa")),
                landmark=_norm(wil.get("landmark")),
            ),
            wilayah_bandingkan=_norm(ent.get("wilayah_bandingkan")),
            yayasan=_norm(ent.get("yayasan")),
            koordinat_user=bool(ent.get("koordinat_user", False)),
            radius_km=_safe_float(ent.get("radius_km")),
            metric_target=_norm(ent.get("metric_target")),
            siswa_min=_safe_int(ent.get("siswa_min")),
            siswa_max=_safe_int(ent.get("siswa_max")),
            sort_by=_norm(ent.get("sort_by")),
            sort_order=_norm(ent.get("sort_order")),
            is_oot=bool(obj.get("is_oot", False)),
            oot_reason=_norm(obj.get("oot_reason")),
            raw_label=raw[:80],
        )
    except Exception as e:
        logger.warning("[CognitiveRouter] parse error: %s | raw=%r", e, raw[:60])
        return None
 
# PATCH: typo normalization untuk cache key
_TYPO_MAP = {
    r"\bbrapa\b":    "berapa",
    r"\bskla[h]?\b": "sekolah",
    r"\bseklah\b":   "sekolah",
    r"\bsekoah\b":   "sekolah",
    r"\bsekolha\b":  "sekolah",
    r"\bnegri\b":    "negeri",
    r"\bnegrei\b":   "negeri",
    r"\bjmlh\b":     "jumlah",
    r"\bjmlah\b":    "jumlah",
    r"\btampilan\b": "tampilkan",
    r"\bdafatra\b":  "daftar",
    r"\bdafrtar\b":  "daftar",
    r"\bprovinsi\b": "provinsi",  # sudah benar tapi include sebagai anchor
    r"\bpropinsi\b": "provinsi",
    r"\bpropinsi\b": "provinsi",
    r"\bkabupetan\b": "kabupaten",
    r"\bkabupetan\b": "kabupaten",
}

def _canonical_query(q: str) -> str:
    """Normalisasi typo umum untuk cache key — jangan ubah query asli ke LLM."""
    q = q.lower().strip()
    q = re.sub(r"\s+", " ", q)
    for pattern, replacement in _TYPO_MAP.items():
        q = re.sub(pattern, replacement, q, flags=re.IGNORECASE)
    return q


def _cache_key(q: str) -> str:
    h = hashlib.md5(_canonical_query(q).encode()).hexdigest()
    return f"cognitive_router:v2:{h}"
# ════════════════════════════════════════════════════════════════
# MAIN CLASSIFIER
# ═══════════════════════════════════════════════════════════════
 
async def classify_and_extract(
    query:     str,
    use_cache: bool  = True,
    timeout:   float = _LLM_TIMEOUT,
) -> CognitiveIntent:
    """
    Entry point utama.
    Returns CognitiveIntent yang berisi module + semua entitas terekstrak.
    Tidak perlu memanggil smart_query_parser lagi setelah ini.
    """
    ck = _cache_key(query)
 
    # Cache read
    if use_cache:
        try:
            cached = await get_cache(ck)
            if cached:
                cached_copy = dict(cached)
                wil_data = cached_copy.pop("wilayah", {})
                intent = CognitiveIntent(**cached_copy)
                intent.wilayah = WilayahEntities(**wil_data)
                return intent
        except Exception:
            pass
 
    # LLM call
    intent: Optional[CognitiveIntent] = None
    try:
        prompt = _SYSTEM_PROMPT + "\n\nQuery: " + query
        raw = await asyncio.wait_for(
            call_deepseek(prompt, max_tokens=500, temperature=0.0),
            timeout,
        )
        intent = _parse_llm_output(raw)
    except Exception as e:
        logger.warning("[CognitiveRouter] LLM call failed: %s", e)
 
    # Keyword fallback
    if intent is None:
        logger.info("[CognitiveRouter] Using keyword fallback for: %r", query[:60])
        intent = _keyword_fallback(query)
 
    # Cache write
    if use_cache:
        try:
            cache_data = {**intent.__dict__}
            cache_data["wilayah"] = intent.wilayah.__dict__
            await set_cache(ck, cache_data, _CACHE_TTL)
        except Exception:
            pass
 
    return intent
 
 
async def classify_intent(
    query: str,
    use_cache: bool = True,
    timeout: float = _LLM_TIMEOUT,
) -> "CognitiveIntent":
    """
    Public alias untuk classify_and_extract.
    Nama ini digunakan oleh rag_server.py agar kompatibel
    dengan bekas llm_intent_classifier.
    """
    return await classify_and_extract(query, use_cache=use_cache, timeout=timeout)
 
 
def is_education_query(query: str) -> bool:
    """
    Shim pengganti utils.domain_filter.is_education_query.
    Menggunakan rule-based classifier — sinkron, tanpa LLM.
    Mengembalikan True jika query terdeteksi sebagai SCHOOL atau AMBIGUOUS.
    """
    from app.services.core.intent_classifier import (
        classify_intent_rule as _rule_classify,
        IntentType,
    )
    result = _rule_classify(query)
    return result != IntentType.OUT_OF_CONTEXT
