"""Look up a ShipSidekick order + its shipments for WISMO hydration.

Called by the hubspot-tickets skill during step 2a (context hydration) for
Carrier Issue / WISMO / shipping-delay tickets. Output gets injected into
`ticket_context.ssk_state` so the drafter in step 2d can cite live carrier
state (status, est delivery, tracking URL) instead of hedged boilerplate.

Usage:
    echo '{"order_number": "TEST321", "merchant": "WKR"}' | py scripts/ssk_order_lookup.py
    echo '{"tracking_number": "1Z02D293YW21069996"}' | py scripts/ssk_order_lookup.py
    echo '{"order_number": "22288", "merchant": "Bruised"}' | py scripts/ssk_order_lookup.py

Resolution order: order_number first (it's the more reliable anchor), then
tracking_number as fallback / enrichment.

Merchant scoping (2026-07-22 — cross-merchant order-number collision fix):
Order numbers COLLIDE across the stores' ShipSidekick orgs (Bruised #22288
vs WKR #22288 are different orders). SSK API keys are org-scoped, so a
GET /orders?search= under the right merchant's key CANNOT return another
store's order. The payload therefore carries the ticket's merchant:

    merchant — the brand/company name from the ticket's REQUIRED
               `company_name` property (customers state their brand on
               every form; falls back to the associated CRM company).
               When set, the lookup runs under that merchant's key or
               refuses (exit 3). It NEVER falls back to the default/
               first key.

Keys are configured under `shipsidekick.merchants` in settings.yaml. Only
when NO merchant signal resolves does the helper use the legacy default
`shipsidekick.token_path` (pre-scoping behavior for unattributable tickets).

Output (stdout JSON):
    {
      "found": true | false,
      "looked_up_by": "order_number" | "tracking_number",
      "merchant_scope": "<configured merchant name the lookup ran under, or 'default'>",
      "order": {
        "id": "...", "name": "...", "alias": "...",
        "fulfillment_status": "...", "financial_status": "...",
        "order_date": "...", "target_delivery_date": "...",
        "ship_to_city": "...", "ship_to_state": "...", "ship_to_country": "..."
      },
      "shipments": [
        {
          "tracking_code": "...",
          "carrier_code": "UPS",
          "service_code": "...",
          "tracking_status": "in_transit",
          "tracking_status_detail": "...",
          "tracking_url": "https://...",
          "est_delivery_date": "...",
          "created_at": "...",
          "updated_at": "..."
        }
      ],
      "not_found_reason": "<string, only when found=false>"
    }

Exit codes:
    0  success (regardless of whether the order was found — check `found` in JSON)
    2  bad args / bad payload
    3  token unavailable for the required scope — file missing/empty, OR the
       ticket's merchant is known but has no configured org-scoped key
       (the helper REFUSES to query under another merchant's key)
    4  ShipSidekick API error (HTTP non-2xx, non-404 on search)
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
DEFAULT_TOKEN_PATH = ROOT / "config" / ".secrets" / "shipsidekick_token.txt"
DEFAULT_BASE_URL = "https://www.shipsidekick.com/api/v1"


def _load_config() -> tuple[str, Path, list[dict]]:
    """Return (base_url, default_token_path, merchants) from settings.yaml."""
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    ssk = cfg.get("shipsidekick") or {}
    base = ssk.get("base_url") or DEFAULT_BASE_URL
    token_path = ROOT / (ssk.get("token_path") or "config/.secrets/shipsidekick_token.txt")
    merchants = ssk.get("merchants") or []
    return base, token_path, merchants


_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm(s: str) -> str:
    """Lowercase and strip everything non-alphanumeric: 'Bruised Brand LLC'
    -> 'bruisedbrandllc', 'bruised-brand' -> 'bruisedbrand'."""
    return _NORM_RE.sub("", (s or "").lower())


def _match_merchant(needle: str, merchants: list[dict]) -> dict | None:
    """Lenient match of the typed Company Name against configured merchants.

    The Company Name form field is free text — spacing, punctuation, casing,
    and suffixes vary ('Bruised Brand', 'bruised-brand LLC', 'BruisedBrand').
    Configured `match` values are the client slug minus hyphens
    ('bruised-brand' -> 'bruisedbrand'). Both sides are normalized to
    alphanumeric-only lowercase and a substring hit EITHER way counts, so a
    partially-typed name still resolves.

    Safety rails: needles shorter than 3 normalized chars never match, and
    an AMBIGUOUS needle (matches >1 configured merchant) returns None —
    the caller then refuses (exit 3) rather than guess between brands.
    """
    n = _norm(needle)
    if len(n) < 3:
        return None
    hits = []
    for m in merchants:
        for c in [m.get("name") or ""] + list(m.get("match") or []):
            c = _norm(c)
            if c and (c in n or n in c):
                hits.append(m)
                break
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        print(
            f"merchant signal {needle!r} ambiguously matches "
            f"{[h.get('name') for h in hits]} — refusing to guess",
            file=sys.stderr,
        )
    return None


def _resolve_token_scope(
    merchant: str,
    merchants: list[dict],
    default_token_path: Path,
) -> tuple[Path, str]:
    """Pick the org-scoped token for the ticket's merchant.

    - `merchant` set: must match a configured merchant, else exit 3 —
      NEVER fall back to the default/first key when the ticket's merchant
      is known; an org-mismatched key returns another store's order for a
      colliding order number and that leaks into customer drafts.
    - `merchant` empty: legacy default token (unattributable tickets only —
      company_name is a required form field, so this should be rare).
    """
    if merchant.strip():
        hit = _match_merchant(merchant, merchants)
        if hit is None:
            print(
                f"ticket merchant '{merchant}' has no org-scoped key under "
                f"shipsidekick.merchants in settings.yaml — refusing to look up "
                f"under another merchant's key (cross-merchant order-number "
                f"collision guard). Add the merchant + token_path to settings.yaml.",
                file=sys.stderr,
            )
            sys.exit(3)
        return ROOT / hit["token_path"], hit.get("name") or merchant
    return default_token_path, "default"


def _get(path: str, token: str, base_url: str) -> dict | None:
    """GET request. Returns parsed JSON on 2xx. Returns None on 404. Exits on other errors."""
    req = urllib.request.Request(f"{base_url}{path}", method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        print(f"SSK HTTP {e.code} on GET {path}: {err_body[:300]}", file=sys.stderr)
        sys.exit(4)


def _normalize_order(order: dict) -> dict:
    ship_to = order.get("shipToAddress") or {}
    tags = [t.get("name") for t in (order.get("tags") or []) if t.get("name")]
    tasks = order.get("tasks") or []

    # Count pick / ship tasks by existence only. We deliberately DO NOT expose
    # task createdAt / updatedAt timestamps — empirically those timestamps on
    # SSK tasks do not correspond to real-world warehouse events in a way that
    # holds up in customer replies (e.g., "pick tasks generated 3 hours ago"
    # is misleading when the tasks haven't been wave-assigned). Citing them
    # produces confident-sounding but wrong claims. Stick to discrete state
    # (task exists, task complete) and tag signals.
    pick_task_total = 0
    pick_task_completed_count = 0
    ship_task_exists = False
    for t in tasks:
        tt = t.get("type")
        if tt == "pick":
            pick_task_total += 1
            if t.get("completedAt"):
                pick_task_completed_count += 1
        if tt == "ship":
            ship_task_exists = True

    # Rate context (from the ship task if present — shippingRate lives there).
    rate_service = ""
    rate_carrier = ""
    rate_total = ""
    rate_eta = ""
    for t in tasks:
        rate = t.get("shippingRate") or {}
        if rate:
            rate_service = rate.get("serviceCode") or rate_service
            rate_total = str(rate.get("rateTotal") or "") or rate_total
            rate_eta = rate.get("estDeliveryDate") or rate_eta
            ca = (rate.get("carrierAccount") or {}).get("carrierCode") or ""
            if ca:
                rate_carrier = ca
            if rate_service:  # first match wins
                break

    # Backorder/hold detection — specific tag patterns Pack'N uses.
    backorder_tags = [t for t in tags if "BACKORDER" in t.upper() or "HOLD" in t.upper()]
    po_tags = [t for t in tags if "PO" in t.upper() and ("FABRIK" in t.upper() or "ASN" in t.upper())]

    return {
        "id": order.get("id") or "",
        "name": order.get("name") or "",
        "alias": order.get("alias") or "",
        "fulfillment_status": order.get("fulfillmentStatus") or "",
        "financial_status": order.get("financialStatus") or "",
        "target_delivery_date": order.get("targetDeliveryDate") or "",
        "ship_to_city": ship_to.get("city") or "",
        "ship_to_state": ship_to.get("state") or "",
        "ship_to_country": ship_to.get("country") or "",
        "ship_method_display": order.get("shipMethod") or "",
        # Task existence only — no timestamps. SSK task createdAt/updatedAt
        # do not correspond to real-world warehouse events reliably.
        "pick_task_total": pick_task_total,            # 0 if none generated
        "pick_task_completed_count": pick_task_completed_count,
        "ship_task_exists": ship_task_exists,
        # Rate context (carrier + service + ETA already bound). rate_eta is
        # the carrier's commitment, safe to quote.
        "rate_carrier": rate_carrier,
        "rate_service": rate_service,
        "rate_total": rate_total,
        "rate_eta": rate_eta,
        # Tag signals — discrete presence, not time-based.
        "tags": tags,
        "backorder_tags": backorder_tags,
        "po_tags": po_tags,
    }


def _normalize_shipment(s: dict) -> dict:
    tracker = s.get("tracker") or {}
    carrier = s.get("carrierAccount") or {}
    rate = s.get("shippingRate") or {}
    return {
        "tracking_code": s.get("trackingCode") or "",
        "carrier_code": carrier.get("carrierCode") or "",
        "service_code": rate.get("serviceCode") or "",
        "tracking_status": tracker.get("status") or "",
        "tracking_status_detail": tracker.get("statusDetail") or "",
        "tracking_url": tracker.get("trackingUrl") or "",
        "est_delivery_date": tracker.get("estDeliveryDate") or rate.get("estDeliveryDate") or "",
        "signed_by": tracker.get("signedBy") or "",
        "tracking_details": tracker.get("trackingDetails") or None,  # preserve structure if present
        "created_at": s.get("createdAt") or "",
        "updated_at": s.get("updatedAt") or "",
    }


def _search_orders(needle: str, token: str, base_url: str) -> dict | None:
    """Search orders by name/alias/tracking. The SSK /orders?search endpoint
    matches against the order name/alias AND (empirically) nested shipment
    tracking codes, so this single call handles both kinds of lookup.

    EXACT matches only: exact name/alias (with/without '#' prefix), else
    exact trackingCode on a nested shipment. A fuzzy search hit that matches
    neither is NOT the customer's order — returning it is how wrong-order
    details leak into drafts. No first-result fallback (2026-07-22, mirrors
    the Pack'N OS posture for the same defect class).
    """
    qs = urllib.parse.urlencode({"search": needle, "limit": 10, "includeArchived": "false"})
    data = _get(f"/orders?{qs}", token, base_url)
    if not data:
        return None
    candidates = data.get("data") or []
    if not candidates:
        return None
    n = needle.strip().lower()
    # Priority 1: exact name/alias
    for o in candidates:
        if (o.get("name") or "").strip().lower() == n:
            return o
        # Try without "#" prefix (common on shop-provided names)
        name_stripped = (o.get("name") or "").strip().lstrip("#").lower()
        if name_stripped == n.lstrip("#"):
            return o
        if (o.get("alias") or "").strip().lower() == n:
            return o
    # Priority 2: exact tracking code on a shipment
    for o in candidates:
        for s in (o.get("shipments") or []):
            if (s.get("trackingCode") or "").strip().lower() == n:
                return o
    # No exact match → not found. Never return a fuzzy first result.
    return None


def _get_order(order_id: str, token: str, base_url: str) -> dict | None:
    data = _get(f"/orders/{order_id}", token, base_url)
    if not data:
        return None
    return data.get("data") or data


def main() -> int:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"invalid stdin (expect UTF-8 JSON): {e}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("payload must be a JSON object", file=sys.stderr)
        return 2

    order_number = (payload.get("order_number") or "").strip()
    tracking_number = (payload.get("tracking_number") or "").strip()
    if not order_number and not tracking_number:
        print("payload must include order_number, tracking_number, or both", file=sys.stderr)
        return 2
    merchant = (payload.get("merchant") or "").strip()

    base_url, default_token_path, merchants = _load_config()
    token_path, merchant_scope = _resolve_token_scope(merchant, merchants, default_token_path)
    if not token_path.exists():
        print(
            f"SSK token missing at {token_path} (scope: {merchant_scope}) — "
            f"generate an org-scoped key in the ShipSidekick dashboard",
            file=sys.stderr,
        )
        return 3
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        print(f"SSK token file at {token_path} is empty (scope: {merchant_scope})", file=sys.stderr)
        return 3

    order = None
    looked_up_by = None
    not_found_reason = ""

    # Order number is the preferred anchor. Tracking number searches use the
    # same /orders?search endpoint because the SSK /shipments list endpoint
    # returns empty for bearer-token access (confirmed 2026-04-23).
    if order_number:
        looked_up_by = "order_number"
        order = _search_orders(order_number, token, base_url)
        if not order and tracking_number:
            looked_up_by = "tracking_number (fallback)"
            order = _search_orders(tracking_number, token, base_url)
        if not order:
            not_found_reason = f"no order matching '{order_number}'" + (f" or tracking '{tracking_number}'" if tracking_number else "")
    else:
        looked_up_by = "tracking_number"
        order = _search_orders(tracking_number, token, base_url)
        if not order:
            not_found_reason = f"no order matching tracking '{tracking_number}'"

    if not order:
        print(json.dumps({
            "found": False,
            "looked_up_by": looked_up_by,
            "merchant_scope": merchant_scope,
            "not_found_reason": not_found_reason,
        }, indent=2))
        return 0

    # If shipments weren't returned by the search endpoint, fetch the full order.
    order_id = order.get("id") or ""
    if order_id and not order.get("shipments"):
        full = _get_order(order_id, token, base_url)
        if full:
            order = full

    shipments = [_normalize_shipment(s) for s in (order.get("shipments") or [])]
    result = {
        "found": True,
        "looked_up_by": looked_up_by,
        "merchant_scope": merchant_scope,
        "order": _normalize_order(order),
        "shipments": shipments,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
