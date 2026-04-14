# services/core/multi_intent_orchestrator.py
"""
MultiIntentOrchestrator — eksekusi paralel beberapa modul dan gabungkan hasilnya.
 
Alur:
    1. Terima IntentResult (primary + secondary + filters) dari classifier baru.
    2. Dispatch primary modul → wajib ada hasil.
    3. Dispatch secondary modul secara paralel (asyncio.gather).
    4. Gabungkan semua hasil menjadi satu merged_context string.
    5. Kirim merged_context ke narrator untuk narasi akhir.
 
Contoh skenario 3 modul:
    Query: "Bandingkan SMK akreditasi A di Bandung dan Bogor, tampilkan juga lokasinya"
    primary:   comparison  → execute_comparison(query, filters)
    secondary: [list]      → execute_list(query, filters)
    merged_context         → gabungan kedua hasil
    narasi akhir           → DeepSeek dengan merged_context sebagai input
"""
 
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
 
from app.services.core.intent_classifier import IntentResult  # single source of truth
 
logger = logging.getLogger(__name__)
 
 
# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────
 
 
@dataclass
class ModuleResult:
    """Hasil eksekusi satu modul."""
    module_name: str
    provider: str
    answer_markdown: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: Optional[str] = None
    num_docs: int = 0
    confidence: float = 0.0
 
 
# ─────────────────────────────────────────────────────────────────────────────
# CONTEXT MERGER
# ─────────────────────────────────────────────────────────────────────────────
 
_SECTION_DIVIDER = "\n\n---\n\n"
 
_MODULE_LABELS = {
    "aggregation":  "Ringkasan Jumlah",
    "comparison":   "Perbandingan",
    "list":         "Daftar Sekolah",
    "profile":      "Profil Sekolah",
    "geo":          "Sekolah Terdekat",
}
 
 
def merge_module_results(
    primary: ModuleResult,
    secondaries: List[ModuleResult],
    *,
    max_chars_per_section: int = 3000,
) -> str:
    """
    Gabungkan hasil modul menjadi satu context string yang terstruktur
    untuk dikirim ke narrator (DeepSeek).
 
    Prinsip:
    - Primary section selalu pertama dan mendapat porsi terbesar.
    - Secondary sections diberi label agar DeepSeek tahu ini data pendukung.
    - Tiap section dibatasi max_chars_per_section agar total ≤ 5000 char.
    - Jika secondary gagal, section-nya dihilangkan (tidak menghentikan alur).
    """
    sections: List[str] = []
 
    # ── Primary ──────────────────────────────────────────────────────────────
    primary_label = _MODULE_LABELS.get(primary.module_name, "Data Utama")
    primary_md    = primary.answer_markdown[:max_chars_per_section]
    sections.append(f"## {primary_label}\n\n{primary_md}")
 
    # ── Secondaries ──────────────────────────────────────────────────────────
    # Bagi sisa kuota char secara merata di antara secondary modules
    remaining_budget = max(500, max_chars_per_section - len(primary_md))
    per_secondary    = remaining_budget // max(1, len(secondaries))
 
    for sec in secondaries:
        if not sec.success or not sec.answer_markdown:
            logger.debug("[Orchestrator] secondary module %s skipped (no result)", sec.module_name)
            continue
 
        sec_label = _MODULE_LABELS.get(sec.module_name, "Data Pendukung")
        sec_md    = sec.answer_markdown[:per_secondary]
        sections.append(f"## {sec_label} (Data Pendukung)\n\n{sec_md}")
 
    return _SECTION_DIVIDER.join(sections)
 
 
# ─────────────────────────────────────────────────────────────────────────────
# FILTER INJECTOR
# ─────────────────────────────────────────────────────────────────────────────
 
