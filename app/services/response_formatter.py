# response_formatter.py
 
import re
import logging
from typing import Dict, Any, List
 
logger = logging.getLogger(__name__)
 
 
def prettify_location_label(label: str) -> str:
    if not label:
        return "Tidak diketahui"
    label = str(label).strip()
    replacements = {
        "prov.": "Provinsi", "provinsi": "Provinsi",
        "kab.":  "Kabupaten", "kabupaten": "Kabupaten",
        "kota":  "Kota", "kecamatan": "Kecamatan",
        "kec.":  "Kec.", "desa": "Desa", "kelurahan": "Kelurahan",
    }
    parts = label.split()
    if parts:
        first = parts[0].lower()
        if first in replacements:
            parts[0] = replacements[first]
    special_upper = {"dki", "di"}
    return " ".join(w.upper() if w.lower() in special_upper else w.capitalize() for w in parts)
 
 
def _clean_prefix(text: str, prefixes: list) -> str:
    if not text:
        return ""
    for prefix in prefixes:
        text = re.sub(rf"^{re.escape(prefix)}\s*", "", text, flags=re.I)
    return text.strip()
 
 
def _format_lokasi(school: Dict[str, Any]) -> str:
    parts = []
    desa = school.get("desa_kelurahan") or ""
    kec  = school.get("kecamatan")      or ""
    kab  = school.get("kabupaten")      or ""
    prov = school.get("provinsi")       or ""
 
    if desa:
        parts.append(f"Ds. {_clean_prefix(desa, ['desa', 'kelurahan']).title()}")
    if kec:
        parts.append(f"Kec. {_clean_prefix(kec, ['kec.', 'kecamatan']).title()}")
    if kab:
        if re.match(r"^kota\s+", kab, re.I):
            parts.append(f"Kota {_clean_prefix(kab, ['kota']).title()}")
        else:
            parts.append(f"Kab. {_clean_prefix(kab, ['kab.', 'kabupaten']).title()}")
    if prov:
        parts.append(f"Prov. {_clean_prefix(prov, ['prov.', 'provinsi']).title()}")
    return ", ".join(parts)
 
 
def _format_bentuk_status(school: Dict[str, Any]) -> str:
    bentuk = school.get("bentuk") or ""
    status = school.get("status") or ""
    if bentuk and status:
        return f"{bentuk}/{status.capitalize()}"
    return bentuk or ""
 
 
def _fmt_candidate_nama(c: Dict) -> str:
    """
    Tampilkan nama kandidat dengan prefix Kab./Kota jika tersedia
    dari data raw kabupaten di LOCATION_HIERARCHY.
    """
    level    = c.get("level", "")
    nama_raw = c.get("nama_raw", "") or c.get("nama", "")
    nama     = c.get("nama", "")
 
    if level == "kabupaten" and nama_raw:
        # nama_raw adalah nilai asli dari ES, misal "Kab. Tangerang" atau "Kota Tangerang"
        return nama_raw.title()
    return nama.title()
 
 
def _fmt_candidate_hierarchy(c: Dict) -> str:
    """Susun hierarki dari kandidat."""
    level = c.get("level", "")
    parts = []
 
    kec  = c.get("kecamatan", "") or ""
    kab  = c.get("kabupaten",  "") or ""
    prov = c.get("provinsi",   "") or ""
 
    if level == "desa_kelurahan":
        if kec:
            parts.append(f"Kec. {_clean_prefix(kec, ['kec.', 'kecamatan']).title()}")
        if kab:
            if re.match(r"^kota\s+", kab, re.I):
                parts.append(f"Kota {_clean_prefix(kab, ['kota']).title()}")
            else:
                parts.append(f"Kab. {_clean_prefix(kab, ['kab.', 'kabupaten']).title()}")
        if prov:
            parts.append(f"Prov. {_clean_prefix(prov, ['prov.', 'provinsi']).title()}")
 
    elif level == "kecamatan":
        if kab:
            if re.match(r"^kota\s+", kab, re.I):
                parts.append(f"Kota {_clean_prefix(kab, ['kota']).title()}")
            else:
                parts.append(f"Kab. {_clean_prefix(kab, ['kab.', 'kabupaten']).title()}")
        if prov:
            parts.append(f"Prov. {_clean_prefix(prov, ['prov.', 'provinsi']).title()}")
 
    elif level == "kabupaten":
        if prov:
            parts.append(f"Prov. {_clean_prefix(prov, ['prov.', 'provinsi']).title()}")
 
    elif level == "provinsi":
        if prov:
            prov_clean = _clean_prefix(prov, ["prov.", "provinsi"])
            parts.append(f"Provinsi {prov_clean.title()}")
 
    return ", ".join(parts)
 
 
