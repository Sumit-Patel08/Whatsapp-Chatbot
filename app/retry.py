"""Small async retry helper for Gemini calls (answers, speech-to-text, text-to-speech).

Retries only errors that are worth retrying: HTTP 429 (rate limit), 5xx
(server trouble) and network timeouts/connection errors. Anything else
(bad request, wrong key, ...) fails immediately.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

log = logging.getLogger(__name__)

T = TypeVar("T")

# Seconds before the first retry; doubled each time. Tests set this to 0.
BASE_DELAY = 1.0


def status_code_of(exc: BaseException) -> int | None:
    """Find an HTTP status code on exceptions from httpx or google-genai."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    for attr in ("status_code", "code"):  # google-genai errors use `code`
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    status = status_code_of(exc)
    return status is not None and (status == 429 or status >= 500)


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    retries: int = 2,
    what: str = "call",
    **kwargs: Any,
) -> T:
    """Await `fn(*args, **kwargs)`, retrying up to `retries` times with backoff."""
    attempt = 0
    while True:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            if attempt >= retries or not is_retryable(exc):
                raise
            delay = BASE_DELAY * (2**attempt) + (random.uniform(0, 0.5) if BASE_DELAY else 0)
            attempt += 1
            log.warning(
                "%s failed (%s: status=%s), retry %d/%d in %.1fs",
                what, type(exc).__name__, status_code_of(exc), attempt, retries, delay,
            )
            await asyncio.sleep(delay)
