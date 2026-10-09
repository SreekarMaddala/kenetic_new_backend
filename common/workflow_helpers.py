"""Shared record reads, values and identifiers for business workflows."""
from boto3.dynamodb.conditions import Key
from decimal import Decimal, ROUND_HALF_UP
from common.authz import SUPERVISOR
from datetime import datetime, timedelta, timezone
from common.dynamo import get_table
import time


class HttpError(Exception):
    def __init__(self, status, payload):
        super().__init__(payload['error']['message'])
        self.status, self.payload = status, payload


def now():
    return datetime.now(timezone.utc).isoformat()


def logistics_month():
    return datetime.fromisoformat(now()).astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m")


def logistics_visible(identity, item):
    if SUPERVISOR not in identity.roles or item.get("tripType") == "vehicle_registration":
        return True
    return str(item.get("date", ""))[:7] == logistics_month()


def rows(table, pk=None, prefix=None, org=None):
    if pk:
        args = {"KeyConditionExpression": Key("PK").eq(pk), "ConsistentRead": True}
        if prefix:
            args['KeyConditionExpression'] &= Key('SK').begins_with(prefix)
    else:
        args = {'IndexName': 'OrgIndex', 'KeyConditionExpression': Key('OrgPK').eq(f'ORG#{org}')}
        if prefix:
            args['KeyConditionExpression'] &= Key('OrgSK').begins_with(prefix)
    result = []
    while True:
        page = table.query(**args)
        items = page.get('Items', [])
        if not pk:
            # Hydrate compact KEYS_ONLY indexes with bounded, consistent reads.
            keys = [{key: item[key] for key in ('PK', 'SK')} for item in items]
            result.extend(_batch_get(table, keys).values())
        else:
            result.extend(items)
        if not page.get("LastEvaluatedKey"):
            return result
        args["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def clean(item):
    return {k: v for k, v in item.items() if k not in {"PK", "SK", "OrgPK", "OrgSK", "DirectoryPK", "DirectorySK", "ApprovalPK", "ApprovalSK", "entityType"}}


def number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{label} must be a number")
    if not Decimal(value).is_finite() or value < 0 or (positive and value == 0):
        raise ValueError(f"{label} must be {'positive' if positive else 'non-negative'}")
    return value


def daily_wage(rate, status, night_shift=False):
    fraction = {"Present": Decimal("1"), "Half Day": Decimal("0.5"), "Absent": Decimal("0")}[status]
    return (Decimal(rate) * fraction * (2 if night_shift else 1)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def material_kind(item):
    # Old requests can be identified without exposing them as stock.
    return item.get("recordKind") or ("indents" if "materialName" in item or "requiredDate" in item else "grn")


def _batch_get(table, keys):
    if not keys:
        return {}
    client = getattr(getattr(table, 'meta', None), 'client', None)
    if client is None:
        return {(
            item.get('PK'), item.get('SK')): item for item in (table.get_item(Key=key, ConsistentRead=True).get('Item') for key in keys) if item}
    result = {}
    keys = list({(key['PK'], key['SK']): key for key in keys}.values())
    for start in range(0, len(keys), 100):
        pending = {'Keys': keys[start:start + 100], 'ConsistentRead': True}
        attempts = 0
        while pending:
            batch = client.batch_get_item(RequestItems={table.name: pending})
            for item in batch.get('Responses', {}).get(table.name, []):
                result[(item['PK'], item['SK'])] = item
            pending = batch.get('UnprocessedKeys', {}).get(table.name, {})
            attempts += 1
            if pending and attempts >= 5:
                raise ValueError('Database reads are throttled. Try again.')
            if pending:
                time.sleep(0.025 * 2 ** (attempts - 1))
    return result


def assignment_names(ids, org):
    ids = [str(i) for i in ids if i]
    if not ids:
        return ""
    table = get_table("USERS_TABLE")
    keys = [{"PK": f"ORG#{org}", "SK": f"USER#{user_id}"} for user_id in ids]
    lookup = _batch_get(table, keys)
    return ", ".join(lookup.get((f"ORG#{org}", f"USER#{user_id}"), {}).get("name", user_id) for user_id in ids)


def re_id(value):
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value))
