# SQLite has no row-level locking, so concurrent writers serialize on
# SQLite's own database-level write lock (see optimizer.optimize_trip and
# DATABASES['default']['OPTIONS'] in settings.py for the full explanation).
# The connection-level `timeout` in OPTIONS is a Python-level retry-loop
# timeout and was found not to reliably prevent "database table is locked"
# errors under real thread contention with SQLite's default (very short)
# internal busy handler. Setting `PRAGMA busy_timeout` directly on every new
# connection is the documented, reliable way to make SQLite itself queue a
# blocked writer instead of erroring immediately.
from django.db.backends.signals import connection_created


def _set_sqlite_busy_timeout(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout = 20000;")  # milliseconds


connection_created.connect(_set_sqlite_busy_timeout)
