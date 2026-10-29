"""Tiny bounded-retry helper: exponential backoff with jitter."""
import logging
import random
import time

log = logging.getLogger(__name__)

_sleep = time.sleep  # tests replace this so they don't actually wait


def retry_call(fn, *, is_retryable, attempts=3, base_delay=1.0, max_delay=20.0, get_delay_hint=None):
    """Call fn(); on a retryable error wait (2^n * base, jittered, capped) and try again.

    * Only errors for which is_retryable(exc) is True are retried.
    * At most `attempts` calls are made in total, so retries can never loop forever.
    * get_delay_hint(exc) may return a server-provided wait (e.g. Retry-After).
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - we re-raise anything non-retryable
            if attempt >= attempts or not is_retryable(exc):
                raise
            backoff = min(max_delay, base_delay * (2 ** (attempt - 1))) * random.uniform(0.5, 1.0)
            hint = get_delay_hint(exc) if get_delay_hint else None
            delay = min(max_delay, max(backoff, hint)) if hint else backoff
            log.warning(
                "Transient failure (%s), retry %d/%d in %.1fs",
                exc.__class__.__name__, attempt, attempts - 1, delay,
            )
            _sleep(delay)
