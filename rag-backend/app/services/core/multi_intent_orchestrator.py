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
import copy
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
    intent=None,
    *,
    timeout: float = 8.0,
    list_handler=None,
    profile_handler=None,
) -> ModuleResult:
    """
    Dispatch satu modul dengan timeout.
    Return ModuleResult.success=False jika timeout atau error.
    Tidak pernah raise exception — caller bisa lanjut.
    """
    enriched_query = inject_filters_to_query(query, filters)

    try:
        result = await asyncio.wait_for(
            _call_module(
                module_name, enriched_query, es_client, user_id,
                intent=intent,
                list_handler=list_handler,
                profile_handler=profile_handler,
                filters=filters,
            ),
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
    intent=None,
    list_handler=None,
    profile_handler=None,
    filters: Optional[Dict[str, Any]] = None,
) -> ModuleResult:
    """
    Routing ke handler yang sesuai.
    Import di dalam fungsi agar tidak ada circular import.
    """
    if module_name == "aggregation":
        from app.services.query_handlers.aggregation_handler import handle_aggregation_query
        raw = await handle_aggregation_query(query=query, es_client=es_client, user_id=user_id, intent=intent)
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
        raw = await handle_comparison_query(query=query, es_client=es_client, intent=intent, filters=filters or {})
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
        if list_handler is None:
            from app.services.query_handlers.list_school_handler import ListSchoolHandler
            import os
            list_handler = ListSchoolHandler(es_client, os.getenv("ELASTICSEARCH_INDEX"))
        raw = await list_handler.handle(query, intent=intent)
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
        logger.warning(
            "[Orchestrator] Secondary geo module not yet supported — "
            "koordinat user hanya tersedia di primary flow"
        )
        return ModuleResult(
            module_name="geo",
            provider="GEO_NOT_SUPPORTED",
            success=False,
            error="Secondary geo not implemented",
        )
 
    elif module_name == "profile":
        if profile_handler is None:
            from app.services.query_handlers.profile_handler import ProfileHandler
            profile_handler = ProfileHandler(es_client)
        raw = await profile_handler.handle(query, intent=intent)
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
 
def _build_injected_intent(original_intent, parent_result: dict, inject_map: dict):
    """
    Return a shallow copy of intent with fields populated from parent_result.
    Does NOT mutate original_intent.

    Supported injection targets:
      - wilayah fields: provinsi, kabupaten, kecamatan, desa
        → setattr(new_intent.wilayah, field_name, value)
      - direct intent fields: jenjang
        → setattr(new_intent, field_name, value)
    """
    new_intent = copy.copy(original_intent)
    new_intent.wilayah = copy.copy(original_intent.wilayah)

    _DIRECT_INTENT_FIELDS = {"jenjang"}

    injected_any = False
    for field_name, result_key in inject_map.items():
        injected_value = parent_result.get(result_key)
        if not injected_value:
            logger.warning(
                "[Orchestrator] inject_from_result: key=%r not found in parent_result "
                "(available keys: %s)",
                result_key,
                list(parent_result.keys()),
            )
            continue

        if field_name in _DIRECT_INTENT_FIELDS:
            setattr(new_intent, field_name, str(injected_value))
            logger.info(
                "[Orchestrator] inject direct: intent.%s = %r",
                field_name, injected_value,
            )
            injected_any = True
        elif hasattr(new_intent.wilayah, field_name):
            setattr(new_intent.wilayah, field_name, str(injected_value).lower())
            new_intent.scope = field_name
            logger.info(
                "[Orchestrator] inject wilayah: intent.wilayah.%s = %r, scope=%s",
                field_name, injected_value, field_name,
            )
            injected_any = True

    if not injected_any:
        logger.warning(
            "[Orchestrator] _build_injected_intent: no field injected! "
            "inject_map=%s parent_result_keys=%s",
            inject_map, list(parent_result.keys()),
        )

    return new_intent


