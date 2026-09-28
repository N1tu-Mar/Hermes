"""Remote-mode runtime state: bounded per-user services and bounded pending OAuth states."""

import asyncio
import logging
import time
from collections import Counter, OrderedDict

log = logging.getLogger("api")

MAX_ACTIVE_SERVICES = 32
IDLE_EVICT_SECONDS = 900.0
MAX_PENDING_OAUTH = 256
MAX_PENDING_PER_USER = 3
OAUTH_TTL = 600.0
MAINTENANCE_INTERVAL = 60.0
RETENTION_INTERVAL = 6 * 3600.0


class Busy(Exception):
    """Every slot is in use by a request or a running job; the caller should retry shortly."""


class ServicePool:
    """At most `limit` live per-user services. Services are built lazily, tracked by last use, and evicted
    (least recently used first) only when no request or job is using them. An evicted user's next request
    waits for the old service to finish shutting down, then rebuilds from disk."""

    def __init__(self, build, grace, limit=MAX_ACTIVE_SERVICES, idle=IDLE_EVICT_SECONDS, clock=time.monotonic):
        self.build, self.grace, self.limit, self.idle, self.clock = build, grace, limit, idle, clock
        self.live = OrderedDict()  # uid -> service, least recently used first
        self.last_used = {}
        self.busy = Counter()  # uid -> requests currently holding the service
        self.closing = {}  # uid -> shutdown task
        self.closed = False

    def __len__(self):
        return len(self.live)

    async def acquire(self, uid):
        while True:
            if self.closed:
                raise Busy("shutting down")
            if uid in self.live:
                self.live.move_to_end(uid)
                self.last_used[uid] = self.clock()
                self.busy[uid] += 1
                return self.live[uid]
            if uid in self.closing:
                await asyncio.shield(self.closing[uid])
                continue
            if len(self.live) + len(self.closing) >= self.limit:
                victim = next((u for u, s in self.live.items() if self._idle(u, s)), None)
                if victim is None and not self.closing:
                    raise Busy("too many active users")
                if victim is not None:
                    self._evict(victim)
                await asyncio.shield(next(iter(self.closing.values())))
                continue
            self.live[uid] = self.build(uid)  # no await between the checks above and this insert
            self.last_used[uid] = self.clock()

    def release(self, uid):
        self.busy[uid] -= 1
        if self.busy[uid] <= 0:
            del self.busy[uid]
        self.last_used[uid] = self.clock()

    def _idle(self, uid, svc):
        return not self.busy[uid] and not svc.jobs.running

    def _evict(self, uid):
        svc = self.live.pop(uid)
        self.last_used.pop(uid, None)
        self.closing[uid] = asyncio.ensure_future(self._shutdown(uid, svc))

    async def _shutdown(self, uid, svc):
        try:
            await svc.shutdown(self.grace)
        except Exception:
            log.warning("service did not shut down cleanly", extra={"user_id": uid}, exc_info=True)
        finally:
            self.closing.pop(uid, None)

    async def reload(self, uid):
        """Drop a user's service now (credentials changed); the next request rebuilds it."""
        if uid in self.live:
            self._evict(uid)
        if uid in self.closing:
            await asyncio.shield(self.closing[uid])

    async def evict_idle(self):
        now = self.clock()
        for uid in [u for u, s in self.live.items() if self._idle(u, s) and now - self.last_used[u] >= self.idle]:
            self._evict(uid)
        if self.closing:
            await asyncio.gather(*self.closing.values())

    async def close(self):
        self.closed = True
        for uid in list(self.live):
            self._evict(uid)
        await asyncio.gather(*self.closing.values())


class OAuthStates:
    """Pending Gmail OAuth states: single-use, expiring, capped overall and per user (oldest dropped first)."""

    def __init__(self, ttl=OAUTH_TTL, limit=MAX_PENDING_OAUTH, per_user=MAX_PENDING_PER_USER, clock=time.monotonic):
        self.ttl, self.limit, self.per_user, self.clock = ttl, limit, per_user, clock
        self.items = OrderedDict()  # state -> (uid, verifier, created); insertion order is age order

    def __len__(self):
        return len(self.items)

    def sweep(self):
        cutoff = self.clock() - self.ttl
        for k in [k for k, v in self.items.items() if v[2] <= cutoff]:
            del self.items[k]

    def add(self, state, uid, verifier):
        self.sweep()
        mine = [k for k, v in self.items.items() if v[0] == uid]
        for k in mine[: max(0, len(mine) - self.per_user + 1)]:
            del self.items[k]
        while len(self.items) >= self.limit:
            self.items.popitem(last=False)
        self.items[state] = (uid, verifier, self.clock())

    def pop(self, state):
        """(uid, verifier) if the state is known and unexpired, else None. Always single-use."""
        hit = self.items.pop(state, None)
        if hit and self.clock() - hit[2] < self.ttl:
            return hit[0], hit[1]
        return None


def purge_users(accounts, pool, days):
    """Retention for every remote user: live services purge through their own cache, idle users' databases are
    opened briefly. Also drops expired sessions. One failing user never stops the rest."""
    from ..cache import Cache

    accounts.cleanup_expired()
    for user in accounts.list_users():
        try:
            svc = pool.live.get(user["id"])
            if svc:
                svc.cache.purge(days)
                continue
            path = accounts.user_dir(user["id"]) / "cache.sqlite3"
            if path.exists():
                cache = Cache(path)
                try:
                    cache.purge(days)
                finally:
                    cache.close()
        except Exception:
            log.warning("retention failed", extra={"user_id": user["id"]}, exc_info=True)
