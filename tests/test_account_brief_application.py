"""Application behavior over real binary HTTP and independently launched clients."""

from __future__ import annotations

import copy
import json
import subprocess
import threading

import pytest
from console.v1 import console_pb2 as pb

from test_account_brief import command, configuration, invocation, owner
from deixic.examples.account_brief_result import parse_account_brief

BRIEF = dict(
    schemaVersion="deixic.account-brief.v1",
    accountName="Example account",
    summary=dict(text="An active account", sourceIds=["account"]),
    opportunities=[dict(name="Renewal", stage="Open", sourceIds=["opportunity"])],
    risks=[dict(description="Renewal has no owner", sourceIds=["opportunity"])],
    sources=[
        dict(id="account", system="crm", resourceId="account-1"),
        dict(id="opportunity", system="crm", resourceId="opportunity-1"),
    ],
    missingData=["Contract end date"],
)


def start(path, environment, language="python", structured=True):
    return command(
        [
            "start",
            str(path),
            "--account",
            "Example account",
            "--trigger",
            "crm-event-001",
            *(["--structured"] if structured else []),
        ],
        environment,
        language,
    )


@pytest.mark.parametrize("language", ["python", "typescript"])
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "read_disconnect",
        "failed_action",
        "unavailable_action",
        "invalid_answer",
        "wrong_account",
        "missing_data",
    ],
)
def test_structured_application_result_and_action_outcomes(tmp_path, language, fault):
    brief = copy.deepcopy(BRIEF)
    if fault == "wrong_account":
        brief["accountName"] = "Other account"
    if fault == "missing_data":
        brief.update(
            summary=None,
            opportunities=[],
            risks=[],
            sources=[],
            missingData=["CRM connection unavailable"],
        )
    state = dict(
        body="not JSON" if fault == "invalid_answer" else json.dumps(brief),
        disconnect_read=fault == "read_disconnect",
        receipt_state=pb.RECEIPT_LIFECYCLE_STATE_FAILED
        if fault == "failed_action"
        else pb.RECEIPT_LIFECYCLE_STATE_UNAVAILABLE
        if fault == "unavailable_action"
        else pb.RECEIPT_LIFECYCLE_STATE_VERIFIED,
    )
    with owner(state=state) as (url, operations, submissions, errors):
        env = configuration(url)
        path = tmp_path / "brief.json"
        assert start(path, env, language)[0] == 0
        code, result = command(["resume", str(path), "--progress"], env, language)
        assert code == (
            2
            if fault
            in (
                "failed_action",
                "unavailable_action",
                "invalid_answer",
                "wrong_account",
            )
            else 0
        ), result
        assert result["status"] == (
            "invalid_result"
            if fault in ("invalid_answer", "wrong_account")
            else "completed"
        )
        if result["status"] == "completed":
            assert result["brief"] == brief
            actions = result["actions"]
            assert (
                actions[0].get("owner_service", actions[0].get("ownerService")) == "crm"
            )
            assert (
                actions[0].get("object_id", actions[0].get("objectId")) == "account-1"
            )
            assert (
                actions[0].get("lifecycle_state", actions[0].get("lifecycleState"))
                == state["receipt_state"]
            )
            assert result.get("action_status", result.get("actionStatus")) == (
                "requires_attention"
                if fault in ("failed_action", "unavailable_action")
                else "owner_reported_success"
            )
        assert len(operations) == len(submissions) == 1
        assert not errors


