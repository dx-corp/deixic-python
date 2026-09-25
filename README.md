# Deixic SDK for Python

`deixic-sdk` is the supported Python client for applications that submit Deixic
tasks, follow durable progress, interrupt work, and approve or deny requested
actions. It uses binary Connect/protobuf requests rather than the browser-only
API.

## Install

```sh
pip install deixic-sdk
```

## Quickstart

```python
import os

from deixic import Deixic

deixic = Deixic(
    api_key=os.environ["DEIXIC_API_KEY"],
    organization_id="org_123",
    workspace_id="ws_456",
)

task = deixic.tasks.start(
    channel_id="company",
    body="Review the open changes.",
    idempotency_key="review-request-001",  # Stable business-event ID for this request.
)

result = task.wait(timeout=60)
if result.status == "completed":
    print(result.body)
else:
    print(result.status, result.reason)
```

Choose a different key for each new business request. Recovery of the same
request keeps its original key and body. Submission proves acceptance.
`task.wait()` follows durable events and returns the final assistant answer
linked to that accepted turn, together with its referenced receipts.

## Task handles and setup checks

`deixic.tasks.check_setup(channel_id="company")` makes one read request to
verify channel access and reports the owner's workspace prerequisites and
selected/default model availability. `accessible` confirms read access; `write_access`
remains `not_checked`. The report contains `next_action` and, on failure, an
SDK error with its request ID. Submission checks write authorization and execution.

For restart recovery, use `tasks.prepare(..., on_checkpoint=save_checkpoint)`
with your application's storage adapter, then `task.submit()`. The callback
runs before submission and after acceptance or consumed event pages. Storage
errors propagate. `tasks.resume(saved_checkpoint)` restores observation
coordinates and never submits work. `task.replay()` explicitly repeats an
unacknowledged request with its original body, key, tenant and origin; it
refuses an accepted task.

Checkpoints use the shared Python/TypeScript `deixic.task.v1` format with
decimal-string int64 cursors. They contain the request body and no SDK
credential. Protect them as customer data and use one observer/storage writer
per checkpoint. A checkpoint never proves authorization or task completion.

`wait()` uses bounded event backfill and polling, reconnecting read requests
after transport/unavailable failures up to `max_reconnect_attempts`. It never
retries a mutation. `timeout` is the observation budget; each read timeout is
capped by the remaining budget, subject to the transport's timeout semantics.
`cancelled=lambda: ...` stops local observation. Remote interruption requires
an explicit `controls.interrupt()` call.

`wait()` continues past a preliminary `responded` state until completion,
failure, interruption or work that needs attention. `result()` reads the current
owner state once. Outcomes are `completed`, `responded`, `waiting`, `failed`, `interrupted`,
`unfinished`, `prepared`, or `unacknowledged`. A waiting outcome includes its
turn's waiting reason and a freshly retrieved request event when retained.
Use that event's request identity with `controls.respond()` after an explicit
application/user decision. Missing request history stays
`waiting/request_not_visible`; no approval is invented or sent automatically.
Progress callbacks receive only new matching-turn events. Callbacks may be
delivered again after a storage failure or restart; make application effects
idempotent. Callback exceptions propagate.

`result.parse(your_parser)` validates/converts the completed answer using your
application's schema. Parser failures propagate. A completed answer does not
prove that every external action succeeded; inspect the receipts' owner-resolved
lifecycle and evidence for those actions.

## Account-brief workflow

Use a workspace with connected CRM data and a policy that permits only CRM
reads for this workflow. The SDK creates no connector, grant or model route.
The example asks for a summary, open opportunities, risks and source references;
Platform enforces the workspace's access and action policy.

Set the three `DEIXIC_*` variables used above, then run the installed example:

```sh
python -m deixic.examples.account_brief check --channel company
python -m deixic.examples.account_brief start account-brief.json \
  --channel company --account 'Example account' --trigger crm-event-001
# A separate process retrieves the result:
python -m deixic.examples.account_brief resume account-brief.json
```

`start` saves a private checkpoint before sending and reports acceptance.
`resume` prints the final brief and receipt IDs when completed. It returns
exit code 2 for unfinished work or work that needs attention. If acceptance was
lost, explicitly run `replay account-brief.json`, then resume. A new `start`
refuses an existing checkpoint path. The same file can be resumed by the
TypeScript account-brief example with the same tenant and origin.


Add `--structured` to `start` to request the versioned JSON brief and validate
facts against its declared source IDs. A restarted worker remembers the saved
format. `resume --progress` writes matching event IDs to stderr and the final
JSON to stdout. Missing CRM data is explicit; malformed results return exit 2
with `invalid_result`, without another submission. Receipt owner, object,
lifecycle and evidence references appear separately in `actions`. A completed
answer with a failed or unavailable receipt still returns exit 2.

