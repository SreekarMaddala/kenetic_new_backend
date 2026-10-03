import json
from decimal import Decimal
from typing import Any, Dict, Optional


class DecimalEncoder(json.JSONEncoder):
    """Custom JSON encoder to handle Decimal types from DynamoDB."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super().default(obj)


def success_response(data: Any, status_code: int = 200, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Generates a JSON success response. API Gateway owns CORS."""
    default_headers = {
        "Content-Type": "application/json",
    }
    if headers:
        default_headers.update(headers)

    body = {
        "success": True,
        "data": data,
    }

    return {
        "statusCode": status_code,
        "headers": default_headers,
        "body": json.dumps(body, cls=DecimalEncoder),
    }


def error_response(message: str, status_code: int = 400, error_code: Optional[str] = None, details: Any = None) -> Dict[str, Any]:
    """Generates a JSON error response. API Gateway owns CORS."""
    headers = {
        "Content-Type": "application/json",
    }

    body: Dict[str, Any] = {
        "success": False,
        "error": {
            "message": message,
        },
    }
    if error_code:
        body["error"]["code"] = error_code
    if details:
        body["error"]["details"] = details

    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body, cls=DecimalEncoder),
    }
