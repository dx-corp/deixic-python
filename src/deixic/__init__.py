from .auth import Credential, CredentialProvider
from .client import Deixic
from .errors import DeixicError
from .federation import (
    AssertionSource,
    WorkloadFederationCredentialProvider,
    environment_assertion_source,
    file_assertion_source,
    github_actions_assertion_source,
)
from .tasks import SetupCheck, Task, TaskCheckpoint, TaskResult

__all__ = [
    "AssertionSource",
    "Credential",
    "CredentialProvider",
    "Deixic",
    "DeixicError",
    "SetupCheck",
    "Task",
    "TaskCheckpoint",
    "TaskResult",
    "WorkloadFederationCredentialProvider",
    "environment_assertion_source",
    "file_assertion_source",
    "github_actions_assertion_source",
]
