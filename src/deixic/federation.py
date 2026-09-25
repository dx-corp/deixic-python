"""Workload federation credentials for CI and cloud workloads.

A workload presents a short-lived signed assertion (an OIDC JWT issued by its
CI system or cloud platform) to Identity's
``/v1/workload-federation/exchange`` endpoint and receives a tenant-bound
access token valid for at most 300 seconds. Identity accepts each assertion
once, so every exchange here obtains a new assertion from the configured
source.

Assertions and access tokens are never logged or included in error messages.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from .auth import Credential
from .errors import DeixicError, validation_error
from .transport import RequestsTransport, Response, Transport

AssertionSource = Callable[[], str]
"""Zero-argument callable returning a fresh signed workload assertion."""

EXCHANGE_PATH = "/v1/workload-federation/exchange"

_REMEMBERED_ASSERTIONS = 16
_REUSED = "workload_assertion_reused"
_UNAVAILABLE = "workload_federation_unavailable"
_TRANSPORT = "workload_exchange_transport"
_EARLY_REFRESH_TOLERATED = frozenset({_REUSED, _UNAVAILABLE, _TRANSPORT})


class WorkloadFederationCredentialProvider:
    """Exchange workload assertions for short-lived Deixic access tokens.

    The first ``get_credential`` call performs an exchange. The token is
    cached and exchanged again ``refresh_margin`` seconds before it expires,
    or after half its lifetime when the token lives less than twice the
    margin. Every exchange requests a new assertion from ``assertion_source``.

    Only HTTP 503 and transport failures are retried, each attempt with a new
    assertion. A 400, 403, or 409 (replayed assertion) response is raised
    immediately.
    """

    can_refresh = True

    def __init__(
        self,
        *,
        identity_url: str,
        assertion_source: AssertionSource,
        refresh_margin: float = 60.0,
        timeout: float = 10.0,
        max_attempts: int = 3,
        retry_delay: float = 0.5,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._exchange_url = _validated_identity_url(identity_url) + EXCHANGE_PATH
        if not callable(assertion_source):
            raise validation_error("assertion_source must be a callable")
        self._assertion_source = assertion_source
        self._refresh_margin = _non_negative(refresh_margin, "refresh_margin")
        self._timeout = _positive(timeout, "timeout")
        self._retry_delay = _non_negative(retry_delay, "retry_delay")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 10
        ):
            raise validation_error("max_attempts must be an integer between 1 and 10")
        self._max_attempts = max_attempts
        self._transport = transport or RequestsTransport()
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._credential: Credential | None = None
        self._refresh_at = 0.0
        self._expires_at = 0.0
        self._used_assertions: deque[str] = deque(maxlen=_REMEMBERED_ASSERTIONS)

    def __repr__(self) -> str:
        return (
            f"WorkloadFederationCredentialProvider(exchange_url={self._exchange_url!r})"
        )

    def get_credential(self) -> Credential:
        with self._lock:
            cached = self._credential
            now = self._clock()
            if cached is not None and now < self._refresh_at:
                return cached
            try:
                return self._exchange()
            except DeixicError as error:
                # An early refresh that fails transiently keeps the unexpired
                # token. A file source that has not rotated yet lands here.
                if (
                    cached is not None
                    and now < self._expires_at
                    and error.code in _EARLY_REFRESH_TOLERATED
                ):
                    return cached
                raise

    def refresh_credential(self, current: Credential) -> Credential:
        with self._lock:
            cached = self._credential
            if (
                cached is not None
                and cached.access_token != current.access_token
                and self._clock() < self._refresh_at
            ):
                # Another caller already replaced the rejected token.
                return cached
            return self._exchange()

    def _exchange(self) -> Credential:
        last_error: DeixicError | None = None
        for attempt in range(self._max_attempts):
            if last_error is not None:
                self._sleep(self._retry_delay * (2 ** (attempt - 1)))
            try:
                assertion = self._next_assertion()
            except DeixicError as error:
                # A retry whose source has no new assertion reports the
                # original unavailable or transport failure.
                if last_error is not None and error.code == _REUSED:
                    raise last_error from None
                raise
            try:
                return self._exchange_once(assertion)
            except DeixicError as error:
                if error.code not in {_UNAVAILABLE, _TRANSPORT}:
                    raise
                last_error = error
        assert last_error is not None
        raise last_error

    def _next_assertion(self) -> str:
        try:
            assertion = self._assertion_source()
        except DeixicError:
            raise
        except Exception as exc:
            raise _assertion_unavailable("workload assertion source failed") from exc
        if not isinstance(assertion, str) or not assertion.strip():
            raise _assertion_unavailable(
                "workload assertion source returned an empty assertion"
            )
        assertion = assertion.strip()
        digest = hashlib.sha256(assertion.encode("utf-8")).hexdigest()
        if digest in self._used_assertions:
            raise DeixicError(
                "workload assertion source returned an assertion that was "
                "already exchanged; Identity accepts each assertion once",
                kind="authentication",
                code=_REUSED,
            )
        self._used_assertions.append(digest)
        return assertion

    def _exchange_once(self, assertion: str) -> Credential:
        body = json.dumps({"assertion": assertion}).encode("utf-8")
        try:
            response = self._transport.send(
                "POST",
                self._exchange_url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body=body,
                timeout=self._timeout,
            )
        except DeixicError:
            raise
        except Exception as exc:
            raise DeixicError(
                "Identity workload exchange transport failed",
                kind="transport",
                code=_TRANSPORT,
            ) from exc
        try:
            status = int(response.status_code)
            if not 200 <= status < 300:
                raise _exchange_error(status, response)
            return self._credential_from(response)
        finally:
            response.close()

    def _credential_from(self, response: Response) -> Credential:
        try:
            payload = json.loads(bytes(response.content).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, Mapping):
            raise _invalid_response("Identity returned an invalid exchange response")
        access_token = _field(payload, "access_token")
        token_type = _field(payload, "token_type")
        principal_id = _field(payload, "principal_id")
        organization_id = _field(payload, "organization_id")
        workspace_id = _field(payload, "workspace_id")
        expires_at = _parse_expiry(_field(payload, "expires_at"))
        scopes = payload.get("scopes")
        if not isinstance(scopes, list) or not all(
            isinstance(scope, str) for scope in scopes
        ):
            raise _invalid_response("Identity exchange response has invalid scopes")
        now = self._clock()
        lifetime = max(0.0, expires_at - now)
        self._refresh_at = now + max(lifetime - self._refresh_margin, lifetime / 2)
        self._expires_at = now + lifetime
        self._credential = Credential(
            access_token=access_token,
            token_type=token_type,
            subject=principal_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            scopes=tuple(scopes),
        )
        return self._credential


def github_actions_assertion_source(
    audience: str,
    *,
    transport: Transport | None = None,
    timeout: float = 10.0,
    environ: Mapping[str, str] | None = None,
) -> AssertionSource:
    """Request a new GitHub Actions OIDC token on every call.

    The job needs ``permissions: id-token: write`` so that GitHub sets
    ``ACTIONS_ID_TOKEN_REQUEST_URL`` and ``ACTIONS_ID_TOKEN_REQUEST_TOKEN``.
    ``audience`` must equal the audience registered on the Identity issuer.
    """

    audience = audience.strip() if isinstance(audience, str) else ""
    if not audience:
        raise validation_error("audience is required")
    timeout = _positive(timeout, "timeout")
    client = transport or RequestsTransport()
    env = os.environ if environ is None else environ

    def source() -> str:
        request_url = (env.get("ACTIONS_ID_TOKEN_REQUEST_URL") or "").strip()
        request_token = (env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN") or "").strip()
        if not request_url or not request_token:
            raise _assertion_unavailable(
                "ACTIONS_ID_TOKEN_REQUEST_URL and ACTIONS_ID_TOKEN_REQUEST_TOKEN "
                "are not set; grant the job the id-token: write permission"
            )
        separator = "&" if urlsplit(request_url).query else "?"
        url = f"{request_url}{separator}{urlencode({'audience': audience})}"
        try:
            response = client.send(
                "GET",
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {request_token}",
                },
                body=b"",
                timeout=timeout,
            )
        except DeixicError:
            raise
        except Exception:
            raise _assertion_unavailable(
                "GitHub Actions OIDC token request failed"
            ) from None
        try:
            status = int(response.status_code)
            if not 200 <= status < 300:
                raise _assertion_unavailable(
                    f"GitHub Actions OIDC token request failed with HTTP {status}"
                )
            try:
                payload = json.loads(bytes(response.content).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            value = payload.get("value") if isinstance(payload, Mapping) else None
            if not isinstance(value, str) or not value.strip():
                raise _assertion_unavailable(
                    "GitHub Actions OIDC token response has no value"
                )
            return value.strip()
        finally:
            response.close()

    return source


def file_assertion_source(path: str | os.PathLike[str]) -> AssertionSource:
    """Read the assertion from a file on every call.

    Use this for a Kubernetes projected service-account token or any file a
    platform agent rewrites with a new token.
    """

    token_path = Path(path)

    def source() -> str:
        try:
            value = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            raise _assertion_unavailable(
                f"workload assertion file {str(token_path)!r} could not be read"
            ) from None
        if not value:
            raise _assertion_unavailable(
                f"workload assertion file {str(token_path)!r} is empty"
            )
        return value

    return source


def environment_assertion_source(
    name: str, *, environ: Mapping[str, str] | None = None
) -> AssertionSource:
    """Read the assertion from an environment variable on every call."""

    name = name.strip() if isinstance(name, str) else ""
    if not name:
        raise validation_error("environment variable name is required")
    env = os.environ if environ is None else environ

    def source() -> str:
        value = (env.get(name) or "").strip()
        if not value:
            raise _assertion_unavailable(
                f"environment variable {name} does not contain a workload assertion"
            )
        return value

    return source


def _exchange_error(status: int, response: Response) -> DeixicError:
    kind, code, message = {
        400: (
            "validation",
            "workload_assertion_invalid",
            "Identity rejected the workload exchange request as malformed",
        ),
        403: (
            "authorization",
            "workload_federation_forbidden",
            "Identity did not match the workload assertion to an active "
            "federation rule",
        ),
        409: (
            "conflict",
            "workload_assertion_replayed",
            "Identity already accepted this workload assertion",
        ),
        503: (
            "unavailable",
            _UNAVAILABLE,
            "Identity workload federation is unavailable",
        ),
    }.get(
        status,
        (
            "protocol",
            "workload_exchange_failed",
            f"Identity workload exchange failed with HTTP {status}",
        ),
    )
    headers = getattr(response, "headers", {}) or {}
    return DeixicError(
        message,
        kind=kind,
        status_code=status,
        code=code,
        request_id=_header(headers, "x-request-id"),
        traceparent=_header(headers, "traceparent"),
    )


def _assertion_unavailable(message: str) -> DeixicError:
    return DeixicError(
        message, kind="authentication", code="workload_assertion_unavailable"
    )


def _invalid_response(message: str) -> DeixicError:
    return DeixicError(message, kind="protocol", code="workload_exchange_invalid")


def _field(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _invalid_response(f"Identity exchange response is missing {name}")
    return value.strip()


def _parse_expiry(value: str) -> float:
    # Identity emits RFC 3339 UTC timestamps such as 2026-09-24T12:00:00Z.
    # datetime.fromisoformat accepts a trailing "Z" only from Python 3.11.
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise _invalid_response(
            "Identity exchange response has an invalid expires_at"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _header(headers: Mapping[str, Any], name: str) -> str | None:
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return None


def _validated_identity_url(value: str) -> str:
    cleaned = value.strip().rstrip("/") if isinstance(value, str) else ""
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise validation_error("identity_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise validation_error(
            "identity_url must not contain credentials, a query, or a fragment"
        )
    return cleaned


def _positive(value: float, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise validation_error(f"{field} must be a positive finite number")
    return float(value)


def _non_negative(value: float, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise validation_error(f"{field} must be a non-negative finite number")
    return float(value)
