"""UUIDv7 (RFC 9562 §5.7): the application-supplied, time-ordered id for every primary key."""

import os
import threading
import time
import uuid

_lock = threading.Lock()
# unix_ts_ms << 12 | a 12-bit counter in rand_a (the RFC's "fixed-length dedicated counter")
_last = 0


def uuid7() -> uuid.UUID:
    """Monotonic within this process: the counter orders ids inside one millisecond, and a clock
    that steps back reuses the last timestamp instead of going backwards."""
    global _last
    with _lock:
        _last = max(time.time_ns() // 1_000_000 << 12, _last + 1)
        ts = _last
    rand_b = int.from_bytes(os.urandom(8)) & ((1 << 62) - 1)
    return uuid.UUID(int=(ts >> 12) << 80 | 0x7 << 76 | (ts & 0xFFF) << 64 | 0b10 << 62 | rand_b)
