"""Redacted operational signals: numbers, timestamps, and fixed-shape labels only.

Output never contains email addresses, message content, tokens, or URLs. Anything
caller-supplied that ends up in the output (queue and service names) must match
a strict identifier pattern or it is dropped, so a stray address or URL cannot leak
through a label. The app records provider/SQLite timings here; everything else
(queue timestamps, sync age, backup time, ...) is handed to `snapshot()` by the caller.
"""

import re
import shutil
import threading
import time
from contextlib import contextmanager

_LABEL = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_LAT_WINDOW = 500  # recent samples kept per latency series


class _Series:
    def __init__(self):
        self.samples = []
        self.count = 0

    def add(self, seconds):
        self.count += 1
        self.samples = (self.samples + [seconds])[-_LAT_WINDOW:]

    def summary(self):
        s = sorted(self.samples)
        pick = lambda q: round(s[min(len(s) - 1, int(q * len(s)))] * 1000, 1) if s else None  # noqa: E731
        return {"count": self.count, "p50_ms": pick(0.5), "p95_ms": pick(0.95), "max_ms": pick(1.0)}


class Metrics:
    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._clock = clock
        self.provider, self.sqlite = _Series(), _Series()
        self.provider_retries = 0
        self.uncertain_sends = 0

    def provider_call(self, seconds, retries=0):
        with self._lock:
            self.provider.add(seconds)
            self.provider_retries += max(0, int(retries))

    def sqlite_call(self, seconds):
        with self._lock:
            self.sqlite.add(seconds)

    @contextmanager
    def time_sqlite(self):
        t = time.perf_counter()
        try:
            yield
        finally:
            self.sqlite_call(time.perf_counter() - t)

    def uncertain_send(self):
        with self._lock:
            self.uncertain_sends += 1

    def snapshot(self, *, queues=None, gmail_last_sync=None, data_root=None, services=(), backup_verified_at=None):
        """queues: {name: [enqueue epoch seconds, ...]}; gmail_last_sync / backup_verified_at: epoch seconds or None."""
        now = self._clock()
        age = lambda t: None if t is None else max(0, round(now - t, 1))  # noqa: E731
        out_q = {}
        for name, times in (queues or {}).items():
            if _LABEL.fullmatch(str(name)):
                out_q[name] = {"depth": len(times), "oldest_age_s": age(min(times)) if times else 0}
        with self._lock:
            out = {
                "queues": out_q,
                "provider": {**self.provider.summary(), "retries": self.provider_retries},
                "sqlite": self.sqlite.summary(),
                "uncertain_sends": self.uncertain_sends,
            }
        out["gmail_sync_age_s"] = age(gmail_last_sync)
        out["backup_verified_age_s"] = age(backup_verified_at)
        names = sorted({str(s) for s in services if _LABEL.fullmatch(str(s))})
        out["services"] = {"active": len(names), "names": names}
        out["disk"] = None
        if data_root is not None:
            try:
                d = shutil.disk_usage(data_root)
                pct = round(100 * d.used / d.total, 1)
                out["disk"] = {"total_bytes": d.total, "free_bytes": d.free, "used_pct": pct}
            except OSError:
                pass
        return out


metrics = Metrics()
