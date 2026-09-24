from __future__ import annotations

import json
import time

import pytest
from deixic import protocol as pb
from deixic import Deixic, DeixicError

from test_client import FakeResponse, FakeTransport, response

C = 9_007_199_254_740_994


def client(transport):
    return Deixic(
        api_key="fixture-secret",
        organization_id="org-fixture",
        workspace_id="ws-fixture",
        base_url="https://api.deixic.test",
        transport=transport,
    )


def accepted():
    return pb.SubmitTaskResponse(
        replay_cursor=9007199254740994,
        accepted_turn=pb.TaskTurn(
            turn_id="target", sequence=2, state=pb.TURN_STATE_ACCEPTED
        ),
    )


def prepared(sdk, **options):
    return sdk.tasks.prepare(
        channel_id="company",
        body="Create an account brief",
        idempotency_key="crm-trigger-1",
        **options,
    )


def test_response_loss_requires_explicit_replay_with_original_request():
    class LostResponse(FakeTransport):
        def send(self, *args, **kwargs):
            result = super().send(*args, **kwargs)
            if len(self.requests) == 1:
                raise ConnectionError("lost after acceptance")
            return result

    transport = LostResponse([response(accepted()), response(accepted())])
    checkpoints = []
    task = prepared(client(transport), on_checkpoint=checkpoints.append)
    with pytest.raises(DeixicError):
        task.submit()
    saved = json.loads(json.dumps(checkpoints[-1]))
    assert saved["submission"] == "unacknowledged"
    assert "fixture-secret" not in json.dumps(saved)
    resumed = client(transport).tasks.resume(saved)
    assert resumed.result().status == "unacknowledged"
    assert len(transport.requests) == 1
    with pytest.raises(DeixicError):
        resumed.submit()
    resumed.replay()
    assert transport.requests[0]["body"] == transport.requests[1]["body"]
    assert resumed.checkpoint()["turnId"] == "target"
    assert resumed.checkpoint()["cursor"] == "9007199254740994"
    with pytest.raises(DeixicError):
        resumed.replay()


def test_completed_result_fetches_exact_linked_message_and_receipts_across_pages():
    transport = FakeTransport(
        [
            response(accepted()),
            response(
                pb.GetThreadResponse(
                    next_page_token="older",
                    turns=[
                        pb.TaskTurn(
                            turn_id="target",
                            sequence=2,
                            state=pb.TURN_STATE_COMPLETED,
                            assistant_message_id="answer",
                        )
                    ],
                    messages=[
                        pb.TaskMessage(
                            id="other-answer",
                            role=pb.MESSAGE_ROLE_ASSISTANT,
                            body="Unrelated",
                        )
                    ],
                    thread=pb.Thread(id="company"),
                )
            ),
            response(
                pb.GetThreadResponse(
                    messages=[
                        pb.TaskMessage(
                            id="answer",
                            role=pb.MESSAGE_ROLE_ASSISTANT,
                            body="Account brief",
                            receipt_ids=["evidence"],
                        )
                    ],
                    thread=pb.Thread(id="company"),
                )
            ),
            response(
                pb.GetReceiptResponse(
                    receipt=pb.Receipt(id="evidence", state=pb.RECEIPT_STATE_VERIFIED)
                )
            ),
        ]
    )
    task = prepared(client(transport)).submit()
    outcome = task.result()
    assert outcome.status == "completed"
    assert outcome.body == "Account brief"
    assert outcome.message.id == "answer"
    assert outcome.receipts[0].id == "evidence"
    assert outcome.parse(str.upper) == "ACCOUNT BRIEF"


def test_checkpoint_cannot_move_between_tenants_or_origins():
    transport = FakeTransport([])
    saved = prepared(client(transport)).checkpoint()
    for name in ("organizationId", "workspaceId", "baseUrl"):
        changed = dict(saved, **{name: "different"})
        with pytest.raises(DeixicError):
            client(transport).tasks.resume(changed)
    assert not transport.requests


