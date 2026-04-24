"""Look up a ShipSidekick order + its shipments for WISMO hydration.

Called by the hubspot-tickets skill during step 2a (context hydration) for
Carrier Issue / WISMO / shipping-delay tickets. Output gets injected into
`ticket_context.ssk_state` so the drafter in step 2d can cite live carrier
state (status, est delivery, tracking URL) instead of hedged boilerplate.

Usage:
    echo '{"order_number": "TEST321"}' | py scripts/ssk_order_lookup.py
    echo '{"tracking_number": "1Z02D293YW21069996"}' | py scripts/ssk_order_lookup.py
    echo '{"order_number": "X", "tracking_number": "Y"}' | py scripts/ssk_order_lookup.py

Resolution order: order_number first (it's the more reliable anchor), then
tracking_number as fallback / enrichment.

Output (stdout JSON):
    {
      "found": true | false,
      "looked_up_by": "order_number" | "tracking_number",
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
    3  token file missing
    4  ShipSidekick API error (HTTP non-2xx, non-404 on search)
"""
from __future__ import annotations

import json
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


def _load_config() -> tuple[str, Path]:
    """Return (base_url, token_path) from settings.yaml with defaults."""
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    ssk = cfg.get("shipsidekick") or {}
    base = ssk.get("base_url") or DEFAULT_BASE_URL
    token_path = ROOT / (ssk.get("token_path") or "config/.secrets/shipsidekick_token.txt")
    return base, token_path


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

    Preference: exact match on name/alias > exact match on any nested
    shipment's trackingCode > first result.
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
    # Priority 3: first result
    return candidates[0]


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

    base_url, token_path = _load_config()
    if not token_path.exists():
        print(f"SSK token missing at {token_path} — generate a key in ShipSidekick dashboard", file=sys.stderr)
        return 3
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        print(f"SSK token file at {token_path} is empty", file=sys.stderr)
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
        "order": _normalize_order(order),
        "shipments": shipments,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
