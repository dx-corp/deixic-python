from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from deixic import protocol as public_pb2

from deixic import (
    Deixic,
    DeixicError,
    WorkloadFederationCredentialProvider,
    environment_assertion_source,
    file_assertion_source,
    github_actions_assertion_source,
)

IDENTITY = "https://identity.deixic.test"
EXCHANGE = f"{IDENTITY}/v1/workload-federation/exchange"
NOW = 1_790_000_000.0


@dataclass
class FakeResponse:
    status_code: int
    content: bytes = b""
    headers: Mapping[str, str] | None = None
    closed: bool = False

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))

    def iter_content(self, chunk_size: int | None = None) -> Iterator[bytes]:
        return iter((self.content,))

    def close(self) -> None:
        self.closed = True

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {}


class FakeTransport:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
        stream: bool = False,
    ) -> FakeResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
                "stream": stream,
            }
        )
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


class Assertions:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"assertion-{self.calls}"


def issued(token: str, *, lifetime: int = 300) -> FakeResponse:
    from datetime import datetime, timezone

    expires = datetime.fromtimestamp(NOW + lifetime, tz=timezone.utc)
    return FakeResponse(
        200,
        json.dumps(
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "organization_id": "org-a",
                "workspace_id": "workspace-a",
                "principal_id": "principal-a",
                "rule_id": "rule-a",
                "scopes": ["console:write"],
            }
        ).encode(),
        {"Cache-Control": "no-store"},
    )


