"""Postgres connection pool for the Pack'N OS helper.

Pitfall 4: every DB op MUST use `with pool.connection() as conn:` so the
context manager guarantees release-on-exception. Pool is sized at min=1
max=2 - the helper's call sites are single-tick cron writes, never
concurrent batch jobs, so saturation is structurally impossible.

Env var contract:
    PACKN_OS_DATABASE_URL - Postgres URL pointing at the Pack'N OS DB.
        Distinct from the existing automation's own DATABASE_URL so an
        operator can read this var name and immediately see "this is Pack'N
        OS connection state, not the existing automation's local DB".
"""

import os
from typing import Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: Optional[ConnectionPool] = None


def get_pool() -> ConnectionPool:
    """Lazy-init module-scoped connection pool.

    min_size=1, max_size=2 - matches RESEARCH Pattern 8. The pool is opened
    on first call so test harnesses that never invoke `get_pool()` don't pay
    the connection cost.
    """
    global _pool
    if _pool is None:
        # Explicit `open=True` - psycopg-pool 3.3 deprecation warning says the
        # default flips to False in a future release. Explicit-open eliminates
        # the warning + lets us upgrade psycopg-pool without behavior drift.
        _pool = ConnectionPool(
            os.environ["PACKN_OS_DATABASE_URL"],
            min_size=1,
            max_size=2,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def close_pool() -> None:
    """Close the pool. Optional - the pool's __del__ also closes connections.

    Useful when an operator wants explicit teardown after a one-shot CLI run
    (avoids the noisy "psycopg pool was not closed" warning).
    """
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
