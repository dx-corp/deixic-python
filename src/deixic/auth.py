from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .errors import authentication_error, validation_error


_HTTP_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


@dataclass(frozen=True)
class Credential:
    access_token: str
    token_type: str = "Bearer"
    subject: str | None = None
    organization_id: str | None = None
    workspace_id: str | None = None
    scopes: tuple[str, ...] = ()


class CredentialProvider(Protocol):
    @property
    def can_refresh(self) -> bool: ...

    def get_credential(self) -> Credential: ...

    def refresh_credential(self, current: Credential) -> Credential: ...


class StaticCredentialProvider:
    can_refresh = False

    def __init__(self, token: str) -> None:
        token = token.strip()
        if not token:
            raise validation_error("api_key must be non-empty")
        self._credential = Credential(access_token=token)

    def get_credential(self) -> Credential:
        return self._credential

    def refresh_credential(self, current: Credential) -> Credential:
        return current


class CredentialIdentity:
    def __init__(self, organization_id: str, workspace_id: str) -> None:
        self._organization_id = organization_id
        self._workspace_id = workspace_id
        self._subject: str | None = None
        self._scopes: frozenset[str] | None = None
        self._declared_organization = False
        self._declared_workspace = False

    def check(self, credential: Credential) -> Credential:
        access_token = credential.access_token
        if not access_token.strip():
            raise authentication_error(
                "credential provider returned an empty access token"
            )
        if access_token != access_token.strip() or any(
            ord(char) < 0x20 or ord(char) == 0x7F for char in access_token
        ):
            raise authentication_error(
                "credential provider returned an invalid access token"
            )
        token_type = credential.token_type.strip()
        if token_type != credential.token_type or not _HTTP_TOKEN.fullmatch(token_type):
            raise authentication_error(
                "credential provider returned an invalid token type"
            )
        organization_id = _clean(credential.organization_id)
        workspace_id = _clean(credential.workspace_id)
        if organization_id and organization_id != self._organization_id:
            raise authentication_error(
                "credential organization_id does not match the client scope"
            )
        if workspace_id and workspace_id != self._workspace_id:
            raise authentication_error(
                "credential workspace_id does not match the client scope"
            )
        if self._declared_organization and not organization_id:
            raise authentication_error(
                "credential refresh removed its declared organization_id"
            )
        if self._declared_workspace and not workspace_id:
            raise authentication_error(
                "credential refresh removed its declared workspace_id"
            )
        self._declared_organization = self._declared_organization or bool(
            organization_id
        )
        self._declared_workspace = self._declared_workspace or bool(workspace_id)

        subject = _clean(credential.subject)
        if self._subject and not subject:
            raise authentication_error(
                "credential refresh removed the authenticated subject"
            )
        if self._subject and subject != self._subject:
            raise authentication_error(
                "credential refresh changed the authenticated subject"
            )
        if subject:
            self._subject = subject

        scopes = frozenset(
            scope.strip() for scope in credential.scopes if scope.strip()
        )
        if self._scopes is not None and not scopes:
            raise authentication_error(
                "credential refresh removed its declared OAuth scopes"
            )
        if self._scopes is not None and scopes != self._scopes:
            raise authentication_error(
                "credential refresh changed its declared OAuth scopes"
            )
        if scopes and self._scopes is None:
            self._scopes = scopes
        return credential

    def check_refresh(self, current: Credential, refreshed: Credential) -> Credential:
        current_subject = _clean(current.subject)
        refreshed_subject = _clean(refreshed.subject)
        if not current_subject or refreshed_subject != current_subject:
            raise authentication_error(
                "credential refresh requires the same stable subject before replay"
            )
        return self.check(refreshed)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
