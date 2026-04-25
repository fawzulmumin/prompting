import os
import json
import time as _time
import logging
from typing import Optional, Dict, Any
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from dotenv import load_dotenv

from app.utils.cache import increment_counter, get_counter

# ================= LOAD ENV =================
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / (".env.production" if os.getenv("PROD") == "production" else ".env")
load_dotenv(ENV_FILE)

# ================= LOGGER =================
logger = logging.getLogger(__name__)

# ================= EXCEPTIONS =================
class DeepSeekUnavailableError(Exception):
    """Provider DeepSeek unavailable (quota, 5xx, timeout, network)"""

class DeepSeekClientError(RuntimeError):
    """Client-side error (auth, bad request, config)"""

# ================= CONFIG =================
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

DEFAULT_MODEL = "deepseek-chat"
# Per-phase timeout. Read timeout paling penting karena LLM streaming body
# bisa 10-30 detik untuk output kompleks.
DEFAULT_TIMEOUT = 45
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_READ_TIMEOUT = 60
DEFAULT_WRITE_TIMEOUT = 10
DEFAULT_POOL_TIMEOUT = 5
DEFAULT_TEMPERATURE = 0.7
USER_AGENT = "RAG-Server/1.0"

if not DEEPSEEK_API_KEY:
    logger.warning("DEEPSEEK_API_KEY tidak ditemukan di environment variables")

# ================= STRUCTURED LOG HELPER =================
def _log_rl(level: str, event: str, **kwargs):
    payload = {"event": event, "ts": round(_time.time(), 3), **kwargs}
    getattr(logger, level)(json.dumps(payload, default=str))

# ================= RATE LIMIT & CIRCUIT BREAKER CONSTANTS =================
_RL_KEY        = "ratelimit:deepseek:global"
_RL_WINDOW     = 60    # detik, 1 menit rolling window
_RL_MAX_REQ    = int(os.getenv("DEEPSEEK_RL_MAX_REQ", 50))

_CB_KEY        = "circuit:deepseek:failures"
_CB_THRESHOLD  = int(os.getenv("DEEPSEEK_CB_THRESHOLD", 5))
_CB_RESET_TTL  = 120   # detik circuit breaker open sebelum half-open

# ================= HTTP CLIENT =================
_http_client: Optional[httpx.AsyncClient] = None

async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=DEFAULT_CONNECT_TIMEOUT,
                read=DEFAULT_READ_TIMEOUT,
                write=DEFAULT_WRITE_TIMEOUT,
                pool=DEFAULT_POOL_TIMEOUT,
            ),
            headers={"User-Agent": USER_AGENT},
        )
    return _http_client

# ================= RETRY STRATEGY =================
# 2 attempts (1 retry): gagal-cepat lebih penting daripada retrying terus-terusan.
# Total worst-case: timeout × 2 + 1s wait ≈ 2× timeout + 1
retry_strategy = retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.5, min=1, max=3),
    retry=retry_if_exception_type(DeepSeekUnavailableError),
    reraise=True
)

# ================= RATE LIMIT & CIRCUIT BREAKER HELPERS =================
async def _check_rate_limit() -> bool:
    """
    Return True jika request boleh dilanjutkan, False jika harus ditolak.
    Graceful: jika Redis tidak available, return True (fail open).
    """
    try:
        current = await get_counter(_RL_KEY) or 0
        if current >= _RL_MAX_REQ:
            _log_rl("warning", "rate_limit_exceeded",
                    current=current, max=_RL_MAX_REQ, window=_RL_WINDOW)
            return False
        await increment_counter(_RL_KEY, ttl=_RL_WINDOW)
        return True
    except Exception as e:
        logger.warning("[DeepSeek] rate limit check failed (Redis?): %s", e)
        return True  # fail open


async def _check_circuit_breaker() -> bool:
    """
    Return True jika circuit CLOSED (boleh request).
    Return False jika circuit OPEN (fail-fast).
    """
    try:
        failures = await get_counter(_CB_KEY) or 0
        if failures >= _CB_THRESHOLD:
            _log_rl("warning", "circuit_breaker_open",
                    failures=failures, threshold=_CB_THRESHOLD)
            return False
        return True
    except Exception:
        return True  # fail open


async def _record_cb_failure():
    """Increment circuit breaker counter setelah 429."""
    try:
        await increment_counter(_CB_KEY, ttl=_CB_RESET_TTL)
    except Exception:
        pass


async def _reset_cb():
    """Reset circuit breaker setelah sukses."""
    try:
        from app.utils.cache import delete_cache
        await delete_cache(_CB_KEY)
    except Exception:
        pass