_LEVEL_LABEL = {
    "desa_kelurahan": "Desa/Kelurahan",
    "kecamatan":      "Kecamatan",
    "kabupaten":      "Kabupaten/Kota",
    "provinsi":       "Provinsi",
}
 
 
# =====================================================
# CLARIFICATION FORMATTER
# =====================================================
 
def format_clarification_answer(result: Dict[str, Any]) -> Dict[str, str]:
    keyword:    str        = result.get("keyword", "")
    candidates: List[Dict] = result.get("candidates", []) or []
 
    nama_kw = keyword.title() if keyword else "lokasi yang dimaksud"
 
    by_level: Dict[str, List[Dict]] = {
        "desa_kelurahan": [], "kecamatan": [],
        "kabupaten": [],      "provinsi":  [],
    }
    for c in candidates:
        lv = c.get("level", "kabupaten")
        if lv in by_level:
            by_level[lv].append(c)
 
    has_candidates = any(v for v in by_level.values())
 
    # ── Markdown ─────────────────────────────────────────────
    md_lines = [
        f"### Nama \"{nama_kw}\" Ditemukan di Beberapa Wilayah\n",
        "Mohon perjelas wilayah yang dimaksud.\n",
    ]
 
    if has_candidates:
        for level, label in _LEVEL_LABEL.items():
            items = by_level.get(level, [])
            if not items:
                continue
            md_lines.append(f"\n**{label} dengan nama \"{nama_kw}\":**")
            for c in items:
                nama_c   = _fmt_candidate_nama(c)
                hierarki = _fmt_candidate_hierarchy(c)
                if hierarki:
                    md_lines.append(f"- **{nama_c}** - {hierarki}")
                else:
                    md_lines.append(f"- **{nama_c}**")
 
        md_lines.append(
            f"\n**Cara memperjelas pencarian:**\n"
            f"- _\"daftar sekolah di **provinsi** {keyword}\"_\n"
            f"- _\"daftar sekolah di **desa** {keyword}\"_\n"
            f"- _\"daftar sekolah di **kecamatan** {keyword}\"_\n"
            f"- _\"daftar sekolah di **kabupaten** {keyword}\"_\n"
        )
    else:
        md_lines.append(
            f"\nNama **{nama_kw}** tidak ditemukan dalam data wilayah.\n"
            f"Pastikan ejaan sudah benar atau coba:\n\n"
            f"- _\"daftar sekolah di provinsi {keyword}\"_\n"
            f"- _\"daftar sekolah di desa {keyword}\"_\n"
            f"- _\"daftar sekolah di kecamatan {keyword}\"_\n"
        )
 
    answer_markdown = "\n".join(md_lines)
 
    # ── HTML ─────────────────────────────────────────────────
    html_parts = [
        f"<h3>Nama \"{nama_kw}\" Ditemukan di Beberapa Wilayah</h3>",
        "<p>Mohon perjelas wilayah yang dimaksud.</p>",
    ]
 
    if has_candidates:
        for level, label in _LEVEL_LABEL.items():
            items = by_level.get(level, [])
            if not items:
                continue
            html_parts.append(f"<p><strong>{label} dengan nama \"{nama_kw}\":</strong></p><ul>")
            for c in items:
                nama_c   = _fmt_candidate_nama(c)
                hierarki = _fmt_candidate_hierarchy(c)
                detail   = f" - <small>{hierarki}</small>" if hierarki else ""
                html_parts.append(f"<li><strong>{nama_c}</strong>{detail}</li>")
            html_parts.append("</ul>")
 
        html_parts.append(
            f"<p><strong>Cara memperjelas:</strong> "
            f"<em>\"daftar sekolah di provinsi {keyword}\"</em>, "
            f"<em>\"daftar sekolah di desa {keyword}\"</em>, atau "
            f"<em>\"daftar sekolah di kabupaten {keyword}\"</em></p>"
        )
    else:
        html_parts.append(
            f"<p>Nama <strong>{nama_kw}</strong> tidak ditemukan. "
            f"Pastikan ejaan benar atau tambahkan tingkat wilayah.</p>"
        )
 
    return {
        "answer_markdown": answer_markdown,
        "answer_html":     "\n".join(html_parts),
    }
 
 
