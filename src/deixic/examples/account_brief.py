"""Prepare an account brief from CRM data available in your Deixic workspace."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from console.v1 import console_pb2 as pb
from deixic import Deixic, DeixicError

from .task_result import save
from .account_brief_result import FORMAT_INSTRUCTION, parse_account_brief


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("check", "start", "resume", "replay", "approve", "deny")
    )
    parser.add_argument("state", type=Path, nargs="?")
    parser.add_argument("--channel")
    parser.add_argument("--account")
    parser.add_argument(
        "--structured",
        action="store_true",
        help="Request a validated JSON account brief",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Write matching-turn event IDs to stderr",
    )
    parser.add_argument(
        "--request", help="Approval request ID selected by the authorized operator"
    )
    parser.add_argument(
        "--decision-key", help="Stable idempotency key for this explicit decision"
    )
    parser.add_argument(
        "--trigger", help="Stable ID for this business request; reused on recovery"
    )
    parser.add_argument(
        "--timeout", type=float, default=60, help="Observation budget in seconds"
    )
    args = parser.parse_args()
    if args.action != "check" and args.state is None:
        parser.error("This action requires a checkpoint path")
    if args.action == "start" and (not args.account or not args.trigger):
        parser.error("start requires --account and --trigger")
    if args.action in ("resume", "replay", "approve", "deny") and any(
        (args.account, args.trigger, args.channel, args.structured)
    ):
        parser.error(
            "This action uses the saved request; omit --account, --trigger, --channel and --structured"
        )
    if args.action in ("approve", "deny"):
        if not args.request or not args.decision_key:
            parser.error("approve and deny require --request and --decision-key")
    elif args.request or args.decision_key:
        parser.error("--request and --decision-key require approve or deny")
    if args.structured and args.action != "start":
        parser.error("--structured requires start; resume uses the saved format")
    try:
        required = ("DEIXIC_API_KEY", "DEIXIC_ORGANIZATION_ID", "DEIXIC_WORKSPACE_ID")
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))
        client = Deixic(
            api_key=os.environ["DEIXIC_API_KEY"],
            organization_id=os.environ["DEIXIC_ORGANIZATION_ID"],
            workspace_id=os.environ["DEIXIC_WORKSPACE_ID"],
            base_url=os.environ.get("DEIXIC_BASE_URL", "https://app.deixic.com"),
        )
        if args.action == "check":
            check = client.tasks.check_setup(channel_id=args.channel or "company")
            print(
                json.dumps(
                    dict(
                        status=check.status,
                        channel_id=check.channel_id,
                        write_access=check.write_access,
                        next_action=check.next_action,
                        requirements=[
                            dict(
                                service=item.service,
                                missing_requirements=list(item.missing_requirements),
                                reason_codes=[
                                    reason.reason_code
                                    for reason in item.missing_requirement_states
                                ],
                            )
                            for item in check.capabilities
                        ],
                        model_ready=check.selected_model.ready
                        if check.selected_model
                        else None,
                        error_kind=check.error.kind if check.error else None,
                        request_id=check.error.request_id if check.error else None,
                    )
                )
            )
            return 0 if check.status == "accessible" else 2

        if args.action == "start":
            # A new file is exclusive; subsequent writes atomically replace it.
            creating = True

            def persist(state):
                nonlocal creating
                save(args.state, state, create=creating)
                creating = False

            task = client.tasks.prepare(
                channel_id=args.channel or "company",
                body="Prepare an account brief for " + json.dumps(args.account) + ". "
                "Use CRM data available in this workspace. Include the account summary, "
                "open opportunities, risks and source references. State which data is missing. "
                "Do not update records or send messages."
                + (FORMAT_INSTRUCTION if args.structured else ""),
                idempotency_key=args.trigger,
                on_checkpoint=persist,
            )
            task.submit()
        else:
            task = client.tasks.resume(
                json.loads(args.state.read_text()),
                on_checkpoint=lambda state: save(args.state, state),
            )
            if args.action == "replay":
                task.replay()
            elif args.action in ("approve", "deny"):
                outcome = task.result()
                event = outcome.event
                if (
                    outcome.status != "waiting"
                    or event is None
                    or event.request_type != pb.OPERATING_THREAD_REQUEST_TYPE_APPROVAL
                    or event.request_id != args.request
                ):
                    print(
                        json.dumps(
                            dict(
                                status="needs_attention",
                                reason="approval_request_not_current",
                            )
                        )
                    )
                    return 2
                client.controls.respond(
                    channel_id=task.checkpoint()["channelId"],
                    turn_id=outcome.turn_id,
                    response=pb.OperatingThreadResponse(
                        request_id=event.request_id,
                        call_id=event.request_call_id,
                        request_type=event.request_type,
                        action=pb.OPERATING_THREAD_RESPONSE_ACTION_APPROVE
                        if args.action == "approve"
                        else pb.OPERATING_THREAD_RESPONSE_ACTION_DENY,
                    ),
                    idempotency_key=args.decision_key,
                )
                print(
                    json.dumps(
                        dict(
                            status="decision_submitted",
                            turn_id=outcome.turn_id,
                            next_action="resume the checkpoint to observe the owner outcome",
                        )
                    )
                )
                return 0
            else:

                def progress(event):
                    if args.progress:
                        print(
                            json.dumps(
                                dict(
                                    event_id=event.event_id,
                                    turn_id=event.turn_id,
                                    kind=event.kind,
                                )
                            ),
                            file=sys.stderr,
                            flush=True,
                        )

                outcome = task.wait(timeout=args.timeout, on_event=progress)
                structured = FORMAT_INSTRUCTION in task.checkpoint()["body"]
                brief = None
                if outcome.status == "completed" and structured:
                    try:
                        brief = outcome.parse(parse_account_brief)
                        expected, _ = json.JSONDecoder().raw_decode(
                            task.checkpoint()["body"][
                                len("Prepare an account brief for ") :
                            ]
                        )
                        if brief["accountName"] != expected:
                            raise ValueError("Account mismatch")
                    except (ValueError, TypeError):
                        print(
                            json.dumps(
                                dict(
                                    status="invalid_result",
                                    remote_status="completed",
                                    turn_id=outcome.turn_id,
                                    next_action="inspect the answer; do not resubmit this request",
                                )
                            )
                        )
                        return 2
                actions = [
                    dict(
                        id=item.id,
                        owner_service=item.owner_service,
                        object_id=item.object_id,
                        kind=item.kind,
                        lifecycle_state=item.lifecycle_state,
                        evidence_refs=[
                            dict(resource_type=ref.resource_type, id=ref.id)
                            for ref in item.evidence_refs
                        ],
                    )
                    for item in outcome.receipts
                ]
                attention = any(
                    item.lifecycle_state
                    not in (
                        pb.RECEIPT_LIFECYCLE_STATE_SUCCEEDED,
                        pb.RECEIPT_LIFECYCLE_STATE_VERIFIED,
                    )
                    for item in outcome.receipts
                )
                print(
                    json.dumps(
                        dict(
                            status=outcome.status,
                            turn_id=outcome.turn_id,
                            reason=outcome.reason,
                            body=outcome.body if not structured else None,
                            brief=brief,
                            receipt_ids=[item.id for item in outcome.receipts],
                            actions=actions,
                            action_status="not_reported"
                            if not actions
                            else "requires_attention"
                            if attention
                            else "owner_reported_success",
                            request_id=outcome.event.request_id
                            if outcome.event
                            else None,
                            request_type=outcome.event.request_type
                            if outcome.event
                            else None,
                            error_code=outcome.turn.error_code
                            if outcome.turn
                            else None,
                        )
                    )
                )
                return 0 if outcome.status == "completed" and not attention else 2
        print(
            json.dumps(
                dict(
                    status="accepted",
                    turn_id=task.checkpoint()["turnId"],
                    next_action="resume this checkpoint to retrieve the account brief",
                )
            )
        )
        return 0
    except DeixicError as error:
        print(
            json.dumps(
                dict(
                    status="error",
                    kind=error.kind,
                    code=error.code,
                    request_id=error.request_id,
                )
            )
        )
        return 1
    except (ValueError, OSError) as error:
        # Only our missing-configuration diagnostic is safe to print verbatim.
        message = (
            str(error)
            if str(error).startswith("Missing configuration:")
            else "Check configuration and checkpoint storage"
        )
        print(
            json.dumps(dict(status="error", kind="configuration", next_action=message))
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
