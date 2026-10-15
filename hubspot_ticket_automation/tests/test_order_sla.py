"""GET /api/tickets/order-sla (2026-10-15): the client sends exactly the
contract's request and returns the verdict; the order lookup attaches it to
ssk_state.sla_status and FAILS OPEN, so a missing secret or an unreachable OS
never costs the lookup. Reuses the signer tests' fake transport. No network."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from packn_os_hubspot_client import tickets_api
from packn_os_hubspot_client.tickets_api import TicketsApiError
from tests.test_tickets_api_signer import (
    ORIGIN,
    answers,
    expected_signature,
    header,
    refuses,
    seams,
)

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))  # scripts/ is imported flat

import ssk_order_lookup  # noqa: E402

ORDER_ID = "cml37ot2z003v0cl8wznoweff"


def test_sends_exactly_the_path_and_the_two_query_parameters() -> None:
    opener = answers({"contract_version": "1", "ssk_order_id": ORDER_ID, "verdict": "late"})
    tickets_api.fetch_order_sla(ORDER_ID, **seams(opener))
    request = opener.request
    assert request.get_method() == "GET"
    assert request.full_url == f"{ORIGIN}/api/tickets/order-sla?contract_version=1&ssk_order_id={ORDER_ID}"
    assert request.data is None
    assert header(request, "x-packn-signature") == expected_signature(request)


@pytest.mark.parametrize("verdict", [*tickets_api.ORDER_SLA_VERDICTS, None])
def test_returns_the_verdict_or_none(verdict) -> None:
    body = {"contract_version": "1", "ssk_order_id": ORDER_ID, "verdict": verdict}
    assert tickets_api.fetch_order_sla(ORDER_ID, **seams(answers(body))) == verdict


def test_a_verdict_outside_the_contract_is_refused() -> None:
    body = {"contract_version": "1", "ssk_order_id": ORDER_ID, "verdict": "maybe"}
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.fetch_order_sla(ORDER_ID, **seams(answers(body)))
    assert caught.value.code == "list_error"


@pytest.mark.parametrize(("status", "code"), [(400, "list_error"), (401, "signature_rejected"), (500, "list_error")])
def test_a_refusal_raises_its_code(status: int, code: str) -> None:
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.fetch_order_sla(ORDER_ID, **seams(refuses(status)))
    assert caught.value.code == code


@pytest.mark.parametrize("bad", ["", "../x", "a b", "a" * 65])
def test_an_id_outside_the_rule_is_refused_before_any_request(bad: str) -> None:
    opener = answers({"contract_version": "1", "ssk_order_id": "x", "verdict": None})
    with pytest.raises(ValueError):
        tickets_api.fetch_order_sla(bad, **seams(opener))
    assert opener.calls == []


def test_the_lookup_attaches_the_verdict(monkeypatch) -> None:
    monkeypatch.setattr(tickets_api, "fetch_order_sla", lambda order_id: "late" if order_id == ORDER_ID else None)
    assert ssk_order_lookup._sla_status(ORDER_ID) == "late"


@pytest.mark.parametrize(
    "error", [TicketsApiError("secret_missing"), TicketsApiError("list_timeout"), ValueError("bad id"), OSError("x")]
)
def test_the_lookup_fails_open(monkeypatch, capsys, error: Exception) -> None:
    def boom(_order_id: str):
        raise error

    monkeypatch.setattr(tickets_api, "fetch_order_sla", boom)
    assert ssk_order_lookup._sla_status(ORDER_ID) is None
    assert "sla_status unavailable" in capsys.readouterr().err


def test_the_lookup_asks_nothing_without_an_order_id(monkeypatch) -> None:
    def never(_order_id: str):
        raise AssertionError("called")

    monkeypatch.setattr(tickets_api, "fetch_order_sla", never)
    assert ssk_order_lookup._sla_status("") is None
