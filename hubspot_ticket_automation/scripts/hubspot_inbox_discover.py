"""Discover HubSpot help-desk inboxes, channels, and channel accounts.

Read-only. Used once during setup to pick the inbox Claude should send replies
from, then its identifiers are pinned into config/settings.yaml.

Usage:
    py scripts/hubspot_inbox_discover.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = ROOT / "config" / ".secrets" / "hubspot_token.txt"
API_BASE = "https://api.hubapi.com"


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(f"{API_BASE}{path}", method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code} on GET {path}: {err_body}", file=sys.stderr)
        sys.exit(4)


def main() -> int:
    if not TOKEN_PATH.exists():
        print(f"token missing at {TOKEN_PATH}", file=sys.stderr)
        return 3
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()

    inboxes = _get("/conversations/v3/conversations/inboxes?limit=100", token).get("results", [])
    channels = _get("/conversations/v3/conversations/channels?limit=100", token).get("results", [])
    channel_accounts = _get(
        "/conversations/v3/conversations/channel-accounts?limit=100", token
    ).get("results", [])

    print(json.dumps({
        "inboxes": inboxes,
        "channels": channels,
        "channel_accounts": channel_accounts,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
