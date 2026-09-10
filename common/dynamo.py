import os
import boto3
from decimal import Decimal
from typing import Any, Dict, List, Optional
from boto3.dynamodb.conditions import Key, Attr


def get_dynamo_resource():
    """Returns boto3 DynamoDB resource."""
    endpoint_url = os.environ.get("DYNAMODB_ENDPOINT_OVERRIDE")
    if endpoint_url:
        return boto3.resource("dynamodb", endpoint_url=endpoint_url)
    return boto3.resource("dynamodb")


def get_table(table_env_var: str):
    """Retrieves DynamoDB Table object from environment variable name."""
    table_name = os.environ.get(table_env_var)
    if not table_name:
        raise ValueError(f"Environment variable '{table_env_var}' is not configured.")
    dynamo = get_dynamo_resource()
    return dynamo.Table(table_name)


def convert_floats_to_decimals(obj: Any) -> Any:
    """Recursively converts float values to Decimal for DynamoDB serialization."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: convert_floats_to_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_floats_to_decimals(v) for v in obj]
    return obj


def parse_body(event: Dict[str, Any]) -> Dict[str, Any]:
    """Parses and sanitizes request body from API Gateway event."""
    import json
    body = event.get("body")
    if not body:
        return {}
    if isinstance(body, dict):
        return convert_floats_to_decimals(body)
    try:
        data = json.loads(body)
        return convert_floats_to_decimals(data)
    except Exception as e:
        raise ValueError(f"Invalid JSON payload: {str(e)}")
