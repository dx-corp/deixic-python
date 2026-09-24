from __future__ import annotations

import json
import math
import struct
from collections.abc import Iterator
from typing import Any, TypeVar
from urllib.parse import urlsplit

from deixic import protocol as public_pb2
from google.protobuf.message import Message

from .auth import (
    Credential,
    CredentialIdentity,
    CredentialProvider,
    StaticCredentialProvider,
)
from .errors import (
    DeixicError,
    connect_stream_error,
    error_from_response,
    validation_error,
)
from .transport import RequestsTransport, Response, Transport

MessageT = TypeVar("MessageT", bound=Message)

_SERVICE = "deixicpublic.v1.DeixicPublicService"
_MAX_STREAM_FRAME_BYTES = 16 * 1024 * 1024


class Deixic:
    """Scope-bound client for Deixic's durable operating contract."""

    def __init__(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        api_key: str | None = None,
        credential_provider: CredentialProvider | None = None,
        base_url: str = "https://app.deixic.com",
        timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        self._organization_id = _required(organization_id, "organization_id")
        self._workspace_id = _required(workspace_id, "workspace_id")
        if bool(api_key) == bool(credential_provider):
            raise validation_error(
                'provide exactly one of "api_key" or "credential_provider"'
            )
        self._credentials = credential_provider or StaticCredentialProvider(
            api_key or ""
        )
        self._identity = CredentialIdentity(self.organization_id, self.workspace_id)
        self._base_url = _validated_base_url(base_url)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise validation_error("timeout must be a positive finite number")
        self._timeout = float(timeout)
        self._transport = transport or RequestsTransport()

        self.threads = ThreadsClient(self)
        self.events = EventsClient(self)
        self.messages = MessagesClient(self)
        self.controls = ControlsClient(self)
        self.receipts = ReceiptsClient(self)
        from .tasks import TasksClient

        self.tasks = TasksClient(self)

    @property
    def organization_id(self) -> str:
        """Organization scope fixed when this client was created."""
        return self._organization_id

    @property
    def workspace_id(self) -> str:
        """Workspace scope fixed when this client was created."""
        return self._workspace_id

    @property
    def base_url(self) -> str:
        """Platform origin fixed when this client was created."""
        return self._base_url

    def _scope(self) -> public_pb2.Scope:
        return public_pb2.Scope(
            organization_id=self.organization_id,
            workspace_id=self.workspace_id,
        )

    def _unary(
        self,
        method: str,
        request: Message,
        response_type: type[MessageT],
        *,
        timeout: float | None = None,
    ) -> MessageT:
        body = request.SerializeToString(deterministic=True)
        current = self._identity.check(self._credentials.get_credential())
        response = self._send(method, body, current, stream=False, timeout=timeout)
        if int(response.status_code) == 401 and self._credentials.can_refresh:
            response.close()
            refreshed = self._credentials.refresh_credential(current)
            current = self._identity.check_refresh(current, refreshed)
            response = self._send(method, body, current, stream=False, timeout=timeout)
        try:
            if not 200 <= int(response.status_code) < 300:
                raise error_from_response(response)
            result = response_type()
            try:
                result.ParseFromString(bytes(response.content))
            except Exception as exc:
                raise DeixicError(
                    "Deixic returned an invalid protobuf response",
                    kind="protocol",
                    status_code=int(response.status_code),
                ) from exc
            return result
        finally:
            response.close()

    def _stream(
        self,
        method: str,
        request: Message,
        response_type: type[MessageT],
    ) -> Iterator[MessageT]:
        payload = request.SerializeToString(deterministic=True)
        body = struct.pack(">BI", 0, len(payload)) + payload
        current = self._identity.check(self._credentials.get_credential())
        response = self._send(method, body, current, stream=True)
        if int(response.status_code) == 401 and self._credentials.can_refresh:
            response.close()
            refreshed = self._credentials.refresh_credential(current)
            current = self._identity.check_refresh(current, refreshed)
            response = self._send(method, body, current, stream=True)
        if not 200 <= int(response.status_code) < 300:
            try:
                raise error_from_response(response)
            finally:
                response.close()
        return _decode_stream(response, response_type)

    def _send(
        self,
        method: str,
        body: bytes,
        credential: Credential,
        *,
        stream: bool,
        timeout: float | None = None,
    ) -> Response:
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise validation_error("timeout must be a positive finite number")
        content_type = "application/connect+proto" if stream else "application/proto"
        try:
            return self._transport.send(
                "POST",
                f"{self._base_url}/{_SERVICE}/{method}",
                headers={
                    "Authorization": f"{credential.token_type or 'Bearer'} {credential.access_token}",
                    "Connect-Protocol-Version": "1",
                    "Content-Type": content_type,
                    "Accept": content_type,
                    "X-Organization-ID": self.organization_id,
                    "X-Workspace-ID": self.workspace_id,
                },
                body=body,
                timeout=min(self._timeout, timeout)
                if timeout is not None
                else self._timeout,
                stream=stream,
            )
        except DeixicError:
            raise
        except Exception as exc:
            raise DeixicError("Deixic transport failed", kind="transport") from exc


class ThreadsClient:
    def __init__(self, client: Deixic) -> None:
        self._client = client

    def get(
        self,
        *,
        channel_id: str,
        limit: int = 100,
        offset: int = 0,
        page_token: str = "",
        timeout: float | None = None,
    ) -> public_pb2.GetThreadResponse:
        _bounded_int(limit, "limit", minimum=1, maximum=200)
        _bounded_int(offset, "offset", minimum=0, maximum=0)
        request = public_pb2.GetThreadRequest(
            scope=self._client._scope(),
            thread_id=_required(channel_id, "channel_id"),
            limit=limit,
            page_token=page_token,
        )
        return self._client._unary(
            "GetThread",
            request,
            public_pb2.GetThreadResponse,
            timeout=timeout,
        )


class EventsClient:
    def __init__(self, client: Deixic) -> None:
        self._client = client

    def list(
        self,
        *,
        channel_id: str,
        after_cursor: int,
        limit: int = 200,
        timeout: float | None = None,
    ) -> public_pb2.ListEventsResponse:
        _bounded_int(after_cursor, "after_cursor", minimum=0, maximum=2**63 - 1)
        _bounded_int(limit, "limit", minimum=1, maximum=200)
        request = public_pb2.ListEventsRequest(
            scope=self._client._scope(),
            thread_id=_required(channel_id, "channel_id"),
            after_cursor=after_cursor,
            limit=limit,
        )
        return self._client._unary(
            "ListEvents",
            request,
            public_pb2.ListEventsResponse,
            timeout=timeout,
        )

    def watch(
        self,
        *,
        channel_id: str,
        after_cursor: int,
    ) -> Iterator[public_pb2.WatchEventsResponse]:
        _bounded_int(after_cursor, "after_cursor", minimum=0, maximum=2**63 - 1)
        request = public_pb2.WatchEventsRequest(
            scope=self._client._scope(),
            thread_id=_required(channel_id, "channel_id"),
            after_cursor=after_cursor,
        )
        return self._client._stream(
            "WatchEvents",
            request,
            public_pb2.WatchEventsResponse,
        )


class MessagesClient:
    def __init__(self, client: Deixic) -> None:
        self._client = client

    def send(
        self,
        *,
        channel_id: str,
        body: str,
        idempotency_key: str,
        continuation_task_id: str = "",
        reference_task_ids: tuple[str, ...] | list[str] = (),
        project_resource_id: str | None = None,
        coding_acceptance: public_pb2.CodingContract | None = None,
    ) -> public_pb2.SubmitTaskResponse:
        request = public_pb2.SubmitTaskRequest(
            scope=self._client._scope(),
            thread_id=_required(channel_id, "channel_id"),
            body=body,
            idempotency_key=_required(idempotency_key, "idempotency_key"),
            continuation_task_id=continuation_task_id,
            reference_task_ids=list(reference_task_ids),
        )
        if coding_acceptance is not None:
            request.coding_contract.CopyFrom(coding_acceptance)
        if project_resource_id is not None:
            request.project_resource_id = _required(
                project_resource_id, "project_resource_id"
            )
        return self._client._unary(
            "SubmitTask",
            request,
            public_pb2.SubmitTaskResponse,
        )


class ControlsClient:
    def __init__(self, client: Deixic) -> None:
        self._client = client

    def interrupt(
        self,
        *,
        channel_id: str,
        idempotency_key: str,
        turn_id: str = "",
        reason: str = "",
    ) -> public_pb2.InterruptTaskResponse:
        request = public_pb2.InterruptTaskRequest(
            scope=self._client._scope(),
            thread_id=_required(channel_id, "channel_id"),
            turn_id=turn_id,
            idempotency_key=_required(idempotency_key, "idempotency_key"),
            reason=reason,
        )
        return self._client._unary(
            "InterruptTask",
            request,
            public_pb2.InterruptTaskResponse,
        )

    def respond(
        self,
        *,
        channel_id: str,
        turn_id: str,
        response: public_pb2.RespondToRequestRequest,
        idempotency_key: str,
    ) -> public_pb2.RespondToRequestResponse:
        key = _required(idempotency_key, "idempotency_key")
        response_copy = public_pb2.RespondToRequestRequest()
        response_copy.CopyFrom(response)
        if response_copy.idempotency_key and response_copy.idempotency_key != key:
            raise validation_error(
                "response.idempotency_key must match idempotency_key"
            )
        response_copy.idempotency_key = key
        request = public_pb2.RespondToRequestRequest(
            scope=self._client._scope(),
            thread_id=_required(channel_id, "channel_id"),
            turn_id=_required(turn_id, "turn_id"),
            request_id=response_copy.request_id,
            request_kind=response_copy.request_kind,
            action=response_copy.action,
            call_id=response_copy.call_id,
            text=response_copy.text,
            is_error=response_copy.is_error,
            idempotency_key=key,
        )
        return self._client._unary(
            "RespondToRequest",
            request,
            public_pb2.RespondToRequestResponse,
        )


class ReceiptsClient:
    def __init__(self, client: Deixic) -> None:
        self._client = client

    def get(
        self,
        *,
        channel_id: str,
        receipt_id: str,
        include_coding_output_content: bool = False,
        timeout: float | None = None,
    ) -> public_pb2.GetReceiptResponse:
        request = public_pb2.GetReceiptRequest(
            scope=self._client._scope(),
            thread_id=_required(channel_id, "channel_id"),
            receipt_id=_required(receipt_id, "receipt_id"),
            include_coding_output_content=include_coding_output_content,
        )
        return self._client._unary(
            "GetReceipt",
            request,
            public_pb2.GetReceiptResponse,
            timeout=timeout,
        )

    def resolve(
        self,
        *,
        receipt_id: str,
        action: public_pb2.ReceiptAction,
        idempotency_key: str,
    ) -> public_pb2.ResolveReceiptActionResponse:
        request = public_pb2.ResolveReceiptActionRequest(
            scope=self._client._scope(),
            receipt_id=_required(receipt_id, "receipt_id"),
            action_id=_required(action.id, "action.id"),
            idempotency_key=_required(idempotency_key, "idempotency_key"),
        )
        return self._client._unary(
            "ResolveReceiptAction",
            request,
            public_pb2.ResolveReceiptActionResponse,
        )


def _decode_stream(
    response: Response, response_type: type[MessageT]
) -> Iterator[MessageT]:
    def messages() -> Iterator[MessageT]:
        buffer = bytearray()
        ended = False
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if ended:
                    raise DeixicError(
                        "Deixic stream returned bytes after its end envelope",
                        kind="protocol",
                    )
                buffer.extend(chunk)
                while len(buffer) >= 5:
                    flags = buffer[0]
                    length = struct.unpack(">I", buffer[1:5])[0]
                    if length > _MAX_STREAM_FRAME_BYTES:
                        raise DeixicError(
                            "Deixic stream frame exceeds the 16 MiB SDK limit",
                            kind="protocol",
                        )
                    if len(buffer) < 5 + length:
                        break
                    payload = bytes(buffer[5 : 5 + length])
                    del buffer[: 5 + length]
                    if flags == 0x02:
                        try:
                            document = json.loads(payload.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise DeixicError(
                                "Deixic stream returned an invalid end envelope",
                                kind="protocol",
                            ) from exc
                        if not isinstance(document, dict):
                            raise DeixicError(
                                "Deixic stream returned a non-object end envelope",
                                kind="protocol",
                            )
                        if "error" in document:
                            if not isinstance(document["error"], dict):
                                raise DeixicError(
                                    "Deixic stream returned an invalid error envelope",
                                    kind="protocol",
                                )
                            raise connect_stream_error(payload, response.headers)
                        if buffer:
                            raise DeixicError(
                                "Deixic stream returned bytes after its end envelope",
                                kind="protocol",
                            )
                        ended = True
                        break
                    if flags != 0:
                        raise DeixicError(
                            f"Deixic stream used unsupported envelope flags: {flags}",
                            kind="protocol",
                        )
                    message = response_type()
                    try:
                        message.ParseFromString(payload)
                    except Exception as exc:
                        raise DeixicError(
                            "Deixic stream returned an invalid protobuf frame",
                            kind="protocol",
                        ) from exc
                    yield message
            if buffer:
                raise DeixicError(
                    "Deixic stream ended with an incomplete frame", kind="protocol"
                )
            if not ended:
                raise DeixicError(
                    "Deixic stream ended without an end envelope", kind="protocol"
                )
        except DeixicError:
            raise
        except Exception as exc:
            raise DeixicError(
                "Deixic stream transport failed", kind="transport"
            ) from exc
        finally:
            response.close()

    return messages()


def _required(value: str, field: str) -> str:
    cleaned = value.strip() if isinstance(value, str) else ""
    if not cleaned:
        raise validation_error(f"{field} is required")
    return cleaned


def _validated_base_url(value: str) -> str:
    cleaned = _required(value, "base_url").rstrip("/")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise validation_error("base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise validation_error(
            "base_url must not contain credentials, a query, or a fragment"
        )
    return cleaned


def _bounded_int(value: int, field: str, *, minimum: int, maximum: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise validation_error(
            f"{field} must be an integer between {minimum} and {maximum}"
        )
