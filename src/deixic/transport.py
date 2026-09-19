from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Protocol


class Response(Protocol):
    status_code: int
    headers: Mapping[str, str]
    content: bytes

    def json(self) -> Any: ...

    def iter_content(self, chunk_size: int | None = None) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class Transport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
        stream: bool = False,
    ) -> Response: ...


class RequestsTransport:
    def __init__(self) -> None:
        import requests

        self._session = requests.Session()
        # A truthy explicit auth handler prevents requests from replacing the
        # bearer with .netrc credentials. Keep trust_env enabled so enterprise
        # proxy and CA-bundle settings continue to work.
        self._session.auth = _PreserveAuthorizationHeader()

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
        stream: bool = False,
    ) -> Response:
        return self._session.request(
            method,
            url,
            headers=dict(headers),
            data=body,
            timeout=timeout,
            stream=stream,
            allow_redirects=False,
        )


class _PreserveAuthorizationHeader:
    def __call__(self, request: Any) -> Any:
        return request
