# Phase 4 — cross-process HubSpot rate-limit token acquire.
#
# Source contract: src/lib/rate-limit.ts:TAKE_TOKEN_LUA
# Pitfall 1: this script body MUST byte-equal the TS copy.
# DO NOT EDIT WITHOUT UPDATING src/lib/rate-limit.ts — parity is enforced by
# tests/unit/automation/lua-parity.test.ts (TS) +
# python/packn_os_hubspot_client/tests/test_lua_parity.py (Python).
# Marker syntax differs by language (`#` here, `//` in TS); the parity test
# regex (?://|#)\s*LUA-(?:START|END) accepts both forms.
#
# This module provides `acquire_hubspot_token()` for the existing Pack'N
# HubSpot ticket-automation. The SKILL.md cron skill shells out to
# `python -m packn_os_hubspot_client.rate_limit` BEFORE each MCP HubSpot
# call (per RESEARCH 04 Open Q1 Shape A). Exit code 0 is the only signal:
# token acquired OR degraded to passthrough — both let the caller proceed.

import logging
import os
import time
from typing import Optional

from redis import Redis

logger = logging.getLogger(__name__)

# LUA-START
TAKE_TOKEN_LUA = """
  local key = KEYS[1]
  local capacity = tonumber(ARGV[1])
  local refill_per_sec = tonumber(ARGV[2])
  local now_ms = tonumber(ARGV[3])
  local b = redis.call("HMGET", key, "tokens", "last_refill")
  local tokens = tonumber(b[1]) or capacity
  local last = tonumber(b[2]) or now_ms
  local elapsed = (now_ms - last) / 1000
  tokens = math.min(capacity, tokens + elapsed * refill_per_sec)
  if tokens < 1 then
    redis.call("HMSET", key, "tokens", tokens, "last_refill", now_ms)
    redis.call("PEXPIRE", key, 60000)
    return -1
  end
  tokens = tokens - 1
  redis.call("HMSET", key, "tokens", tokens, "last_refill", now_ms)
  redis.call("PEXPIRE", key, 60000)
  return 0
"""
# LUA-END

# Bucket key — sibling to Pack'N OS's `rate:hubspot:packn-os` per
# 02-RESEARCH.md Resolved Question 1. The cross-process budget split is:
#   HUBSPOT_RATE_LIMIT_OS + HUBSPOT_RATE_LIMIT_AUTOMATION <= 5  (Pitfall 3)
# Pack'N OS env-time guard in src/schemas/env.ts enforces the sum.
KEY = "rate:hubspot:existing-automation"
POLL_INTERVAL_S = 0.05
MAX_RETRIES = 1200  # 60s wall-clock cap (matches TS rate-limit.ts MAX_RETRIES)

_redis: Optional[Redis] = None
_take_token_script = None


def _get_redis() -> Redis:
    """Lazy-init module-scoped Redis client. REDIS_URL env var is required."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(os.environ["REDIS_URL"])
    return _redis


def _get_script():
    """Lazy-init the registered Lua script.

    redis-py's `register_script` auto-handles SCRIPT LOAD + EVALSHA cache +
    NOSCRIPT reload. CITED: redis.io/blog/bullet-proofing-lua-scripts-in-redispy.
    """
    global _take_token_script
    if _take_token_script is None:
        _take_token_script = _get_redis().register_script(TAKE_TOKEN_LUA)
    return _take_token_script


def acquire_hubspot_token() -> None:
    """Acquire one token from rate:hubspot:existing-automation.

    Polls every 50ms; degrades to passthrough after 60s wall-clock (matches
    TS rate-limit.ts). Degraded-mode: logs warning + returns (better to
    over-call than wedge the cron tick).
    """
    capacity = int(os.environ.get("HUBSPOT_RATE_LIMIT_AUTOMATION", "3"))
    refill = capacity  # same as TS — refill_per_sec == capacity for steady-state
    script = _get_script()

    for _ in range(MAX_RETRIES):
        now_ms = int(time.time() * 1000)
        try:
            result = script(keys=[KEY], args=[capacity, refill, now_ms])
        except Exception as e:
            # Mirror TS degraded-mode: log + return (better to over-call than wedge)
            logger.warning(
                "rate-limit Lua EVAL failed - degrading to passthrough: %s",
                e,
                extra={"system": "hubspot", "key": KEY},
            )
            return
        if int(result) == 0:
            return
        time.sleep(POLL_INTERVAL_S)
    logger.warning(
        "rate-limit acquireToken exhausted retries - degrading to passthrough",
        extra={"system": "hubspot", "key": KEY, "attempts": MAX_RETRIES},
    )


if __name__ == "__main__":
    # Invoked by the existing automation's SKILL.md before each MCP HubSpot call:
    #   py -m packn_os_hubspot_client.rate_limit
    # Exit 0 = token acquired (or degraded to passthrough); SKILL proceeds with
    # the MCP call. No CLI args needed - the env vars REDIS_URL +
    # HUBSPOT_RATE_LIMIT_AUTOMATION configure the call.
    logging.basicConfig(level=logging.INFO)
    acquire_hubspot_token()
