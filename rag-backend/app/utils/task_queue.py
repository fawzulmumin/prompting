"""
SimpleTaskQueue: asyncio.Queue-based background task queue.

Lebih ringan dari arq (tidak perlu Redis untuk queue).
Digunakan untuk: set_cache non-critical, log_rag_evaluation, analytics flush.

Tidak untuk: operasi kritis yang butuh guarantee delivery antar restart.
"""

import asyncio
import logging
from typing import Coroutine, Optional

logger = logging.getLogger(__name__)


class SimpleTaskQueue:
    def __init__(self, maxsize: int = 100, num_workers: int = 2):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._workers: list = []
        self._num_workers = num_workers
        self._running = False

    async def start(self) -> None:
        """Start worker coroutines. Panggil saat app startup."""
        self._running = True
        for i in range(self._num_workers):
            worker = asyncio.create_task(self._worker(i))
            self._workers.append(worker)
        logger.info("[TaskQueue] Started %d workers", self._num_workers)

    async def stop(self) -> None:
        """Graceful shutdown. Panggil saat app shutdown."""
        self._running = False
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)  # poison pill
            except asyncio.QueueFull:
                pass
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("[TaskQueue] Stopped")

    async def enqueue(self, coro: Coroutine, *, drop_if_full: bool = True) -> bool:
        """Tambahkan coroutine ke queue. Return False jika queue penuh."""
        try:
            self._queue.put_nowait(coro)
            return True
        except asyncio.QueueFull:
            logger.warning("[TaskQueue] Queue full, dropping task")
            coro.close()  # prevent coroutine leak
            return False

    async def _worker(self, worker_id: int) -> None:
        while self._running:
            try:
                coro = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                if coro is None:
                    self._queue.task_done()
                    break  # poison pill
                try:
                    await coro
                except Exception as e:
                    logger.exception("[TaskQueue] Worker %d task error: %s", worker_id, e)
                finally:
                    self._queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.exception("[TaskQueue] Worker %d unexpected error: %s", worker_id, e)

    def stats(self) -> dict:
        return {
            "queue_size": self._queue.qsize(),
            "num_workers": self._num_workers,
            "running": self._running,
        }


# Singleton
_task_queue: Optional[SimpleTaskQueue] = None


def get_task_queue() -> SimpleTaskQueue:
    global _task_queue
    if _task_queue is None:
        _task_queue = SimpleTaskQueue(maxsize=100, num_workers=2)
    return _task_queue