@pytest.mark.parametrize("language", ["python", "typescript"])
@pytest.mark.parametrize(
    "decision", ["approve", "deny", "unavailable_history", "wrong_request", "forbidden"]
)
def test_operator_decision_requires_current_owner_request_and_authorization(
    tmp_path, language, decision
):
    state = dict(
        waiting=True,
        body=json.dumps(BRIEF),
        request_visible=decision != "unavailable_history",
        forbidden_decision=decision == "forbidden",
    )
    with owner(state=state) as (url, operations, submissions, errors):
        env = configuration(url)
        path = tmp_path / "brief.json"
        assert start(path, env, language)[0] == 0
        code, result = command(["resume", str(path)], env, language)
        assert code == 2 and result["status"] == "waiting"
        assert not state["decisions"]  # Observation cannot authorize a mutation.
        if decision == "unavailable_history":
            assert result["reason"] == "request_not_visible"
        code, result = command(
            [
                "deny" if decision == "deny" else "approve",
                str(path),
                "--request",
                "other-request" if decision == "wrong_request" else "approval-1",
                "--decision-key",
                "decision-001",
            ],
            env,
            language,
        )
        if decision in ("unavailable_history", "wrong_request", "forbidden"):
            assert code == (1 if decision == "forbidden" else 2)
            assert not state["decisions"]
        else:
            assert code == 0 and result["status"] == "decision_submitted"
            code, result = command(["resume", str(path)], env, language)
            assert code == (0 if decision == "approve" else 2)
            assert result["status"] == (
                "completed" if decision == "approve" else "failed"
            )
            assert len(state["decisions"]) == 1
        assert len(operations) == len(submissions) == 1
        assert not errors


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_killed_background_worker_resumes_same_accepted_task(tmp_path, language):
    state = dict(
        body=json.dumps(BRIEF),
        block_read=True,
        read_started=threading.Event(),
        release_read=threading.Event(),
    )
    with owner(state=state) as (url, operations, submissions, errors):
        env = configuration(url)
        path = tmp_path / "brief.json"
        assert start(path, env, language)[0] == 0
        accepted = json.loads(path.read_text())
        worker = subprocess.Popen(
            [*invocation(language), "resume", str(path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert state["read_started"].wait(8)
            worker.kill()
            worker.communicate(timeout=5)
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=5)
            state["block_read"] = False
            state["release_read"].set()
        code, result = command(["resume", str(path)], env, language)
        assert code == 0 and result["status"] == "completed"
        assert (
            result.get("turn_id", result.get("turnId"))
            == accepted["turnId"]
            == "target"
        )
        assert result["brief"] == BRIEF
        assert len(operations) == len(submissions) == 1
        assert not errors


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_storage_failure_before_and_after_acceptance_never_retries_mutation(
    tmp_path, language
):
    directory = tmp_path / "private"
    directory.mkdir()
    moved = tmp_path / "preserved"
    path = directory / "brief.json"
    state = dict(body=json.dumps(BRIEF))

    def fail_storage():
        directory.rename(moved)
        directory.write_text("storage temporarily unavailable")
        state["on_accept"] = None

    state["on_accept"] = fail_storage
    with owner(state=state) as (url, operations, submissions, errors):
        env = configuration(url)
        assert start(tmp_path / "missing" / "brief.json", env, language)[0] == 1
        assert not submissions
        assert start(path, env, language)[0] == 1
        assert len(submissions) == 1
        directory.unlink()
        moved.rename(directory)
        checkpoint = json.loads(path.read_text())
        assert checkpoint["submission"] == "unacknowledged"
        code, result = command(["resume", str(path)], env, language)
        assert code == 2 and result["status"] == "unacknowledged"
        assert len(submissions) == 1
        assert command(["replay", str(path)], env, language)[0] == 0
        code, result = command(["resume", str(path)], env, language)
        assert code == 0 and result["brief"] == BRIEF
        assert len(operations) == 1 and submissions[0] == submissions[1]
        assert not errors


@pytest.mark.parametrize(
    "submitter,observer", [("python", "typescript"), ("typescript", "python")]
)
def test_structured_result_survives_cross_language_restart(
    tmp_path, submitter, observer
):
    with owner(state=dict(body=json.dumps(BRIEF))) as (
        url,
        operations,
        submissions,
        errors,
    ):
        env = configuration(url)
        path = tmp_path / "brief.json"
        assert start(path, env, submitter)[0] == 0
        code, result = command(["resume", str(path)], env, observer)
        assert code == 0 and result["brief"] == BRIEF
        assert len(operations) == len(submissions) == 1 and not errors


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "unlinked",
        "duplicate_source",
        "empty_text",
        "wrong_type",
        "missing_explanation",
        "version",
    ],
)
def test_account_brief_parser_rejects_unusable_data(mutation):
    brief = copy.deepcopy(BRIEF)
    if mutation == "extra":
        brief["unexpected"] = True
    if mutation == "unlinked":
        brief["summary"]["sourceIds"] = ["invented"]
    if mutation == "duplicate_source":
        brief["sources"].append(brief["sources"][0])
    if mutation == "empty_text":
        brief["summary"]["text"] = " "
    if mutation == "wrong_type":
        brief["opportunities"] = "not a list"
    if mutation == "missing_explanation":
        brief.update(summary=None, missingData=[])
    if mutation == "version":
        brief["schemaVersion"] = "future"
    with pytest.raises(ValueError):
        parse_account_brief(json.dumps(brief))


def test_duplicate_json_fields_are_rejected():
    body = json.dumps(BRIEF).replace(
        '"accountName":', '"accountName":"Other account","accountName":'
    )
    with pytest.raises(ValueError):
        parse_account_brief(body)


@pytest.mark.parametrize("create", [True, False])
def test_partial_checkpoint_serialization_never_publishes_a_partial_file(
    tmp_path, monkeypatch, create
):
    from deixic.examples import task_result

    path = tmp_path / "checkpoint.json"
    if not create:
        path.write_text('{"preserved": true}')

    def disk_full(value, output):
        output.write('{"partial":')
        raise OSError("fixture storage failure")

    monkeypatch.setattr(task_result.json, "dump", disk_full)
    with pytest.raises(OSError):
        task_result.save(path, dict(schema="fixture"), create=create)
    assert not path.exists() if create else path.read_text() == '{"preserved": true}'
    assert sorted(item.name for item in tmp_path.iterdir()) == (
        [] if create else [path.name]
    )
