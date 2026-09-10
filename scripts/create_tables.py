"""Create the 12 Kinetic ERP v2 DynamoDB tables.

Examples:
  # DynamoDB Local
  python scripts/create_tables.py --endpoint-url http://localhost:8000

  # AWS (explicit acknowledgement required)
  python scripts/create_tables.py --region ap-south-1 --confirm-aws
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError


TABLE_NAMES = (
    "kinetic-v2-organizations",
    "kinetic-v2-users",
    "kinetic-v2-projects",
    "kinetic-v2-parties",
    "kinetic-v2-field-operations",
    "kinetic-v2-site-control",
    "kinetic-v2-materials-logistics",
    "kinetic-v2-document-control",
    "kinetic-v2-inventory",
    "kinetic-v2-finance",
    "kinetic-v2-settings",
    "kinetic-v2-audit-events",
)


def table_definition(name: str) -> dict[str, Any]:
    """All v2 tables use the same tenant/project scoped PK/SK schema."""
    return {
        "TableName": name,
        "BillingMode": "PAY_PER_REQUEST",
        "AttributeDefinitions": [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        "SSESpecification": {"Enabled": True},
        "Tags": [
            {"Key": "Application", "Value": "kinetic-erp"},
            {"Key": "Architecture", "Value": "v2-12-domain"},
        ],
    }


def exists(client: Any, name: str) -> bool:
    try:
        client.describe_table(TableName=name)
        return True
    except client.exceptions.ResourceNotFoundException:
        return False


def create_tables(client: Any, wait: bool) -> None:
    created: list[str] = []
    for name in TABLE_NAMES:
        if exists(client, name):
            if wait:
                client.update_continuous_backups(
                    TableName=name,
                    PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
                )
            print(f"exists   {name}")
            continue
        client.create_table(**table_definition(name))
        if wait:
            # CreateTable is asynchronous. A table must be ACTIVE before
            # DynamoDB accepts the separate PITR configuration request.
            client.get_waiter("table_exists").wait(TableName=name)
            client.update_continuous_backups(
                TableName=name,
                PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
            )
        else:
            print(f"pending PITR  {name}")
        created.append(name)
        print(f"created  {name}")

    if wait and created:
        waiter = client.get_waiter("table_exists")
        for name in created:
            waiter.wait(TableName=name)
            print(f"active   {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create Kinetic ERP v2 DynamoDB tables")
    parser.add_argument("--region", default="ap-south-1", help="AWS region (default: ap-south-1)")
    parser.add_argument("--endpoint-url", help="DynamoDB Local or custom endpoint URL")
    parser.add_argument("--no-wait", action="store_true", help="Do not wait for new AWS tables to become ACTIVE")
    parser.add_argument("--confirm-aws", action="store_true", help="Required when creating tables in AWS")
    args = parser.parse_args()

    if not args.endpoint_url and not args.confirm_aws:
        parser.error("Refusing to create AWS resources. Pass --confirm-aws after checking the target account and region.")

    client = boto3.client(
        "dynamodb",
        region_name=args.region,
        endpoint_url=args.endpoint_url,
        config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2, "mode": "standard"}),
    )
    try:
        create_tables(client, wait=not args.no_wait)
    except EndpointConnectionError:
        target = args.endpoint_url or f"DynamoDB in {args.region}"
        print(f"Could not reach {target}. Check your internet/VPN, AWS region, or --endpoint-url.", file=sys.stderr)
        return 1
    except ClientError as exc:
        print(f"DynamoDB request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
