# router.py

import json
import re
import time
import logging
from typing import TypedDict, Optional, Dict, Any
from langgraph.graph import StateGraph, START, END
 
from app.providers.deepseek import call_deepseek, DeepSeekUnavailableError
from app.utils.cache import get_cache, set_cache, hash_key
from app.utils.cost_tracker import log_cost, estimate_tokens
 
logger = logging.getLogger(__name__)
 
 
# ==========================================================
# STATE — data yang mengalir antar node
# ==========================================================
 
class GraphState(TypedDict):
    # Input
    query: str
    context: str
    history: str
 
    # Intermediate
    raw_response: Optional[str]       # raw output DeepSeek
    parsed_response: Optional[Dict]   # setelah JSON parse
 
    # Output
    final_response: Optional[str]
    evaluation: Optional[Dict[str, Any]]
    provider: Optional[str]
    duration_ms: Optional[int]
 
    # Observability — timing per node
    _t_generate_ms: Optional[int]
    _t_evaluate_ms: Optional[int]
 
 
class RouterResponse(TypedDict):
    provider: str
    response: str
    evaluation: Dict[str, Any]
    duration_ms: int
    from_cache: bool
 
 
# ==========================================================
# UTILITIES
# ==========================================================
 
def _safe_slice(text: str, limit: int) -> str:
    return text[:limit] if text else ""
 
 
def _extract_json_response(raw_text: str) -> Optional[Dict[str, Any]]:
    if not raw_text:
        return None
    try:
        cleaned = re.sub(r"```json|```", "", raw_text, flags=re.IGNORECASE).strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        json_candidate = re.sub(r"[\x00-\x1F\x7F]", "", match.group(0))
        return json.loads(json_candidate)
    except (json.JSONDecodeError, Exception) as e:
        logger.error("[Router] JSON parse error: %s", e)
        return None
 
 
def _is_context_relevant(query: str, context: str) -> bool:
    query_words  = set(re.findall(r'\b\w{3,}\b', query.lower()))
    context_words = set(re.findall(r'\b\w{3,}\b', context.lower()))
    stopwords = {"yang", "dan", "di", "ke", "dari", "dengan", "untuk", "adalah", "ini", "itu"}
    query_words -= stopwords
    overlap = query_words & context_words
    return len(overlap) >= 2 or any(qw in context.lower() for qw in query_words)
 
 
def _build_prompt(query: str, slim_context: str, slim_history: str) -> str:
    return f"""ANDA ADALAH AI AGENT RAG DENGAN DISIPLIN VERIFIKASI TINGGI.
TUGAS ANDA: Memberikan jawaban berbasis data yang akurat dalam format JSON.
 
════════════════════════════════════
[PEDOMAN KERJA]
════════════════════════════════════
1. ANALISIS KONTEKS:
    - Periksa [KONTEKS DOKUMEN].
    - Berikan skor (0-100) kecukupan konteks.
 
2. PENYAJIAN DATA (WAJIB):
    - Berikan pengantar yang informatif sebelum menyajikan data.
    - Jika ada data angka atau daftar, SAJIKAN DALAM BENTUK TABEL MARKDOWN.
    - Setelah tabel, berikan analisis setidaknya 2-3 paragraf.
 
3. VALIDASI SUMBER (WAJIB):
    - Di akhir jawaban, buat bagian "### Sumber Informasi".
    - Cantumkan link/URL dari metadata atau konten konteks sebagai referensi valid.
    - DILARANG membuat link palsu.
 
4. SELF-VERIFICATION:
    - Hapus klaim tanpa sumber.
    - DILARANG menggunakan asumsi di luar data yang diberikan.
 
════════════════════════════════════
[DATA INPUT]
════════════════════════════════════
KONTEKS DOKUMEN:
{slim_context}
 
RIWAYAT PERCAKAPAN:
{slim_history}
 
PERTANYAAN USER:
{query}
 
════════════════════════════════════
[FORMAT OUTPUT JSON WAJIB]
════════════════════════════════════
{{
    "context_sufficiency_score": 0-100,
    "confidence_flag": "HIGH | LOW",
    "answer": "isi jawaban dalam markdown (termasuk tabel dan link sumber)",
    "is_rewritten": false
}}
 
JAWABAN (JSON ONLY):
"""
 
 
# ==========================================================
# NODE 1: GENERATE
# Panggil DeepSeek, handle truncation dengan 1x retry
# ==========================================================
 
