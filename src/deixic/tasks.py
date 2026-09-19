"""Recoverable task handles over Platform-owned threads, events, and receipts."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar, TypedDict, cast

from console.v1 import console_pb2 as pb
from google.protobuf.message import Message

from .errors import DeixicError, validation_error

if TYPE_CHECKING:
    from .client import Deixic

T = TypeVar("T")
MessageT = TypeVar("MessageT", bound=Message)
MAX_CURSOR = 2**63 - 1


class TaskCheckpoint(TypedDict):
    schema: Literal["deixic.task.v1"]
    organizationId: str
    workspaceId: str
    baseUrl: str
    channelId: str
    body: str
    idempotencyKey: str
    projectResourceId: str
    submission: Literal["prepared", "unacknowledged", "accepted"]
    turnId: str
    sequence: str
    cursor: str


@dataclass(frozen=True)
class TaskResult:
    status: Literal[
        "prepared",
        "unacknowledged",
        "unfinished",
        "responded",
        "waiting",
        "completed",
        "failed",
        "interrupted",
    ]
    turn_id: str = ""
    reason: str = ""
    body: str | None = None
    message: pb.OperatingMessage | None = None
    receipts: tuple[pb.OperatingReceipt, ...] = ()
    turn: pb.OperatingThreadTurn | None = None
    event: pb.OperatingThreadEvent | None = None
    error: DeixicError | None = None

    def parse(self, parser: Callable[[str], T]) -> T:
        """Validate/convert a completed answer with the application's parser."""
        if self.status != "completed" or self.body is None:
            raise validation_error("Only a completed task has a final answer to parse")
        return parser(self.body)


@dataclass(frozen=True)
class SetupCheck:
    status: Literal["accessible", "needs_attention", "error"]
    channel_id: str
    capabilities: tuple[pb.OperatingCapabilityState, ...] = ()
    next_action: str = ""
    error: DeixicError | None = None
    model_selection: pb.OperatingModelSelection | None = None
    default_model: pb.InferenceProviderTarget | None = None
    selected_model: pb.InferenceProviderTarget | None = None
    # Read access does not prove mutation grants or connector execution.
    write_access: Literal["not_checked"] = "not_checked"


