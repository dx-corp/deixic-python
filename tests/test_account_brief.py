from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from console.v1 import console_pb2 as pb


C = 9_007_199_254_740_994


@contextmanager
def owner(*, lose_acceptance=False, state=None):
    state = state if state is not None else {}
    state.setdefault("decisions", [])
    operations = {}
    submissions = []
    errors = []
    completed = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal completed
            method = self.path.rsplit("/", 1)[-1]
            body = self.rfile.read(int(self.headers["Content-Length"]))
            try:
                assert self.path.startswith("/deixic.v1.DeixicService/")
                assert self.headers["Content-Type"] == "application/proto"
                assert self.headers["Connect-Protocol-Version"] == "1"
                assert self.headers["Authorization"] == "Bearer fixture-secret"
                request = getattr(pb, method + "Request").FromString(body)
                assert (
                    request.query.organization_id
                    == self.headers["X-Organization-ID"]
                    == "org-fixture"
                )
                assert (
                    request.query.workspace_id
                    == self.headers["X-Workspace-ID"]
                    == "ws-fixture"
                )
                assert request.channel_id == "company"
                if method == "SubmitOperatingMessage":
                    submissions.append(body)
                    key = request.idempotency_key
                    assert key == "crm-event-001"
                    assert "Example account" in request.body
                    if key in operations:
                        assert operations[key] == body
                    operations[key] = body
                    if state.get("on_accept"):
                        state["on_accept"]()
                    if lose_acceptance and len(submissions) == 1:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    message = pb.SubmitOperatingMessageResponse(
                        replay_cursor=C,
                        accepted_turn=pb.OperatingThreadTurn(
                            turn_id="target",
                            sequence=2,
                            state=pb.OPERATING_TURN_STATE_QUEUED,
                        ),
                    )
                elif method == "ListOperatingThreadEvents":
                    if state.get("block_read"):
                        state["read_started"].set()
                        state["release_read"].wait(5)
                    if state.get("disconnect_read") and not state.get("disconnected"):
                        state["disconnected"] = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    completed = (
                        not state.get("waiting") or state.get("decision") == "approve"
                    )
                    message = pb.ListOperatingThreadEventsResponse(
                        next_cursor=C + 1
                        if completed
                        or (
                            state.get("waiting")
                            and not state.get("decision")
                            and state.get("request_visible", True)
                        )
                        else request.after_cursor,
                        events=[
                            pb.OperatingThreadEvent(
                                cursor=C + 1,
                                turn_id="target",
                                event_id="completed",
                                kind=pb.OPERATING_THREAD_EVENT_KIND_TURN_COMPLETED,
                            )
                        ]
                        if completed and request.after_cursor < C + 1
                        else [
                            pb.OperatingThreadEvent(
                                cursor=C + 1,
                                turn_id="target",
                                event_id="approval",
                                kind=pb.OPERATING_THREAD_EVENT_KIND_APPROVAL_REQUIRED,
                                request_id="approval-1",
                                request_call_id="call-1",
                                request_type=pb.OPERATING_THREAD_REQUEST_TYPE_APPROVAL,
                            )
                        ]
                        if state.get("waiting")
                        and not state.get("decision")
                        and state.get("request_visible", True)
                        and request.after_cursor < C + 1
                        else [],
                    )
                elif method == "GetOperatingThread":
                    message = pb.GetOperatingThreadResponse(
                        channel=pb.OperatingChannel(id="company"),
                        default_model=pb.InferenceProviderTarget(
                            provider="fixture", model="fixture", ready=True
                        ),
                        turns=[
                            pb.OperatingThreadTurn(
                                turn_id="target",
                                sequence=2,
                                state=pb.OPERATING_TURN_STATE_COMPLETED
                                if completed
                                else pb.OPERATING_TURN_STATE_FAILED
                                if state.get("decision") == "deny"
                                else pb.OPERATING_TURN_STATE_WAITING
                                if state.get("waiting")
                                else pb.OPERATING_TURN_STATE_QUEUED,
                                waiting_reason=pb.OPERATING_THREAD_WAITING_REASON_APPROVAL
                                if state.get("waiting")
                                else pb.OPERATING_THREAD_WAITING_REASON_NONE,
                                first_cursor=C,
                                last_cursor=C + 1,
                                assistant_message_id="answer" if completed else "",
                            )
                        ],
                        messages=[
                            pb.OperatingMessage(
                                id="answer",
                                channel_id="company",
                                role="assistant",
                                body=state.get(
                                    "body", "Example account: an evidence-linked brief"
                                ),
                                receipt_ids=["evidence"],
                            )
                        ]
                        if completed
                        else [],
                    )
                elif method == "GetOperatingReceipt":
                    assert request.receipt_id == "evidence"
                    message = pb.GetOperatingReceiptResponse(
                        receipt=pb.OperatingReceipt(
                            id="evidence",
                            lifecycle_state=state.get(
                                "receipt_state", pb.RECEIPT_LIFECYCLE_STATE_VERIFIED
                            ),
                            owner_service="crm",
                            object_id="account-1",
                            kind="crm.update",
                            evidence_refs=[
                                pb.RelatedResource(
                                    id="change-1", resource_type="crm-change"
                                )
                            ],
                        )
                    )
                elif method == "RespondOperatingThread":
                    if state.get("forbidden_decision"):
                        self.send_response(403)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(
                            b'{"code":"permission_denied","message":"operator grant required"}'
                        )
                        return
                    assert state.get("waiting") and not state.get("decision")
                    assert request.turn_id == "target"
                    assert request.response.request_id == "approval-1"
                    assert request.response.call_id == "call-1"
                    assert (
                        request.response.request_type
                        == pb.OPERATING_THREAD_REQUEST_TYPE_APPROVAL
                    )
                    assert (
                        request.idempotency_key
                        == request.response.idempotency_key
                        == "decision-001"
                    )
                    state["decisions"].append(body)
                    state["decision"] = (
                        "approve"
                        if request.response.action
                        == pb.OPERATING_THREAD_RESPONSE_ACTION_APPROVE
                        else "deny"
                    )
                    completed = state["decision"] == "approve"
                    message = pb.RespondOperatingThreadResponse(replay_cursor=C + 2)
                else:
                    raise AssertionError("unexpected mutation or RPC: " + method)
            except Exception as error:
                errors.append(str(error))
                self.send_error(400)
                return
            data = message.SerializeToString(deterministic=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/proto")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The crash-recovery test intentionally kills the waiting reader.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", operations, submissions, errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def invocation(language="python"):
    if language == "python":
        return [sys.executable, "-m", "deixic.examples.account_brief"]
    script = os.environ.get("DEIXIC_TYPESCRIPT_EXAMPLE") or str(
        Path(__file__).resolve().parents[2] / "typescript/examples/account-brief.mjs"
    )
    assert shutil.which("node") and Path(script).is_file()
    return ["node", script]


def command(arguments, environment, language="python"):
    result = subprocess.run(
        [*invocation(language), *arguments],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
    )
    return result.returncode, json.loads(result.stdout)


def configuration(url):
    return dict(
        os.environ,
        DEIXIC_API_KEY="fixture-secret",
        DEIXIC_ORGANIZATION_ID="org-fixture",
        DEIXIC_WORKSPACE_ID="ws-fixture",
        DEIXIC_BASE_URL=url,
    )


def test_account_brief_submits_and_recovers_in_separate_processes(tmp_path):
    with owner() as (url, operations, submissions, errors):
        environment = configuration(url)
        path = str(tmp_path / "brief.json")
        status, check = command(["check"], environment)
        assert status == 0 and check["status"] == "accessible"
        assert check["write_access"] == "not_checked"
        status, started = command(
            [
                "start",
                path,
                "--account",
                "Example account",
                "--trigger",
                "crm-event-001",
            ],
            environment,
        )
        assert status == 0 and started["status"] == "accepted"
        status, outcome = command(["resume", path], environment)
        assert status == 0 and outcome["status"] == "completed"
        assert outcome["body"] == "Example account: an evidence-linked brief"
        assert outcome["receipt_ids"] == ["evidence"]
        assert len(operations) == len(submissions) == 1
        assert not errors
        assert "fixture-secret" not in Path(path).read_text()
        assert Path(path).stat().st_mode & 0o777 == 0o600
        status, _ = command(
            [
                "start",
                path,
                "--account",
                "Example account",
                "--trigger",
                "crm-event-001",
            ],
            environment,
        )
        assert status != 0 and len(submissions) == 1


def test_lost_acceptance_resume_is_read_only_and_replay_preserves_request(tmp_path):
    with owner(lose_acceptance=True) as (url, operations, submissions, errors):
        environment = configuration(url)
        path = str(tmp_path / "brief.json")
        status, _ = command(
            [
                "start",
                path,
                "--account",
                "Example account",
                "--trigger",
                "crm-event-001",
            ],
            environment,
        )
        assert status == 1
        status, pending = command(["resume", path], environment)
        assert status == 2 and pending["status"] == "unacknowledged"
        assert len(submissions) == 1
        status, replayed = command(["replay", path], environment)
        assert status == 0 and replayed["status"] == "accepted"
        status, completed = command(["resume", path], environment)
        assert status == 0 and completed["status"] == "completed"
        assert len(operations) == 1 and submissions[0] == submissions[1]
        assert not errors


@pytest.mark.parametrize("lose_acceptance", [False, True])
def test_typescript_checkpoint_recovers_in_python_without_duplicate_operation(
    tmp_path, lose_acceptance
):
    # The component builds TypeScript before running Python tests. Standalone
    # Python consumers can run their tests without a Node development checkout.
    sdk_root = Path(__file__).resolve().parents[2]
    explicit_script = os.environ.get("DEIXIC_TYPESCRIPT_EXAMPLE")
    script = (
        Path(explicit_script)
        if explicit_script
        else sdk_root / "typescript/examples/account-brief.mjs"
    )
    compiled = sdk_root / "typescript/dist/sdk/deixic/typescript/src/index.js"
    if explicit_script:
        assert script.is_file() and shutil.which("node"), (
            "Configured TypeScript example is unavailable"
        )
    elif not shutil.which("node") or not compiled.is_file():
        pytest.skip("Build the TypeScript SDK to run cross-language checkpoint proof")
    with owner(lose_acceptance=lose_acceptance) as (
        url,
        operations,
        submissions,
        errors,
    ):
        environment = configuration(url)
        path = str(tmp_path / "brief.json")
        result = subprocess.run(
            [
                "node",
                str(script),
                "start",
                path,
                "--account",
                "Example account",
                "--trigger",
                "crm-event-001",
            ],
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == (1 if lose_acceptance else 0), result.stdout
        if lose_acceptance:
            status, pending = command(["resume", path], environment)
            assert status == 2 and pending["status"] == "unacknowledged"
            assert len(submissions) == 1
            status, replayed = command(["replay", path], environment)
            assert status == 0 and replayed["status"] == "accepted"
            assert submissions[0] == submissions[1]
        status, completed = command(["resume", path], environment)
        assert status == 0 and completed["status"] == "completed"
        assert len(operations) == 1 and len(submissions) == (
            2 if lose_acceptance else 1
        )
        assert not errors
