"""Submit once, save recovery coordinates, and fetch the accepted turn's result."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from deixic import protocol as pb
from deixic import Deixic, DeixicError


def save(path: Path, state: dict[str, Any], *, create: bool = False) -> None:
    # The checkpoint contains the request body, but never a credential.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(state, output)
            output.flush()
            os.fsync(output.fileno())
        if create:
            os.link(
                temporary, path
            )  # Atomic and exclusive: partial initial files are never visible.
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(
    path: Path,
    *,
    organization_id: str,
    workspace_id: str,
    base_url: str,
    channel_id: str,
    body: str,
) -> dict[str, Any]:
    state = dict(
        organization_id=organization_id,
        workspace_id=workspace_id,
        base_url=base_url.rstrip("/"),
        channel_id=channel_id,
        body=body,
        idempotency_key=str(uuid.uuid4()),
        turn_id="",
        cursor=0,
        turn_state=pb.TURN_STATE_UNSPECIFIED,
    )
    save(path, state, create=True)
    return state


def load(
    path: Path, *, organization_id: str, workspace_id: str, base_url: str
) -> dict[str, Any]:
    state = json.loads(path.read_text())
    if (state["organization_id"], state["workspace_id"], state["base_url"]) != (
        organization_id,
        workspace_id,
        base_url.rstrip("/"),
    ):
        raise ValueError("Checkpoint belongs to a different tenant or Platform URL")
    return state


def submit(client: Deixic, path: Path, state: dict[str, Any]) -> None:
    if state["turn_id"]:
        raise ValueError("This checkpoint already has an accepted turn; use resume")
    accepted = client.messages.send(
        channel_id=state["channel_id"],
        body=state["body"],
        idempotency_key=state["idempotency_key"],
    )
    if not accepted.accepted_turn.turn_id:
        raise ValueError("Submission omitted the accepted turn identity")
    state.update(
        turn_id=accepted.accepted_turn.turn_id,
        cursor=accepted.replay_cursor,
        turn_state=accepted.accepted_turn.state,
    )
    save(path, state)


def result(client: Deixic, state: dict[str, Any]) -> dict[str, Any] | None:
    page_token = ""
    turn = None
    messages = {}
    seen_tokens = set()
    while True:
        thread = client.threads.get(
            channel_id=state["channel_id"], limit=200, page_token=page_token
        )
        turn = next(
            (item for item in thread.turns if item.turn_id == state["turn_id"]), turn
        )
        messages.update({message.id: message for message in thread.messages})
        if not thread.next_page_token:
            break
        if thread.next_page_token in seen_tokens:
            raise ValueError("Thread pagination repeated a page token")
        page_token = thread.next_page_token
        seen_tokens.add(page_token)
    if turn is None:
        return None
    state["turn_state"] = turn.state
    if turn.state in (pb.TURN_STATE_FAILED, pb.TURN_STATE_INTERRUPTED):
        return dict(
            status="failed" if turn.state == pb.TURN_STATE_FAILED else "interrupted",
            turn_id=turn.turn_id,
            error_code=turn.terminal_error.code,
        )
    if turn.state != pb.TURN_STATE_COMPLETED:
        return None
    message = messages.get(turn.assistant_message_id)
    if message is None or message.role != pb.MESSAGE_ROLE_ASSISTANT:
        raise ValueError("Completed turn omitted its linked final assistant message")
    receipts = []
    for receipt_id in message.receipt_ids:
        receipt = client.receipts.get(
            channel_id=state["channel_id"], receipt_id=receipt_id
        ).receipt
        if receipt.id != receipt_id:
            raise ValueError("Receipt lookup returned a different identity")
        receipts.append(receipt.id)
    return dict(
        status="completed",
        turn_id=turn.turn_id,
        message_id=message.id,
        body=message.body,
        receipt_ids=receipts,
    )


def apply_page(path: Path, state: dict[str, Any], page: Any) -> None:
    if page.reset_required:
        # Replace the old projection; retained events cannot reconstruct it.
        turn = next(
            (item for item in page.snapshot_turns if item.turn_id == state["turn_id"]),
            None,
        )
        state["turn_state"] = turn.state if turn else pb.TURN_STATE_UNSPECIFIED
    for event in page.events:
        if event.turn_id != state["turn_id"]:
            continue
        terminal = {
            pb.EVENT_KIND_TURN_COMPLETED: pb.TURN_STATE_COMPLETED,
            pb.EVENT_KIND_TURN_FAILED: pb.TURN_STATE_FAILED,
            pb.EVENT_KIND_TURN_INTERRUPTED: pb.TURN_STATE_INTERRUPTED,
        }.get(event.kind)
        if terminal is not None:
            state["turn_state"] = terminal
    state["cursor"] = page.next_cursor
    save(path, state)


def resume(client: Deixic, path: Path, state: dict[str, Any]) -> dict[str, Any]:
    if not state["turn_id"]:
        return dict(
            status="unacknowledged", action="replay the saved request explicitly"
        )
    outcome = result(client, state)
    save(path, state)
    if outcome is not None:
        return outcome
    while True:
        previous_cursor = state["cursor"]
        page = client.events.list(
            channel_id=state["channel_id"], after_cursor=previous_cursor
        )
        apply_page(path, state, page)
        outcome = result(client, state)
        save(path, state)
        if outcome is not None:
            return outcome
        if not page.has_more:
            break
        if state["cursor"] == previous_cursor:
            raise ValueError("Event pagination did not advance its cursor")
    stream = client.events.watch(
        channel_id=state["channel_id"], after_cursor=state["cursor"]
    )
    try:
        for page in stream:
            apply_page(path, state, page)
            outcome = result(client, state)
            save(path, state)
            if outcome is not None:
                return outcome
    finally:
        stream.close()
    return dict(
        status="unfinished",
        reason="watch_eof",
        turn_id=state["turn_id"],
        cursor=state["cursor"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "replay", "resume"))
    parser.add_argument("state", type=Path)
    parser.add_argument("--channel", default="company")
    parser.add_argument("--body")
    args = parser.parse_args()
    organization = os.environ["DEIXIC_ORGANIZATION_ID"]
    workspace = os.environ["DEIXIC_WORKSPACE_ID"]
    base_url = os.environ.get("DEIXIC_BASE_URL", "https://app.deixic.com")
    client = Deixic(
        api_key=os.environ["DEIXIC_API_KEY"],
        organization_id=organization,
        workspace_id=workspace,
        base_url=base_url,
    )
    if args.action == "start":
        if not args.body:
            parser.error("start requires --body")
        state = prepare(
            args.state,
            organization_id=organization,
            workspace_id=workspace,
            base_url=base_url,
            channel_id=args.channel,
            body=args.body,
        )
    else:
        if args.body is not None:
            parser.error("replay and resume use the saved request; omit --body")
        state = load(
            args.state,
            organization_id=organization,
            workspace_id=workspace,
            base_url=base_url,
        )
    try:
        if args.action in ("start", "replay"):
            submit(client, args.state, state)
        outcome = resume(client, args.state, state)
    except DeixicError as error:
        # Request bodies and bearer credentials do not belong in diagnostics.
        print(json.dumps(dict(status="error", kind=error.kind, code=error.code)))
        return 1
    print(json.dumps(outcome))
    return 0 if outcome["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
