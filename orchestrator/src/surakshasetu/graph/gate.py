"""The Redis (valkey) keys of TDD §7.2 that a turn uses: the single-writer lock, the per-subject
rate limit, the idempotency hint, and the SSE fan-out. Every error propagates: the API answers 503
before a turn starts (fail closed), and after a commit the caller only logs it."""

import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from redis.asyncio import Redis

from surakshasetu.config import Settings

# Delete the lock only if it is still ours (it may have expired and been taken by another turn).
_RELEASE = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
)


class RedisGate:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._lock_ms = settings.session_lock_ttl_s * 1000
        self._idem_ttl = settings.idempotency_ttl_s
        self._limits = settings.rate_limits

    async def ping(self) -> None:
        """Readiness (/readyz): raises when valkey does not answer."""
        await self._redis.ping()

    async def acquire(self, session_id: UUID) -> str | None:
        """lock:session:{id}. The token if taken, None if another turn holds it."""
        token = uuid4().hex
        key = f"lock:session:{session_id}"
        taken = await self._redis.set(key, token, nx=True, px=self._lock_ms)
        return token if taken else None

    async def release(self, session_id: UUID, token: str) -> None:
        await self._redis.eval(_RELEASE, 1, f"lock:session:{session_id}", token)

    async def over_limit(self, subject_ref: UUID) -> bool:
        """rl:{subject_ref}:{window}: counts this turn in every fixed window."""
        now = int(time.time())
        over = False
        for window, limit in self._limits.items():
            key = f"rl:{subject_ref}:{window}:{now // window}"
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, window)
            over = over or count > limit
        return over

    async def get_idem(self, turn_key: UUID) -> str | None:
        value = await self._redis.get(f"idem:{turn_key}")
        return None if value is None else str(value)

    async def set_idem(self, turn_key: UUID, rendered_sha256: str) -> None:
        await self._redis.set(f"idem:{turn_key}", rendered_sha256, ex=self._idem_ttl)

    async def publish(self, session_id: UUID, event: str, data: dict[str, Any]) -> None:
        message = json.dumps({"event": event, "data": data})
        await self._redis.publish(f"events:{session_id}", message)

    async def subscribe(self, session_id: UUID) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """The session's events until the client goes away. Nothing is replayed: a client that
        connects late gets the released message from the turn's HTTP response."""
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(f"events:{session_id}")
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    decoded = json.loads(message["data"])
                    yield decoded["event"], decoded["data"]
        finally:
            await pubsub.aclose()  # type: ignore[no-untyped-call]
