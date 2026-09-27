"""Bounded worker coordinator: two research workers, one writer, bounded queues.

Request handlers only enqueue (via a background task, so a full queue gives
backpressure without blocking the HTTP response). Job state lives in SQLite
so a restart can mark interrupted work resumable. Each job logs under its own
job_id and the request_id that queued it.

Shutdown: close() stops workers taking new items, gives running jobs `grace`
seconds to finish, cancels the rest, and checkpoints every unfinished job as
`interrupted` so Resume picks it up after the next start.
"""
import asyncio
import logging
import secrets
import time

from .logs import job_id as job_ctx
from .logs import request_id as request_ctx

log = logging.getLogger("jobs")


class Coordinator:
    def __init__(self, cache, handlers, research_workers=2, queue_size=16):
        self.cache = cache
        self.handlers = handlers  # kind -> async fn(campaign_id, candidate_id)
        self.queues = {"research": asyncio.Queue(queue_size), "write": asyncio.Queue(queue_size)}
        self.workers = {"research": research_workers, "write": 1}
        self.stopped = set()
        self.tasks = []
        self.running = 0
        self.closing = False

    def start(self):
        self.loop = asyncio.get_running_loop()
        for lane, n in self.workers.items():
            for _ in range(n):
                self.tasks.append(asyncio.create_task(self._run(lane)))

    async def close(self, grace=0.0):
        self.closing = True
        end = time.monotonic() + grace
        while self.running and time.monotonic() < end:
            await asyncio.sleep(0.05)
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        self.cache.mark_interrupted()  # checkpoint: queued/running rows become resumable
        log.info("job workers stopped", extra={"status": "interrupted" if self.running else "drained"})

    def depths(self):
        return {lane: q.qsize() for lane, q in self.queues.items()}

    def alive(self):
        return bool(self.tasks) and not any(t.done() for t in self.tasks)

    @staticmethod
    def lane(kind):
        return "write" if kind == "write" else "research"  # discover shares research lane

    def submit(self, campaign_id, kind, candidate_id=None):
        if self.closing:
            raise RuntimeError("shutting down; not accepting new jobs")
        job_id = f"job_{secrets.token_hex(5)}"
        self.cache.put_job(job_id, campaign_id, kind, candidate_id, "queued")
        self.stopped.discard(campaign_id)
        item = (job_id, campaign_id, kind, candidate_id, request_ctx.get())
        # thread-safe: sync route handlers run in a threadpool
        asyncio.run_coroutine_threadsafe(self.queues[self.lane(kind)].put(item), self.loop)
        return job_id

    async def _run(self, lane):
        q = self.queues[lane]
        while True:
            job_id, campaign_id, kind, candidate_id, req = await q.get()
            job_ctx.set(job_id)
            request_ctx.set(req)
            extra = {"campaign_id": campaign_id, "kind": kind}
            started = time.monotonic()
            try:
                if campaign_id in self.stopped or self.closing:
                    self.cache.put_job(job_id, campaign_id, kind, candidate_id, "stopped" if not self.closing else "interrupted")
                    continue
                self.running += 1
                self.cache.put_job(job_id, campaign_id, kind, candidate_id, "running")
                log.info("job started", extra=extra)
                try:
                    await self.handlers[kind](campaign_id, candidate_id)
                finally:
                    self.running -= 1
                self.cache.put_job(job_id, campaign_id, kind, candidate_id, "done")
                log.info("job done", extra={**extra, "duration_ms": int((time.monotonic() - started) * 1000)})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # one failure never blocks the batch
                log.exception("job failed", extra=extra)
                self.cache.put_job(job_id, campaign_id, kind, candidate_id, "failed", f"{type(e).__name__}: {str(e)[:200]}")
            finally:
                q.task_done()

    def stop(self, campaign_id):
        self.stopped.add(campaign_id)
