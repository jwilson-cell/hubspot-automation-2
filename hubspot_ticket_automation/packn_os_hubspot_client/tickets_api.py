"""Signed client for the Pack'N OS tickets routes (contract packn-tickets-v1).

RED skeleton (Phase 93 plan 12, task 1): the public surface exists so the unit
tests import and fail on their assertions; no function is implemented yet.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_PATH = ROOT / "config" / ".secrets" / "tickets_api_secret.txt"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"

SIGNATURE_VERSION = "packn-tickets-v1"
SIGNATURE_HEADER = "x-packn-signature"
TIMESTAMP_HEADER = "x-packn-timestamp"
CONTRACT_VERSION = "1"
MIN_SECRET_LENGTH = 32
TIMEOUT_S = 5.0
MAX_LIST_LIMIT = 100
SECRET_NOT_CONFIGURED_ERROR = "tickets_secret_not_configured"


class TicketsApiError(Exception):
    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def canonical_query(pairs):
    return None


def canonical_string(method, path, pairs, timestamp, body):
    return None


def sign(secret, canonical):
    return None


def read_secret(path=SECRET_PATH):
    return None


def read_os_origin():
    return None


def iso_millis_z(dt):
    return None


def fetch_unprocessed(limit, *, base_url=None, secret=None, now=None, opener=None):
    return None


def fetch_ticket(ticket_ref, *, base_url=None, secret=None, now=None, opener=None):
    return None


def post_classification(os_ticket_id, payload, *, base_url=None, secret=None, now=None, opener=None):
    return None


def post_source_shadow_observations(payload, *, base_url=None, secret=None, now=None, opener=None):
    return None
