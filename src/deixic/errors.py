from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class DeixicError(Exception):
    """Base failure raised by the Deixic SDK."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "protocol",
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        traceparent: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.traceparent = traceparent
        self.details = dict(details or {})


def error_from_response(response: Any) -> DeixicError:
    status_code = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    payload = _payload(response)
    code = _string(payload.get("code"))
    message = _string(payload.get("message"))
    nested = payload.get("error")
    if isinstance(nested, Mapping):
        code = code or _string(nested.get("code"))
        message = message or _string(nested.get("message"))
    elif isinstance(nested, str):
        message = message or nested
    message = message or f"Deixic request failed with HTTP {status_code}"
    return DeixicError(
        message,
        kind=_kind(status_code, code),
        status_code=status_code or None,
        code=_header(headers, "x-evalops-error-code") or code,
        request_id=_header(headers, "x-request-id"),
        traceparent=_header(headers, "traceparent"),
        details=payload,
    )


def connect_stream_error(payload: bytes, headers: Mapping[str, str]) -> DeixicError:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        document = {}
    error = document.get("error") if isinstance(document, Mapping) else None
    if not isinstance(error, Mapping):
        error = {}
    code = _string(error.get("code")) or "unknown"
    message = _string(error.get("message")) or "Deixic stream ended with an error"
    return DeixicError(
        message,
        kind=_connect_kind(code),
        code=code,
        request_id=_header(headers, "x-request-id"),
        traceparent=_header(headers, "traceparent"),
        details=document if isinstance(document, Mapping) else {},
    )


def validation_error(message: str) -> DeixicError:
    return DeixicError(message, kind="validation", status_code=400)


def authentication_error(message: str) -> DeixicError:
    return DeixicError(message, kind="authentication", status_code=401)


def _payload(response: Any) -> dict[str, Any]:
    try:
        value = response.json()
    except (ValueError, AttributeError):
        content = getattr(response, "content", b"")
        if isinstance(content, bytes):
            try:
                value = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = {}
        else:
            value = {}
    return dict(value) if isinstance(value, Mapping) else {}


def _header(headers: Mapping[str, Any], name: str) -> str | None:
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _kind(status_code: int, code: str | None) -> str:
    if status_code == 401:
        return "authentication"
    if status_code == 403:
        return "authorization"
    if status_code in {400, 422}:
        return "validation"
    if status_code == 404:
        return "not_found"
    if status_code in {409, 412}:
        return "conflict"
    if status_code == 429:
        return "rate_limited"
    if status_code in {502, 503, 504}:
        return "unavailable"
    return _connect_kind(code or "unknown")


def _connect_kind(code: str) -> str:
    return {
        "unauthenticated": "authentication",
        "permission_denied": "authorization",
        "invalid_argument": "validation",
        "out_of_range": "validation",
        "not_found": "not_found",
        "already_exists": "conflict",
        "aborted": "conflict",
        "failed_precondition": "conflict",
        "resource_exhausted": "rate_limited",
        "unavailable": "unavailable",
        "deadline_exceeded": "unavailable",
        "canceled": "transport",
    }.get(code.lower(), "protocol")
