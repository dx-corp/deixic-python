from __future__ import annotations

import json
import struct
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from console.v1 import console_pb2

from deixic import Credential, Deixic, DeixicError


@dataclass
class FakeResponse:
    status_code: int
    content: bytes = b""
    headers: Mapping[str, str] | None = None
    chunks: tuple[bytes, ...] = ()
    closed: bool = False

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))

    def iter_content(self, chunk_size: int | None = None) -> Iterator[bytes]:
        return iter(self.chunks or (self.content,))

    def close(self) -> None:
        self.closed = True

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {}


class FakeTransport:
    def __init__(self, responses: list[FakeResponse]) -> None:
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
        return self.responses.pop(0)


class RotatingCredentials:
    can_refresh = True

    def __init__(self, refreshed: Credential) -> None:
        self.refreshed = refreshed
        self.refreshes = 0

    def get_credential(self) -> Credential:
        return Credential(
            access_token="old-token",
            subject="subject-a",
            organization_id="org-a",
            workspace_id="workspace-a",
            scopes=("console:write",),
        )

    def refresh_credential(self, current: Credential) -> Credential:
        self.refreshes += 1
        return self.refreshed


def response(message: Any) -> FakeResponse:
    return FakeResponse(200, message.SerializeToString(deterministic=True))


def test_send_uses_binary_connect_and_fixed_scope() -> None:
    transport = FakeTransport(
        [
            response(
                console_pb2.SubmitOperatingMessageResponse(
                    replay_cursor=8,
                    accepted_turn=console_pb2.OperatingThreadTurn(
                        turn_id="turn-1",
                        sequence=2,
                    ),
                )
            )
        ]
    )
    client = Deixic(
        api_key="sdk-test-key",
        organization_id="org-a",
        workspace_id="workspace-a",
        base_url="https://api.deixic.test",
        transport=transport,
    )

    result = client.messages.send(
        channel_id="company",
        body="Review this change.",
        idempotency_key="send-1",
    )

    assert result.accepted_turn.turn_id == "turn-1"
    request = transport.requests[0]
    assert request["url"].endswith("/deixic.v1.DeixicService/SubmitOperatingMessage")
    assert request["headers"]["Content-Type"] == "application/proto"
    assert request["headers"]["Authorization"] == "Bearer sdk-test-key"
    assert request["headers"]["X-Organization-ID"] == "org-a"
    assert request["headers"]["X-Workspace-ID"] == "workspace-a"
    decoded = console_pb2.SubmitOperatingMessageRequest.FromString(request["body"])
    assert decoded.query.organization_id == "org-a"
    assert decoded.query.workspace_id == "workspace-a"
    assert decoded.idempotency_key == "send-1"


@pytest.mark.parametrize("field", ["organization_id", "workspace_id"])
def test_client_scope_cannot_be_reassigned_or_deleted(field: str) -> None:
    transport = FakeTransport([response(console_pb2.GetOperatingThreadResponse())])
    client = Deixic(
        api_key="sdk-test-key",
        organization_id="org-a",
        workspace_id="workspace-a",
        transport=transport,
    )

    with pytest.raises(AttributeError):
        setattr(client, field, "different-tenant")
    with pytest.raises(AttributeError):
        delattr(client, field)

    client.threads.get(channel_id="company")
    request = transport.requests[0]
    decoded = console_pb2.GetOperatingThreadRequest.FromString(request["body"])
    assert decoded.query.organization_id == "org-a"
    assert decoded.query.workspace_id == "workspace-a"
    assert request["headers"]["X-Organization-ID"] == "org-a"
    assert request["headers"]["X-Workspace-ID"] == "workspace-a"


def test_authentication_refresh_preserves_request_bytes_and_identity() -> None:
    first = FakeResponse(
        401,
        b'{"code":"unauthenticated","message":"expired"}',
        {"content-type": "application/json"},
    )
    transport = FakeTransport(
        [first, response(console_pb2.InterruptOperatingThreadResponse())]
    )
    credentials = RotatingCredentials(
        Credential(
            access_token="new-token",
            subject="subject-a",
            organization_id="org-a",
            workspace_id="workspace-a",
            scopes=("console:write",),
        )
    )
    client = Deixic(
        credential_provider=credentials,
        organization_id="org-a",
        workspace_id="workspace-a",
        transport=transport,
    )

    client.controls.interrupt(channel_id="company", idempotency_key="interrupt-1")

    assert credentials.refreshes == 1
    assert first.closed is True
    assert transport.requests[0]["body"] == transport.requests[1]["body"]
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer old-token"
    assert transport.requests[1]["headers"]["Authorization"] == "Bearer new-token"