def inject_filters_to_query(query: str, filters: Dict[str, Any]) -> str:
    if not filters:
        return query
 
    q_lower = query.lower()
    extra_parts = []
 
    # Dukung key sederhana (jenjang, status, akreditasi) MAUPUN key ES internal
    akreditasi = (
        filters.get("akreditasi")
        or filters.get("akreditasi_exact")
        or (filters.get("akreditasi_in") or [None])[0]
        or (filters.get("akreditasi_gte") or [None])[0]
    )
    if akreditasi:
        akr_lower = str(akreditasi).lower()
        # Cek frasa "akreditasi X" ada di query, bukan sekadar huruf tunggal
        if f"akreditasi {akr_lower}" not in q_lower:
            extra_parts.append(f"akreditasi={akreditasi}")
 
    jenjang = (
        filters.get("jenjang")
        or filters.get("bentuk_pendidikan")
        or (filters.get("bentuk_pendidikan_in") or [None])[0]
    )
    if jenjang and jenjang.lower() not in q_lower:
        extra_parts.append(f"jenjang={jenjang}")
 
    status = filters.get("status") or filters.get("status_sekolah")
    if status and status.lower() not in q_lower:
        extra_parts.append(f"status={status}")
 
    if extra_parts:
        return f"{query} [filter: {' '.join(extra_parts)}]"
    return query
 
 
# ─────────────────────────────────────────────────────────────────────────────
# DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────
 
