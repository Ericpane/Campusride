"""Small retry helper for transient database concurrency errors."""
from __future__ import annotations

import logging
import random
import time
from functools import wraps

from django.db import OperationalError, transaction

logger = logging.getLogger(__name__)

_RETRY_MESSAGES = (
    "database is locked",
    "database table is locked",
    "could not serialize access",
    "serialization failure",
    "deadlock detected",
)


def _is_retryable_db_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(message in msg for message in _RETRY_MESSAGES)


def retry_on_db_lock(max_attempts=5, base_delay=0.08):
    """Retry transient SQLite/PostgreSQL contention errors with backoff."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except OperationalError as exc:
                    if not _is_retryable_db_error(exc) or attempt >= max_attempts:
                        raise
                    delay = base_delay * (2 ** (attempt - 1)) * (1 + random.random())
                    logger.warning(
                        "%s: transient DB contention; retry %s/%s after %.3fs",
                        fn.__name__, attempt, max_attempts, delay,
                    )
                    time.sleep(delay)
        return wrapper
    return decorator
