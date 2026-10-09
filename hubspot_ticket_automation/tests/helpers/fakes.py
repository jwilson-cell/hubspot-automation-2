"""Test fakes for the cron (Phase 93, plan 12).

RED skeleton: the names exist so the self-tests import and fail on their
assertions; nothing records or replays yet.
"""
from __future__ import annotations

RESULT_PATH_ENV = ""


class FakeHubSpotTransport:
    def __init__(self, responses=None):
        self.calls = []
        self.bodies = []

    def respond(self, prefix, response):
        return None

    def calls_matching(self, prefix):
        return None

    def __call__(self, method, path, token=None, body=None):
        return None


class FakeOsApi:
    def __init__(self, unprocessed=None, tickets=None):
        self.calls = []
        self.classification_results = {}
        self.source_shadow_result = None

    def fail_next(self, function_name, code, http_status=None):
        return None

    def fail_next_list(self, code, http_status=None):
        return None

    def calls_to(self, function_name):
        return None

    def fetch_unprocessed(self, limit):
        return None

    def fetch_ticket(self, ticket_ref):
        return None

    def post_classification(self, os_ticket_id, payload):
        return None

    def post_source_shadow_observations(self, payload):
        return None


class FakeLauncher:
    def __init__(self, exit_code, result=None):
        self.calls = []

    def __call__(self, command, env):
        return None


def temp_state_file(tmp_path, state):
    return None


def assert_marker_absent(marker, caplog, capfd):
    return None
