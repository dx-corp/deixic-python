from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from deixic import protocol as pb
from deixic import Credential, Deixic, DeixicError
from deixic.examples import task_result as example

from test_client import (
    FakeResponse,
    FakeTransport,
    RotatingCredentials,
    _frame,
    response,
)
from test_http_journey import FixtureCredentials, loopback_owner


def checkpoint(tmp_path: Path, *, url: str = "https://api.deixic.test"):
    path = tmp_path / "task.json"
    state = example.prepare(
        path,
        organization_id="org-fixture",
        workspace_id="ws-fixture",
        base_url=url,
        channel_id="company",
        body="Return a verified result",
    )
    return path, state


def client(transport):
    return Deixic(
        api_key="fixture-secret",
        organization_id="org-fixture",
        workspace_id="ws-fixture",
        base_url="https://api.deixic.test",
        transport=transport,
    )


def test_saved_request_survives_response_loss_and_explicit_replay(tmp_path):
    path, state = checkpoint(tmp_path)
    accepted = pb.SubmitTaskResponse(
        replay_cursor=9007199254740994,
        accepted_turn=pb.TaskTurn(
            turn_id="same-turn", sequence=1, state=pb.TURN_STATE_ACCEPTED
        ),
    )

    class LostResponse(FakeTransport):
        def send(self, *args, **kwargs):
            result = super().send(*args, **kwargs)
            if len(self.requests) == 1:
                raise ConnectionError("response lost after owner acceptance")
            return result

    transport = LostResponse([response(accepted), response(accepted)])
    with pytest.raises(DeixicError, match="transport failed"):
        example.submit(client(transport), path, state)
    recovered = example.load(
        path,
        organization_id="org-fixture",
        workspace_id="ws-fixture",
        base_url="https://api.deixic.test",
    )
    assert not recovered["turn_id"]
    assert (
        example.resume(client(transport), path, recovered)["status"] == "unacknowledged"
    )
    assert len(transport.requests) == 1  # Observation does not retry acceptance.
    example.submit(client(transport), path, recovered)
    assert transport.requests[0]["body"] == transport.requests[1]["body"]
    assert recovered["turn_id"] == "same-turn"
    assert recovered["cursor"] == 9_007_199_254_740_994
    with pytest.raises(ValueError, match="already has an accepted turn"):
        example.submit(client(transport), path, recovered)
    assert "fixture-secret" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_restart_recovers_exact_turn_final_message_and_receipt_over_http(tmp_path):
    with loopback_owner("WatchEvents") as (url, calls, _):
        path, state = checkpoint(tmp_path, url=url)
        original = Deixic(
            credential_provider=FixtureCredentials(),
            organization_id="org-fixture",
            workspace_id="ws-fixture",
            base_url=url,
        )
        example.submit(original, path, state)
        resumed = Deixic(
            credential_provider=FixtureCredentials(),
            organization_id="org-fixture",
            workspace_id="ws-fixture",
            base_url=url,
        )
        recovered = example.load(
            path, organization_id="org-fixture", workspace_id="ws-fixture", base_url=url
        )
        outcome = example.resume(resumed, path, recovered)
        assert outcome["status"] == "completed"
        assert outcome["turn_id"] == "turn-fixture"
        assert outcome["receipt_ids"] == ["receipt-fixture"]
        assert len([call for call in calls if call[0] == "SubmitTask"]) == 1
        watches = [call for call in calls if call[0] == "WatchEvents"]
        assert len(watches) == 2
        assert watches[0][2] == watches[1][2]


def test_cursor_reset_replaces_old_terminal_projection(tmp_path):
    path, state = checkpoint(tmp_path)
    state.update(turn_id="target", cursor=100, turn_state=pb.TURN_STATE_COMPLETED)
    page = pb.ListEventsResponse(
        reset_required=True,
        next_cursor=200,
        snapshot_turns=[
            pb.TaskTurn(turn_id="target", sequence=1, state=pb.TURN_STATE_RUNNING)
        ],
        events=[
            pb.TaskEvent(
                turn_id="unrelated", cursor=200, kind=pb.EVENT_KIND_TURN_COMPLETED
            )
        ],
    )
    example.apply_page(path, state, page)
    assert json.loads(path.read_text())["turn_state"] == pb.TURN_STATE_RUNNING
    assert state["cursor"] == 200
    page.ClearField("snapshot_turns")
    example.apply_page(path, state, page)
    assert state["turn_state"] == pb.TURN_STATE_UNSPECIFIED


