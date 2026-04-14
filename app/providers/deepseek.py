import os
import logging
from typing import Optional, Dict, Any
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from dotenv import load_dotenv

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
DEFAULT_TIMEOUT = 30
DEFAULT_TEMPERATURE = 0.7
USER_AGENT = "RAG-Server/1.0"

if not DEEPSEEK_API_KEY:
    logger.warning("DEEPSEEK_API_KEY tidak ditemukan di environment variables")

# ================= HTTP CLIENT =================
_http_client: Optional[httpx.AsyncClient] = None

async def get_http_client() -> httpx.AsyncClient: 
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0), 
            headers={"User-Agent": USER_AGENT}
        )
    return _http_client

# ================= RETRY STRATEGY =================
retry_strategy = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(DeepSeekUnavailableError),
    reraise=True
)

# ================= CORE =================
@retry_strategy
async def call_deepseek(
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
        response = await client.post(
            DEEPSEEK_API_URL,
            json=payload,
            headers=headers,
            timeout=timeout
        )

        response.raise_for_status()
        data = response.json()

        choices = data.get("choices")
        if not choices:
            raise DeepSeekClientError("Invalid DeepSeek response format")

        return choices[0]["message"]["content"].strip()

    except httpx.HTTPStatusError as e:
        status = e.response.status_code

        if status in (402, 429):
            raise DeepSeekUnavailableError("DeepSeek quota / rate limit exceeded")
        elif status >= 500:
            raise DeepSeekUnavailableError("DeepSeek internal server error")
        elif status == 401:
            raise DeepSeekClientError("DeepSeek API unauthorized")
        else:
            raise DeepSeekClientError(f"DeepSeek HTTP error {status}")

    except (httpx.TimeoutException, httpx.ConnectError):
        raise DeepSeekUnavailableError("DeepSeek network error")

    except httpx.RequestError as e:
        raise DeepSeekUnavailableError(f"DeepSeek request error: {e}")

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