async def orchestrate(
    intent,
    query: str,
    es_client,
    user_id: str,
    filters: Optional[Dict[str, Any]] = None,
    list_handler=None,
    profile_handler=None,
) -> Dict[str, Any]:
    """
    Entry point utama. Dependency-aware 2-tier execution:
      Tier 1 — primary + independent secondary run in parallel.
      Tier 2 — dependent secondary run after Tier 1, with result injection.

    Returns dict dengan:
        - merged_context: str
        - primary_result, secondary_results, providers, total_docs, confidence
    """
    sub_queries  = getattr(intent, "sub_queries",  {}) or {}
    dependencies = getattr(intent, "dependencies", {}) or {}

    logger.info(
        "[Orchestrator] primary=%s secondary=%s dependencies=%s",
        intent.primary, intent.secondary, list(dependencies.keys()),
    )

    def _query_for(module_name: str) -> str:
        return sub_queries.get(module_name, query)

    all_secondary        = list(intent.secondary)
    dependent_modules    = [m for m in all_secondary if m in dependencies]
    independent_secondary = [m for m in all_secondary if m not in dependencies]

    if dependent_modules:
        logger.info(
            "[Orchestrator] independent layer: [%s %s], dependent layer: %s",
            intent.primary, independent_secondary, dependent_modules,
        )

    # ── Tier 1: primary + independent secondary in parallel ──────────────────
    tier1_tasks = [
        _dispatch_single(
            intent.primary, _query_for(intent.primary), filters or {},
            es_client, user_id, intent=intent, timeout=10.0,
            list_handler=list_handler, profile_handler=profile_handler,
        )
    ]
    for sec_module in independent_secondary:
        tier1_tasks.append(
            _dispatch_single(
                sec_module, _query_for(sec_module), filters or {},
                es_client, user_id, intent=intent, timeout=6.0,
                list_handler=list_handler, profile_handler=profile_handler,
            )
        )

    tier1_done   = await asyncio.gather(*tier1_tasks, return_exceptions=False)
    primary_result = tier1_done[0]

    results_map: Dict[str, ModuleResult] = {intent.primary: primary_result}
    for i, sec_module in enumerate(independent_secondary):
        results_map[sec_module] = tier1_done[i + 1]

    # ── Tier 2: dependent secondary with injection ────────────────────────────
    if dependent_modules:
        tier2_tasks = []
        for dep_module in dependent_modules:
            dep_config    = dependencies[dep_module]
            parent_module = dep_config.get("depends_on")
            inject_map    = dep_config.get("inject_from_result", {})

            parent_data = results_map.get(
                parent_module,
                ModuleResult(module_name=parent_module or "", provider="MISSING"),
            ).data

            injected_intent = _build_injected_intent(intent, parent_data, inject_map)

            for field_name, result_key in inject_map.items():
                logger.info(
                    "[Orchestrator] [%s] injecting %s=%s from %s",
                    dep_module, field_name, parent_data.get(result_key), parent_module,
                )

            tier2_tasks.append(
                _dispatch_single(
                    dep_module, _query_for(dep_module), filters or {},
                    es_client, user_id, intent=injected_intent, timeout=6.0,
                    list_handler=list_handler, profile_handler=profile_handler,
                )
            )

        tier2_done = await asyncio.gather(*tier2_tasks, return_exceptions=False)
        for i, dep_module in enumerate(dependent_modules):
            results_map[dep_module] = tier2_done[i]

    # ── Reconstruct secondary_results in original order ───────────────────────
    secondary_results: List[ModuleResult] = [
        results_map[m] for m in all_secondary if m in results_map
    ]

    # ── Merge context ─────────────────────────────────────────────────────────
    merged_context = merge_module_results(primary_result, secondary_results)

    # ── Aggregate metadata ────────────────────────────────────────────────────
    all_results = [primary_result] + secondary_results
    providers   = [r.provider for r in all_results if r.success]
    total_docs  = sum(r.num_docs for r in all_results)
    confidences = [r.confidence for r in all_results if r.success and r.confidence > 0]
    avg_conf    = sum(confidences) / len(confidences) if confidences else 0.0

    return {
        "merged_context":    merged_context,
        "primary_result":    primary_result,
        "secondary_results": secondary_results,
        "providers":         providers,
        "total_docs":        total_docs,
        "confidence":        avg_conf,
        "is_multi_module":   bool(secondary_results and any(r.success for r in secondary_results)),
    }