def test_second_authentication_failure_is_not_retried() -> None:
    first = FakeResponse(401, b'{"code":"unauthenticated"}')
    second = FakeResponse(401, b'{"code":"unauthenticated"}')
    transport = FakeTransport([first, second])
    credentials = RotatingCredentials(
        Credential(
            access_token="new-token",
            subject="subject-a",
            organization_id="org-a",
            workspace_id="workspace-a",
            scopes=("console:write",),
        )
    )
    client = Deixic(
        credential_provider=credentials,
        organization_id="org-a",
        workspace_id="workspace-a",
        transport=transport,
    )

    with pytest.raises(DeixicError) as caught:
        client.controls.interrupt(channel_id="company", idempotency_key="interrupt-1")

    assert caught.value.kind == "authentication"
    assert credentials.refreshes == 1
    assert len(transport.requests) == 2
    assert first.closed is True
    assert second.closed is True


def test_refresh_rejects_subject_change_before_replay() -> None:
    first = FakeResponse(
        401, b'{"code":"unauthenticated"}', {"content-type": "application/json"}
    )
    transport = FakeTransport([first])
    credentials = RotatingCredentials(
        Credential(
            access_token="new-token",
            subject="subject-b",
            organization_id="org-a",
            workspace_id="workspace-a",
            scopes=("console:write",),
        )
    )
    client = Deixic(
        credential_provider=credentials,
        organization_id="org-a",
        workspace_id="workspace-a",
        transport=transport,
    )

    with pytest.raises(DeixicError, match="same stable subject") as caught:
        client.controls.interrupt(channel_id="company", idempotency_key="interrupt-1")

    assert caught.value.kind == "authentication"
    assert len(transport.requests) == 1
    assert first.closed is True


def test_watch_decodes_bounded_connect_stream_frames() -> None:
    first = console_pb2.WatchOperatingThreadResponse(next_cursor=4)
    second = console_pb2.WatchOperatingThreadResponse(
        next_cursor=8, reset_required=True
    )
    frames = b"".join(
        [
            _frame(first.SerializeToString(deterministic=True)),
            _frame(second.SerializeToString(deterministic=True)),
            _frame(b"{}", flags=2),
        ]
    )
    stream_response = FakeResponse(
        200,
        headers={"content-type": "application/connect+proto"},
        chunks=(frames[:7], frames[7:19], frames[19:]),
    )
    transport = FakeTransport([stream_response])
    client = Deixic(
        api_key="sdk-test-key",
        organization_id="org-a",
        workspace_id="workspace-a",
        transport=transport,
    )

    pages = list(client.events.watch(channel_id="company", after_cursor=0))

    assert [page.next_cursor for page in pages] == [4, 8]
    assert pages[1].reset_required is True
    assert stream_response.closed is True
    assert transport.requests[0]["stream"] is True
    assert (
        transport.requests[0]["headers"]["Content-Type"] == "application/connect+proto"
    )
    request_body = transport.requests[0]["body"]
    assert request_body[0] == 0
    assert struct.unpack(">I", request_body[1:5])[0] == len(request_body) - 5
    request = console_pb2.WatchOperatingThreadRequest.FromString(request_body[5:])
    assert request.channel_id == "company"


def test_mutations_require_caller_idempotency_before_transport() -> None:
    transport = FakeTransport([])
    client = Deixic(
        api_key="sdk-test-key",
        organization_id="org-a",
        workspace_id="workspace-a",
        transport=transport,
    )

    with pytest.raises(DeixicError, match="idempotency_key is required"):
        client.messages.send(channel_id="company", body="hello", idempotency_key=" ")

    assert transport.requests == []


