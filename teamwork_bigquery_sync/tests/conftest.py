"""Shared fixtures.

Everything here runs offline: no Teamwork API, no BigQuery, no credentials.
The modules under test import google-cloud-bigquery and python-dotenv at
module level, so those must be installed (they are, via requirements.txt) —
but nothing in these tests ever constructs a real client.
"""

import json

import pytest


class FakeResponse:
    """Stands in for a requests.Response."""

    def __init__(self, status_code=200, payload=None, headers=None, body=None,
                 truncated=False):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}
        self._truncated = truncated
        self.text = body if body is not None else json.dumps(self._payload)

    def json(self):
        if self._truncated:
            # What requests raises when a body is cut off mid-transfer.
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeSession:
    """Replays a scripted list of responses/exceptions and records every
    request, so tests can assert on the query params actually sent.
    """

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.auth = None
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.requests.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        item = self.script[min(len(self.requests) - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def calls(self):
        return len(self.requests)


@pytest.fixture
def no_sleep(monkeypatch):
    """Records backoff durations instead of actually sleeping."""
    import teamwork_client

    slept = []
    monkeypatch.setattr(teamwork_client.time, "sleep", slept.append)
    return slept


@pytest.fixture
def client(monkeypatch):
    """A TeamworkClient whose session is scripted by the test."""
    import teamwork_client

    def _make(script):
        c = teamwork_client.TeamworkClient("https://example.teamwork.com", "key")
        c.session = FakeSession(script)
        return c

    return _make
