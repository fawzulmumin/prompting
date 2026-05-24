"""
Tier 3: LLM-based intent classifier.
Menggunakan DeepSeek untuk classify + extract entities sekaligus.
Dipindah dan diimplementasi dari cognitive_router.py (P17-B refactor).
"""
import re
import json
import logging
from typing import Optional

from app.services.core.intent_models import (
    CognitiveIntent, WilayahEntities, NumericFilter,
    VALID_MODULES, VALID_SCOPES,
)

logger = logging.getLogger(__name__)


def _safe_float(v) -> Optional[float]:
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


class LLMClassifier:
    """
    Tier 3 classifier: single DeepSeek call untuk classify intent + extract entities.
    Hanya dipanggil jika Tier 1 + Tier 2 tidak confident.
    """

    def parse_llm_output(self, raw: str, query: str = "") -> Optional[CognitiveIntent]:
        """Parse raw LLM JSON output menjadi CognitiveIntent."""
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

            _scope_raw = obj.get("scope") or "nasional"
            scope = str(_scope_raw).lower().strip()
            if scope not in VALID_SCOPES:
                scope = "nasional"

            secondary = [
                s.lower() for s in (obj.get("secondary") or [])
                if isinstance(s, str) and s.lower() in VALID_MODULES
            ]

            _sub_queries_raw = obj.get("sub_queries") or {}
            sub_queries = {}
            if isinstance(_sub_queries_raw, dict):
                for mod_name, sub_q in _sub_queries_raw.items():
                    if mod_name in VALID_MODULES and isinstance(sub_q, str) and sub_q.strip():
                        sub_queries[mod_name] = sub_q.strip()

            if secondary and not sub_queries:
                logger.warning(
                    "[LLMClassifier] secondary=%s tapi sub_queries kosong → fallback ke query asli",
                    secondary,
                )
            elif secondary and query:
                if module not in sub_queries:
                    sub_queries[module] = query
                for sec in secondary:
                    if sec not in sub_queries:
                        logger.warning(
                            "[LLMClassifier] missing sub_query for secondary=%s → fallback", sec)
                        sub_queries[sec] = query

            _deps_raw = obj.get("dependencies") or {}
            dependencies = {}
            if isinstance(_deps_raw, dict):
                for _dep_mod, _dep_cfg in _deps_raw.items():
                    if _dep_mod in VALID_MODULES and isinstance(_dep_cfg, dict):
                        dependencies[_dep_mod] = _dep_cfg

            ent = obj.get("entities", {}) or {}
            wil = ent.get("wilayah", {}) or {}

            def _norm(v):
                if v in (None, "null", "", "None"):
                    return None
                return str(v).strip() or None

            _akr_list = ent.get("akreditasi_list", [])
            akreditasi_list = (
                [str(x).upper() for x in _akr_list if x]
                if isinstance(_akr_list, list) else []
            )

            def _safe_bool_field(v) -> Optional[bool]:
                if v in (None, "null", "", "None"):
                    return None
                if isinstance(v, bool):
                    return v
                if isinstance(v, str):
                    return v.lower() == "true"
                return bool(v)

            _VALID_OPS = {"gt", "gte", "lt", "lte", "between"}
            numeric_filters = []
            for nf in (ent.get("numeric_filters") or []):
                if not isinstance(nf, dict):
                    continue
                op = str(nf.get("operator", "")).lower()
                f  = str(nf.get("field", "")).strip()
                if not f or op not in _VALID_OPS:
                    continue
                numeric_filters.append(NumericFilter(
                    field=f,
                    operator=op,
                    value=_safe_float(nf.get("value")),
                    value_min=_safe_float(nf.get("value_min")),
                    value_max=_safe_float(nf.get("value_max")),
                ))

            return CognitiveIntent(
                module=module,
                secondary=secondary,
                scope=scope,
                sub_queries=sub_queries,
                jenjang=_norm(ent.get("jenjang")),
                jenjang_list=[
                    j for j in (ent.get("jenjang_list") or [])
                    if isinstance(j, str) and j.strip()
                ],
                status=_norm(ent.get("status")),
                akreditasi=_norm(ent.get("akreditasi")),
                akreditasi_mode=_norm(ent.get("akreditasi_mode")),
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
                metric_target_list=[
                    m for m in (ent.get("metric_target_list") or [])
                    if isinstance(m, str) and m.strip()
                ],
                distribusi_field=_norm(ent.get("distribusi_field")),
                infrastruktur_filter=_norm(ent.get("infrastruktur_filter")),
                numeric_filters=numeric_filters,
                keaktifan=_norm(ent.get("keaktifan")),
                wilayah_terpencil=_safe_bool_field(ent.get("wilayah_terpencil")),
                wilayah_perbatasan=_safe_bool_field(ent.get("wilayah_perbatasan")),
                partisipasi_bos=_safe_bool_field(ent.get("partisipasi_bos")),
                qc_masalah_only=bool(ent.get("qc_masalah_only", False)),
                qc_field=_safe_int(ent.get("qc_field")),
                qc_min_masalah=_safe_int(ent.get("qc_min_masalah")),
                sort_by=_norm(ent.get("sort_by")),
                sort_order=_norm(ent.get("sort_order")),
                qc_persen_min=_safe_float(ent.get("qc_persen_min")),
                qc_persen_max=_safe_float(ent.get("qc_persen_max")),
                qc_field_list=[
                    x for x in (ent.get("qc_field_list") or [])
                    if isinstance(x, int) and 1 <= x <= 13
                ],
                qc_persen_query=bool(ent.get("qc_persen_query", False)),
                is_oot=bool(obj.get("is_oot", False)),
                oot_reason=_norm(obj.get("oot_reason")),
                dependencies=dependencies,
                raw_label=raw[:80],
            )
        except Exception as e:
            logger.warning("[LLMClassifier] parse error: %s | raw=%r", e, raw[:60])
            return None