The installed example also supports explicit `approve` and `deny` commands with
`--request` and `--decision-key`. They re-fetch the current owner request;
Platform checks the operator's authorization. Observation never approves work.
See the [complete application guide](https://www.deixic.com/developers/sdk/account-brief)
for the trigger/worker integration, result format, approval decisions, receipt
interpretation and tested recovery cases. The private-file storage example
requires a POSIX filesystem supporting atomic rename, hard links and fsync;
hosted applications use their existing durable storage and job queue.

## Submit and recover a final result

Create a **Deixic tasks** token in Settings → API access and keep it in your
server-side secret manager. Set `DEIXIC_API_KEY`, `DEIXIC_ORGANIZATION_ID`, and
`DEIXIC_WORKSPACE_ID` for that tenant. `DEIXIC_BASE_URL` defaults to
`https://app.deixic.com`; use an explicit URL for a test environment.

The installed package includes a runnable example:

```sh
python -m deixic.examples.task_result start task.json \
  --channel company --body 'Review the open changes.'
```

It saves the request body, tenant, Platform URL, and idempotency key before
submission. After acceptance it saves the turn ID and replay cursor, then
backfills events, watches progress, and fetches the final assistant message
linked to that turn and any referenced receipts. Preliminary answers and
another turn's completion do not establish success. Exit code 0 means the
matching turn completed; code 2 means unfinished, failed, interrupted, or
unacknowledged; code 1 reports an SDK error.

After a process restart or stream closure, resume observation:

```sh
python -m deixic.examples.task_result resume task.json
```

`resume` never submits work. It uses the saved cursor, and replaces its turn
projection from the owner snapshot when retention requires a reset. A stream
closing without matching completion remains `unfinished/watch_eof`.

If the submission response was lost, the checkpoint may have no accepted
turn ID. Explicitly replay the original saved request:

```sh
python -m deixic.examples.task_result replay task.json
```

Replay reuses the original request body and idempotency key; it refuses an
already accepted checkpoint. Do not start again with a new key to recover the
same request. A checkpoint cannot be moved to a different tenant or Platform
URL. Its file permissions are restricted to the current user, it contains no
credential, and it must still be protected because it contains the task body.
Use one process per checkpoint. This example does not run tools or approve
actions automatically; waiting work retains the owner's state.

### Verify against a real test tenant

The package also includes `deixic.examples.verify_test_journey`. Supply an
explicit dedicated test tenant, its task token, `DEIXIC_BASE_URL`,
`DEIXIC_IDENTITY_API_KEY_VALIDATE_URL` (Identity's `/v1/api-keys/validate`),
and `DEIXIC_TEST_CHANNEL_ID`, in addition to the organization/workspace
variables above. The probe self-introspects the supplied key with Identity
and requires task read/write grants in the expected organization. Platform
still owns workspace authorization and execution; the probe grants no access.

```sh
python -m deixic.examples.verify_test_journey submit journey.json
# The first process exits after acceptance. Start a separate recovery process:
python -m deixic.examples.verify_test_journey resume journey.json
```

The second process reloads the checkpoint, creates a new client, and verifies
the matching turn's final linked message against a unique harmless request.
Its receipt omits the token, prompt, and generated body. It returns nonzero for
unfinished work, denied Identity grants, or mismatched output. This opt-in
probe does not provision tenants or credentials, and must not be pointed at
a customer workspace. Running fixture tests does not establish this live proof.

For an automated run with that explicit configuration:

```sh
DEIXIC_REAL_JOURNEY=1 python -m pytest -q \
  sdk/deixic/python/tests/test_real_journey.py
```

That acceptance test launches submission and recovery in separate processes.
It is skipped in ordinary unit CI because it requires real test-tenant services.

The organization and workspace are fixed when the client is created. Every
request carries both values in the typed query and request headers. Mutations
require caller-owned idempotency keys. Mutation methods do not retry unavailable
or transport failures; task observation has the bounded read recovery described above.

Use API keys only from trusted server-side applications. Rotating workloads can
provide a `CredentialProvider`; an authentication replay is allowed only when
the refreshed credential retains the same subject, tenant, and declared
scopes.

## Workload federation

A CI job or cloud workload can authenticate without a stored API key. An
organization admin first registers an issuer, a service account, and a rule in
Identity settings, as described in the
[workload federation guide](https://github.com/dx-corp/mono/blob/main/docs/services/identity/workload-federation.md).
The workload then exchanges a signed assertion from its platform for a Deixic
access token that lasts at most 300 seconds:

```python
import os

from deixic import (
    Deixic,
    WorkloadFederationCredentialProvider,
    github_actions_assertion_source,
)

identity_url = os.environ["DEIXIC_IDENTITY_URL"]
credentials = WorkloadFederationCredentialProvider(
    identity_url=identity_url,
    assertion_source=github_actions_assertion_source(
        audience=f"{identity_url}/v1/workload-federation/exchange",
    ),
)
deixic = Deixic(
    credential_provider=credentials,
    organization_id="org_123",
    workspace_id="ws_456",
)
```

The provider exchanges an assertion on the first request and caches the token.
It exchanges again 60 seconds before expiry (`refresh_margin`) and after an
HTTP 401 from Deixic. Identity accepts each assertion once, so every exchange
calls the assertion source for a new assertion. The provider refuses to send
an assertion it has already exchanged and raises `DeixicError` with code
`workload_assertion_reused`. When an early refresh cannot obtain a new
assertion and the cached token has not expired, the provider keeps using the
cached token.

Assertion sources:

- `github_actions_assertion_source(audience)` requests a new OIDC token from
  GitHub Actions on each call. The job needs `permissions: id-token: write`.
- `file_assertion_source(path)` reads a file on each call, such as a Kubernetes
  projected service-account token. The kubelet rewrites that file after 80% of
  the token's `expirationSeconds`. A file token can therefore be exchanged once
  per rotation. Set `expirationSeconds` so that rotation happens more often
  than the 300-second Deixic token lifetime, or pass a callable that requests
  a new token from the Kubernetes TokenRequest API.
- `environment_assertion_source(name)` reads an environment variable on each
  call. The application must write a new value before each exchange.
- Any zero-argument callable that returns a new JWT string.

Exchange failures raise `DeixicError`:

| HTTP status | `kind` | `code` | Retried |
| --- | --- | --- | --- |
| 400 | `validation` | `workload_assertion_invalid` | No |
| 403 | `authorization` | `workload_federation_forbidden` | No |
| 409 | `conflict` | `workload_assertion_replayed` | No |
| 503 | `unavailable` | `workload_federation_unavailable` | Yes |
| Transport failure | `transport` | `workload_exchange_transport` | Yes |

A retry waits `retry_delay` seconds, doubled on each attempt, up to
`max_attempts` total attempts (default 3). Each retry uses a new assertion.
A 403 means no active rule matched the assertion's issuer, audience, subject,
and claims. The provider never logs the assertion or the access token and
omits both from error messages.

## Coding output readback

`messages.send(coding_acceptance=contract)` sends the typed coding contract and
marks the request as coding implementation. `contract.output_paths` can select
up to eight UTF-8 files whose contents differ from the admitted baseline (or
are newly added), totaling at most 64 KiB, for native completion
capture. Completion reads those files from the clean committed Git revision;
caller-provided output bytes are not a completion input.

Native validation authenticates the candidate commit and trees, then compares
all tracked physical bytes and executable modes with committed blobs. It does
not trust Git's cached clean status or run clean filters. Paths must be UTF-8;
symlinks must resolve into the authenticated tracked tree. Submodules, dangling
or external symlinks, and missing local Git objects fail validation. Missing
objects are not fetched during validation.

This check supports up to 100,000 tracked entries and 100,000 tree objects,
256 directory levels, 64 MiB per tracked blob, and 512 MiB of tracked content.
Commit objects are limited to 1 MiB and individual tree objects to 16 MiB.
These source-verification limits also apply when `output_paths` is omitted;
output publication remains optional. The separate 64 KiB output limit applies
to the new output bytes, so a larger baseline file may shrink to a valid output.

When Platform accepts the runtime's coding proof, the work receipt can contain
`coding_acceptance`, including its work/run, originating actor, commit, and
immutable VFS version and SHA-256 for each output. Request the bytes explicitly:

```python
receipt = deixic.receipts.get(
    channel_id=channel_id,
    receipt_id=receipt_id,
    include_coding_output_content=True,
).receipt
```

The read requires the originating actor and exact tenant scope. Ordinary thread
and receipt responses omit output bytes. A consumer must compare the returned
bytes against its own acceptance criteria; a completed turn or an artifact
reference alone does not prove the requested result.

These APIs do not provision execution capabilities. The hosted Operating runner
currently grants external Computer tools and excludes native `coding_task` and
Bash. Its Computer sandbox and the native coding checkout are separate owners;
this output contract alone does not make hosted coding qualification available.

## Recovery

Start with `deixic.threads.get()`, retain `replay_cursor`, and backfill with
`deixic.events.list()`. If an event page sets `reset_required`, replace the
local projection with its supplied snapshot and authoritative
`thread_execution.replay_cursor`. `deixic.events.watch()` yields bounded protobuf pages from one
server stream; the caller decides reconnection and resumes from the last saved
cursor.

## Public protocol boundary

This package includes only the `deixicpublic.v1.DeixicPublicService` contract
and its standard protobuf dependencies. Read responses expose public thread,
message, event, setup, and receipt projections. Receipt evidence uses public
resource references; service ownership and internal execution records are not
part of this contract. Pagination uses page tokens.

Existing `deixic.task.v1` checkpoints remain readable, including accepted turn
IDs and decimal-string cursors. Their `channelId` identifies the public thread.
Python message types are available from `deixic.protocol`; TypeScript exports
public message types and schemas from the package root. Existing TypeScript
operating-type names are aliases of these public types.