def finished(**options):
    turn_options = options.pop("turn", {})
    message_options = options.pop("message", {})
    return pb.GetThreadResponse(
        thread=pb.Thread(id="company"),
        turns=[
            pb.TaskTurn(
                **dict(
                    dict(
                        turn_id="target",
                        sequence=2,
                        state=pb.TURN_STATE_COMPLETED,
                        assistant_message_id="answer",
                    ),
                    **turn_options,
                )
            )
        ],
        messages=[
            pb.TaskMessage(
                **dict(
                    dict(
                        id="answer",
                        role=pb.MESSAGE_ROLE_ASSISTANT,
                        body="Account brief",
                    ),
                    **message_options,
                )
            )
        ],
        **options,
    )


def test_retention_reset_uses_owner_execution_cursor_and_ignores_reset_events():
    events = []
    transport = FakeTransport(
        [
            response(accepted()),
            response(
                pb.ListEventsResponse(
                    reset_required=True,
                    next_cursor=C + 9,
                    snapshot=pb.Thread(replay_cursor=C + 2),
                    events=[
                        pb.TaskEvent(
                            cursor=C + 9,
                            turn_id="target",
                            kind=pb.EVENT_KIND_TURN_COMPLETED,
                        )
                    ],
                )
            ),
            response(finished()),
        ]
    )
    task = prepared(client(transport)).submit()
    assert task.wait(on_event=events.append).status == "completed"
    assert task.checkpoint()["cursor"] == str(C + 2)
    assert events == []


def test_reconnect_retries_observation_without_resubmitting():
    transport = FakeTransport(
        [
            response(accepted()),
            FakeResponse(503, b'{"code":"unavailable"}'),
            response(pb.ListEventsResponse(next_cursor=C)),
            response(finished()),
        ]
    )
    task = prepared(client(transport)).submit()
    assert task.wait(poll_interval=0.001).status == "completed"
    assert [item["url"].rsplit("/", 1)[-1] for item in transport.requests] == [
        "SubmitTask",
        "ListEvents",
        "ListEvents",
        "GetThread",
    ]


def test_progress_callback_gets_only_new_matching_events_and_cannot_forge_result():
    own = pb.TaskEvent(
        cursor=C + 2, turn_id="target", id="own", kind=pb.EVENT_KIND_PROGRESS
    )
    transport = FakeTransport(
        [
            response(accepted()),
            response(
                pb.ListEventsResponse(
                    next_cursor=C + 2,
                    events=[
                        pb.TaskEvent(
                            cursor=C + 1,
                            turn_id="another",
                            kind=pb.EVENT_KIND_TURN_COMPLETED,
                        ),
                        own,
                        own,
                    ],
                )
            ),
            response(finished()),
        ]
    )
    events = []

    def progress(event):
        events.append(event.id)
        event.turn_id = "forged"
        event.kind = pb.EVENT_KIND_TURN_FAILED

    task = prepared(client(transport)).submit()
    result = task.wait(on_event=progress)
    assert events == ["own"]
    assert result.status == "completed"
    assert result.event.turn_id == "target"
    assert task.checkpoint()["cursor"] == str(C + 2)


def test_approval_request_is_recovered_from_owner_after_restart():
    saved_transport = FakeTransport([response(accepted())])
    saved = prepared(client(saved_transport)).submit().checkpoint()
    request = pb.TaskEvent(
        cursor=C - 1,
        id="approval-event",
        turn_id="target",
        kind=pb.EVENT_KIND_APPROVAL_REQUIRED,
        request_id="approval-1",
        request_kind=pb.REQUEST_KIND_APPROVAL,
    )
    transport = FakeTransport(
        [
            response(
                finished(
                    turn=dict(
                        state=pb.TURN_STATE_WAITING,
                        waiting_reason=pb.WAITING_REASON_APPROVAL,
                        first_cursor=C - 3,
                    )
                )
            ),
            response(pb.ListEventsResponse(next_cursor=C - 1, events=[request])),
        ]
    )
    task = client(transport).tasks.resume(saved)
    result = task.result()
    assert result.status == "waiting"
    assert result.event.request_id == "approval-1"
    assert result.body is None
    request = pb.ListEventsRequest.FromString(transport.requests[1]["body"])
    assert request.after_cursor == C - 4
    assert all("Submit" not in item["url"] for item in transport.requests)


