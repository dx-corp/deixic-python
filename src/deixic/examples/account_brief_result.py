"""Application-owned account-brief format; validation grants no source authority."""

from __future__ import annotations

import json

VERSION = "deixic.account-brief.v1"
FORMAT_INSTRUCTION = (
    ' Return only JSON with schemaVersion "deixic.account-brief.v1", accountName '
    "(the requested name), summary (null or {text, sourceIds}), opportunities "
    "([{name, stage, sourceIds}]), risks ([{description, sourceIds}]), sources "
    "([{id, system, resourceId}]), and missingData ([strings]). No extra fields "
    "or markdown. Every factual item must reference at least one sources.id. "
    "Use null summary and empty lists when facts are unavailable, explaining "
    "the missing data. Never invent records, references or opportunities."
)


def parse_account_brief(body: str) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate account-brief field")
            result[key] = value
        return result

    brief = json.loads(body, object_pairs_hook=pairs)

    def fields(value, names):
        if not isinstance(value, dict) or set(value) != set(names):
            raise ValueError("Invalid account-brief fields")

    def text(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Invalid account-brief text")

    def items(value):
        if not isinstance(value, list) or len(value) > 100:
            raise ValueError("Invalid account-brief list")
        return value

    fields(
        brief,
        (
            "schemaVersion",
            "accountName",
            "summary",
            "opportunities",
            "risks",
            "sources",
            "missingData",
        ),
    )
    if brief["schemaVersion"] != VERSION:
        raise ValueError("Unsupported account-brief version")
    text(brief["accountName"])
    ids = set()
    for source in items(brief["sources"]):
        fields(source, ("id", "system", "resourceId"))
        for value in source.values():
            text(value)
        if source["id"] in ids:
            raise ValueError("Duplicate account-brief source")
        ids.add(source["id"])

    def fact(value, names):
        fields(value, (*names, "sourceIds"))
        for name in names:
            text(value[name])
        refs = items(value["sourceIds"])
        if (
            not refs
            or any(not isinstance(ref, str) or ref not in ids for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise ValueError("Unlinked account-brief fact")

    if brief["summary"] is not None:
        fact(brief["summary"], ("text",))
    for item in items(brief["opportunities"]):
        fact(item, ("name", "stage"))
    for item in items(brief["risks"]):
        fact(item, ("description",))
    for value in items(brief["missingData"]):
        text(value)
    if brief["summary"] is None and not brief["missingData"]:
        raise ValueError("Unavailable summary must explain missing data")
    return brief
