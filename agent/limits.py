"""Small daily counters that protect the public demo (and its API bill) from abuse."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from django.core.cache import cache

DAY = 60 * 60 * 26  # a little over a day, so a key always outlives the date it counts


def client_ip(request) -> str:
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _key(kind: str, who: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    digest = hashlib.sha256(who.encode()).hexdigest()[:20]  # never store raw IP addresses
    return f"agentdesk:{kind}:{today}:{digest}"


def allow(kind: str, who: str, limit: int) -> bool:
    """Count one use. Returns False once `who` has used `kind` more than `limit` times today."""
    key = _key(kind, who)
    cache.add(key, 0, DAY)
    try:
        used = cache.incr(key)
    except ValueError:  # entry expired between add() and incr()
        cache.set(key, 1, DAY)
        used = 1
    return used <= limit