def test_missing_retained_request_stays_waiting_without_inventing_approval():
    transport = FakeTransport(
        [
            response(accepted()),
            response(
                finished(
                    turn=dict(
                        state=pb.TURN_STATE_WAITING,
                        waiting_reason=pb.WAITING_REASON_APPROVAL,
                    )
                )
            ),
            response(pb.ListEventsResponse(reset_required=True)),
        ]
    )
    result = prepared(client(transport)).submit().result()
    assert result.status == "waiting"
    assert result.reason == "request_not_visible"
    assert result.event is None


@pytest.mark.parametrize(
    "bad", ["sequence", "message_id", "role", "channel", "receipt"]
)
def test_completed_results_fail_closed_on_mismatched_owner_links(bad):
    page = finished()
    tail = []
    if bad == "sequence":
        page.turns[0].sequence = 3
    if bad == "message_id":
        page.turns[0].assistant_message_id = "missing"
    if bad == "role":
        page.messages[0].role = pb.MESSAGE_ROLE_USER
    if bad == "channel":
        page.thread.id = "another"
    if bad == "receipt":
        page.messages[0].receipt_ids.append("requested")
        tail = [response(pb.GetReceiptResponse(receipt=pb.Receipt(id="wrong")))]
    transport = FakeTransport([response(accepted()), response(page), *tail])
    with pytest.raises(DeixicError) as error:
        prepared(client(transport)).submit().result()
    assert error.value.kind == "protocol"


@pytest.mark.parametrize(
    "state,status",
    [
        (pb.TURN_STATE_RESPONDED, "responded"),
        (pb.TURN_STATE_FAILED, "failed"),
        (pb.TURN_STATE_INTERRUPTED, "interrupted"),
        (pb.TURN_STATE_RUNNING, "unfinished"),
    ],
)
def test_noncompleted_owner_states_have_no_final_answer(state, status):
    transport = FakeTransport(
        [response(accepted()), response(finished(turn=dict(state=state)))]
    )
    result = prepared(client(transport)).submit().result()
    assert result.status == status
    assert result.body is None
    with pytest.raises(DeixicError):
        result.parse(str)


def test_storage_failure_prevents_submission_and_callback_failure_is_not_retried():
    transport = FakeTransport([response(accepted())])

    def save(state):
        if state["submission"] == "unacknowledged":
            raise OSError("storage failed")

    task = prepared(client(transport), on_checkpoint=save)
    with pytest.raises(OSError):
        task.submit()
    assert not transport.requests

    own = pb.TaskEvent(cursor=C + 1, turn_id="target", id="own")
    transport = FakeTransport(
        [
            response(accepted()),
            response(pb.ListEventsResponse(next_cursor=C + 1, events=[own])),
        ]
    )
    task = prepared(client(transport)).submit()
    failure = DeixicError("application callback failed", kind="transport")
    with pytest.raises(DeixicError) as error:
        task.wait(on_event=lambda event: (_ for _ in ()).throw(failure))
    assert error.value is failure
    assert len(transport.requests) == 2
    assert task.checkpoint()["cursor"] == str(C)


def test_cancelled_observation_does_not_interrupt_or_submit():
    transport = FakeTransport([response(accepted())])
    task = prepared(client(transport)).submit()
    assert task.wait(cancelled=lambda: True).reason == "cancelled"
    assert len(transport.requests) == 1


