# profile_handler.py
 
import json
import hashlib
import re
import logging
from typing import Dict, Optional
logger = logging.getLogger(__name__)

from app.services.core.es_query_builder import extract_filters_from_intent
from app.services.query_handlers.list_school_handler import _intent_to_legacy_parsed
from app.services.search.profile_search_service import ProfileSearchService
from app.services.core.profile_builder import build_profile, compute_profile_completeness
from app.services.core.data_quality_analyzer import analyze_profile_quality
from app.services.core.ranking_engine import should_auto_resolve, apply_smart_ranking, strip_location_prefix, normalize_text
from rapidfuzz import fuzz
from app.utils.cache import get_cache, set_cache
 
 
MIN_CONFIDENT_SCORE = 80
MIN_AMBIGUOUS_SCORE = 40
 
 
class ProfileHandler:
 
    def __init__(self, es_client):
        self.search_service = ProfileSearchService(es_client)
 
    async def handle(
        self,
        query: str,
        intent=None,
        filters: Optional[Dict] = None,
    ) -> dict:
        try:
            query = self._normalize_query(query)

            cached = await self._get_cache(query)
            if cached:
                return cached

            filters_dict = filters or extract_filters_from_intent(intent) or {}
            parsed = _intent_to_legacy_parsed(filters_dict)

            # Populate school_name dan wilayah dari intent jika belum ada
            if intent and not parsed.get("school_name"):
                _nama = getattr(intent, "nama_sekolah", None)
                if _nama:
                    parsed["school_name"] = _nama

            if intent and not parsed.get("wilayah"):
                _w = getattr(intent, "wilayah", None)
                if _w:
                    parsed["wilayah"] = (
                        getattr(_w, "kecamatan", None)
                        or getattr(_w, "kabupaten", None)
                        or getattr(_w, "provinsi", None)
                    )

            # Fallback: jika school_name masih kosong, parse dari query langsung
            if not parsed.get("school_name"):
                from app.services.core.smart_query_parser import parse_school_query
                _parsed_fallback = parse_school_query(query)
                if _parsed_fallback.get("school_name"):
                    parsed.update({k: v for k, v in _parsed_fallback.items() if v and not parsed.get(k)})

            search_result = await self._execute_search(query, parsed)

            if not search_result or search_result["total"] == 0:
                _npsn_q = self._extract_npsn(query)
                if _npsn_q:
                    return {
                        "status": "not_found",
                        "message": f"Sekolah dengan NPSN {_npsn_q} tidak ditemukan dalam database.",
                    }
                return self._not_found()

            # ── NPSN path: bypass ranking — NPSN adalah identifier unik ────────
            if search_result.get("is_npsn_search") and search_result["total"] >= 1:
                _npsn_hit = search_result["hits"][0]
                logger.info(
                    "[ProfileHandler] NPSN search bypass ranking → langsung return hit"
                )
                try:
                    _npsn_profile = self._build_profile(_npsn_hit)
                except (KeyError, AttributeError) as _qc_err:
                    logger.warning(
                        "[ProfileHandler] QC field error building NPSN profile: %s", _qc_err
                    )
                    _npsn_profile = build_profile(_npsn_hit.get("_source", {}), {})
                _npsn_response = self._build_response(_npsn_profile, _npsn_hit, is_npsn=True)
                await self._set_cache(query, _npsn_response)
                return _npsn_response
            # ─────────────────────────────────────────────────────────────────────

            hits          = search_result["hits"]
            hits_returned = search_result["total"]
            total_es      = search_result.get("total_found", hits_returned)

            if total_es > 10000:
                total_found    = hits_returned
                is_count_exact = False
            else:
                total_found    = total_es
                is_count_exact = True

            hits = search_result["hits"]
            hits = apply_smart_ranking(parsed, hits)

            wilayah_resolved = search_result.get("wilayah_resolved", True)

            decision = self._decide_result(
                parsed, hits, total_found, is_count_exact,
                wilayah_resolved=wilayah_resolved
            )

            if decision["status"] != "success":
                return decision

            hit        = decision["hit"]
            weak_match = decision.get("weak_match", False)
            try:
                profile = self._build_profile(hit)
            except (KeyError, AttributeError) as qc_err:
                logger.warning(
                    "[ProfileHandler] QC field error building profile, returning partial: %s", qc_err
                )
                profile = build_profile(hit.get("_source", {}), {})

            response = self._build_response(profile, hit, weak_match=weak_match)

            await self._set_cache(query, response)

            return response

        except Exception as e:
            logger.exception("[ProfileHandler] Unhandled error for query=%r: %s", query, e)
            return {
                "status": "error",
                "message": "Terjadi kesalahan internal saat memproses permintaan profil sekolah.",
            }

    # =====================================================
    # SEARCH LOGIC
    # =====================================================
 
    async def _execute_search(self, query, parsed):
 
        npsn = self._extract_npsn(query)
 
        if npsn:
            result = await self.search_service.search_by_npsn(
                npsn, parsed.get("year")
            )
            result["wilayah_resolved"] = True
            result["is_npsn_search"]   = True
            return result
 
        school_name = parsed.get("school_name")
        if not school_name:
            return None
 
        wilayah        = parsed.get("wilayah")
        number         = parsed.get("number")
        bentuk         = parsed.get("bentuk")
        year           = parsed.get("year")
        strict_wilayah = bool(number and wilayah)
 
        result = await self.search_service.search_by_name(
            school_name=school_name,
            bentuk=bentuk,
            wilayah=wilayah,
            year=year,
            number=number,
            strict_wilayah=strict_wilayah
        )
 
        # Fallback 1: strict_wilayah tidak ada hasil → coba tanpa strict
        if result["total"] == 0 and strict_wilayah:
            result = await self.search_service.search_by_name(
                school_name=school_name,
                bentuk=bentuk,
                wilayah=wilayah,
                year=year,
                number=number,
                strict_wilayah=False
            )
 
        # Fallback 2: masih kosong → coba tanpa wilayah sama sekali
        if result["total"] == 0 and wilayah:
            result = await self.search_service.search_by_name(
                school_name=school_name,
                bentuk=bentuk,
                wilayah=None,
                year=year,
                number=number,
                strict_wilayah=False
            )
            # Jika fallback ke tanpa wilayah menghasilkan 100K+ hits,
            # wilayah yang di-parse tidak ada di data
            total_es = result.get("total_found", 0)
            result["wilayah_resolved"] = total_es < 50000
            return result
 
        # Cek apakah hasil mencurigakan: total_found sangat besar
        # menandakan filter wilayah tidak efektif (wilayah tidak dikenali ES)
        total_es = result.get("total_found", 0)
        result["wilayah_resolved"] = total_es < 50000
 
        return result
 
    # =====================================================
    # RESULT DECISION
    # =====================================================
 
    def _decide_result(
        self, parsed_query, hits, total,
        is_count_exact=True, wilayah_resolved=True
    ):
 
        if not hits:
            return self._not_found()
 
        top_hit   = hits[0]
        top_score = top_hit.get("_custom_score", 0)
 
        if top_score < MIN_AMBIGUOUS_SCORE:
            return self._not_found()
 
        # ── Tentukan kekuatan query ────────────────────────────────────────────
        wilayah     = parsed_query.get("wilayah")
        school_name = parsed_query.get("school_name", "")
 
        # Wilayah "valid" hanya jika memang ditemukan di data ES
        # (wilayah_resolved=False berarti fallback ke tanpa filter → wilayah tidak dikenali)
        name_tokens = len(school_name.split()) if school_name else 0
        
        has_wilayah  = bool(wilayah) and wilayah_resolved
        
        if wilayah and not has_wilayah:
            logger.debug(
                "[DECIDE] wilayah='%s' tidak efektif membatasi hasil → diabaikan",
                wilayah
            )
 
 
        # Query dianggap "lemah" jika:
        # 1. Tidak ada wilayah valid (resolved), DAN
        # 2. Nama sekolah pendek (<= 4 token) — "sd 3 akreditasinya apa" = 4 token
        #    ATAU wilayah di-parse tapi tidak resolve (sinyal nama sekolah pendek)
        is_weak_query = (
            not has_wilayah and (
                name_tokens <= 4
                or not wilayah_resolved  # wilayah ada tapi tidak efektif
            )
        )
        # ── Gap check ─────────────────────────────────────────────────────────
        GAP_STRONG = 15
        GAP_WEAK   = 35
 
        if len(hits) >= 2:
            second_score = hits[1].get("_custom_score", 0)
            score_gap    = top_score - second_score
            gap_needed   = GAP_WEAK if is_weak_query else GAP_STRONG
            has_clear_winner = score_gap >= gap_needed
        else:
            score_gap        = top_score
            has_clear_winner = True
 
        # Fix: jika user sebut "kabupaten X" tapi rank 1 di "kota X" dan rank 2 di "kab. X"
        # → batalkan has_clear_winner agar tidak auto-resolve ke sekolah yang salah
        if has_clear_winner and wilayah and len(hits) >= 2:
            wilayah_lower = wilayah.lower()
            if "kabupaten" in wilayah_lower:
                top_kab_raw  = hits[0]["_source"].get("kabupaten", "").lower()
                rank2_kab_raw = hits[1]["_source"].get("kabupaten", "").lower()
                core = wilayah_lower.replace("kabupaten", "").strip()
                if (core and "kota" in top_kab_raw and core in top_kab_raw
                        and "kab." in rank2_kab_raw and core in rank2_kab_raw):
                    # Sebelum force ambiguous, cek apakah ada kandidat yang
                    # namanya mengandung "kabupaten <core>" → langsung resolve
                    smart_hit = None
                    for h in hits:
                        nama = h["_source"].get("nama", "").lower()
                        h_kab = h["_source"].get("kabupaten", "").lower()
                        if "kabupaten" in nama and core in nama and "kab." in h_kab and core in h_kab:
                            smart_hit = h
                            break
                    if smart_hit:
                        logger.info(
                            "[PROFILE] Smart resolve kabupaten: '%s'",
                            smart_hit["_source"].get("nama", "")
                        )
                        return {"status": "success", "hit": smart_hit}
                    has_clear_winner = False  # force ambiguous
 
        logger.debug(
            "[DECIDE] top=%.1f gap=%.1f needed=%d winner=%s weak=%s "
            "wilayah=%s resolved=%s",
            top_score, score_gap,
            GAP_WEAK if is_weak_query else GAP_STRONG,
            has_clear_winner, is_weak_query,
            wilayah, wilayah_resolved,
        )
 
        # ── Keputusan ──────────────────────────────────────────────────────────

        # Guard: jika wilayah disebutkan user tapi tidak dikenali ES (wilayah_resolved=False)
        # DAN total hasil sangat besar (> 50.000 = pool nasional tanpa filter),
        # gap score antar kandidat tidak bermakna → paksa ambiguous.
        # Ini mencegah auto-resolve ke sekolah yang salah provinsi.
        # Contoh: "sdn 1 pujon malang" → Kapuas, bukan Malang (total 147.422).
        LARGE_POOL_THRESHOLD = 50_000
        if (
            wilayah                           # user menyebut wilayah
            and not wilayah_resolved          # tapi ES tidak mengenalinya
            and total > LARGE_POOL_THRESHOLD  # pool hasil sangat besar
        ):
            logger.info(
                "[DECIDE] Force ambiguous: wilayah='%s' tidak resolved, total=%d > %d",
                wilayah, total, LARGE_POOL_THRESHOLD,
            )
            return self._ambiguous(hits, total, is_count_exact)

        if top_score < MIN_CONFIDENT_SCORE:
            return self._ambiguous(hits, total, is_count_exact)
 
        if not has_clear_winner and is_weak_query and total > 1:
            logger.info(
                "[DECIDE] Ambiguous: gap=%.1f < %d (weak query, wilayah tidak resolved)",
                score_gap, GAP_WEAK,
            )
            return self._ambiguous(hits, total, is_count_exact)
 
        if total > 1:
            wilayah_extra = parsed_query.get("wilayah_extra")
            if wilayah_extra:
                resolved = self._try_resolve_with_extra(
                    hits, wilayah_extra, school_name
                )
                if resolved:
                    logger.info(
                        "[PROFILE] Resolved via wilayah_extra='%s'", wilayah_extra
                    )
                    return {"status": "success", "hit": resolved}
 
            if not has_clear_winner and not should_auto_resolve(parsed_query, hits):
                return self._ambiguous(hits, total, is_count_exact)
 
        if wilayah and has_wilayah and total > 1:
            if not self._result_matches_wilayah(top_hit, wilayah):
                matching = [
                    h for h in hits
                    if self._result_matches_wilayah(h, wilayah)
                ]
                if matching:
                    return self._ambiguous(hits, total, is_count_exact)
 
        return {
            "status": "success",
            "hit": top_hit,
            "weak_match": is_weak_query,
        }

    def _result_matches_wilayah(self, hit, wilayah: str) -> bool:
        """
        Cek apakah hasil search berlokasi di wilayah yang diminta.
        """
        source = hit["_source"]
        wilayah_clean = strip_location_prefix(normalize_text(wilayah))
 
        kab  = strip_location_prefix(normalize_text(source.get("kabupaten", "")))
        kec  = strip_location_prefix(normalize_text(source.get("kecamatan", "")))
        prov = strip_location_prefix(normalize_text(source.get("provinsi", "")))
 
        return (
            wilayah_clean in kab
            or wilayah_clean in kec
            or wilayah_clean in prov
            or kab in wilayah_clean
        )
 
    # =====================================================
    # PROFILE BUILD
    # =====================================================
 
    def _build_profile(self, hit):
 
        source = hit["_source"]
 
        raw_content = source.get("content") or "{}"
 
        try:
            parsed_content = json.loads(raw_content)
        except Exception:
            parsed_content = {}
 
        return build_profile(source, parsed_content)
    
    # =====================================================
    # VALIDATE LOCATION
    # =====================================================
    def _is_valid_location(self, wilayah: str) -> bool:
        """
        Cek apakah wilayah yang di-parse benar-benar ada di LOCATION_INDEX.
        Jika tidak ada → kemungkinan bukan nama lokasi tapi bagian nama sekolah.
        """
        if not wilayah:
            return False
        try:
            from app.services.location_service import LOCATION_INDEX
        except ImportError:
            return True  # Jika tidak bisa import, asumsikan valid
 
        wilayah_clean = wilayah.lower().strip()
        # Bersihkan prefix
        import re
        wilayah_clean = re.sub(
            r'^(kota\s+adm\.?\s*|kota\s+|kab\.?\s+|kabupaten\s+|'
            r'prov\.?\s+|provinsi\s+|kec\.?\s+|kecamatan\s+)',
            '', wilayah_clean
        ).strip()
 
        for level in ("kabupaten", "kecamatan", "provinsi", "desa_kelurahan"):
            lookup = LOCATION_INDEX.get(level, {})
            if wilayah_clean in lookup:
                return True
            # Cek juga tanpa spasi (untuk "tangsel" dst)
            if wilayah_clean.replace(" ", "") in lookup:
                return True
 
        return False
    
    # =====================================================
    # RESPONSE
    # =====================================================
 
    def _build_response(self, profile, hit, weak_match: bool = False, is_npsn: bool = False):

        completeness = compute_profile_completeness(profile)

        quality = analyze_profile_quality(profile)

        if is_npsn:
            confidence       = 0.95
            answer_confident = True
        else:
            score            = hit.get("_custom_score", 0)
            confidence       = min(0.95, score / 100)
            answer_confident = confidence >= 0.7

        return {
            "status":             "success",
            "data":               profile,
            "confidence":         confidence,
            "answer_confident":   answer_confident,
            "completeness_score": completeness,
            "quality":            quality,
            "weak_match":         weak_match,
        }
 
    # =====================================================
    # CACHE
    # =====================================================
 
    async def _get_cache(self, query):
 
        key = self._cache_key(query)
 
        return await get_cache(key)
 
    async def _set_cache(self, query, data):
 
        key = self._cache_key(query)
 
        await set_cache(key, data, ttl=3600)
 
    def _cache_key(self, query):
 
        return f"profile:{hashlib.md5(query.encode()).hexdigest()}"
 
    # =====================================================
    # HELPERS
    # =====================================================
 
    def _normalize_query(self, query: str) -> str:
 
        q = query.lower()
        q = re.sub(r"[?]", "", q)
        q = re.sub(r"\s+", " ", q)
 
        return q.strip()
 
    def _extract_npsn(self, query: str):
 
        match = re.search(r"\b\d{8}\b", query)
 
        if match:
            return match.group(0)
 
        return None
 
    def _not_found(self):
 
        return {
            "status": "not_found",
            "message": "Sekolah tidak ditemukan."
        }
        
    def _try_resolve_with_extra(self, hits: list, wilayah_extra: str,
                                 query_name: str = "") -> Optional[dict]:
        """
        Coba resolve ambiguous dengan mencocokkan wilayah_extra.
        Prioritas:
        1. Exact match desa → jika hanya 1 → resolve
        2. Fuzz name similarity → jika top-2 diff > 10 → resolve
        """
        from app.services.core.ranking_engine import normalize_text, strip_location_prefix
        from rapidfuzz import fuzz
 
        extra_clean = normalize_text(wilayah_extra)
 
        # Step 1: cari semua yang cocok (partial match)
        matching = []
        for h in hits:
            src = h["_source"]
            desa = strip_location_prefix(normalize_text(src.get("desa_kelurahan") or ""))
            kec  = strip_location_prefix(normalize_text(src.get("kecamatan") or ""))
 
            if extra_clean in desa or extra_clean in kec or desa in extra_clean or kec in extra_clean:
                matching.append(h)
 
        if not matching:
            return None
 
        if len(matching) == 1:
            logger.info("[PROFILE] Resolved via wilayah_extra='%s' (1 match)", wilayah_extra)
            return matching[0]
 
        # Step 2: Jika 2+ matching, coba exact desa match
        exact_desa = []
        for h in matching:
            src = h["_source"]
            desa = strip_location_prefix(normalize_text(src.get("desa_kelurahan") or ""))
            if extra_clean == desa:
                exact_desa.append(h)
 
        if len(exact_desa) == 1:
            logger.info("[PROFILE] Resolved via wilayah_extra='%s' (exact desa match)", wilayah_extra)
            return exact_desa[0]
 
        # Step 3: Fallback ke fuzz name similarity
        if query_name:
            qn = normalize_text(query_name)
            scores = [
                (h, fuzz.token_set_ratio(qn, normalize_text(h["_source"].get("nama", ""))))
                for h in matching
            ]
            scores.sort(key=lambda x: x[1], reverse=True)
 
            if len(scores) >= 2 and scores[0][1] - scores[1][1] > 10:
                logger.info("[PROFILE] Resolved via wilayah_extra='%s' + name_similarity",
                            wilayah_extra)
                return scores[0][0]
 
        return None
    
    def _ambiguous(self, hits, total: int = 0, is_count_exact: bool = True):
 
        candidates = []
        seen = set()
 
        for h in hits:
            s    = h["_source"]
            nama = s.get("nama") or ""
            kab  = s.get("kabupaten") or ""
            dedup_key = f"{nama.lower()}|{kab.lower()}"
 
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
 
            # FIX: filter None dan string kosong dari lokasi parts
            lokasi_parts = [
                s.get("desa_kelurahan"),
                s.get("kecamatan"),
                kab,
                s.get("provinsi"),
            ]
            lokasi = ", ".join(
                str(p) for p in lokasi_parts
                if p and str(p).strip().lower() not in ("none", "", "-")
            )
 
            candidates.append({
                "nama":   nama,
                "npsn":   s.get("npsn"),
                "lokasi": lokasi or "-",
            })
 
            if len(candidates) >= 10:
                break
 
        shown = len(candidates)
 
        if total > shown:
            if is_count_exact:
                msg = (
                    f"Ditemukan **{total}** sekolah, menampilkan {shown} teratas. "
                    f"Mohon perjelas sekolah yang dimaksud:"
                )
            else:
                msg = (
                    f"Ditemukan setidaknya **{total}** sekolah, menampilkan {shown} teratas. "
                    f"Mohon perjelas sekolah yang dimaksud:"
                )
        else:
            msg = "Mohon perjelas sekolah yang dimaksud:"
 
        return {
            "status":      "ambiguous",
            "message":     msg,
            "candidates":  candidates,
            "total_found": total,
        }