def test_default_origin_is_the_deployed_deixic_api() -> None:
    transport = FakeTransport([response(console_pb2.GetOperatingThreadResponse())])
    client = Deixic(
        api_key="sdk-test-key",
        organization_id="org-a",
        workspace_id="workspace-a",
        transport=transport,
    )

    client.threads.get(channel_id="company")

    assert transport.requests[0]["url"].startswith("https://app.deixic.com/")


def test_transport_failure_is_typed_and_not_retried() -> None:
    class FailingTransport:
        calls = 0

        def send(self, *args: Any, **kwargs: Any) -> FakeResponse:
            self.calls += 1
            raise OSError("socket contained implementation details")

    transport = FailingTransport()
    client = Deixic(
        api_key="sdk-test-key",
        organization_id="org-a",
        workspace_id="workspace-a",
        transport=transport,
    )

    with pytest.raises(DeixicError, match="Deixic transport failed") as caught:
        client.threads.get(channel_id="company")

    assert caught.value.kind == "transport"
    assert transport.calls == 1


def test_watch_rejects_malformed_end_envelope_as_protocol_error() -> None:
    stream_response = FakeResponse(
        200,
        headers={"content-type": "application/connect+proto"},
        chunks=(_frame(b"not-json", flags=2),),
    )
    client = Deixic(
        api_key="sdk-test-key",
        organization_id="org-a",
        workspace_id="workspace-a",
        transport=FakeTransport([stream_response]),
    )

    with pytest.raises(DeixicError, match="invalid end envelope") as caught:
        list(client.events.watch(channel_id="company", after_cursor=0))

    assert caught.value.kind == "protocol"
    assert stream_response.closed is True


@pytest.mark.parametrize(
    "base_url",
    [
        "deixic.example",
        "ftp://deixic.example",
        "https://user@deixic.example",
        "https://deixic.example?token=x",
    ],
)
def test_client_rejects_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(DeixicError) as caught:
        Deixic(
            api_key="sdk-test-key",
            organization_id="org-a",
            workspace_id="workspace-a",
            base_url=base_url,
            transport=FakeTransport([]),
        )

    assert caught.value.kind == "validation"


@pytest.mark.parametrize("api_key", [None, " "])
def test_client_reports_invalid_auth_configuration_as_typed_validation(
    api_key: str | None,
) -> None:
    with pytest.raises(DeixicError) as caught:
        Deixic(
            api_key=api_key,
            organization_id="org-a",
            workspace_id="workspace-a",
            transport=FakeTransport([]),
        )

    assert caught.value.kind == "validation"


def _frame(payload: bytes, *, flags: int = 0) -> bytes:
    return bytes([flags]) + struct.pack(">I", len(payload)) + payload


def test_coding_submission_preserves_explicit_acceptance_and_declares_kind() -> None:
    transport = FakeTransport([FakeResponse(200, console_pb2.SubmitOperatingMessageResponse().SerializeToString())])
    client = Deixic(api_key="test-key", organization_id="org-a", workspace_id="workspace-a", transport=transport)
    contract = console_pb2.CodingAcceptanceContract(repository_id="fixture", generation=1,
        required_assertion_ids=["sum"], require_review=True, require_behavior=True, readiness_requirements=["test"])
    client.messages.send(channel_id="coding", body="Implement fixture", idempotency_key="coding-1", coding_acceptance=contract)
    request = console_pb2.SubmitOperatingMessageRequest.FromString(transport.requests[0]["body"])
    assert request.coding_acceptance == contract
    assert request.task_kind == console_pb2.OPERATING_TASK_KIND_CODING_IMPLEMENTATION


@pytest.mark.parametrize("include_content", [False, True])
def test_coding_output_content_requires_explicit_receipt_read(include_content: bool) -> None:
    transport = FakeTransport([FakeResponse(200, console_pb2.GetOperatingReceiptResponse().SerializeToString())])
    client = Deixic(api_key="test-key", organization_id="org-a", workspace_id="workspace-a", transport=transport)
    client.receipts.get(channel_id="coding", receipt_id="receipt-1", include_coding_output_content=include_content)
    request = console_pb2.GetOperatingReceiptRequest.FromString(transport.requests[0]["body"])
    assert request.include_coding_output_content == include_content
    assert request.query.organization_id == "org-a"
    assert request.query.workspace_id == "workspace-a"
