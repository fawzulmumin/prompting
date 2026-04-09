# services/core/response_dispatcher.py (FILE BARU)
"""
Memutuskan apakah response diformat via template Python
atau dikirim ke narrator LLM.

ATURAN BYPASS NARRATOR:
  1. Aggregation dengan hasil tunggal (angka count saja)    → template
  2. Profile dengan 1 dokumen ditemukan, tidak ambigu       → template
  3. List dengan 0 hasil                                    → template (not found)
  4. OOT                                                    → template (sudah ada)
  5. Semua kasus lain                                       → narrator LLM
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from app.services.core.cognitive_router import CognitiveIntent


# ═══════════════════════════════════════════════════════════════
# THRESHOLD DECISION
# ═══════════════════════════════════════════════════════════════

@dataclass
class DispatchDecision:
    use_narrator: bool
    reason: str          # untuk logging/debugging


def should_use_narrator(
    module: str,
    es_result: Dict[str, Any],
    intent: CognitiveIntent,
    secondary_modules: List[str],
) -> DispatchDecision:
    """
    Evaluasi apakah perlu memanggil narrator LLM.
    Return DispatchDecision dengan alasan keputusan.
    """
    
    # Selalu pakai narrator jika ada secondary module
    # (konten gabungan membutuhkan narasi kohesif)
    if secondary_modules:
        return DispatchDecision(True, "multi_module_always_narrate")
    
    # ── AGGREGATION ────────────────────────────────────────────
    if module == "aggregation":
        # aggregation_handler menyimpan breakdown langsung di root result
        breakdown = {
            k: v for k, v in es_result.items()
            if k.startswith("by_") and isinstance(v, dict)
        }
        # Fallback: cek key "breakdown" jika ada
        if not breakdown:
            breakdown = es_result.get("breakdown", {})

        is_nasional = es_result.get("scope") == "nasional"

        if not breakdown and not is_nasional:
            return DispatchDecision(False, "agg_simple_count")

        total_unique = sum(len(v) for v in breakdown.values() if isinstance(v, dict))
        if total_unique <= 8 and not is_nasional:
            return DispatchDecision(False, "agg_short_breakdown")

        return DispatchDecision(True, "agg_complex_breakdown")
    
    # ── PROFILE ────────────────────────────────────────────────
    if module == "profile":
        ambiguity = es_result.get("ambiguity_type")
        
        if ambiguity == "not_found":
            return DispatchDecision(False, "profile_not_found")
        
        if ambiguity == "ambiguous":
            # Perlu tanya user → template klarifikasi
            return DispatchDecision(False, "profile_needs_clarification")
        
        # Single result yang jelas → template detail
        # (data sekolah memiliki field terstruktur yang bisa di-render)
        return DispatchDecision(False, "profile_single_doc")
    
    # ── LIST ───────────────────────────────────────────────────
    if module == "list":
        num_results = es_result.get("total_hits", 0)
        
        if num_results == 0:
            return DispatchDecision(False, "list_empty")
        
        # List pendek (≤5) → template card
        if num_results <= 5:
            return DispatchDecision(False, "list_short")
        
        # List panjang (>5) dengan filter kompleks → narrator analisis
        has_complex_filter = (
            intent.yayasan or 
            intent.akreditasi_mode in ("gte", "exclude") or
            intent.scope == "nasional"
        )
        if has_complex_filter:
            return DispatchDecision(True, "list_complex_filter")
        
        # List standar panjang → template tabel
        return DispatchDecision(False, "list_table")
    
    # ── COMPARISON ─────────────────────────────────────────────
    if module == "comparison":
        # Comparison selalu butuh narasi interpretasi
        return DispatchDecision(True, "comparison_always_narrate")
    
    # ── GEO ────────────────────────────────────────────────────
    if module == "geo":
        num_results = es_result.get("total_hits", 0)
        if num_results == 0:
            return DispatchDecision(False, "geo_empty")
        # Geo dengan distance info → template card dengan peta
        return DispatchDecision(False, "geo_results")
    
    # Default: pakai narrator
    return DispatchDecision(True, "default_fallback")


# ═══════════════════════════════════════════════════════════════
# TEMPLATE RENDERER — satu fungsi per modul
# ═══════════════════════════════════════════════════════════════

def render_template(
    module: str,
    es_result: Dict[str, Any],
    intent: CognitiveIntent,
    decision_reason: str,
) -> str:
    """
    Format respons langsung dari data ES tanpa memanggil LLM.
    Output: markdown string yang langsung dikirim ke user.
    """
    
    renderers = {
        "aggregation": _render_aggregation,
        "profile":     _render_profile,
        "list":        _render_list,
        "geo":         _render_geo,
    }
    
    renderer = renderers.get(module)
    if not renderer:
        return "Maaf, tidak dapat memformat hasil untuk modul ini."
    
    return renderer(es_result, intent, decision_reason)


def _render_aggregation(es_result: dict, intent: CognitiveIntent, reason: str) -> str:
    total     = es_result.get("total", 0)
    jenjang   = intent.jenjang or "sekolah"
    wilayah   = _format_wilayah(intent)
    akreditasi = f" akreditasi {intent.akreditasi}" if intent.akreditasi else ""
    status    = f" {intent.status.lower()}" if intent.status else ""
    breakdown = es_result.get("breakdown", {})
    
    # Simple count — satu kalimat
    if reason == "agg_simple_count":
        lokasi_str = f"di {wilayah}" if wilayah else "secara nasional"
        return (
            f"Terdapat **{total:,} {jenjang}{status}{akreditasi}** "
            f"{lokasi_str}."
        )

    # Baris breakdown header juga:
    lines = [
        f"Terdapat **{total:,} {jenjang}{status}{akreditasi}** "
        f"{'di ' + wilayah if wilayah else 'secara nasional'}.\n"
    ]
    
    if "by_akreditasi" in breakdown:
        lines.append("\n| Akreditasi | Jumlah |")
        lines.append("|---|---|")
        for k, v in sorted(breakdown["by_akreditasi"].items()):
            lines.append(f"| {k or '-'} | {v:,} |")
    
    if "by_status" in breakdown:
        lines.append("\n| Status | Jumlah |")
        lines.append("|---|---|")
        for k, v in sorted(breakdown["by_status"].items()):
            lines.append(f"| {k or '-'} | {v:,} |")
    
    lines.append(f"\n*Data per {es_result.get('tahun', 'tahun terbaru')}.*")
    return "\n".join(lines)


def _render_profile(es_result: dict, intent: CognitiveIntent, reason: str) -> str:
    
    if reason == "profile_not_found":
        nama = intent.nama_sekolah or "Sekolah yang dicari"
        wilayah = _format_wilayah(intent)
        return (
            f"**{nama}** tidak ditemukan dalam database"
            f"{' di ' + wilayah if wilayah else ''}.\n\n"
            f"Kemungkinan penyebab:\n"
            f"- Nama sekolah tidak lengkap atau berbeda dengan data resmi\n"
            f"- Sekolah belum terdata di Dapodik/EMIS\n"
            f"- Coba sertakan NPSN untuk pencarian yang lebih presisi"
        )
    
    if reason == "profile_needs_clarification":
        candidates = es_result.get("candidates", [])
        lines = [
            f"Ditemukan **{len(candidates)} sekolah** dengan nama serupa. "
            f"Silakan pilih yang dimaksud:\n"
        ]
        for i, c in enumerate(candidates, 1):
            lines.append(
                f"{i}. **{c['nama']}** — {c['lokasi']} "
                f"({c['jenjang']} {c['status']}, Akreditasi: {c['akreditasi']}) "
                f"| NPSN: `{c['npsn']}`"
            )
        lines.append(
            "\n*Ketik NPSN sekolah untuk mendapatkan profil lengkap.*"
        )
        return "\n".join(lines)
    
    # Single profile
    doc = es_result.get("doc", {})
    if not doc:
        return "Data sekolah tidak tersedia."
    
    # Field dengan fallback "-" agar tidak ada None di output
    def f(key): return doc.get(key) or "-"
    
    lines = [
        f"## {f('nama')}",
        f"",
        f"| Informasi | Detail |",
        f"|---|---|",
        f"| NPSN | `{f('npsn')}` |",
        f"| Jenjang | {f('bentuk_pendidikan')} |",
        f"| Status | {f('status_sekolah')} |",
        f"| Akreditasi | {f('akreditasi')} |",
        f"| Alamat | {f('alamat_jalan')} |",
        f"| Kecamatan | {f('kecamatan').title()} |",
        f"| Kabupaten | {f('kabupaten').title()} |",
        f"| Provinsi | {f('provinsi').title()} |",
        f"| Telepon | {f('nomor_telepon')} |",
        f"| Website | {f('website')} |",
        f"| Penyelenggara | {f('penyelenggara')} |",
    ]
    
    # Koordinat jika ada
    lat, lon = doc.get("lintang"), doc.get("bujur")
    if lat and lon:
        maps_url = f"https://maps.google.com/?q={lat},{lon}"
        lines.append(f"| Lokasi | [{lat}, {lon}]({maps_url}) |")
    
    return "\n".join(lines)


def _render_list(es_result: dict, intent: CognitiveIntent, reason: str) -> str:
    
    if reason == "list_empty":
        jenjang   = intent.jenjang or "Sekolah"
        wilayah   = _format_wilayah(intent)
        akreditasi = f" akreditasi {intent.akreditasi}" if intent.akreditasi else ""
        status    = f" {intent.status.lower()}" if intent.status else ""
        return (
            f"Tidak ditemukan **{jenjang}{status}{akreditasi}** "
            f"{('di ' + wilayah) if wilayah else 'yang sesuai'}.\n\n"
            f"Coba perluas area pencarian atau hapus beberapa filter."
        )
    
    hits  = es_result.get("hits", [])
    total = es_result.get("total_hits", len(hits))
    jenjang = intent.jenjang or "sekolah"
    wilayah = _format_wilayah(intent)
    
    lines = [
        f"Ditemukan **{total:,} {jenjang}** "
        f"{('di ' + wilayah) if wilayah else ''}",
        f"(menampilkan {len(hits)} teratas):\n",
        f"| No | Nama | Akreditasi | Status | Kecamatan |",
        f"|---|---|---|---|---|",
    ]
    
    for i, hit in enumerate(hits, 1):
        src = hit.get("_source", hit)
        lines.append(
            f"| {i} | {src.get('nama','-')} "
            f"| {src.get('akreditasi','-')} "
            f"| {src.get('status_sekolah','-')} "
            f"| {(src.get('kecamatan') or '-').title()} |"
        )
    
    if total > len(hits):
        lines.append(
            f"\n*Masih ada {total - len(hits):,} sekolah lainnya. "
            f"Gunakan filter lebih spesifik untuk mempersempit hasil.*"
        )
    
    return "\n".join(lines)


def _render_geo(es_result: dict, intent: CognitiveIntent, reason: str) -> str:
    
    if reason == "geo_empty":
        radius = intent.radius_km or 5
        return (
            f"Tidak ditemukan sekolah dalam radius **{radius} km** "
            f"dari posisi Anda.\n\n"
            f"Coba perluas radius pencarian."
        )
    
    hits = es_result.get("hits", [])
    radius = intent.radius_km or 5
    
    lines = [
        f"Ditemukan **{len(hits)} sekolah** dalam radius {radius} km:\n",
        f"| No | Nama | Jarak | Akreditasi | Kecamatan |",
        f"|---|---|---|---|---|",
    ]
    
    for i, hit in enumerate(hits, 1):
        src  = hit.get("_source", hit)
        dist = hit.get("fields", {}).get("distance_km", [None])[0]
        dist_str = f"{dist:.1f} km" if dist else "-"
        lines.append(
            f"| {i} | {src.get('nama','-')} "
            f"| {dist_str} "
            f"| {src.get('akreditasi','-')} "
            f"| {(src.get('kecamatan') or '-').title()} |"
        )
    
    return "\n".join(lines)


def _format_wilayah(intent: CognitiveIntent) -> str:
    """Format wilayah menjadi string yang natural untuk ditampilkan."""
    wil = intent.wilayah
    
    parts = []
    if wil.desa:
        parts.append(f"Desa {wil.desa.title()}")
    if wil.kecamatan:
        parts.append(f"Kec. {wil.kecamatan.title()}")
    if wil.kabupaten:
        parts.append(wil.kabupaten.title())
    if wil.provinsi and not wil.kabupaten:
        parts.append(f"Prov. {wil.provinsi.title()}")
    
    if not parts and intent.scope == "nasional":
        return "seluruh Indonesia"
    
    return ", ".join(parts) if parts else ""
