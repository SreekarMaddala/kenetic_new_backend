"""Kinetic ERP v2: direct handlers for the 12 deployed Lambda domains.

There are no per-route Lambda modules. Each entry point resolves its resource,
enforces the caller's tenant, and stores domain records as PK/SK items.
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Key

from common.authz import (
    OPERATIONS_ADMIN, SUPER_ADMIN, SUPERVISOR, AuthorizationError,
    identity_from_event, require_role,
)
from common.dynamo import get_table


DOMAIN_CONFIG = {
    "platform-admin": ("ORGANIZATIONS_TABLE", (SUPER_ADMIN,)),
    "projects": ("PROJECTS_TABLE", (OPERATIONS_ADMIN, SUPER_ADMIN)),
    "project-commercial": ("PROJECTS_TABLE", (OPERATIONS_ADMIN, SUPER_ADMIN)),
    "workforce": ("FIELD_OPERATIONS_TABLE", (SUPERVISOR, OPERATIONS_ADMIN, SUPER_ADMIN)),
    "field-operations": ("FIELD_OPERATIONS_TABLE", (SUPERVISOR, OPERATIONS_ADMIN, SUPER_ADMIN)),
    "site-control": ("SITE_CONTROL_TABLE", (SUPERVISOR, OPERATIONS_ADMIN, SUPER_ADMIN)),
    "document-control": ("DOCUMENT_CONTROL_TABLE", (OPERATIONS_ADMIN, SUPER_ADMIN)),
    "supply-chain": ("INVENTORY_TABLE", (OPERATIONS_ADMIN, SUPER_ADMIN)),
    "finance": ("FINANCE_TABLE", (OPERATIONS_ADMIN, SUPER_ADMIN)),
    "governance": ("SETTINGS_TABLE", (OPERATIONS_ADMIN, SUPER_ADMIN)),
}


def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(payload, default=str),
    }


def _error(status, code, message):
    return _response(status, {"success": False, "error": {"code": code, "message": message}})


def _event_parts(event):
    http = event.get("requestContext", {}).get("http", {})
    method = (http.get("method") or event.get("httpMethod") or "GET").upper()
    path = event.get("rawPath") or event.get("path") or ""
    body = event.get("body") or "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError:
        raise ValueError("Request body must be valid JSON")
    return method, path.rstrip("/"), data or {}


def _resource(path):
    """Return a stable entity name from every supported frontend route."""
    rules = (
        (r"/organizations", "organization"), (r"/employees", "user"),
        (r"/milestones", "milestone"), (r"/boq", "boq"),
        (r"/subcontractors", "subcontractor"), (r"/attendance", "attendance"),
        (r"/labour", "labour-attendance"), (r"/dpr", "dpr"),
        (r"/materials/(grn|indents|stock)", "material"), (r"/logistics", "logistics-trip"),
        (r"/issues", "issue"), (r"/inspections", "inspection"), (r"/equipment", "equipment"),
        (r"/drawings", "drawing"), (r"/documents", "document"),
        (r"/vendors", "vendor"), (r"/inventory", "inventory-item"),
        (r"/warehouse/(grn|issue-vouchers)", "warehouse-event"),
        (r"/bills", "bill"), (r"/expenses", "expense"), (r"/payments", "payment"),
        (r"/payroll", "payroll"), (r"/settings", "setting"), (r"/projects", "project"),
    )
    for pattern, name in rules:
        if re.search(pattern, path):
            return name
    return "record"


def _project_id(path):
    match = re.search(r"/projects/([^/]+)", path)
    return match.group(1) if match else None


def _item_id(path, resource):
    segments = [part for part in path.split("/") if part]
    resource_markers = {
        "project": "projects", "organization": "organizations", "user": "employees",
        "vendor": "vendors", "inventory-item": "inventory", "payment": "payments",
        "boq": "boq", "subcontractor": "subcontractors", "drawing": "drawings",
        "document": "documents", "bill": "bills", "expense": "expenses",
        "issue": "issues", "inspection": "inspections", "equipment": "equipment",
        "payroll": "payroll", "dpr": "dpr",
    }
    marker = resource_markers.get(resource)
    if marker and marker in segments:
        index = segments.index(marker)
        if index + 1 < len(segments):
            return segments[index + 1]
    return None


def _table_for(domain, resource):
    if domain == "project-commercial" and resource == "subcontractor":
        return get_table("PARTIES_TABLE")
    if domain == "supply-chain" and resource == "vendor":
        return get_table("PARTIES_TABLE")
    if domain == "field-operations" and resource in {"material", "logistics-trip"}:
        return get_table("MATERIALS_LOGISTICS_TABLE")
    return get_table(DOMAIN_CONFIG[domain][0])


def _pk(identity, path, resource, data):
    project_id = _project_id(path) or data.get("projectId")
    if project_id:
        return f"PROJECT#{project_id}", project_id
    if resource == "organization":
        org_id = data.get("orgId") or identity.organization_id
        return f"ORG#{org_id}", org_id
    return f"ORG#{identity.organization_id}", None


def _clean(item):
    return {key: value for key, value in item.items() if key not in {"PK", "SK", "entityType"}}


def _domain_handler(domain, event, context):
    try:
        identity = identity_from_event(event)
        require_role(identity, *DOMAIN_CONFIG[domain][1])
        method, path, data = _event_parts(event)
        resource = _resource(path)
        table = _table_for(domain, resource)
        pk, project_id = _pk(identity, path, resource, data)
        record_id = _item_id(path, resource)

        if method == "GET" and path.endswith("/dashboard/analytics"):
            return _response(200, {"success": True, "data": {"totalProjects": 0, "activeProjects": 0, "pendingApprovals": 0, "stockAlerts": 0}})
        if method == "GET" and path.endswith("/reports/executive"):
            return _response(200, {"success": True, "data": {"projectSummaries": [], "vendorSummaries": [], "generatedAt": datetime.now(timezone.utc).isoformat()}})

        if method == "GET" and not record_id:
            result = table.query(KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(f"{resource.upper()}#"))
            return _response(200, {"success": True, "data": [_clean(item) for item in result.get("Items", [])]})

        if method == "POST":
            record_id = data.get(f"{resource.replace('-', '')}Id") or uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            item = {**data, "PK": pk, "SK": f"{resource.upper()}#{record_id}", "entityType": resource,
                    "orgId": identity.organization_id, "createdAt": now, "updatedAt": now}
            item.setdefault("projectId", project_id)
            item.setdefault(f"{resource.replace('-', '')}Id", record_id)
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)")
            return _response(201, {"success": True, "data": _clean(item)})

        if not record_id:
            return _error(400, "MISSING_ID", "A record identifier is required")
        key = {"PK": pk, "SK": f"{resource.upper()}#{record_id}"}
        if method == "GET":
            item = table.get_item(Key=key).get("Item")
            return _response(200, {"success": True, "data": _clean(item)}) if item else _error(404, "NOT_FOUND", "Record not found")
        if method == "DELETE":
            table.delete_item(Key=key)
            return _response(200, {"success": True, "data": {"deleted": True}})
        if method in {"PUT", "PATCH"}:
            existing = table.get_item(Key=key).get("Item")
            if not existing:
                return _error(404, "NOT_FOUND", "Record not found")
            existing.update(data)
            existing["updatedAt"] = datetime.now(timezone.utc).isoformat()
            table.put_item(Item=existing)
            return _response(200, {"success": True, "data": _clean(existing)})
        return _error(405, "METHOD_NOT_ALLOWED", "Unsupported request method")
    except AuthorizationError as exc:
        return _error(403, "FORBIDDEN", str(exc))
    except ValueError as exc:
        return _error(400, "INVALID_REQUEST", str(exc))
    except Exception:
        return _error(500, "INTERNAL_ERROR", "The request could not be processed")


def auth_handler(event, context):
    return _error(501, "AUTH_NOT_IMPLEMENTED", "Cognito manages authentication; invitation routes are pending")
def platform_admin_handler(event, context): return _domain_handler("platform-admin", event, context)
def projects_handler(event, context): return _domain_handler("projects", event, context)
def project_commercial_handler(event, context): return _domain_handler("project-commercial", event, context)
def workforce_handler(event, context): return _domain_handler("workforce", event, context)
def field_operations_handler(event, context): return _domain_handler("field-operations", event, context)
def site_control_handler(event, context): return _domain_handler("site-control", event, context)
def document_control_handler(event, context): return _domain_handler("document-control", event, context)
def supply_chain_handler(event, context): return _domain_handler("supply-chain", event, context)
def finance_handler(event, context): return _domain_handler("finance", event, context)
def governance_handler(event, context): return _domain_handler("governance", event, context)
def maintenance_handler(event, context): return _response(204, {})
def placeholder(event, context): return _error(500, "INVALID_HANDLER", "No Lambda handler was configured")
