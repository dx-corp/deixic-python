from __future__ import annotations

import struct
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from console.v1 import console_pb2 as pb

from deixic import Credential, Deixic


@contextmanager
def loopback_owner(expired_method: str):
    """Exercise the installed requests transport without a customer tenant."""
    calls: list[tuple[str, dict[str, str], bytes]] = []
    cursor = 9_007_199_254_740_994
    completed = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            nonlocal completed
            method = self.path.rsplit("/", 1)[-1]
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append((method, dict(self.headers), body))
            if method == "WatchOperatingThread" and (
                len(body) < 5 or body[0] != 0
                or struct.unpack(">I", body[1:5])[0] != len(body) - 5
            ):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"code":"invalid_argument","message":"invalid Connect frame"}')
                return
            if (
                method == expired_method
                and self.headers["Authorization"] == "Bearer old-fixture-token"
            ):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"code":"unauthenticated","message":"expired"}')
                return

            streaming = False
            if method == "SubmitOperatingMessage":
                message = pb.SubmitOperatingMessageResponse(
                    replay_cursor=cursor,
                    accepted_turn=pb.OperatingThreadTurn(
                        turn_id="turn-fixture", sequence=2,
                        state=pb.OPERATING_TURN_STATE_QUEUED,
                    ),
                )
            elif method == "GetOperatingThread":
                finished = completed
                message = pb.GetOperatingThreadResponse(
                    replay_cursor=cursor + (3 if finished else 2),
                    turns=[pb.OperatingThreadTurn(
                        turn_id="turn-fixture", sequence=2,
                        state=(pb.OPERATING_TURN_STATE_COMPLETED if finished
                               else pb.OPERATING_TURN_STATE_RUNNING),
                        assistant_message_id="answer-fixture" if finished else "",
                    )],
                    messages=[pb.OperatingMessage(
                        id="answer-fixture", channel_id="company", role="assistant",
                        body="Verified result" if finished else "Still working",
                        receipt_ids=["receipt-fixture"] if finished else [],
                    )],
                )
            elif method == "ListOperatingThreadEvents":
                message = pb.ListOperatingThreadEventsResponse(
                    next_cursor=cursor + 2,
                    events=[
                        pb.OperatingThreadEvent(
                            cursor=cursor + 1, event_id="other-completed",
                            turn_id="other-turn",
                            kind=pb.OPERATING_THREAD_EVENT_KIND_TURN_COMPLETED,
                        ),
                        pb.OperatingThreadEvent(
                            cursor=cursor + 2, event_id="own-progress",
                            turn_id="turn-fixture",
                            kind=pb.OPERATING_THREAD_EVENT_KIND_PROGRESS,
                            safe_text="Still working",
                        ),
                    ],
                )
            elif method == "WatchOperatingThread":
                completed = True
                streaming = True
                message = pb.WatchOperatingThreadResponse(
                    next_cursor=cursor + 3,
                    events=[pb.OperatingThreadEvent(
                        cursor=cursor + 3, event_id="own-completed",
                        turn_id="turn-fixture",
                        kind=pb.OPERATING_THREAD_EVENT_KIND_TURN_COMPLETED,
                    )],
                )
            elif method == "GetOperatingReceipt":
                message = pb.GetOperatingReceiptResponse(receipt=pb.OperatingReceipt(
                    id="receipt-fixture", summary="Verified result",
                    lifecycle_state=pb.RECEIPT_LIFECYCLE_STATE_VERIFIED,
                ))
            else:
                self.send_error(404)
                return

            payload = message.SerializeToString(deterministic=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/connect+proto" if streaming
                             else "application/proto")
            self.end_headers()
            if streaming:
                payload = struct.pack(">BI", 0, len(payload)) + payload
                payload += struct.pack(">BI", 2, 2) + b"{}"
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls, cursor
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class FixtureCredentials:
    can_refresh = True

    def __init__(self) -> None:
        self.refreshed = False

    def get_credential(self) -> Credential:
        return Credential(
            access_token="new-fixture-token" if self.refreshed else "old-fixture-token",
            subject="fixture-subject", organization_id="org-fixture",
            workspace_id="ws-fixture", scopes=("console:read", "console:write"),
        )

    def refresh_credential(self, current: Credential) -> Credential:
        self.refreshed = True
        return self.get_credential()


@pytest.mark.parametrize("expired_method", ["SubmitOperatingMessage", "WatchOperatingThread"])
def test_http_task_result_preserves_scope_and_replay_identity(expired_method: str) -> None:
    with loopback_owner(expired_method) as (base_url, calls, cursor):
        client = Deixic(
            credential_provider=FixtureCredentials(), base_url=base_url,
            organization_id="org-fixture", workspace_id="ws-fixture",
        )
        for field in ("organization_id", "workspace_id"):
            with pytest.raises(AttributeError):
                setattr(client, field, "different-tenant")

        accepted = client.messages.send(
            channel_id="company", body="Return a verified result",
            idempotency_key="fixture-operation",
        )
        assert accepted.accepted_turn.state == pb.OPERATING_TURN_STATE_QUEUED
        early = client.threads.get(channel_id="company")
        assert early.turns[0].state == pb.OPERATING_TURN_STATE_RUNNING
        assert early.messages[0].body == "Still working"
        page = client.events.list(channel_id="company", after_cursor=accepted.replay_cursor)
        matching = [event for event in page.events
                    if event.turn_id == accepted.accepted_turn.turn_id]
        assert [event.kind for event in matching] == [pb.OPERATING_THREAD_EVENT_KIND_PROGRESS]
        assert page.next_cursor == cursor + 2
        stream = list(client.events.watch(channel_id="company", after_cursor=page.next_cursor))
        assert stream[0].next_cursor == cursor + 3
        assert stream[0].events[0].turn_id == accepted.accepted_turn.turn_id
        assert stream[0].events[0].kind == pb.OPERATING_THREAD_EVENT_KIND_TURN_COMPLETED
        final = client.threads.get(channel_id="company")
        assert final.turns[0].state == pb.OPERATING_TURN_STATE_COMPLETED
        assert final.turns[0].assistant_message_id == final.messages[0].id
        assert final.messages[0].body == "Verified result"
        receipt = client.receipts.get(
            channel_id="company", receipt_id=final.messages[0].receipt_ids[0],
        ).receipt
        assert receipt.lifecycle_state == pb.RECEIPT_LIFECYCLE_STATE_VERIFIED

        replayed_calls = [call for call in calls if call[0] == expired_method]
        assert len(replayed_calls) == 2
        assert replayed_calls[0][2] == replayed_calls[1][2]
        assert replayed_calls[0][1]["Authorization"] == "Bearer old-fixture-token"
        assert replayed_calls[1][1]["Authorization"] == "Bearer new-fixture-token"
        for method, headers, body in calls:
            assert headers["X-Organization-ID"] == "org-fixture"
            assert headers["X-Workspace-ID"] == "ws-fixture"
            assert headers["Connect-Protocol-Version"] == "1"
            request_type = getattr(pb, method + "Request")
            streaming = method == "WatchOperatingThread"
            assert headers["Content-Type"] == (
                "application/connect+proto" if streaming else "application/proto"
            )
            request = request_type.FromString(body[5:] if streaming else body)
            assert request.query.organization_id == "org-fixture"
            assert request.query.workspace_id == "ws-fixture"
            if method == "SubmitOperatingMessage":
                assert request.idempotency_key == "fixture-operation"
        assert len(calls) == 7
