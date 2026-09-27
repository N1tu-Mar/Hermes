"""Bounded worker coordinator: two research workers, one writer, bounded queues.

Request handlers only enqueue (via a background task, so a full queue gives
backpressure without blocking the HTTP response). Job state lives in SQLite
so a restart can mark interrupted work resumable.
"""
import asyncio
import logging
import secrets

log = logging.getLogger("jobs")


class Coordinator:
    def __init__(self, cache, handlers, research_workers=2, queue_size=16):
        self.cache = cache
        self.handlers = handlers  # kind -> async fn(campaign_id, candidate_id)
        self.queues = {"research": asyncio.Queue(queue_size), "write": asyncio.Queue(queue_size)}
        self.workers = {"research": research_workers, "write": 1}
        self.stopped = set()
        self.on_finish = None  # optional fn(job_id, campaign_id, kind, status, error) for notifications
        self.tasks = []

    def start(self):
        self.loop = asyncio.get_running_loop()
        for lane, n in self.workers.items():
            for _ in range(n):
                self.tasks.append(asyncio.create_task(self._run(lane)))

    async def close(self):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    @staticmethod
    def lane(kind):
        return "write" if kind in ("write", "followup") else "research"  # discover shares research lane

    def submit(self, campaign_id, kind, candidate_id=None):
        job_id = f"job_{secrets.token_hex(5)}"
        self.cache.put_job(job_id, campaign_id, kind, candidate_id, "queued")
        self.stopped.discard(campaign_id)
        # thread-safe: sync route handlers run in a threadpool
        asyncio.run_coroutine_threadsafe(self.queues[self.lane(kind)].put((job_id, campaign_id, kind, candidate_id)), self.loop)
        return job_id

    async def _run(self, lane):
        q = self.queues[lane]
        while True:
            job_id, campaign_id, kind, candidate_id = await q.get()
            try:
                if campaign_id in self.stopped:
                    self.cache.put_job(job_id, campaign_id, kind, candidate_id, "stopped")
                    continue
                self.cache.put_job(job_id, campaign_id, kind, candidate_id, "running")
                await self.handlers[kind](campaign_id, candidate_id)
                self.cache.put_job(job_id, campaign_id, kind, candidate_id, "done")
                self._finished(job_id, campaign_id, kind, "done", None)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # one failure never blocks the batch
                log.exception("job %s failed", job_id)
                error = f"{type(e).__name__}: {str(e)[:200]}"
                self.cache.put_job(job_id, campaign_id, kind, candidate_id, "failed", error)
                self._finished(job_id, campaign_id, kind, "failed", error)
            finally:
                q.task_done()

    def _finished(self, *args):
        try:
            if self.on_finish:
                self.on_finish(*args)
        except Exception:
            log.exception("on_finish hook failed")

    def stop(self, campaign_id):
        self.stopped.add(campaign_id)
