"""Redis storage: de-duplication, daily limits, language, memory and the chat log.

Everything that must survive a restart lives here (never in Python memory).

Keys (all prefixed "ks:"):
  ks:seen:{message_id}          de-dup marker, 24 h
  ks:count:{phone}:{YYYYMMDD}   messages today (IST), ~2 days
  ks:limitnote:{phone}:{date}   "limit reached" already sent today
  ks:lang:{phone}               saved language, no expiry
  ks:hist:{phone}               last N Q/A pairs as JSON list, 7 days
Stream:
  chatlog                       one entry per exchange (see ChatLogEntry)
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis

IST = timezone(timedelta(hours=5, minutes=30))

DEDUP_TTL = 24 * 3600
COUNT_TTL = 48 * 3600
HISTORY_TTL = 7 * 24 * 3600
HISTORY_PAIRS = 6
CHATLOG_STREAM = "chatlog"
CHATLOG_MAXLEN = 200_000  # approximate cap so Redis memory stays bounded


def ist_today() -> str:
    return datetime.now(IST).strftime("%Y%m%d")


@dataclass
class ChatLogEntry:
    """One question/answer exchange. Field names are chosen so this can become a
    Postgres table row later without changing the handlers."""

    phone: str
    type: str  # text | audio | image | interactive | unsupported ...
    language: str
    question: str
    answer: str
    latency_ms: int
    status: str = "ok"  # ok | error | limited | ...
    message_id: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(IST).isoformat(timespec="seconds"))


class Store:
    def __init__(self, redis: Redis) -> None:
        # The client must be created with decode_responses=True.
        self.redis = redis

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    # ── De-duplication ─────────────────────────────────────────────────────

    async def first_time_seen(self, message_id: str) -> bool:
        """True the first time a WhatsApp message id is seen (atomic SET NX)."""
        return bool(await self.redis.set(f"ks:seen:{message_id}", "1", nx=True, ex=DEDUP_TTL))

    # ── Daily limit ────────────────────────────────────────────────────────

    async def count_message(self, phone: str) -> int:
        """Add one to today's (IST) counter and return the new total."""
        key = f"ks:count:{phone}:{ist_today()}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, COUNT_TTL)
            count, _ = await pipe.execute()
        return int(count)

    async def claim_limit_notice(self, phone: str) -> bool:
        """True only the first time today, so "limit reached" is sent once."""
        key = f"ks:limitnote:{phone}:{ist_today()}"
        return bool(await self.redis.set(key, "1", nx=True, ex=COUNT_TTL))

    # ── Language ───────────────────────────────────────────────────────────

    async def get_language(self, phone: str) -> str | None:
        return await self.redis.get(f"ks:lang:{phone}")

    async def set_language(self, phone: str, lang: str) -> None:
        await self.redis.set(f"ks:lang:{phone}", lang)

    # ── Conversation memory ────────────────────────────────────────────────

    async def get_history(self, phone: str) -> list[dict[str, str]]:
        """Oldest first: [{"q": ..., "a": ...}, ...]."""
        raw = await self.redis.lrange(f"ks:hist:{phone}", 0, -1)
        return [json.loads(item) for item in raw]

    async def add_history(self, phone: str, question: str, answer: str) -> None:
        key = f"ks:hist:{phone}"
        item = json.dumps({"q": question, "a": answer}, ensure_ascii=False)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key, item)
            pipe.ltrim(key, -HISTORY_PAIRS, -1)
            pipe.expire(key, HISTORY_TTL)
            await pipe.execute()

    # ── Chat log ───────────────────────────────────────────────────────────
    # To move to Postgres later, re-implement just these two methods.

    async def log_exchange(self, entry: ChatLogEntry) -> None:
        fields = {k: str(v) for k, v in asdict(entry).items()}
        await self.redis.xadd(CHATLOG_STREAM, fields, maxlen=CHATLOG_MAXLEN, approximate=True)

    async def recent_logs(self, phone: str | None = None, limit: int = 50) -> list[dict[str, str]]:
        """Newest first. With `phone`, scans back through the stream (bounded)."""
        results: list[dict[str, str]] = []
        max_id, scanned, page = "+", 0, 500
        deadline = time.monotonic() + 2.0
        while len(results) < limit and scanned < 20_000 and time.monotonic() < deadline:
            batch = await self.redis.xrevrange(CHATLOG_STREAM, max=max_id, min="-", count=page)
            if not batch:
                break
            for entry_id, fields in batch:
                if phone is None or fields.get("phone") == phone:
                    results.append({"id": entry_id, **fields})
                    if len(results) >= limit:
                        break
            scanned += len(batch)
            if len(batch) < page:
                break
            max_id = f"({batch[-1][0]}"  # exclusive: continue before the last id
        return results