def test_watch_eof_is_unfinished_and_resume_uses_saved_cursor_without_submission(
    tmp_path,
):
    path, state = checkpoint(tmp_path)
    state.update(turn_id="target", cursor=9_007_199_254_740_994)
    running = pb.GetThreadResponse(
        turns=[pb.TaskTurn(turn_id="target", sequence=1, state=pb.TURN_STATE_RUNNING)]
    )
    page = pb.WatchEventsResponse(next_cursor=state["cursor"] + 1)
    watch = FakeResponse(
        200, content=_frame(page.SerializeToString()) + _frame(b"{}", flags=2)
    )
    transport = FakeTransport(
        [
            response(running),
            response(pb.ListEventsResponse(next_cursor=state["cursor"])),
            response(running),
            watch,
            response(running),
        ]
    )
    assert example.resume(client(transport), path, state)["reason"] == "watch_eof"
    saved = example.load(
        path,
        organization_id="org-fixture",
        workspace_id="ws-fixture",
        base_url="https://api.deixic.test",
    )
    assert saved["cursor"] == 9_007_199_254_740_995
    next_transport = FakeTransport(
        [
            response(running),
            response(pb.ListEventsResponse(next_cursor=saved["cursor"])),
            response(running),
            FakeResponse(200, content=_frame(b"{}", flags=2)),
        ]
    )
    assert example.resume(client(next_transport), path, saved)["status"] == "unfinished"
    listed = pb.ListEventsRequest.FromString(next_transport.requests[1]["body"])
    assert listed.after_cursor == saved["cursor"]
    assert all(
        "SubmitTask" not in call["url"]
        for call in transport.requests + next_transport.requests
    )
    assert watch.closed


@pytest.mark.parametrize("changed", ["organization_id", "workspace_id", "base_url"])
def test_checkpoint_cannot_move_between_tenants_or_platforms(tmp_path, changed):
    path, _ = checkpoint(tmp_path)
    coordinates = dict(
        organization_id="org-fixture",
        workspace_id="ws-fixture",
        base_url="https://api.deixic.test",
    )
    coordinates[changed] = "different"
    with pytest.raises(ValueError, match="different tenant or Platform"):
        example.load(path, **coordinates)


@pytest.mark.parametrize("ending", [b"\x00", _frame(b"broken")[:-1]])
def test_truncated_watch_is_protocol_failure_and_closes_response(ending):
    reply = FakeResponse(200, chunks=tuple(bytes([byte]) for byte in ending))
    transport = FakeTransport([reply])
    with pytest.raises(DeixicError, match="incomplete frame") as error:
        list(client(transport).events.watch(channel_id="company", after_cursor=0))
    assert error.value.kind == "protocol"
    assert transport.responses == []
    assert reply.closed


def test_fragmented_http_200_terminal_error_is_not_success():
    document = json.dumps(
        {"error": {"code": "permission_denied", "message": "token revoked"}}
    ).encode()
    frame = _frame(document, flags=2)
    reply = FakeResponse(200, chunks=tuple(bytes([byte]) for byte in frame))
    transport = FakeTransport([reply])
    with pytest.raises(DeixicError) as error:
        list(client(transport).events.watch(channel_id="company", after_cursor=0))
    assert error.value.kind == "authorization"
    assert error.value.code == "permission_denied"
    assert len(transport.requests) == 1
    assert reply.closed


def test_revoked_static_credential_stops_before_watch_or_submission(tmp_path):
    path, state = checkpoint(tmp_path)
    state["turn_id"] = "target"
    reply = FakeResponse(401, content=b'{"code":"unauthenticated"}')
    transport = FakeTransport([reply])
    with pytest.raises(DeixicError) as error:
        example.resume(client(transport), path, state)
    assert error.value.kind == "authentication"
    assert len(transport.requests) == 1
    assert reply.closed


@pytest.mark.parametrize(
    "turn_state,status",
    [
        (pb.TURN_STATE_FAILED, "failed"),
        (pb.TURN_STATE_INTERRUPTED, "interrupted"),
    ],
)
def test_matching_terminal_failure_never_returns_success(tmp_path, turn_state, status):
    path, state = checkpoint(tmp_path)
    state["turn_id"] = "target"
    transport = FakeTransport(
        [
            response(
                pb.GetThreadResponse(
                    turns=[pb.TaskTurn(turn_id="target", sequence=1, state=turn_state)],
                    messages=[
                        pb.TaskMessage(
                            id="unrelated",
                            role=pb.MESSAGE_ROLE_ASSISTANT,
                            body="Everything is done",
                        )
                    ],
                )
            )
        ]
    )
    assert example.resume(client(transport), path, state)["status"] == status
    assert len(transport.requests) == 1