class TasksClient:
    def __init__(self, client: Deixic) -> None:
        self._client = client

    def prepare(
        self,
        *,
        channel_id: str,
        body: str,
        idempotency_key: str,
        project_resource_id: str = "",
        on_checkpoint: Callable[[TaskCheckpoint], None] | None = None,
    ) -> Task:
        state: TaskCheckpoint = dict(
            schema="deixic.task.v1",
            organizationId=self._client.organization_id,
            workspaceId=self._client.workspace_id,
            baseUrl=self._client.base_url,
            channelId=_identity(channel_id, "channel_id"),
            body=_body(body),
            idempotencyKey=_identity(idempotency_key, "idempotency_key"),
            projectResourceId=_identity(project_resource_id, "project_resource_id")
            if project_resource_id != ""
            else "",
            submission="prepared",
            turnId="",
            sequence="0",
            cursor="0",
        )
        task = Task(self._client, state, on_checkpoint)
        task._save()
        return task

    def start(
        self,
        *,
        channel_id: str,
        body: str,
        idempotency_key: str,
        project_resource_id: str = "",
        on_checkpoint: Callable[[TaskCheckpoint], None] | None = None,
    ) -> Task:
        """Prepare and submit once. Use on_checkpoint for restart recovery."""
        return self.prepare(
            channel_id=channel_id,
            body=body,
            idempotency_key=idempotency_key,
            project_resource_id=project_resource_id,
            on_checkpoint=on_checkpoint,
        ).submit()

    def resume(
        self,
        checkpoint: Mapping[str, Any],
        *,
        on_checkpoint: Callable[[TaskCheckpoint], None] | None = None,
    ) -> Task:
        # A checkpoint supplies coordinates, never authorization or completion.
        keys = TaskCheckpoint.__required_keys__
        if not isinstance(checkpoint, Mapping) or set(checkpoint) != keys:
            raise validation_error("Invalid task checkpoint fields")
        state = dict(checkpoint)
        if state["schema"] != "deixic.task.v1":
            raise validation_error("Unsupported task checkpoint schema")
        if (state["organizationId"], state["workspaceId"], state["baseUrl"]) != (
            self._client.organization_id,
            self._client.workspace_id,
            self._client.base_url,
        ):
            raise validation_error(
                "Checkpoint belongs to a different tenant or Platform URL"
            )
        for name in ("channelId", "idempotencyKey"):
            _identity(state[name], name)
        _body(state["body"])
        if state["projectResourceId"]:
            _identity(state["projectResourceId"], "projectResourceId")
        elif state["projectResourceId"] != "":
            raise validation_error("Invalid projectResourceId")
        sequence, cursor = _decimal(state["sequence"]), _decimal(state["cursor"])
        if state["submission"] == "accepted":
            _identity(state["turnId"], "turnId")
            if sequence == 0:
                raise validation_error(
                    "Accepted checkpoint requires a positive sequence"
                )
        elif state["submission"] not in ("prepared", "unacknowledged") or (
            state["turnId"] != "" or sequence != 0 or cursor != 0
        ):
            raise validation_error("Invalid submission coordinates")
        return Task(self._client, cast(TaskCheckpoint, state), on_checkpoint)

    def check_setup(self, *, channel_id: str) -> SetupCheck:
        channel_id = _identity(channel_id, "channel_id")
        try:
            thread = self._client.threads.get(channel_id=channel_id, limit=1)
            if thread.channel.id != channel_id:
                raise _protocol("Setup lookup omitted or changed the channel identity")
            capabilities = tuple(_copy(item) for item in thread.capabilities)
            if thread.channel.HasField("capability_state"):
                capabilities += (_copy(thread.channel.capability_state),)
            missing = any(
                item.missing_requirements or item.missing_requirement_states
                for item in capabilities
            )
            model = (
                _copy(thread.default_model)
                if thread.HasField("default_model")
                else None
            )
            selection = (
                _copy(thread.model_selection)
                if thread.HasField("model_selection")
                else None
            )
            selected = model
            if selection is not None and (selection.provider or selection.model):
                selected = next(
                    (
                        _copy(item)
                        for item in thread.available_models
                        if (item.provider, item.model)
                        == (selection.provider, selection.model)
                    ),
                    None,
                )
            unavailable_model = selected is not None and not selected.ready
            return SetupCheck(
                "needs_attention" if missing or unavailable_model else "accessible",
                channel_id,
                capabilities,
                "Resolve the reported workspace prerequisites"
                if missing
                else "Check the reported model availability in workspace settings"
                if unavailable_model
                else "Submit a task to check execution and write access",
                model_selection=selection,
                default_model=model,
                selected_model=selected,
            )
        except DeixicError as error:
            action = {
                "authentication": "Replace or refresh the expired or invalid credential",
                "authorization": "Check the credential's organization and workspace read grants",
                "not_found": "Check the channel ID in this workspace",
            }.get(
                error.kind,
                "Check API connectivity and share the request ID with support",
            )
            return SetupCheck("error", channel_id, next_action=action, error=error)


