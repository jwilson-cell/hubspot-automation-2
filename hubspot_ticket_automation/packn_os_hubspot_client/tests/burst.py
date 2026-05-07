"""HUB-07 Pattern 9 - burst test client. Invoked from TS via subprocess.

Usage: python burst.py <count>

Fires <count> acquire_hubspot_token() calls back-to-back, prints one
timestamp per call to stdout. Used by the TS test harness in Plan 04-04+ to
verify the cross-process bucket actually paces both Pack'N OS and the
existing automation against the same Redis key.

Output format (CSV-like, one line per acquired token):
    <index>,<unix_ms>

Excluded from pytest collection: pyproject.toml `python_files = ["test_*.py"]`.
"""

import sys
import time

# Side-effect: imports redis-py and registers the Lua script on first call.
from packn_os_hubspot_client.rate_limit import acquire_hubspot_token


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    for i in range(count):
        acquire_hubspot_token()
        # Print AFTER acquire so the TS harness can timestamp the moment
        # the token was actually granted.
        print(f"{i},{int(time.time() * 1000)}")
    sys.exit(0)