def test_paginated_final_message_and_receipt_are_linked_to_accepted_turn(tmp_path):
    path, state = checkpoint(tmp_path)
    state["turn_id"] = "target"
    transport = FakeTransport(
        [
            response(
                pb.GetThreadResponse(
                    next_page_token="older",
                    turns=[
                        pb.TaskTurn(
                            turn_id="target",
                            sequence=1,
                            state=pb.TURN_STATE_COMPLETED,
                            assistant_message_id="final",
                        )
                    ],
                    messages=[
                        pb.TaskMessage(
                            id="preliminary",
                            role=pb.MESSAGE_ROLE_ASSISTANT,
                            body="Working",
                        )
                    ],
                )
            ),
            response(
                pb.GetThreadResponse(
                    messages=[
                        pb.TaskMessage(
                            id="final",
                            role=pb.MESSAGE_ROLE_ASSISTANT,
                            body="Finished",
                            receipt_ids=["receipt"],
                        )
                    ]
                )
            ),
            response(pb.GetReceiptResponse(receipt=pb.Receipt(id="receipt"))),
        ]
    )
    outcome = example.resume(client(transport), path, state)
    assert outcome["body"] == "Finished"
    assert outcome["message_id"] == "final"
    assert outcome["receipt_ids"] == ["receipt"]
    assert (
        pb.GetThreadRequest.FromString(transport.requests[1]["body"]).page_token
        == "older"
    )


def test_completed_turn_cannot_use_an_unrelated_message(tmp_path):
    path, state = checkpoint(tmp_path)
    state["turn_id"] = "target"
    transport = FakeTransport(
        [
            response(
                pb.GetThreadResponse(
                    turns=[
                        pb.TaskTurn(
                            turn_id="target",
                            sequence=1,
                            state=pb.TURN_STATE_COMPLETED,
                            assistant_message_id="missing",
                        )
                    ],
                    messages=[
                        pb.TaskMessage(
                            id="unrelated",
                            role=pb.MESSAGE_ROLE_ASSISTANT,
                            body="Finished",
                        )
                    ],
                )
            )
        ]
    )
    with pytest.raises(ValueError, match="linked final assistant message"):
        example.resume(client(transport), path, state)


@pytest.mark.parametrize("changed", ["organization_id", "workspace_id", "scopes"])
def test_authentication_refresh_cannot_change_tenant_or_declared_grants(changed):
    coordinates = dict(
        access_token="new-token",
        subject="subject-a",
        organization_id="org-a",
        workspace_id="workspace-a",
        scopes=("console:write",),
    )
    coordinates[changed] = (
        ("console:read", "console:write") if changed == "scopes" else "different"
    )
    provider = RotatingCredentials(Credential(**coordinates))
    reply = FakeResponse(401, content=b'{"code":"unauthenticated"}')
    transport = FakeTransport([reply])
    sdk = Deixic(
        credential_provider=provider,
        organization_id="org-a",
        workspace_id="workspace-a",
        transport=transport,
    )
    with pytest.raises(DeixicError) as error:
        sdk.messages.send(
            channel_id="company", body="Task", idempotency_key="same-request"
        )
    assert error.value.kind == "authentication"
    assert len(transport.requests) == 1
    assert provider.refreshes == 1
    assert reply.closed


def test_installed_example_cli_restarts_without_resubmitting(tmp_path):
    with loopback_owner("WatchEvents") as (url, calls, _):
        env = dict(
            os.environ,
            DEIXIC_API_KEY="fixture-cli-secret",
            DEIXIC_ORGANIZATION_ID="org-fixture",
            DEIXIC_WORKSPACE_ID="ws-fixture",
            DEIXIC_BASE_URL=url,
        )
        path = tmp_path / "cli.json"
        command = [sys.executable, "-m", "deixic.examples.task_result"]
        started = subprocess.run(
            command + ["start", str(path), "--body", "Return a verified result"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert started.returncode == 0, started.stderr
        resumed = subprocess.run(
            command + ["resume", str(path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert resumed.returncode == 0, resumed.stderr
        first, second = json.loads(started.stdout), json.loads(resumed.stdout)
        assert first == second
        assert second["status"] == "completed"
        assert len([call for call in calls if call[0] == "SubmitTask"]) == 1
        assert "fixture-cli-secret" not in path.read_text()
