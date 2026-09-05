"""
Small helper for retrying a DB write on transient lock/serialization
failures.

Why this exists: SQLite has no row-level locking, so two concurrent
transaction.atomic() blocks that both write can hit
`sqlite3.OperationalError: database is locked` (or "database table is
locked") even with a busy_timeout PRAGMA set, because SQLite's deferred-
transaction model can produce a lock conflict that isn't a simple
"wait for the other writer to finish" situation. The robust fix, and the
one SQLite's own documentation recommends for this exact error, is for the
application to catch the error and retry the whole transaction after a
short backoff -- not to try to out-configure SQLite's single-writer model.

This also happens to be the right shape of fix for Postgres/MySQL under
SERIALIZABLE or high-contention REPEATABLE READ, where a serialization
failure is likewise meant to be handled by retrying the transaction, so
this helper is not a SQLite-only workaround.
"""
from __future__ import annotations

import logging
import random
import sqlite3
import time
from functools import wraps

from django.db import OperationalError, transaction

logger = logging.getLogger(__name__)

_LOCK_MESSAGES = ("database is locked", "database table is locked")


def _is_lock_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _LOCK_MESSAGES)


def retry_on_db_lock(max_attempts=8, base_delay=0.06):
    """
    Decorator: retries the wrapped function on a SQLite lock error (or the
    equivalent OperationalError Django surfaces for one), with a short
    randomized exponential backoff. Re-raises immediately for any other
    exception, and re-raises the lock error itself once max_attempts is
    exhausted rather than hiding a genuine persistent problem.

    The wrapped function should be idempotent-on-retry from the caller's
    point of view -- i.e. safe to run again from scratch if a previous
    attempt didn't commit. Every current use of this decorator wraps a
    function whose entire body is one transaction.atomic() block, so a
    failed attempt is fully rolled back before the retry runs.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                attempt += 1
                try:
                    return fn(*args, **kwargs)
                except (OperationalError, sqlite3.OperationalError) as e:
                    if not _is_lock_error(e) or attempt >= max_attempts:
                        raise
                    delay = base_delay * (2 ** (attempt - 1)) * (1 + random.random())
                    logger.warning(
                        "%s: DB locked, retrying (attempt %s/%s) after %.3fs",
                        fn.__name__, attempt, max_attempts, delay,
                    )
                    time.sleep(delay)
        return wrapper
    return decorator
