"""The cron's one source setting, and the candidate rule it shares (Phase 93, plan 13).

Three groups:

    PIN      what the cron sends to HubSpot today, written down as literals and run
             green against the untouched code BEFORE any function was moved. A later
             edit that changes one byte of the `hubspot`-mode search turns these red.
    MODE     the mode reader's three honest outcomes (a valid mode, source_unreadable,
             source_unknown) and the absent / valid / unrecognised rule.
    READERS  the pre-gate and the engine-shadow pipeline branching on that one mode.

No test here reaches a database, HubSpot, a model, a secret file or the tracked
config/state.json: the transport, the client read and the model calls are fakes,
and every path the code would open is redirected under the pytest temp directory.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))  # scripts/ is imported flat, as run.py does

import pregate_tickets as pg  # noqa: E402
from packn_os_hubspot_client import client  # noqa: E402
from tests.helpers import fakes  # noqa: E402
from ticket_pipeline import hubspot_api  # noqa: E402
from ticket_pipeline import run as pipeline_run  # noqa: E402

SEARCH_PATH = "/crm/v3/objects/tickets/search"

# --------------------------------------------------------------------------- PIN
#
# Literals copied from the code as it stood on `main` (063b75f). They are not built
# from the functions under test: a literal is the only thing a moved function can be
# compared with.

PINNED_BODY = {
    "filterGroups": [
        {
            "filters": [
                {"propertyName": "hs_lastmodifieddate", "operator": "GT", "value": 123456},
                {"propertyName": "hs_pipeline_stage", "operator": "IN", "values": ["1", "3"]},
                {
                    "propertyName": "hs_last_message_from_visitor",
                    "operator": "EQ",
                    "value": "true",
                },
            ]
        }
    ],
    "properties": ["hs_lastmodifieddate"],
    "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
    "limit": 25,
}

PINNED_COMPACT_JSON = (
    '{"filterGroups":[{"filters":[{"propertyName":"hs_lastmodifieddate","operator":"GT",'
    '"value":123456},{"propertyName":"hs_pipeline_stage","operator":"IN","values":["1","3"]},'
    '{"propertyName":"hs_last_message_from_visitor","operator":"EQ","value":"true"}]}],'
    '"properties":["hs_lastmodifieddate"],"sorts":[{"propertyName":"hs_lastmodifieddate",'
    '"direction":"ASCENDING"}],"limit":25}'
)

# What goes on the wire: both request builders serialise with json.dumps' defaults.
PINNED_WIRE_JSON = (
    '{"filterGroups": [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GT", '
    '"value": 123456}, {"propertyName": "hs_pipeline_stage", "operator": "IN", "values": '
    '["1", "3"]}, {"propertyName": "hs_last_message_from_visitor", "operator": "EQ", "value": '
    '"true"}]}], "properties": ["hs_lastmodifieddate"], "sorts": [{"propertyName": '
    '"hs_lastmodifieddate", "direction": "ASCENDING"}], "limit": 25}'
)

PINNED_COMPACT_JSON_NO_VISITOR_FILTER = (
    '{"filterGroups":[{"filters":[{"propertyName":"hs_lastmodifieddate","operator":"GT",'
    '"value":123456},{"propertyName":"hs_pipeline_stage","operator":"IN","values":["1","3"]}]}],'
    '"properties":["hs_lastmodifieddate"],"sorts":[{"propertyName":"hs_lastmodifieddate",'
    '"direction":"ASCENDING"}],"limit":25}'
)

# The property list the pipeline asks for, before the settings' custom properties.
PINNED_PIPELINE_BASE_PROPERTIES = [
    "subject",
    "content",
    "hs_pipeline_stage",
    "hs_ticket_priority",
    "hs_lastmodifieddate",
    "createdate",
    "source_type",
    "hs_last_message_from_visitor",
]

# 123456 ms after the epoch, so a state file can reproduce the pinned `GT` value.
LAST_RUN_AT_FOR_PIN = "1970-01-01T00:02:03.456Z"

SETTINGS = {
    "active_stages": ["1", "3"],
    "require_last_message_from_visitor": True,
    "per_run_cap": 25,
    "ticket_custom_properties": ["topic_of_ticket", "order_number"],
    "pipeline": {"shadow_enabled": True, "shadow_limit": 25},
}


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def _absent_setting() -> dict:
    return {"found": False, "value": None}


@pytest.fixture
def pipeline_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the pipeline at a settings file, a state file and an output directory
    under the pytest temp directory, and give it a token without a token file."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(SETTINGS), encoding="utf-8")  # JSON is valid YAML
    categories_path = tmp_path / "categories.json"
    categories_path.write_text("{}", encoding="utf-8")
    state_path = fakes.temp_state_file(
        tmp_path, {"last_run_at": LAST_RUN_AT_FOR_PIN, "ticket_fingerprints": {}}
    )
    monkeypatch.setattr(pipeline_run, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(pipeline_run, "CATEGORIES_PATH", categories_path)
    monkeypatch.setattr(pipeline_run, "STATE_PATH", state_path)
    monkeypatch.setattr(pipeline_run, "SHADOW_DIR", tmp_path / "engine-shadow-out")
    monkeypatch.setattr(hubspot_api, "read_token", lambda: "test-token")
    monkeypatch.delenv("PACKN_SOURCE_MODE", raising=False)
    return tmp_path


def test_pin_search_body_is_todays_dict() -> None:
    assert pg.build_search_body(123456, ["1", "3"], True, 25) == PINNED_BODY


def test_pin_search_body_is_todays_compact_json() -> None:
    assert _compact(pg.build_search_body(123456, ["1", "3"], True, 25)) == PINNED_COMPACT_JSON


def test_pin_search_body_is_todays_bytes_on_the_wire() -> None:
    body = pg.build_search_body(123456, ["1", "3"], True, 25)
    assert json.dumps(body).encode("utf-8") == PINNED_WIRE_JSON.encode("utf-8")


def test_pin_search_body_without_the_visitor_filter() -> None:
    body = pg.build_search_body(123456, ["1", "3"], False, 25)
    assert _compact(body) == PINNED_COMPACT_JSON_NO_VISITOR_FILTER


def test_pin_pipeline_base_property_list_is_todays() -> None:
    assert hubspot_api.TICKET_BASE_PROPERTIES == PINNED_PIPELINE_BASE_PROPERTIES


def test_pin_pipeline_search_request_with_the_setting_absent(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine-shadow pipeline's one search, end to end, with no source setting stored."""
    # `raising=False`: the reader did not exist when this pin was first run.
    monkeypatch.setattr(client, "read_ticket_source_mode", _absent_setting, raising=False)
    transport = fakes.FakeHubSpotTransport({SEARCH_PATH: {"results": []}})
    monkeypatch.setattr(hubspot_api, "_request", transport)

    assert pipeline_run.run_shadow() == 0

    assert transport.calls == [("POST", SEARCH_PATH)]
    expected = dict(PINNED_BODY)
    expected["properties"] = PINNED_PIPELINE_BASE_PROPERTIES + ["topic_of_ticket", "order_number"]
    assert transport.bodies == [expected]
    assert list(transport.bodies[0]) == list(expected)  # key order is part of the bytes