# =====================================================
# SCHOOL LIST FORMATTER (normal)
# =====================================================
 
def format_school_list_answer(result: Dict[str, Any]) -> Dict[str, str]:
    schools: List[Dict] = result.get("schools", []) or []
    total: int          = result.get("total", 0)    or 0
    parsed: Dict        = result.get("parsed", {})  or {}
 
    wilayah = parsed.get("wilayah") or ""
    bentuk  = parsed.get("bentuk")  or ""
    status  = parsed.get("status")  or ""
 
    header_parts = ["Daftar Sekolah"]
    if bentuk:
        header_parts.append(bentuk)
    if status:
        header_parts.append(status.capitalize())
    if wilayah:
        header_parts.append(f"di {wilayah.title()}")
    header = " ".join(header_parts)
 
    md_lines = [f"### {header}\n"]
    md_lines.append(f"Menampilkan **{len(schools)}** dari **{total:,}** sekolah yang ditemukan.\n")
    for s in schools:
        nama   = s.get("nama") or "-"
        label  = _format_bentuk_status(s)
        lokasi = _format_lokasi(s)
        md_lines.append(f"- **{nama}**" + (f" `{label}`" if label else ""))
        if lokasi:
            md_lines.append(f"  {lokasi}")
 
    answer_markdown = "\n".join(md_lines)
 
    html_items = []
    for s in schools:
        nama   = s.get("nama") or "-"
        label  = _format_bentuk_status(s)
        lokasi = _format_lokasi(s)
        badge  = f' <span class="school-badge">{label}</span>' if label else ""
        loc_el = f"<br><small>{lokasi}</small>"               if lokasi else ""
        html_items.append(f"<li><strong>{nama}</strong>{badge}{loc_el}</li>")
 
    answer_html = (
        f"<h3>{header}</h3>\n"
        f"<p>Menampilkan <strong>{len(schools)}</strong> dari "
        f"<strong>{total:,}</strong> sekolah yang ditemukan.</p>\n"
        f"<ul>\n" + "\n".join(html_items) + "\n</ul>"
    )
 
    return {"answer_markdown": answer_markdown, "answer_html": answer_html}
 
 
# =====================================================
# AGGREGATION / COUNT FORMATTER
# =====================================================
 
def format_school_answer(result: Dict[str, Any]) -> str:
    raw_total = result.get("total") or 0
    try:
        total = int(raw_total)
    except (TypeError, ValueError):
        total = 0
    entities: Dict[str, Any] = result.get("entities", {}) or {}
    location = build_location_label(entities)
    title    = _build_title(location, entities)
    return _build_markdown_response(title, total)
 
 
def _build_title(location: str, entities: Dict[str, Any]) -> str:
    base_title   = f"Jumlah Sekolah di {location}"
    filters_text = []
    status = entities.get("status_sekolah")
    tahun  = entities.get("tahun")
    if status:
        filters_text.append(f"status {status}")
    if tahun:
        filters_text.append(f"tahun {tahun}")
    if filters_text:
        return f"{base_title} ({', '.join(filters_text)})"
    return base_title
 
 
def _build_markdown_response(title: str, total: int) -> str:
    return (
        f"### {title}\n\n"
        f"Jumlah sekolah yang terdata adalah:\n\n"
        f"**{total:,} sekolah**\n\n"
        f"---\n"
        f"_Data dihitung langsung dari indeks Elasticsearch._"
    )
 
 
def build_reject_answer() -> Dict[str, Any]:
    return {
        "answer_markdown": (
            "### Pertanyaan Tidak Relevan\n\n"
            "Maaf, sistem ini hanya mendukung pertanyaan terkait:\n\n"
            "- Jumlah sekolah\n"
            "- Status sekolah (negeri/swasta)\n"
            "- Lokasi sekolah\n\n"
            "Silakan ajukan pertanyaan terkait data sekolah."
        ),
        "provider": "SYSTEM_REJECT",
        "num_docs": 0,
    }
    
 
def build_location_label(context: Dict[str, Any]) -> str:
    levels = [
        context.get("desa_kelurahan"),
        context.get("kecamatan"),
        context.get("kabupaten"),
        context.get("provinsi"),
    ]
    available = [lvl for lvl in levels if lvl]
    if not available:
        return "Tidak diketahui"
    return ", ".join(prettify_location_label(lvl) for lvl in available)
