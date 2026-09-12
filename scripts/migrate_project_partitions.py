"""Copy legacy PROJECT# records into organization-scoped partitions; never delete sources."""
import argparse

import boto3
from botocore.exceptions import ClientError

TABLE_SUFFIXES = ["projects", "parties", "field-operations", "site-control", "materials-logistics", "document-control", "inventory", "finance", "settings"]


def migrate(db, prefix, apply=False):
    counts = {"copied": 0, "pending": 0, "existing": 0, "conflicts": 0}
    projects = db.Table(f"{prefix}-projects")
    for suffix in TABLE_SUFFIXES:
        table = db.Table(f"{prefix}-{suffix}")
        options = {}
        while True:
            page = table.scan(**options)
            for item in page.get("Items", []):
                if not item.get("PK", "").startswith("PROJECT#"):
                    continue
                org_id = item.get("orgId")
                project_id = item["PK"].removeprefix("PROJECT#")
                parent = projects.get_item(Key={"PK": f"ORG#{org_id}", "SK": f"PROJECT#{project_id}"}, ConsistentRead=True).get("Item")
                if not org_id or not parent or parent.get("orgId") != org_id:
                    counts["conflicts"] += 1
                    continue
                target = {**item, "PK": f"ORG#{org_id}#PROJECT#{project_id}"}
                key = {"PK": target["PK"], "SK": target["SK"]}
                existing = table.get_item(Key=key, ConsistentRead=True).get("Item")
                if existing:
                    counts["existing" if existing == target else "conflicts"] += 1
                elif not apply:
                    counts["pending"] += 1
                else:
                    try:
                        table.put_item(Item=target, ConditionExpression="attribute_not_exists(PK)")
                        counts["copied"] += 1
                    except ClientError as exc:
                        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                            raise
                        counts["conflicts"] += 1
            if not page.get("LastEvaluatedKey"):
                break
            options["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument("--table-prefix", default="kinetic-v2")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = migrate(boto3.resource("dynamodb", region_name=args.region), args.table_prefix, args.apply)
    print(result)
    raise SystemExit(1 if result["conflicts"] else 0)