class Task:
    def __init__(
        self,
        client: Deixic,
        state: TaskCheckpoint,
        on_checkpoint: Callable[[TaskCheckpoint], None] | None,
    ) -> None:
        if on_checkpoint is not None and not callable(on_checkpoint):
            raise validation_error("on_checkpoint must be callable")
        self._client = client
        self._state = cast(TaskCheckpoint, dict(state))
        self._on_checkpoint = on_checkpoint
        self._event: pb.OperatingThreadEvent | None = None
        self._submitting = False
        self._observing = False

    def checkpoint(self) -> TaskCheckpoint:
        """JSON-safe coordinates and original request; excludes credentials."""
        return cast(TaskCheckpoint, dict(self._state))

    def _save(self) -> None:
        if self._on_checkpoint:
            self._on_checkpoint(self.checkpoint())

    def submit(self) -> Task:
        return self._submit("prepared")

    def replay(self) -> Task:
        """Explicitly replay only a request whose acceptance was not received."""
        return self._submit("unacknowledged")

    def _submit(self, required: str) -> Task:
        if self._submitting or self._state["submission"] != required:
            raise validation_error(
                "Submission already attempted; resume or explicitly replay an unacknowledged request"
            )
        self._submitting = True
        try:
            self._state["submission"] = "unacknowledged"
            self._save()  # A storage failure prevents the network mutation.
            accepted = self._client.messages.send(
                channel_id=self._state["channelId"],
                body=self._state["body"],
                idempotency_key=self._state["idempotencyKey"],
                project_resource_id=self._state["projectResourceId"] or None,
            )
            turn = accepted.accepted_turn
            if (
                not turn.turn_id.strip()
                or turn.turn_id != turn.turn_id.strip()
                or turn.sequence <= 0
                or accepted.replay_cursor < 0
            ):
                raise _protocol("Submission omitted valid accepted-turn coordinates")
            self._state.update(
                submission="accepted",
                turnId=turn.turn_id,
                sequence=str(turn.sequence),
                cursor=str(accepted.replay_cursor),
            )
            self._save()
            return self
        finally:
            self._submitting = False

    def result(self, *, max_pages: int = 10) -> TaskResult:
        return self._result(_pages(max_pages), None)

    def _result(self, max_pages: int, deadline: float | None) -> TaskResult:
        if self._state["submission"] != "accepted":
            return TaskResult(
                self._state["submission"],
                reason="submit_required"
                if self._state["submission"] == "prepared"
                else "explicit_replay_required",
            )
        turn_id, sequence = self._state["turnId"], int(self._state["sequence"])
        turn = None
        messages = {}
        page_token = ""
        seen = set()
        for _ in range(max_pages):
            page = self._client.threads.get(
                channel_id=self._state["channelId"],
                limit=200,
                page_token=page_token,
                timeout=_remaining(deadline),
            )
            if page.channel.id and page.channel.id != self._state["channelId"]:
                raise _protocol("Thread lookup changed the channel identity")
            candidate = next(
                (item for item in page.turns if item.turn_id == turn_id), None
            )
            if candidate is not None:
                if candidate.sequence != sequence:
                    raise _protocol("Thread lookup changed the accepted turn sequence")
                if turn is None:
                    turn = _copy(candidate)
            for item in page.messages:
                messages.setdefault(item.id, item)
            if turn is not None and (
                turn.state != pb.OPERATING_TURN_STATE_COMPLETED
                or not turn.assistant_message_id
                or turn.assistant_message_id in messages
            ):
                break
            if not page.next_page_token:
                break
            if page.next_page_token in seen:
                raise _protocol("Thread pagination repeated a page token")
            seen.add(page.next_page_token)
            page_token = page.next_page_token
        else:
            return TaskResult("unfinished", turn_id, "result_page_limit")
        if turn is None:
            return TaskResult("unfinished", turn_id, "turn_not_visible")
        status = {
            pb.OPERATING_TURN_STATE_COMPLETED: "completed",
            pb.OPERATING_TURN_STATE_RESPONDED: "responded",
            pb.OPERATING_TURN_STATE_FAILED: "failed",
            pb.OPERATING_TURN_STATE_INTERRUPTED: "interrupted",
            pb.OPERATING_TURN_STATE_WAITING: "waiting",
        }.get(turn.state, "unfinished")
        event = _copy(self._event) if self._event else None
        if status == "waiting":
            event = self._waiting_request(turn, max_pages, deadline)
            return TaskResult(
                "waiting",
                turn_id,
                reason="" if event else "request_not_visible",
                turn=turn,
                event=event,
            )
        if status != "completed":
            return TaskResult(status, turn_id, turn=turn, event=event)
        message = messages.get(turn.assistant_message_id)
        if (
            message is None
            or message.role != "assistant"
            or message.channel_id != self._state["channelId"]
        ):
            raise _protocol("Completed turn omitted its linked final assistant message")
        receipts = []
        for receipt_id in dict.fromkeys(message.receipt_ids):
            receipt = self._client.receipts.get(
                channel_id=self._state["channelId"],
                receipt_id=receipt_id,
                timeout=_remaining(deadline),
            ).receipt
            if receipt.id != receipt_id:
                raise _protocol("Receipt lookup changed the receipt identity")
            receipts.append(_copy(receipt))
        return TaskResult(
            "completed",
            turn_id,
            body=message.body,
            message=_copy(message),
            receipts=tuple(receipts),
            turn=turn,
            event=event,
        )

    def _waiting_request(
        self, turn: pb.OperatingThreadTurn, max_pages: int, deadline: float | None
    ) -> pb.OperatingThreadEvent | None:
        # Re-read request identity from owner history, including after restart.
        # A saved cursor or locally cached event cannot authorize a response.
        cursor = max(0, turn.first_cursor - 1)
        request = None
        for _ in range(max_pages):
            page = self._client.events.list(
                channel_id=self._state["channelId"],
                after_cursor=cursor,
                timeout=_remaining(deadline),
            )
            if page.reset_required:
                return None
            _validate_page(page, cursor)
            for event in sorted(page.events, key=lambda item: item.cursor):
                if (
                    event.turn_id == turn.turn_id
                    and event.cursor > cursor
                    and event.request_id
                ):
                    request = _copy(event)
            cursor = page.next_cursor
            if not page.has_more:
                expected_type = {
                    pb.OPERATING_THREAD_WAITING_REASON_APPROVAL: pb.OPERATING_THREAD_REQUEST_TYPE_APPROVAL,
                    pb.OPERATING_THREAD_WAITING_REASON_USER_INPUT: pb.OPERATING_THREAD_REQUEST_TYPE_USER_INPUT,
                    pb.OPERATING_THREAD_WAITING_REASON_CLIENT_TOOL: pb.OPERATING_THREAD_REQUEST_TYPE_CLIENT_TOOL,
                    pb.OPERATING_THREAD_WAITING_REASON_EXTERNAL_RETRY: pb.OPERATING_THREAD_REQUEST_TYPE_EXTERNAL_RETRY,
                }.get(turn.waiting_reason)
                return (
                    request
                    if request and request.request_type == expected_type
                    else None
                )
        return None

    def _backfill(
        self,
        max_pages: int,
        deadline: float,
        on_event: Callable[[pb.OperatingThreadEvent], None] | None,
    ) -> bool:
        for _ in range(max_pages):
            cursor = int(self._state["cursor"])
            page = self._client.events.list(
                channel_id=self._state["channelId"],
                after_cursor=cursor,
                timeout=_remaining(deadline),
            )
            if page.reset_required:
                if (
                    not page.HasField("thread_execution")
                    or page.thread_execution.replay_cursor < cursor
                ):
                    raise _protocol(
                        "Retention reset omitted a valid owner execution cursor"
                    )
                self._event = None
                self._state["cursor"] = str(page.thread_execution.replay_cursor)
                _application_call(self._save)
                return (
                    True  # Re-fetch current owner state; reset events are not history.
                )
            _validate_page(page, cursor)
            seen = set()
            for event in sorted(page.events, key=lambda item: item.cursor):
                if (
                    event.turn_id != self._state["turnId"]
                    or event.cursor <= cursor
                    or event.cursor in seen
                ):
                    continue
                seen.add(event.cursor)
                if on_event:
                    _application_call(
                        on_event, _copy(event)
                    )  # Failed callbacks leave the page unconsumed.
                self._event = _copy(event)
            self._state["cursor"] = str(page.next_cursor)
            _application_call(self._save)
            if not page.has_more:
                return True
        return False

    def wait(
        self,
        *,
        timeout: float = 60,
        poll_interval: float = 1,
        max_pages: int = 10,
        max_reconnect_attempts: int = 3,
        on_event: Callable[[pb.OperatingThreadEvent], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> TaskResult:
        """Bounded durable polling. Stopping observation never interrupts work."""
        _positive(timeout, "timeout")
        _positive(poll_interval, "poll_interval")
        _pages(max_pages)
        if on_event is not None and not callable(on_event):
            raise validation_error("on_event must be callable")
        if cancelled is not None and not callable(cancelled):
            raise validation_error("cancelled must be callable")
        if (
            not isinstance(max_reconnect_attempts, int)
            or isinstance(max_reconnect_attempts, bool)
            or not 0 <= max_reconnect_attempts <= 100
        ):
            raise validation_error("max_reconnect_attempts must be between 0 and 100")
        if self._observing:
            raise validation_error("This task already has an active observer")
        if self._state["submission"] != "accepted":
            return self.result()
        deadline = time.monotonic() + timeout
        reconnects = 0
        self._observing = True
        try:
            while True:
                if cancelled and cancelled():
                    return TaskResult("unfinished", self._state["turnId"], "cancelled")
                try:
                    _remaining(deadline)
                    caught_up = self._backfill(max_pages, deadline, on_event)
                    if cancelled and _application_call(cancelled):
                        return TaskResult(
                            "unfinished", self._state["turnId"], "cancelled"
                        )
                    outcome = self._result(max_pages, deadline)
                    if cancelled and _application_call(cancelled):
                        return TaskResult(
                            "unfinished", self._state["turnId"], "cancelled"
                        )
                    _remaining(deadline)
                    if outcome.status not in ("unfinished", "responded"):
                        return outcome
                    if not caught_up:
                        return TaskResult(
                            "unfinished", self._state["turnId"], "backfill_limit"
                        )
                    reconnects = 0
                except _DeadlineReached:
                    return TaskResult("unfinished", self._state["turnId"], "deadline")
                except DeixicError as error:
                    if error.kind not in ("transport", "unavailable"):
                        raise
                    if time.monotonic() >= deadline:
                        return TaskResult(
                            "unfinished", self._state["turnId"], "deadline", error=error
                        )
                    if reconnects >= max_reconnect_attempts:
                        return TaskResult(
                            "unfinished",
                            self._state["turnId"],
                            "observation_error",
                            error=error,
                        )
                    reconnects += 1
                delay = min(poll_interval * 2**reconnects, 5) * random.uniform(0.8, 1.2)
                # Short sleeps make local cancellation responsive without a background worker.
                until = min(deadline, time.monotonic() + delay)
                while time.monotonic() < until:
                    if cancelled and cancelled():
                        return TaskResult(
                            "unfinished", self._state["turnId"], "cancelled"
                        )
                    time.sleep(min(0.1, max(0, until - time.monotonic())))
        except _ApplicationCallbackError as error:
            raise error.original from error
        finally:
            self._observing = False


class _DeadlineReached(Exception):
    pass


class _ApplicationCallbackError(Exception):
    def __init__(self, original: Exception) -> None:
        self.original = original


def _application_call(callback: Callable[..., T], *args: Any) -> T:
    try:
        return callback(*args)
    except Exception as error:
        raise _ApplicationCallbackError(error) from error


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DeadlineReached()
    return remaining


def _identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise validation_error(
            f"{name} must be a non-empty string without surrounding whitespace"
        )
    return value


def _body(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 20_000:
        raise validation_error("body must contain between 1 and 20000 characters")
    return value


def _decimal(value: str) -> int:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 19
        or not value.isascii()
        or not value.isdecimal()
        or str(int(value)) != value
        or int(value) > MAX_CURSOR
    ):
        raise validation_error(
            "Checkpoint cursor and sequence must be canonical int64 decimal strings"
        )
    return int(value)


def _pages(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 50:
        raise validation_error("max_pages must be between 1 and 50")
    return value


def _positive(value: float, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise validation_error(f"{name} must be a positive finite number")


def _copy(message: MessageT) -> MessageT:
    result = type(message)()
    result.CopyFrom(message)
    return result


def _protocol(message: str) -> DeixicError:
    return DeixicError(message, kind="protocol")


def _validate_page(page: pb.ListOperatingThreadEventsResponse, cursor: int) -> None:
    expected = max([cursor] + [event.cursor for event in page.events])
    if page.next_cursor != expected or (page.has_more and expected <= cursor):
        raise _protocol("Event pagination returned an invalid watermark")
    records = {}
    for event in page.events:
        if event.cursor <= 0:
            raise _protocol("Event cursor must be positive")
        encoded = event.SerializeToString(deterministic=True)
        if event.cursor in records and records[event.cursor] != encoded:
            raise _protocol("Different events reused the same cursor")
        records[event.cursor] = encoded