# ================= CORE (inner, decorated with retry) =================
@retry_strategy
async def _call_deepseek_inner(
    prompt: str,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: Optional[int] = None,
    timeout: int = DEFAULT_TIMEOUT
) -> str:

    if not DEEPSEEK_API_KEY:
        raise DeepSeekClientError("DEEPSEEK_API_KEY tidak ditemukan")

    if not prompt or not prompt.strip():
        raise ValueError("Prompt tidak boleh kosong")

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(temperature)
    }

    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }

    client = await get_http_client()

    try:
        # Treat caller's timeout as read timeout; connect stays short
        request_timeout = httpx.Timeout(
            connect=min(DEFAULT_CONNECT_TIMEOUT, timeout),
            read=timeout,
            write=DEFAULT_WRITE_TIMEOUT,
            pool=DEFAULT_POOL_TIMEOUT,
        )
        response = await client.post(
            DEEPSEEK_API_URL,
            json=payload,
            headers=headers,
            timeout=request_timeout,
        )

        response.raise_for_status()
        data = response.json()

        choices = data.get("choices")
        if not choices:
            raise DeepSeekClientError("Invalid DeepSeek response format")

        await _reset_cb()
        return choices[0]["message"]["content"].strip()

    except httpx.HTTPStatusError as e:
        status = e.response.status_code

        if status in (402, 429):
            await _record_cb_failure()
            raise DeepSeekUnavailableError("DeepSeek quota / rate limit exceeded")
        elif status >= 500:
            raise DeepSeekUnavailableError("DeepSeek internal server error")
        elif status == 401:
            raise DeepSeekClientError("DeepSeek API unauthorized")
        else:
            raise DeepSeekClientError(f"DeepSeek HTTP error {status}")

    except httpx.ReadTimeout as e:
        raise DeepSeekUnavailableError(
            f"DeepSeek read timeout — LLM generation lebih lama dari timeout ({e})"
        )
    except httpx.ConnectTimeout as e:
        raise DeepSeekUnavailableError(
            f"DeepSeek connect timeout — network slow ({e})"
        )
    except httpx.ConnectError as e:
        raise DeepSeekUnavailableError(f"DeepSeek connection error: {e}")
    except httpx.TimeoutException as e:
        raise DeepSeekUnavailableError(f"DeepSeek generic timeout: {type(e).__name__}: {e}")

    except httpx.RequestError as e:
        raise DeepSeekUnavailableError(f"DeepSeek request error: {e}")


# ================= PUBLIC ENTRY POINT (pre-checks, no retry) =================
async def call_deepseek(
    prompt: str,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: Optional[int] = None,
    timeout: int = DEFAULT_TIMEOUT
) -> str:
    if not await _check_circuit_breaker():
        raise DeepSeekUnavailableError(
            "DeepSeek circuit breaker open — terlalu banyak error 429"
        )

    if not await _check_rate_limit():
        raise DeepSeekUnavailableError(
            "DeepSeek rate limit lokal tercapai — coba lagi dalam 60 detik"
        )

    return await _call_deepseek_inner(prompt, model, temperature, max_tokens, timeout)


# ================= HEALTH =================
async def check_deepseek_health() -> Dict[str, Any]:
    try:
        await call_deepseek("ping", temperature=0.0, max_tokens=1)
        return {"status": "OK", "provider": "deepseek", "reachable": True}
    except DeepSeekUnavailableError as e:
        return {"status": "DEGRADED", "provider": "deepseek", "reachable": False, "error": str(e)}
    except Exception as e:
        return {"status": "ERROR", "provider": "deepseek", "reachable": False, "error": str(e)}

# ================= SHUTDOWN =================
async def close_deepseek_client():
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None

# ================= MAIN TEST =================
if __name__ == "__main__":
    import asyncio

    async def test():
        print("=" * 60)
        print("🧪 TESTING DEEPSEEK PROVIDER (PRODUCTION VERSION)")
        print("=" * 60)

        health = await check_deepseek_health()
        print("Health:", health)
        logger.warning(f"[DeepSeek] ENV FILE USED: {ENV_FILE}")
        logger.warning(f"[DeepSeek] API KEY loaded: {bool(DEEPSEEK_API_KEY)}")

        try:
            res = await call_deepseek("Apa ibukota Indonesia?", max_tokens=50)
            print("Response:", res)
        except Exception as e:
            print("Error:", e)

        await close_deepseek_client()

    asyncio.run(test())