async def generate_node(state: GraphState) -> GraphState:
    t0 = time.time()
 
    slim_context = _safe_slice(state.get("context", ""), 5000) or "TIDAK ADA DATA."
    slim_history = _safe_slice(state.get("history", ""), 1200)
    query = state["query"]
 
    prompt = _build_prompt(query, slim_context, slim_history)
    raw_response = ""
    provider = "deepseek"
 
    try:
        raw_response = await call_deepseek(
            prompt,
            max_tokens=1000,
            temperature=0.3
        )
 
        # Deteksi truncation → retry 1x dengan token lebih besar
        if raw_response and not raw_response.strip().endswith("}"):
            logger.warning("[Router:generate] Response possibly truncated, retrying with more tokens")
            raw_response = await call_deepseek(
                prompt,
                max_tokens=1500,
                temperature=0.3
            )
 
    except DeepSeekUnavailableError:
        logger.warning("[Router:generate] DeepSeek unavailable")
        provider = "FALLBACK"
        raw_response = json.dumps({
            "answer": "Maaf, layanan AI sedang tidak tersedia. Silakan coba kembali.",
            "context_sufficiency_score": 0,
            "confidence_flag": "LOW"
        })
 
    except Exception as e:
        logger.exception("[Router:generate] Unexpected error: %s", e)
        provider = "FALLBACK"
 
        # Heuristic fallback — coba ekstrak angka dari konteks
        is_relevant = _is_context_relevant(query, slim_context)
        if is_relevant and re.search(r'\bberapa\b|\btotal\b|\bjumlah\b', query.lower()):
            numbers = [
                int(n) for n in re.findall(r'\b(\d{3,})\b', slim_context)
                if 100 <= int(n) <= 1_000_000
            ]
            if numbers:
                raw_response = json.dumps({
                    "answer": (
                        f"Berdasarkan data yang tersedia, total estimasi adalah "
                        f"**{sum(numbers):,}**.\n\n"
                        "_Catatan: Hasil heuristik karena layanan AI tidak tersedia._"
                    ),
                    "context_sufficiency_score": 40,
                    "confidence_flag": "LOW"
                })
            else:
                raw_response = json.dumps({
                    "answer": "Data ditemukan namun tidak dapat diproses saat ini.",
                    "context_sufficiency_score": 20,
                    "confidence_flag": "LOW"
                })
        else:
            raw_response = json.dumps({
                "answer": "Maaf, terjadi kesalahan dalam memproses pertanyaan Anda.",
                "context_sufficiency_score": 0,
                "confidence_flag": "LOW"
            })
 
    t_generate_ms = int((time.time() - t0) * 1000)
 
    # Log cost
    try:
        await log_cost(
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(raw_response),
            query=query,
            duration_ms=t_generate_ms,
        )
    except Exception:
        logger.warning("[Router:generate] log_cost failed")
 
    return {
        **state,
        "raw_response":   raw_response,
        "provider":       provider,
        "_t_generate_ms": t_generate_ms,
    }
 
 
# ==========================================================
# NODE 2: EVALUATE
# Parse JSON, normalize score, siapkan output final
# Independen dari DeepSeek → bisa di-test dengan raw_response mock
# ==========================================================
 
