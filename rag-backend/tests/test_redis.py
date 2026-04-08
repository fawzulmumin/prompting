from dotenv import load_dotenv
import os
import redis.asyncio as r
import asyncio

load_dotenv()

async def t():
    rc = r.from_url(os.getenv("REDIS_URL"))
    await rc.ping()
    print("✅ Redis OK")
    await rc.close()

asyncio.run(t())