def provider(
    transport: FakeTransport,
    source: Any,
    clock: Clock | None = None,
    sleeps: list[float] | None = None,
) -> WorkloadFederationCredentialProvider:
    return WorkloadFederationCredentialProvider(
        identity_url=IDENTITY + "/",
        assertion_source=source,
        transport=transport,
        clock=clock or Clock(),
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


def test_first_exchange_posts_assertion_and_caches_token() -> None:
    transport = FakeTransport([issued("token-1")])
    source = Assertions()
    credentials = provider(transport, source)

    credential = credentials.get_credential()
    again = credentials.get_credential()

    assert again is credential
    assert credential.access_token == "token-1"
    assert credential.token_type == "Bearer"
    assert credential.subject == "principal-a"
    assert credential.organization_id == "org-a"
    assert credential.workspace_id == "workspace-a"
    assert credential.scopes == ("console:write",)
    assert source.calls == 1
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == EXCHANGE
    assert request["headers"]["Content-Type"] == "application/json"
    assert "Authorization" not in request["headers"]
    assert json.loads(request["body"]) == {"assertion": "assertion-1"}


def test_refresh_before_expiry_fetches_a_new_assertion() -> None:
    clock = Clock()
    transport = FakeTransport([issued("token-1"), issued("token-2", lifetime=540)])
    source = Assertions()
    credentials = provider(transport, source, clock)

    assert credentials.get_credential().access_token == "token-1"
    clock.now = NOW + 239
    assert credentials.get_credential().access_token == "token-1"
    clock.now = NOW + 240
    assert credentials.get_credential().access_token == "token-2"

    bodies = [json.loads(request["body"]) for request in transport.requests]
    assert bodies == [{"assertion": "assertion-1"}, {"assertion": "assertion-2"}]


def test_refresh_credential_always_exchanges_a_new_assertion() -> None:
    transport = FakeTransport([issued("token-1"), issued("token-2")])
    source = Assertions()
    credentials = provider(transport, source)

    current = credentials.get_credential()
    refreshed = credentials.refresh_credential(current)

    assert refreshed.access_token == "token-2"
    assert source.calls == 2
    assert json.loads(transport.requests[1]["body"]) == {"assertion": "assertion-2"}


def test_forbidden_is_not_retried() -> None:
    transport = FakeTransport([FakeResponse(403, headers={"x-request-id": "req-1"})])
    source = Assertions()
    credentials = provider(transport, source)

    with pytest.raises(DeixicError) as error:
        credentials.get_credential()

    assert error.value.kind == "authorization"
    assert error.value.status_code == 403
    assert error.value.code == "workload_federation_forbidden"
    assert error.value.request_id == "req-1"
    assert source.calls == 1
    assert len(transport.requests) == 1


def test_replay_is_not_retried() -> None:
    transport = FakeTransport([FakeResponse(409)])
    source = Assertions()
    credentials = provider(transport, source)

    with pytest.raises(DeixicError) as error:
        credentials.get_credential()

    assert error.value.kind == "conflict"
    assert error.value.code == "workload_assertion_replayed"
    assert len(transport.requests) == 1


def test_unavailable_is_retried_with_a_new_assertion() -> None:
    transport = FakeTransport([FakeResponse(503), FakeResponse(503), issued("token-1")])
    source = Assertions()
    sleeps: list[float] = []
    credentials = provider(transport, source, sleeps=sleeps)

    assert credentials.get_credential().access_token == "token-1"
    bodies = [
        json.loads(request["body"])["assertion"] for request in transport.requests
    ]
    assert bodies == ["assertion-1", "assertion-2", "assertion-3"]
    assert sleeps == [0.5, 1.0]


def test_unavailable_stops_after_max_attempts() -> None:
    transport = FakeTransport([FakeResponse(503)] * 3)
    credentials = provider(transport, Assertions())

    with pytest.raises(DeixicError) as error:
        credentials.get_credential()

    assert error.value.kind == "unavailable"
    assert error.value.code == "workload_federation_unavailable"
    assert len(transport.requests) == 3


def test_transport_failure_is_retried() -> None:
    transport = FakeTransport([ConnectionError("reset"), issued("token-1")])
    credentials = provider(transport, Assertions())

    assert credentials.get_credential().access_token == "token-1"
    assert len(transport.requests) == 2


def test_reused_assertion_is_never_sent() -> None:
    clock = Clock()
    transport = FakeTransport([issued("token-1")])
    credentials = provider(transport, lambda: "same-assertion", clock)

    credentials.get_credential()
    clock.now = NOW + 301
    with pytest.raises(DeixicError) as error:
        credentials.get_credential()

    assert error.value.code == "workload_assertion_reused"
    assert "same-assertion" not in str(error.value)
    assert len(transport.requests) == 1


def test_early_refresh_keeps_unexpired_token_when_source_has_not_rotated() -> None:
    clock = Clock()
    transport = FakeTransport([issued("token-1")])
    credentials = provider(transport, lambda: "same-assertion", clock)

    credentials.get_credential()
    clock.now = NOW + 250
    assert credentials.get_credential().access_token == "token-1"
    assert len(transport.requests) == 1


def test_errors_do_not_contain_assertion_or_token() -> None:
    transport = FakeTransport([FakeResponse(200, b'{"access_token":"leak"}')])
    credentials = provider(transport, lambda: "secret-assertion")

    with pytest.raises(DeixicError) as error:
        credentials.get_credential()

    assert error.value.kind == "protocol"
    assert "secret-assertion" not in str(error.value)
    assert "leak" not in str(error.value)
    assert "secret-assertion" not in repr(credentials)


def test_github_actions_source_requests_audience_bound_token() -> None:
    transport = FakeTransport(
        [
            FakeResponse(200, b'{"value":"github-jwt-1"}'),
            FakeResponse(200, b'{"value":"github-jwt-2"}'),
        ]
    )
    source = github_actions_assertion_source(
        EXCHANGE,
        transport=transport,
        environ={
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.test/id?api-version=2.0",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
        },
    )

    assert source() == "github-jwt-1"
    assert source() == "github-jwt-2"
    request = transport.requests[0]
    assert request["method"] == "GET"
    assert request["url"] == (
        "https://token.actions.test/id?api-version=2.0&audience="
        "https%3A%2F%2Fidentity.deixic.test%2Fv1%2Fworkload-federation%2Fexchange"
    )
    assert request["headers"]["Authorization"] == "Bearer request-token"
    assert len(transport.requests) == 2


def test_github_actions_source_requires_id_token_permission() -> None:
    source = github_actions_assertion_source(
        "aud", transport=FakeTransport([]), environ={}
    )

    with pytest.raises(DeixicError) as error:
        source()

    assert error.value.code == "workload_assertion_unavailable"
    assert "id-token: write" in str(error.value)


def test_file_source_rereads_the_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("k8s-jwt-1\n")
    source = file_assertion_source(token_file)

    assert source() == "k8s-jwt-1"
    token_file.write_text("k8s-jwt-2\n")
    assert source() == "k8s-jwt-2"


def test_environment_source_reads_current_value() -> None:
    environ = {"DEIXIC_WORKLOAD_ASSERTION": "env-jwt-1"}
    source = environment_assertion_source("DEIXIC_WORKLOAD_ASSERTION", environ=environ)

    assert source() == "env-jwt-1"
    environ["DEIXIC_WORKLOAD_ASSERTION"] = "env-jwt-2"
    assert source() == "env-jwt-2"


def test_client_replays_401_with_a_newly_exchanged_token() -> None:
    identity = FakeTransport([issued("token-1"), issued("token-2")])
    credentials = provider(identity, Assertions())
    platform = FakeTransport(
        [
            FakeResponse(401),
            FakeResponse(
                200,
                public_pb2.GetThreadResponse().SerializeToString(deterministic=True),
            ),
        ]
    )
    client = Deixic(
        credential_provider=credentials,
        organization_id="org-a",
        workspace_id="workspace-a",
        base_url="https://api.deixic.test",
        transport=platform,
    )

    client.threads.get(channel_id="company")

    assert [r["headers"]["Authorization"] for r in platform.requests] == [
        "Bearer token-1",
        "Bearer token-2",
    ]