def evaluate_node(state: GraphState) -> GraphState:
    t0 = time.time()
 
    raw_response = state.get("raw_response") or ""
    query        = state["query"]
    context      = state.get("context", "")
 
    parsed = _extract_json_response(raw_response)
 
    # Fallback jika parse gagal total
    if not parsed:
        logger.warning("[Router:evaluate] JSON parse failed, using raw as answer")
        parsed = {
            "answer":                    raw_response,
            "context_sufficiency_score": 0,
            "confidence_flag":           "LOW",
        }
 
    # Normalize score → pastikan 0-100
    score = parsed.get("context_sufficiency_score", 0)
    try:
        score = float(score)
        # LLM kadang return 0.0-1.0 range
        if score <= 1.0 and score > 0:
            score = score * 100
        score = int(min(score, 100))
    except (TypeError, ValueError):
        score = 0
 
    final_response = parsed.get("answer") or raw_response
 
    t_evaluate_ms = int((time.time() - t0) * 1000)
 
    logger.info(
        "[Router] done provider=%s score=%d t_gen=%dms t_eval=%dms",
        state.get("provider", "deepseek"),
        score,
        state.get("_t_generate_ms") or 0,
        t_evaluate_ms,
    )
 
    return {
        **state,
        "parsed_response":  parsed,
        "final_response":   final_response,
        "evaluation": {
            "score":      score,
            "confidence": parsed.get("confidence_flag", "LOW"),
        },
        "duration_ms":     (state.get("_t_generate_ms") or 0) + t_evaluate_ms,
        "_t_evaluate_ms":  t_evaluate_ms,
    }


# ==========================================================
# COMPILED GRAPH SINGLETON
# Nodes are stateless — compiled once at module load, reused per request.
# ==========================================================

_workflow = StateGraph(GraphState)
_workflow.add_node("generate", generate_node)
_workflow.add_node("evaluate", evaluate_node)
_workflow.add_edge(START,      "generate")
_workflow.add_edge("generate", "evaluate")
_workflow.add_edge("evaluate", END)
_COMPILED_GRAPH = _workflow.compile()


# ==========================================================
# ENTRYPOINT — dipanggil dari rag_server.py (RAG flow saja)
# Interface SAMA dengan versi lama: signature dan return keys identik
# ==========================================================

async def route_with_fallback(
    query: str,
    initial_context: str,
    history_context: str = "",
    es_client=None,           # retained for signature compatibility, not used here
) -> RouterResponse:
 
    # Cache key hanya dari query + initial_context.
    # history_context sengaja dikecualikan: setiap sesi konversasi menghasilkan
    # key unik sehingga cache tidak pernah hit untuk query yang sama. Jawaban
    # faktual (data sekolah) tidak berubah signifikan karena riwayat percakapan.
    cache_key = hash_key(query, initial_context)
    cached = await get_cache(cache_key)
    if cached:
        logger.info("[Router] Cache hit")
        return {**cached, "from_cache": True, "provider": cached.get("provider", "cache")}
 
    initial_state: GraphState = {
        "query":           query,
        "context":         initial_context,
        "history":         history_context,
        "raw_response":    None,
        "parsed_response": None,
        "final_response":  None,
        "evaluation":      None,
        "provider":        "deepseek",
        "duration_ms":     None,
        "_t_generate_ms":  None,
        "_t_evaluate_ms":  None,
    }
 
    final_state = await _COMPILED_GRAPH.ainvoke(initial_state)
 
    result: RouterResponse = {
        "provider":    final_state.get("provider") or "none",
        "response":    final_state.get("final_response") or "Maaf, terjadi kesalahan internal.",
        "evaluation":  final_state.get("evaluation") or {"score": 0, "confidence": "LOW"},
        "duration_ms": final_state.get("duration_ms") or 0,
        "from_cache":  False,
    }
 
    # Cache result
    await set_cache(cache_key, {
        "provider":   result["provider"],
        "response":   result["response"],
        "evaluation": result["evaluation"],
        "duration_ms": result["duration_ms"],
    })
 
    return result
