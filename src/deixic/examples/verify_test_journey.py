"""Two-process Identity/Platform acceptance probe for an explicitly supplied test tenant."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from deixic import Deixic, DeixicError
from deixic.transport import RequestsTransport

from . import task_result


REQUIRED = (
    "DEIXIC_API_KEY", "DEIXIC_ORGANIZATION_ID", "DEIXIC_WORKSPACE_ID",
    "DEIXIC_BASE_URL", "DEIXIC_IDENTITY_API_KEY_VALIDATE_URL", "DEIXIC_TEST_CHANNEL_ID",
)


def configuration() -> dict[str, str]:
    missing = [name for name in REQUIRED if not os.environ.get(name, "").strip()]
    if missing:
        raise ValueError("Missing test-tenant configuration: " + ", ".join(missing))
    values = {name: os.environ[name].strip() for name in REQUIRED}
    for name in ("DEIXIC_BASE_URL", "DEIXIC_IDENTITY_API_KEY_VALIDATE_URL"):
        parsed = urlsplit(values[name])
        if (parsed.scheme not in ("https", "http") or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"))):
            raise ValueError(name + " must use HTTPS or a loopback HTTP endpoint")
    return values


def validate_identity(values: dict[str, str], transport=None) -> str:
    # Introspect only the supplied key. Identity owns validity, grants, and org scope.
    response = (transport or RequestsTransport()).send(
        "POST", values["DEIXIC_IDENTITY_API_KEY_VALIDATE_URL"],
        headers={"Authorization": "Bearer " + values["DEIXIC_API_KEY"],
                 "Content-Type": "application/json"},
        body=json.dumps({"key": values["DEIXIC_API_KEY"]}).encode(), timeout=20,
    )
    try:
        if response.status_code != 200:
            raise ValueError("Identity key validation failed")
        document = response.json()
        key = document.get("api_key", {})
        scopes = set(key.get("scopes", []))
        if (document.get("active") is not True
                or key.get("organization_id") != values["DEIXIC_ORGANIZATION_ID"]
                or not key.get("id")
                or not ({"deixic:read", "console:read"} & scopes)
                or not ({"deixic:write", "console:write"} & scopes)):
            raise ValueError("Identity did not validate the required test-tenant key grants")
        return key["id"]
    finally:
        response.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("submit", "resume"))
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    try:
        values = configuration()
        key_id = validate_identity(values)
        coordinates = dict(organization_id=values["DEIXIC_ORGANIZATION_ID"],
                           workspace_id=values["DEIXIC_WORKSPACE_ID"],
                           base_url=values["DEIXIC_BASE_URL"])
        client = Deixic(api_key=values["DEIXIC_API_KEY"], **coordinates)
        if args.phase == "submit":
            marker = "SDK-journey-" + str(uuid.uuid4())
            state = task_result.prepare(args.state, **coordinates,
                channel_id=values["DEIXIC_TEST_CHANNEL_ID"],
                body="Reply with exactly this text and no other content: " + marker)
            state["expected_text"] = marker
            state["identity_key_id"] = key_id
            task_result.save(args.state, state)
            task_result.submit(client, args.state, state)
            # Process one exits after acceptance; it does not watch or claim success.
            print(json.dumps(dict(status="accepted", turn_id=state["turn_id"])))
            return 0
        state = task_result.load(args.state, **coordinates)
        if state.get("identity_key_id") != key_id:
            raise ValueError("The recovery process must use the same Identity key")
        outcome = task_result.resume(client, args.state, state)
        if outcome["status"] != "completed":
            print(json.dumps({key: value for key, value in outcome.items() if key != "body"}))
            return 2
        if outcome["body"].strip() != state["expected_text"]:
            raise ValueError("Final owner-linked message did not match this probe's unique request")
        print(json.dumps(dict(status="completed", turn_id=outcome["turn_id"],
                              message_id=outcome["message_id"], receipt_ids=outcome["receipt_ids"],
                              identity_key_id=key_id, restarted_client=True)))
        return 0
    except (ValueError, DeixicError) as error:
        # Do not include owner payloads, tokens, prompts, or generated bodies in receipts.
        print(json.dumps(dict(status="error", kind=error.kind if isinstance(error, DeixicError)
                               else "validation")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
