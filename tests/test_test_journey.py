from __future__ import annotations

import json

import pytest
from deixic.examples import verify_test_journey as probe

from test_client import FakeResponse, FakeTransport


def values():
    return dict(DEIXIC_API_KEY="fixture-private-key", DEIXIC_ORGANIZATION_ID="org-test",
                DEIXIC_WORKSPACE_ID="ws-test", DEIXIC_BASE_URL="https://platform.test",
                DEIXIC_IDENTITY_API_KEY_VALIDATE_URL="https://identity.test/v1/api-keys/validate",
                DEIXIC_TEST_CHANNEL_ID="sdk-test")


@pytest.mark.parametrize("mutation", [
    {"active": False},
    {"api_key": {"id": "key", "organization_id": "other", "scopes": ["deixic:read", "deixic:write"]}},
    {"api_key": {"id": "key", "organization_id": "org-test", "scopes": ["deixic:read"]}},
])
def test_identity_denial_or_wrong_grants_stop_before_platform(mutation):
    document = {"active": True, "api_key": {"id": "key", "organization_id": "org-test",
                                               "scopes": ["deixic:read", "deixic:write"]}}
    document.update(mutation)
    reply = FakeResponse(200, content=json.dumps(document).encode())
    transport = FakeTransport([reply])
    with pytest.raises(ValueError, match="required test-tenant key grants"):
        probe.validate_identity(values(), transport)
    assert reply.closed
    assert len(transport.requests) == 1


def test_identity_probe_self_introspects_only_its_supplied_key():
    reply = FakeResponse(200, content=json.dumps({"active": True, "api_key": {
        "id": "key", "organization_id": "org-test", "scopes": ["deixic:read", "deixic:write"],
    }}).encode())
    transport = FakeTransport([reply])
    assert probe.validate_identity(values(), transport) == "key"
    request = transport.requests[0]
    assert json.loads(request["body"]) == {"key": "fixture-private-key"}
    assert request["headers"]["Authorization"] == "Bearer fixture-private-key"
    assert reply.closed


def test_probe_requires_explicit_test_endpoints_and_tenant(monkeypatch):
    for name in probe.REQUIRED:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="Missing test-tenant configuration"):
        probe.configuration()
    for name, value in values().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DEIXIC_BASE_URL", "http://external.test")
    with pytest.raises(ValueError, match="HTTPS or a loopback"):
        probe.configuration()