@pytest.mark.parametrize("cancel_on", ["ListEvents", "GetThread"])
def test_cancellation_during_read_stops_observation_without_remote_mutation(cancel_on):
    class CancelDuringRead(FakeTransport):
        cancelled = False

        def send(self, method, url, **kwargs):
            result = super().send(method, url, **kwargs)
            if url.endswith("/" + cancel_on):
                self.cancelled = True
            return result

    transport = CancelDuringRead(
        [
            response(accepted()),
            response(pb.ListEventsResponse(next_cursor=C)),
            response(finished()),
        ]
    )
    task = prepared(client(transport)).submit()
    outcome = task.wait(cancelled=lambda: transport.cancelled)
    assert outcome.status == "unfinished" and outcome.reason == "cancelled"
    assert len(transport.requests) == (2 if cancel_on == "ListEvents" else 3)
    assert task.checkpoint()["submission"] == "accepted"
    assert all("Interrupt" not in item["url"] for item in transport.requests)


def test_cancellation_callback_failure_is_not_a_network_error():
    transport = FakeTransport(
        [
            response(accepted()),
            response(pb.ListEventsResponse(next_cursor=C)),
        ]
    )
    task = prepared(client(transport)).submit()
    failure = DeixicError("application predicate failed", kind="transport")
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise failure
        return False

    with pytest.raises(DeixicError) as error:
        task.wait(cancelled=cancelled, max_reconnect_attempts=0)
    assert error.value is failure
    assert len(transport.requests) == 2
    assert task.checkpoint()["submission"] == "accepted"


def test_deadline_caps_read_timeout_and_returns_recoverable_checkpoint():
    class SlowRead(FakeTransport):
        def send(self, *args, **kwargs):
            if len(self.requests) == 1:
                self.requests.append(kwargs)
                time.sleep(kwargs["timeout"])
                raise TimeoutError("read deadline")
            return super().send(*args, **kwargs)

    transport = SlowRead([response(accepted())])
    task = prepared(client(transport)).submit()
    result = task.wait(timeout=0.02)
    assert result.status == "unfinished" and result.reason == "deadline"
    assert 0 < transport.requests[-1]["timeout"] <= 0.02
    assert task.checkpoint()["submission"] == "accepted"


@pytest.mark.parametrize(
    "page",
    [
        pb.ListEventsResponse(next_cursor=C + 2),
        pb.ListEventsResponse(next_cursor=C, has_more=True),
        pb.ListEventsResponse(reset_required=True),
        pb.ListEventsResponse(
            next_cursor=C + 1,
            events=[
                pb.TaskEvent(cursor=C + 1, turn_id="target", id="a"),
                pb.TaskEvent(cursor=C + 1, turn_id="target", id="b"),
            ],
        ),
    ],
)
def test_invalid_event_watermarks_do_not_advance_checkpoint(page):
    transport = FakeTransport([response(accepted()), response(page)])
    task = prepared(client(transport)).submit()
    with pytest.raises(DeixicError) as error:
        task.wait()
    assert error.value.kind == "protocol"
    assert task.checkpoint()["cursor"] == str(C)


@pytest.mark.parametrize(
    "name,value",
    [
        ("cursor", 9_007_199_254_740_994),
        ("sequence", "02"),
        ("cursor", "-1"),
        ("cursor", "9223372036854775808"),
        ("sequence", "x"),
        ("cursor", "9" * 5000),
        ("projectResourceId", False),
    ],
)
def test_invalid_checkpoint_coordinates_are_rejected_before_network(name, value):
    transport = FakeTransport([])
    saved = prepared(client(transport)).checkpoint()
    saved[name] = value
    with pytest.raises(DeixicError):
        client(transport).tasks.resume(saved)
    assert not transport.requests


def test_setup_returns_owner_requirements_without_claiming_write_access():
    transport = FakeTransport(
        [
            response(
                pb.GetThreadResponse(
                    thread=pb.Thread(id="company"),
                    setup=pb.SetupReadiness(
                        missing_requirements=["Connect CRM"], accessible=True
                    ),
                )
            )
        ]
    )
    report = client(transport).tasks.check_setup(channel_id="company")
    assert report.status == "needs_attention"
    assert report.write_access == "not_checked"
    assert report.capabilities[0].missing_requirements[0] == "Connect CRM"
    assert len(transport.requests) == 1


