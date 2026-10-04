"""Append-only mutation history, committed alongside domain writes."""
import uuid
from datetime import datetime, timezone

from common.dynamo import get_table


def operation(identity, action, before=None, after=None, reason=None):
    record = after or before
    stamp = datetime.now(timezone.utc).isoformat()
    event_id = uuid.uuid4().hex
    event = {
        "PK": f"ORG#{record['orgId']}", "SK": f"AUDIT#{stamp}#{event_id}",
        "auditEventId": event_id, "entityType": "audit-event", "orgId": record["orgId"],
        "projectId": record.get("projectId"), "actorId": identity.user_id,
        "action": action, "createdAt": stamp,
        "recordKey": {key: record[key] for key in ("PK", "SK")},
        "before": before, "after": after,
    }
    if reason:
        event["reason"] = reason
    return {"Put": {"TableName": get_table("AUDIT_EVENTS_TABLE").name, "Item": event,
                    "ConditionExpression": "attribute_not_exists(PK)"}}


def put(table, identity, item, before=None, action=None, **conditions):
    if not conditions:
        version = (before or {}).get("version")
        item["version"] = (version or 0) + 1
        conditions = {"ConditionExpression": "attribute_not_exists(#v)" if version is None else "#v = :v",
                      "ExpressionAttributeNames": {"#v": "version"}}
        if version is not None:
            conditions["ExpressionAttributeValues"] = {":v": version}
    write = {"TableName": table.name, "Item": item, **conditions}
    table.meta.client.transact_write_items(TransactItems=[
        {"Put": write}, operation(identity, action or f"{item['entityType']}.{'updated' if before else 'created'}", before, item),
    ])