async def _dispatch_single(
    module_name: str,
    query: str,
    filters: Dict[str, Any],
    es_client,
    user_id: str,
    *,
    timeout: float = 8.0,
) -> ModuleResult:
    """
    Dispatch satu modul dengan timeout.
    Return ModuleResult.success=False jika timeout atau error.
    Tidak pernah raise exception — caller bisa lanjut.
    """
    enriched_query = inject_filters_to_query(query, filters)
 
    try:
        result = await asyncio.wait_for(
            _call_module(module_name, enriched_query, es_client, user_id),
            timeout=timeout,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning("[Orchestrator] module %s timeout after %.1fs", module_name, timeout)
        return ModuleResult(
            module_name=module_name,
            provider=f"TIMEOUT_{module_name.upper()}",
            success=False,
            error=f"Module {module_name} timeout",
        )
    except Exception as e:
        logger.exception("[Orchestrator] module %s error: %s", module_name, e)
        return ModuleResult(
            module_name=module_name,
            provider=f"ERROR_{module_name.upper()}",
            success=False,
            error=str(e),
        )
 
 
async def _call_module(
    module_name: str,
    query: str,
    es_client,
    user_id: str,
) -> ModuleResult:
    """
    Routing ke handler yang sesuai.
    Import di dalam fungsi agar tidak ada circular import.
    """
    if module_name == "aggregation":
        from app.services.query_handlers.aggregation_handler import handle_aggregation_query
        raw = await handle_aggregation_query(query=query, es_client=es_client, user_id=user_id)
        return ModuleResult(
            module_name="aggregation",
            provider=raw.get("provider", "ELASTICSEARCH_AGG"),
            answer_markdown=raw.get("answer_markdown", ""),
            data=raw,
            num_docs=raw.get("total", 0),
            confidence=raw.get("confidence", 0.0),
            success=bool(raw and raw.get("provider") not in ("SYSTEM_ERROR", "SYSTEM_VALIDATION")),
        )
 
    elif module_name == "comparison":
        from app.services.query_handlers.comparison_handler import handle_comparison_query
        raw = await handle_comparison_query(query=query, es_client=es_client)
        return ModuleResult(
            module_name="comparison",
            provider=raw.get("provider", "ELASTICSEARCH_COMPARISON"),
            answer_markdown=raw.get("answer_markdown", ""),
            data=raw,
            num_docs=raw.get("num_docs", 0),
            confidence=raw.get("confidence", 0.0),
            success=bool(raw and "SYSTEM" not in raw.get("provider", "")),
        )
 
    elif module_name == "list":
        from app.services.query_handlers.list_school_handler import ListSchoolHandler
        import os
        handler = ListSchoolHandler(es_client, os.getenv("ELASTICSEARCH_INDEX"))
        raw = await handler.handle(query)
        schools = raw.get("schools", [])
        # Format ringkas untuk secondary context
        lines = [f"- **{s.get('nama')}** ({s.get('bentuk_pendidikan','')}) — {s.get('kecamatan','')}"
                 for s in schools[:20]]
        md = "\n".join(lines) if lines else "Tidak ada sekolah ditemukan."
        return ModuleResult(
            module_name="list",
            provider="ELASTICSEARCH_LIST",
            answer_markdown=md,
            data=raw,
            num_docs=raw.get("total", 0),
            confidence=0.8 if schools else 0.0,
            success=bool(schools),
        )
 
    elif module_name == "geo":
        # Geo memerlukan koordinat — hanya bisa dijalankan sebagai secondary
        # jika koordinat tersedia dari primary request
        # Jika tidak ada koordinat → return empty result
        return ModuleResult(
            module_name="geo",
            provider="GEO_SKIPPED",
            success=False,
            error="Koordinat tidak tersedia untuk secondary geo module",
        )
 
    elif module_name == "profile":
        from app.services.query_handlers.profile_handler import ProfileHandler
        handler = ProfileHandler(es_client)
        raw = await handler.handle(query)
        return ModuleResult(
            module_name="profile",
            provider="ELASTICSEARCH_PROFILE",
            answer_markdown=raw.get("formatted", ""),
            data=raw,
            num_docs=1 if raw.get("status") == "success" else 0,
            confidence=raw.get("confidence", 0.0),
            success=raw.get("status") == "success",
        )
 
    else:
        return ModuleResult(
            module_name=module_name,
            provider="UNKNOWN_MODULE",
            success=False,
            error=f"Module '{module_name}' tidak dikenal",
        )
 
 
# ─────────────────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────
 
async def orchestrate(
    intent,
    query: str,
    es_client,
    user_id: str,
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Entry point utama. Dipanggil dari rag_server.py sebagai pengganti
    dispatch langsung ke satu handler.
 
    Returns dict dengan:
        - merged_context: str  → untuk dikirim ke narrator
        - primary_result:      → ModuleResult modul utama
        - secondary_results:   → list ModuleResult modul pendukung
        - providers:           → list provider string untuk logging
        - total_docs:          → jumlah dokumen dari semua modul
        - confidence:          → confidence gabungan (rata-rata weighted)
    """
    logger.info(
        "[Orchestrator] primary=%s secondary=%s filters=%s",
        intent.primary, intent.secondary
    )
 
    # ── Jalankan primary (wajib) + semua secondary secara paralel ────────────
    tasks = [
        _dispatch_single(
            intent.primary, query, filters or {},
            es_client, user_id, timeout=10.0,
        )
    ]
    for sec_module in intent.secondary:
        tasks.append(
            _dispatch_single(
                sec_module, query, filters or {},
                es_client, user_id, timeout=6.0,   # secondary dapat timeout lebih pendek
            )
        )
 
    results = await asyncio.gather(*tasks, return_exceptions=False)
    primary_result    = results[0]
    secondary_results = list(results[1:]) if len(results) > 1 else []
 
    # ── Merge context ─────────────────────────────────────────────────────────
    merged_context = merge_module_results(primary_result, secondary_results)
 
    # ── Aggregate metadata ────────────────────────────────────────────────────
    all_results  = [primary_result] + secondary_results
    providers    = [r.provider for r in all_results if r.success]
    total_docs   = sum(r.num_docs for r in all_results)
    confidences  = [r.confidence for r in all_results if r.success and r.confidence > 0]
    avg_conf     = sum(confidences) / len(confidences) if confidences else 0.0
 
    return {
        "merged_context":    merged_context,
        "primary_result":    primary_result,
        "secondary_results": secondary_results,
        "providers":         providers,
        "total_docs":        total_docs,
        "confidence":        avg_conf,
        "is_multi_module":   bool(secondary_results and any(r.success for r in secondary_results)),
    }