def test_setup_failure_has_action_and_support_reference():
    transport = FakeTransport(
        [
            FakeResponse(
                403, b'{"code":"permission_denied"}', {"x-request-id": "support-1"}
            )
        ]
    )
    report = client(transport).tasks.check_setup(channel_id="company")
    assert report.status == "error"
    assert "read grants" in report.next_action
    assert report.error.request_id == "support-1"


def test_setup_reports_owner_model_availability_without_inventing_a_route():
    transport = FakeTransport(
        [
            response(
                pb.GetThreadResponse(
                    thread=pb.Thread(id="company"),
                    setup=pb.SetupReadiness(
                        default_model=pb.AvailableModel(
                            provider="fixture", model="chosen", ready=False
                        ),
                        accessible=True,
                    ),
                )
            )
        ]
    )
    report = client(transport).tasks.check_setup(channel_id="company")
    assert report.status == "needs_attention"
    assert report.default_model.ready is False
    assert report.model_selection is None


@pytest.mark.parametrize("explicit_selection", [False, True])
def test_setup_rejects_an_absent_execution_route(explicit_selection):
    response_body = pb.GetThreadResponse(
        thread=pb.Thread(id="company"), setup=pb.SetupReadiness(accessible=True)
    )
    if explicit_selection:
        response_body.setup.selection.CopyFrom(
            pb.ModelSelection(provider="fixture", model="removed")
        )
    transport = FakeTransport([response(response_body)])
    report = client(transport).tasks.check_setup(channel_id="company")
    assert report.status == "needs_attention"
    assert report.selected_model is None
    assert "model availability" in report.next_action


def test_wait_continues_past_preliminary_response_until_owner_completion():
    transport = FakeTransport(
        [
            response(accepted()),
            response(pb.ListEventsResponse(next_cursor=C)),
            response(finished(turn=dict(state=pb.TURN_STATE_RESPONDED))),
            response(pb.ListEventsResponse(next_cursor=C)),
            response(finished()),
        ]
    )
    task = prepared(client(transport)).submit()
    assert task.wait(poll_interval=0.001).status == "completed"
    assert len(transport.requests) == 5


def test_visible_final_answer_does_not_require_scanning_unrelated_old_history():
    page = finished(next_page_token="unrelated-old-history")
    transport = FakeTransport([response(accepted()), response(page)])
    assert (
        prepared(client(transport)).submit().result(max_pages=1).body == "Account brief"
    )
    assert len(transport.requests) == 2


def test_selected_ready_model_is_not_blocked_by_unavailable_default():
    transport = FakeTransport(
        [
            response(
                pb.GetThreadResponse(
                    thread=pb.Thread(id="company"),
                    setup=pb.SetupReadiness(
                        default_model=pb.AvailableModel(
                            provider="fixture", model="default", ready=False
                        ),
                        selection=pb.ModelSelection(provider="fixture", model="chosen"),
                        available_models=[
                            pb.AvailableModel(
                                provider="fixture", model="chosen", ready=True
                            )
                        ],
                        accessible=True,
                    ),
                )
            )
        ]
    )
    report = client(transport).tasks.check_setup(channel_id="company")
    assert report.status == "accessible"
    assert report.selected_model.model == "chosen"
    assert report.selected_model.ready


def test_result_parser_failure_propagates_without_changing_completion():
    transport = FakeTransport(
        [response(accepted()), response(finished(message=dict(body="invalid JSON")))]
    )
    result = prepared(client(transport)).submit().result()
    with pytest.raises(ValueError):
        result.parse(json.loads)
    assert result.status == "completed"


def test_invalid_acceptance_stays_unacknowledged_and_requires_explicit_replay():
    transport = FakeTransport([response(pb.SubmitTaskResponse())])
    task = prepared(client(transport))
    with pytest.raises(DeixicError) as error:
        task.submit()
    assert error.value.kind == "protocol"
    assert task.result().status == "unacknowledged"
    assert len(transport.requests) == 1
