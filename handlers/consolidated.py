"""Kinetic ERP: direct request handlers for the application's business domains.

There are no per-route Lambda modules. Each entry point resolves its resource,
enforces the caller's tenant, and stores domain records as PK/SK items.
"""

import json
import re
import logging
from decimal import Decimal
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from common.authz import AuthorizationError, OPERATIONS_ADMIN, SUPERVISOR, SUPER_ADMIN, active_identity, require_organization, require_role
from common.dynamo import convert_floats_to_decimals, get_table
from common.accounts import create_account, resend_invitation, update_account
from common import audit, contracts, finance, inventory, model, pagination, reporting, storage, workflow_helpers, workflow_validation, workflows, workforce


DOMAIN_CONFIG = {
    "platform-admin": ("ORGANIZATIONS_TABLE", (SUPER_ADMIN, OPERATIONS_ADMIN)),
    "projects": ("PROJECTS_TABLE", (SUPERVISOR, OPERATIONS_ADMIN, SUPER_ADMIN)),
    "project-commercial": ("PROJECTS_TABLE", (OPERATIONS_ADMIN, SUPER_ADMIN)),
    "workforce": ("FIELD_OPERATIONS_TABLE", (SUPERVISOR, OPERATIONS_ADMIN, SUPER_ADMIN)),
    "field-operations": ("FIELD_OPERATIONS_TABLE", (SUPERVISOR, OPERATIONS_ADMIN, SUPER_ADMIN)),
    "site-control": ("SITE_CONTROL_TABLE", (SUPERVISOR, OPERATIONS_ADMIN, SUPER_ADMIN)),
    "document-control": ("DOCUMENT_CONTROL_TABLE", (OPERATIONS_ADMIN, SUPER_ADMIN)),
    "supply-chain": ("INVENTORY_TABLE", (OPERATIONS_ADMIN, SUPER_ADMIN)),
    "finance": ("FINANCE_TABLE", (OPERATIONS_ADMIN, SUPER_ADMIN)),
    "governance": ("SETTINGS_TABLE", (OPERATIONS_ADMIN, SUPER_ADMIN)),
}

# Public API field names are intentionally explicit.  They must remain stable
# for the TypeScript clients rather than being derived from an internal entity
# name (for example, ``inventory-item`` is exposed as ``itemId``).
ID_FIELDS = {
    "organization": "orgId",
    "user": "employeeId",
    "project": "projectId",
    "milestone": "milestoneId",
    "boq": "boqId",
    "subcontractor": "subcontractorId",
    "attendance": "attendanceId",
    "labour-attendance": "labourAttendanceId",
    "dpr": "dprId",
    "material": "materialId",
    "logistics-trip": "tripId",
    "issue": "issueId",
    "inspection": "inspectionId",
    "equipment": "equipmentId",
    "drawing": "drawingId",
    "document": "documentId",
    "vendor": "vendorId",
    "inventory-item": "itemId",
    "warehouse-event": "warehouseEventId",
    "bill": "billId",
    "expense": "expenseId",
    "payment": "paymentId",
    "payroll": "cycleId",
    "setting": "settingKey",
    "record": "recordId",
}


def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(payload, default=lambda value: int(value) if isinstance(value, Decimal) and value == value.to_integral_value() else float(value) if isinstance(value, Decimal) else str(value)),
    }


def _error(status, code, message):
    return _response(status, {"success": False, "error": {"code": code, "message": message}})


def _event_parts(event):
    http = event.get("requestContext", {}).get("http", {})
    method = (http.get("method") or event.get("httpMethod") or "GET").upper()
    path = event.get("rawPath") or event.get("path") or ""
    stage = event.get("requestContext", {}).get("stage") or ""
    if stage and path.startswith(f"/{stage}"):
        path = path[len(stage) + 1:]
    if not path.startswith("/"):
        path = "/" + path
    body = event.get("body") or "{}"
    try:
        data = json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError:
        raise ValueError("Request body must be valid JSON")
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    return method, path.rstrip("/"), convert_floats_to_decimals(data)


def _resource(path):
    """Return a stable entity name from every supported frontend route."""
    segments = path.strip("/").split("/")
    # Identify the collection, never a user-controlled record ID later in the URL.
    if segments[0] == "super-admin":
        return {"organizations": "organization", "employees": "user"}.get(segments[1], "record")
    if segments[0] == "employees":
        return "user"
    if segments[0] == "projects":
        if len(segments) <= 2 or segments[2] in {"status", "invitation"}:
            return "project"
        path = "/" + segments[2]
    elif segments[0] == "supervisor":
        path = "/" + "/".join(segments[1:3] if segments[1] == "materials" else segments[1:2])
    elif segments[0] == "warehouse":
        path = "/" + "/".join(segments[:2])
    else:
        path = "/" + segments[0]
    rules = (
        (r"/organizations", "organization"), (r"/employees", "user"),
        (r"/milestones", "milestone"), (r"/boq", "boq"),
        (r"/subcontractors", "subcontractor"), (r"/labour", "labour-attendance"),
        (r"/attendance", "attendance"), (r"/dpr", "dpr"),
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
        "milestone": "milestones", "document": "documents", "bill": "bills", "expense": "expenses",
        "issue": "issues", "inspection": "inspections", "equipment": "equipment",
        "payroll": "payroll", "dpr": "dpr",
    }
    marker = resource_markers.get(resource)
    if marker:
        index = 2 if segments[0] == "projects" and resource != "project" else 1 if segments[0] in {"super-admin", "supervisor"} else 0
        if index < len(segments) and segments[index] == marker and index + 1 < len(segments):
            return segments[index + 1]
    if resource == "material" and len(segments) >= 4 and segments[0] == "supervisor" and segments[1] == "materials" and segments[2] in {"grn", "indents", "stock"}:
        return segments[3]
    if resource == "logistics-trip" and len(segments) >= 4 and segments[0] == "supervisor" and segments[1] == "logistics" and segments[2] == "trips":
        return segments[3]
    if resource == "labour-attendance" and len(segments) >= 4 and segments[0] == "supervisor" and segments[1] == "labour" and segments[2] == "attendance":
        return segments[3]
    if resource == "warehouse-event" and len(segments) >= 3 and segments[0] == "warehouse" and segments[1] in {"grn", "issue-vouchers"}:
        return segments[2]
    return None


def _id_field(resource):
    return ID_FIELDS.get(resource, "recordId")


def _table_for(domain, resource):
    if resource in {'attendance', 'labour-attendance'}:
        return get_table('WORKFORCE_TABLE')
    if resource == 'payroll':
        return get_table('PAYROLL_TABLE')
    if resource == 'logistics-trip':
        return get_table('TRANSPORT_TABLE')
    if resource == 'material':
        return get_table('MATERIALS_TABLE')
    if resource == "user":
        return get_table("USERS_TABLE")
    if domain == "project-commercial" and resource == "subcontractor":
        return get_table("PARTIES_TABLE")
    if domain == "supply-chain" and resource == "vendor":
        return get_table("PARTIES_TABLE")
    return get_table(DOMAIN_CONFIG[domain][0])


def _pk(identity, path, resource, data, query=None):
    query = query or {}
    org_id = query.get("orgId") or data.get("orgId") or identity.organization_id
    require_organization(identity, org_id)
    model.validate_id('organization', org_id)
    project_id = _project_id(path) or data.get("projectId")
    project_id = project_id or query.get("projectId")
    if (project_id):
        model.validate_id('project', project_id)
    if resource == "project":
        return f"ORG#{org_id}", project_id
    if project_id:
        project = get_table("PROJECTS_TABLE").get_item(
            Key={"PK": f"ORG#{org_id}", "SK": f"PROJECT#{project_id}"}, ConsistentRead=True,
        ).get("Item")
        if not project or project.get("orgId") != org_id:
            raise AuthorizationError("Project is not accessible")
        if SUPERVISOR in identity.roles and identity.user_id not in project.get("supervisorIds", []):
            raise AuthorizationError("You are not assigned to this project")
        return f"ORG#{org_id}#PROJECT#{project_id}", project_id
    if resource == "organization":
        org_id = _item_id(path, resource) or data.get("orgId") or identity.organization_id
        return f"ORG#{org_id}", org_id
    if SUPERVISOR in identity.roles:
        raise ValueError("A projectId is required")
    return f"ORG#{org_id}", None


def _clean(item):
    return {key: value for key, value in item.items() if key not in {"PK", "SK", "OrgPK", "OrgSK", "DirectoryPK", "DirectorySK", "ApprovalPK", "ApprovalSK", "entityType", "cognitoUsername", "cognitoSub"}}


def _all_items(table, **kwargs):
    items = []
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _check_route(domain, path, method):
    """Greedy API Gateway routes must not turn unknown URLs into arbitrary CRUD."""
    if domain == 'governance' and re.match(r'/projects/[^/]+/payroll(?:/|$)', path):
        match = re.fullmatch(r'/projects/[^/]+/payroll(?:/(me|staff|disburse|\d{4}-(?:0[1-9]|1[0-2])))?', path)
        if not match:
            return False
        operation = match.group(1)
        methods = {None: {'GET', 'POST'}, 'me': {'GET'}, 'staff': {'GET', 'POST'}, 'disburse': {'POST'}}
        return method in methods.get(operation, {'DELETE'})
    if domain == "projects" and method == "GET" and re.fullmatch(r"/projects/[^/]+/spending", path):
        return True
    roots = {
        "platform-admin": r"/(?:super-admin/(?:organizations|employees)|employees)",
        "projects": r"/projects",
        "project-commercial": r"/(?:subcontractors|projects/[^/]+/(?:boq|milestones|subcontractors))",
        "workforce": r"/supervisor/(?:attendance/(?:check-in|check-out|history)|labour/attendance)",
        "field-operations": r"/supervisor/(?:dpr|materials/(?:grn|indents|stock)|logistics/trips)",
        "site-control": r"/projects/[^/]+/(?:issues|inspections|equipment)",
        "document-control": r"/projects/[^/]+/(?:drawings|documents)",
        "supply-chain": r"/(?:vendors|inventory|warehouse/(?:grn|issue-vouchers))",
        "finance": r"/(?:payments|expenses|projects/[^/]+/(?:payments|bills|expenses))",
        "governance": r"/(?:settings|projects/[^/]+/payroll)",
    }
    if (domain, path, method) in {
        ("governance", "/dashboard/analytics", "GET"),
        ("governance", "/reports/executive", "GET"),
        ("platform-admin", "/super-admin/metrics", "GET"),
    }:
        return True
    suffix = r"(?:/[^/]+(?:/(?:status|invitation|download|extract|reverse))?)?"
    return bool(re.fullmatch(roots[domain] + suffix, path)) and method in {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _validate_data(resource, data, updating=False):
    immutable = {"PK", "SK", "OrgPK", "OrgSK", "DirectoryPK", "DirectorySK", "ApprovalPK", "ApprovalSK", "cognitoSub", "entityType", "createdAt", "updatedAt", "createdBy", "cognitoUsername", "version", "recordKind", "approvedBy", "approvedAt", "paidAmount"}
    if updating:
        immutable |= {"orgId", "projectId", _id_field(resource), "storageKey"}
    if set(data) & immutable:
        raise ValueError("Request contains protected fields")
    if not updating and resource in {"organization", "project", "vendor", "inventory-item"}:
        if not isinstance(data.get("name"), str) or not data["name"].strip():
            raise ValueError("Name is required")
    for field in {"budget", "amount", "grossAmount", "netPayable", "spent"} & data.keys():
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or not Decimal(value).is_finite() or value < 0:
            raise ValueError(f"{field} must be a non-negative number")
    if "supervisorIds" in data:
        ids = data["supervisorIds"]
        if resource != "project" or not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(ids) > 100:
            raise ValueError("supervisorIds must be a list of at most 100 account IDs")


def _validate_assignments(data, org_id):
    users_table = get_table("USERS_TABLE")
    ids = [user_id for user_id in data.get("supervisorIds", []) if user_id]
    lookup = workflow_helpers._batch_get(users_table, [{"PK": f"ORG#{org_id}", "SK": f"USER#{user_id}"} for user_id in ids])
    for user_id in ids:
        key = (f"ORG#{org_id}", f"USER#{user_id}")
        profile = lookup.get(key)


        if not profile:
            raise ValueError("Assignments must reference active supervisors in this organization")

        if str(profile.get("orgId", "")).lower() != str(org_id).lower():
            raise ValueError("Assignments must reference active supervisors in this organization")
        if str(profile.get("role", "")).lower() != SUPERVISOR:
            raise ValueError("Assignments must reference active supervisors in this organization")
        if str(profile.get("status", "")).lower() != "active":
            raise ValueError("Assignments must reference active supervisors in this organization")


def _attendance_location(body):
    location = body.get("location")
    if not isinstance(location, dict):
        raise ValueError("Allow location access before checking in or out")
    result = {}
    for field, low, high in [("latitude", -90, 90), ("longitude", -180, 180), ("accuracy", 0, None)]:
        value = location.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or not Decimal(value).is_finite() or value < low or (high is not None and value > high):
            raise ValueError(f"Invalid location {field}")
        result[field] = value
    return result


def _attendance(identity, table, pk, project_id, org_id, path, method, body):
    if method == "POST" and SUPERVISOR not in identity.roles:
        raise AuthorizationError("Only supervisors can check in or out")
    now = datetime.now(timezone.utc)
    record_id = f"{identity.user_id}#{now.date().isoformat()}"
    key = {"PK": pk, "SK": f"ATTENDANCE#{record_id}"}
    if method == "POST" and path.endswith("/check-in"):
        location = _attendance_location(body)
        item = {**key, "attendanceId": (model.new_id('attendance', f'{org_id}|{project_id}|{record_id}')), "entityType": "attendance", "orgId": org_id,
                "projectId": project_id, "supervisorId": identity.user_id, "createdBy": identity.user_id,
                "date": now.date().isoformat(), "checkIn": now.isoformat(), "createdAt": now.isoformat(),
                "checkInLocation": {**location, "recordedAt": now.isoformat()}}
        audit.put(table, identity, item, ConditionExpression="attribute_not_exists(PK)")
        return _response(201, {"success": True, "data": _clean(item)})
    if method == "POST" and path.endswith("/check-out"):
        location = _attendance_location(body)
        existing = table.get_item(Key=key, ConsistentRead=True).get("Item")
        if not existing or existing.get("orgId") != org_id:
            return _error(409, "NO_CHECK_IN", "Check in before checking out")
        seconds = int((now - datetime.fromisoformat(existing["checkIn"])).total_seconds())
        updated = dict(existing, checkOut=now.isoformat(), durationSeconds=seconds,
                       checkOutLocation={**location, "recordedAt": now.isoformat()})
        audit.put(table, identity, updated, before=existing, action="attendance.checked-out",
                  ConditionExpression="attribute_exists(checkIn) AND attribute_not_exists(checkOut) AND orgId = :org",
                  ExpressionAttributeValues={":org": org_id})
        return _response(200, {"success": True, "data": _clean(updated)})
    if method == "GET" and path.endswith("/history"):
        prefix = f"ATTENDANCE#{identity.user_id}#" if SUPERVISOR in identity.roles else "ATTENDANCE#"
        items = _all_items(table, KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(prefix))
        return _response(200, {"success": True, "data": [_clean(i) for i in items if i.get("orgId") == org_id]})
    return _error(405, "METHOD_NOT_ALLOWED", "Unsupported attendance operation")


def _collection_page(identity, table, resource, path, query, pk, org):
    prefix = resource.upper() + '#'
    directory = None
    query_pk = pk
    if resource in {'organization', 'user'} and SUPER_ADMIN in identity.roles and not query.get('orgId'):
        directory, query_pk = 'DIRECTORY#' + resource, None
    elif resource in {'expense', 'payment'} and '#PROJECT#' not in pk:
        query_pk, prefix = None, resource + '#'
    elif resource == 'logistics-trip':
        query_pk, prefix = None, resource + '#'
    elif resource == 'warehouse-event' or (resource == 'material' and ('/grn' in path or '/stock' in path)):
        table = get_table('INVENTORY_TABLE')
        if path.endswith('/stock'):
            prefix = 'BALANCE#'
        else:
            query_pk = 'ORG#' + org
            prefix = 'WAREHOUSE-ISSUE-VOUCHERS#' if 'issue-vouchers' in path else 'WAREHOUSE-GRN#'
    scope = [identity.user_id, org, path, sorted((k, v) for k, v in query.items() if k not in {'cursor', 'limit'})]
    items, meta = pagination.page(table, query, scope, pk=query_pk, prefix=prefix, org=None if directory else org, directory=directory)
    if not directory:
        items = [item for item in items if item.get('orgId') == org]
    if resource == 'material' and '/indents' in path:
        items = [item for item in items if workflow_helpers.material_kind(item) == 'indents']
    if resource == 'logistics-trip':
        items = [item for item in items if workflow_helpers.logistics_visible(identity, item) and (not query.get('projectId') or item.get('tripType') == 'vehicle_registration' or item.get('projectId') == query['projectId'])]
    if resource == 'project':
        if SUPERVISOR in identity.roles:
            items = [item for item in items if identity.user_id in item.get('supervisorIds', [])]
        items = reporting.project_totals(items, org)
    if path.endswith('/stock'):
        items = [dict(item, materialId=item['itemId'], stock=item.get('quantity', 0), totalStock=item.get('quantity', 0)) for item in items]
    if path.startswith('/supervisor/') and resource in {'warehouse-event', 'material'} and '/grn' in path:
        items = [item for item in items if item.get('targetProjectId') == pk.split('#PROJECT#')[-1]]
    if resource == 'inventory-item' and query.get('catalog') != 'true':
        for item in items:
            key = item['itemId']
            central = table.get_item(Key={'PK': pk, 'SK': 'BALANCE#' + key}, ConsistentRead=True).get('Item', {}).get('quantity', 0)
            total = table.get_item(Key={'PK': pk, 'SK': 'TOTAL#' + key}, ConsistentRead=True).get('Item', {}).get('quantity', 0)
            item.update(centralStock=central, totalStock=total, deployedStock=total - central)
    return _response(200, {'success': True, 'data': [_clean(item) for item in items], 'pagination': meta})


def _directory_rows(table, resource):
    cursor = None
    result = []
    while True:
        query = {'limit': '100'}
        if cursor:
            query['cursor'] = cursor
        items, meta = pagination.page(table, query, ['platform-metrics', resource], directory='DIRECTORY#' + resource)
        result.extend(items)
        cursor = meta['nextCursor']
        if not cursor:
            return result


def _domain_handler(domain, event, context):
    try:
        identity = active_identity(event)
        method, path, data = _event_parts(event)
        personal_salary = domain == "governance" and method == "GET" and re.fullmatch(r"/projects/[^/]+/payroll/me", path)
        if not personal_salary:
            require_role(identity, *DOMAIN_CONFIG[domain][1])
        if not _check_route(domain, path, method):
            return _error(404, "NOT_FOUND", "Unknown operation")
        if path.startswith("/super-admin/"):
            require_role(identity, SUPER_ADMIN)
        if domain == "projects" and method != "GET":
            require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        if SUPERVISOR in identity.roles and method == "DELETE":
            raise AuthorizationError("Supervisors cannot delete records")
        resource = _resource(path)
        if resource == "organization":
            require_role(identity, SUPER_ADMIN)
        table = _table_for(domain, resource)
        query = event.get("queryStringParameters") or {}
        contracts.validate(resource, path, method, data)
        request_data = dict(data)
        pk, project_id = _pk(identity, path, resource, data, query)
        org_id = query.get("orgId") or data.get("orgId") or identity.organization_id
        record_id = _item_id(path, resource)
        if (method == 'GET' and resource == 'labour-attendance'):
            from common import worker_views
            if record_id:
                return _response(200, {'success': True, 'data': worker_views.profile(identity, table, org_id, project_id, record_id, query)})
            day = str(query.get('date') or workflow_helpers.now()[:10])
            items, meta = pagination.page(table, query, [identity.user_id, org_id, path, project_id, day, query.get('roster')], pk='ORG#' + org_id, prefix='WORKER#')
            if project_id:
                roster = query.get('roster') == 'true'
                if roster:
                    require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
                items = workforce.labour_list(table, pk, day, roster, selected_workers=items)
                if SUPERVISOR in identity.roles:
                    items = [worker for worker in items if worker['allocatedProjectId'] == project_id]
            else:
                require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
            return _response(200, {'success': True, 'data': [_clean(worker_views.attendance_safe(item)) for item in items], 'pagination': meta})
        if path.endswith('/reverse') and resource in {'expense', 'payment'} and project_id:
            if method != 'POST':
                return _error(405, 'METHOD_NOT_ALLOWED', 'Use POST to reverse an approved transaction')
            existing = table.get_item(Key={'PK': pk, 'SK': f'{resource.upper()}#{record_id}'}, ConsistentRead=True).get('Item')
            if not existing or existing.get('orgId') != org_id:
                return _error(404, 'NOT_FOUND', 'Transaction not found')
            return _response(200, {'success': True, 'data': _clean(finance.reverse_financial(table, identity, resource, existing, data))})
        if domain == "projects" and path.endswith("/spending"):
            return _response(200, {"success": True, "data": reporting.spending_ledger(identity, org_id, project_id)})
        if (method == 'GET' and (not record_id) and (resource in {'organization', 'user', 'project', 'boq', 'milestone', 'subcontractor', 'dpr', 'material', 'logistics-trip', 'issue', 'inspection', 'equipment', 'document', 'drawing', 'vendor', 'inventory-item', 'warehouse-event', 'bill', 'expense', 'payment'})):
            return _collection_page(identity, table, resource, path, query, pk, org_id)
        special = workflows.handle(identity, domain, resource, method, path, data, query, pk, project_id, org_id, record_id)
        if special is not None:
            return _response(200, {"success": True, "data": special})
        workflow_validation.validate(identity, resource, method, path, data)
        if resource == "attendance":
            _validate_data(resource, data)
            return _attendance(identity, table, pk, project_id, org_id, path, method, data)
        if method == "GET" and path in {"/dashboard/analytics", "/reports/executive"}:
            period = reporting.report_period(query) if path == "/reports/executive" else None
            projects = _all_items(get_table("PROJECTS_TABLE"), KeyConditionExpression=Key("PK").eq(f"ORG#{org_id}") & Key("SK").begins_with("PROJECT#"))
            projects = reporting.project_totals(projects, org_id, period)
            if path.endswith("analytics"):
                approvals = reporting.approval_queue(org_id)
                result = {"totalProjects": len(projects), "activeProjects": sum(p.get("status") not in {"Completed", "Archived"} for p in projects),
                          "totalBudget": sum(p.get("budget", 0) for p in projects), "totalSpent": sum(p.get("spent", 0) for p in projects),
                          "pendingApprovals": len(approvals), "approvals": approvals}
                count = get_table('REPORTING_TABLE').get_item(Key={'PK': 'ORG#' + org_id, 'SK': 'SUMMARY#APPROVALS'}, ConsistentRead=True).get('Item', {}).get('amount', 0)
                result.update(pendingApprovals=count, approvalsLimited=count > len(approvals))
            else:
                result = {"projectSummaries": [_clean(p) for p in projects],
                          "totalExpenses": (reporting.expense_total(org_id, period)),
                          "generatedAt": datetime.now(timezone.utc).isoformat()}
            return _response(200, {"success": True, "data": result})
        if method == "GET" and path == "/super-admin/metrics":
            organizations = (_directory_rows(get_table('ORGANIZATIONS_TABLE'), 'organization'))
            users = (_directory_rows(get_table('USERS_TABLE'), 'user'))
            return _response(200, {"success": True, "data": {"totalOrganizations": len(organizations), "activeOrganizations": sum(o.get("status") == "Active" for o in organizations), "totalEmployees": len(users), "activeEmployees": sum(u.get("status") == "Active" for u in users)}})

        if resource == "setting":
            record_id = "global"

        if method == "POST":
            if resource == "user":
                if record_id:
                    item = table.get_item(Key={"PK": pk, "SK": f"USER#{record_id}"}, ConsistentRead=True).get("Item")
                    if not item:
                        return _error(404, "NOT_FOUND", "Account not found")
                    if not path.endswith("/invitation"):
                        return _error(405, "METHOD_NOT_ALLOWED", "Unsupported account operation")
                    item = resend_invitation(identity, item)
                else:
                    item = create_account(identity, data)
                return _response(201, {"success": True, "data": _clean(item)})
            if record_id:
                return _error(405, "METHOD_NOT_ALLOWED", "Create records using the collection URL")
            _validate_data(resource, data)
            workflow_validation.validate_record(resource, data)
            if resource in {"bill", "expense", "payment", "dpr"}:
                data.setdefault("status", "Pending")
            if resource == "material":
                data["recordKind"] = path.split("/")[3]
                data.setdefault("status", "pending")
            if resource == "project":
                _validate_assignments(data, org_id)
                if "supervisorIds" in data:
                    data["supervisor"] = workflow_helpers.assignment_names(data["supervisorIds"], org_id)
            id_field = _id_field(resource)
            if data.get("requestId") and not workflow_helpers.re_id(str(data["requestId"])):
                raise ValueError("Invalid requestId")
            supplied_id = data.get(id_field)
            record_id = model.validate_id(resource, supplied_id) if supplied_id else model.new_id(resource, data.get('requestId'))
            if resource == "organization":
                org_id = record_id
                pk = f"ORG#{org_id}"
            now = datetime.now(timezone.utc).isoformat()
            item = {**data, "PK": pk, "SK": f"{resource.upper()}#{record_id}", "entityType": resource,
                    "orgId": org_id, "createdAt": now, "updatedAt": now, "createdBy": identity.user_id, "version": 1}
            if resource not in {"project", "organization"} and project_id:
                item["projectId"] = project_id
            if SUPERVISOR in identity.roles:
                item["supervisorId"] = identity.user_id
            item.setdefault(id_field, record_id)
            if resource in {"organization", "project"}:
                item.setdefault("status", "Active")
            if resource == "project":
                item.setdefault("supervisorIds", [])
                item.setdefault("spent", 0)
                item['openingSpent'] = item['spent']
            if data.get("requestId"):
                digest_data = request_data
                if resource == 'inventory-item':
                    digest_data = dict(request_data, name=data['name'].casefold(), unit=data['unit'].casefold())
                item['requestDigest'] = contracts.fingerprint(digest_data)
                prior = table.get_item(Key={"PK": pk, "SK": item["SK"]}, ConsistentRead=True).get("Item")
                if prior:
                    mismatched = prior.get('requestDigest') != item['requestDigest'] if (prior.get('requestDigest')) else any(prior.get(k) != v for k, v in data.items() if k != "status")
                    if mismatched:
                        raise ValueError("Request ID already used for different data")
                    return _response(200, {"success": True, "data": _clean(prior)})
            audit.put(table, identity, item, ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)")
            if resource == "organization" and data.get("adminEmail"):
                admin_email = str(data["adminEmail"]).strip().lower()
                admin_name = str(data.get("adminName") or f"{item['name']} Admin").strip()
                try:
                    admin_user = create_account(identity, {
                        "name": admin_name,
                        "email": admin_email,
                        "role": OPERATIONS_ADMIN,
                        "orgId": org_id,
                    })
                    resend_invitation(identity, admin_user)
                except Exception as exc:
                    logging.exception("Failed to create admin user for organization %s: %s", org_id, exc)
                    item["provisioningWarning"] = "Organization saved, but administrator setup failed. Create or resend the administrator invitation from Employees."
            return _response(201, {"success": True, "data": _clean(item)})

        if not record_id:
            return _error(400, "MISSING_ID", "A record identifier is required")
        key = {"PK": pk, "SK": f"{resource.upper()}#{record_id}"}
        existing = table.get_item(Key=key, ConsistentRead=True).get("Item")
        if not existing and resource == "setting" and method in {"GET", "PUT"}:
            existing = {**key, "orgId": org_id, "entityType": "setting", "settingKey": "global"}
        if not existing:
            return _error(404, "NOT_FOUND", "Record not found")
        require_organization(identity, existing.get("orgId", ""))
        if existing.get("orgId") != org_id and resource != "organization":
            raise AuthorizationError("Record is not in the selected organization")
        if resource == "logistics-trip":
            if not workflow_helpers.logistics_visible(identity, existing):
                raise AuthorizationError("Supervisors can only access logistics for the current month")
            if existing.get("tripType") == "vehicle_registration" and method != "GET":
                require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        if SUPERVISOR in identity.roles and resource == "project" and identity.user_id not in existing.get("supervisorIds", []):
            raise AuthorizationError("You are not assigned to this project")
        if resource == "material" and workflow_helpers.material_kind(existing) != path.split("/")[3]:
            return _error(404, "NOT_FOUND", "Record not found in this collection")
        if method == "GET":
            if resource == "project":
                existing = reporting.project_totals([existing], org_id)[0]
            return _response(200, {"success": True, "data": _clean(existing)})
        if method == "DELETE":
            return _error(405, 'METHOD_NOT_ALLOWED', 'Archive or deactivate records to preserve references and history')
            if resource in {"user", "organization", "project"}:
                return _error(405, "METHOD_NOT_ALLOWED", "Disable or archive this record instead of deleting it")
            storage.transact(table, [
                {"Delete": {"TableName": table.name, "Key": key, "ConditionExpression": "orgId = :org", "ExpressionAttributeValues": {":org": org_id}}},
                audit.operation(identity, f"{resource}.deleted", before=existing),
            ])
            return _response(200, {"success": True, "data": {"deleted": True}})
        if method in {"PUT", "PATCH"}:
            expected_version = data.pop("expectedVersion", None)
            if expected_version is not None and (isinstance(expected_version, bool) or not isinstance(expected_version, (int, Decimal)) or expected_version != existing.get("version", 0)):
                return _error(409, "CONFLICT", "The record changed. Refresh before saving your changes.")
            if SUPERVISOR in identity.roles and resource in {"dpr", "labour-attendance"} and existing.get("createdBy") != identity.user_id:
                raise AuthorizationError("You can only edit your own submissions")
            if resource == "user":
                return _response(200, {"success": True, "data": _clean(update_account(identity, existing, data))})
            _validate_data(resource, data, updating=True)
            if (resource == 'project' and 'spent' in data):
                raise ValueError('Spending is derived from posted transactions; record an expense or payment')
            contracts.validate(resource, path, method, request_data, existing)
            workflow_validation.validate_record(resource, data, existing)
            if resource == "organization" and "status" in data:
                if existing["orgId"] == identity.organization_id or data["status"] not in {"Active", "Suspended", "Archived"}:
                    raise ValueError("Cannot suspend your own organization; use Active, Suspended or Archived")
            if resource == "project":
                _validate_assignments(data, org_id)
                if "supervisorIds" in data:
                    data["supervisor"] = workflow_helpers.assignment_names(data["supervisorIds"], org_id)
            if resource == "inventory-item" and inventory.catalog_identity({**existing, **data}) != inventory.catalog_identity(existing):
                raise ValueError("Material name and unit identify a catalog entry; add a new material instead")
            if resource == "payment":
                if "status" in data:
                    if set(data) != {"status"}:
                        raise ValueError("Review a payment separately from changes to its details")
                    data["status"] = str(data["status"]).title()
                elif data:
                    merged = {**existing, **data}
                    finance.payment_material(merged, org_id, pk)
                    data.update({k: merged[k] for k in ("vendorName", "material", "materialUnit")})
            if resource == "subcontractor":
                workflow_validation.validate_subcontractor(data, org_id, pk, project_id, existing)
            workflow_validation.validate_transition(identity, resource, existing, data)
            if resource == "payment" and existing.get("billId") and data.get("status") == "Approved" and existing.get("status") != "Approved":
                updated = finance.approve_linked_payment(table, existing, data, identity)
                return _response(200, {"success": True, "data": _clean(updated)})
            previous_version = existing.get("version")
            before = dict(existing)
            existing.update(data)
            existing["updatedAt"] = datetime.now(timezone.utc).isoformat()
            existing["version"] = (previous_version or 0) + 1
            condition = "attribute_not_exists(#v)" if previous_version is None else "#v = :version"
            options = {"ConditionExpression": condition, "ExpressionAttributeNames": {"#v": "version"}}
            if previous_version is not None:
                options["ExpressionAttributeValues"] = {":version": previous_version}
            audit.put(table, identity, existing, before=before, **options)
            return _response(200, {"success": True, "data": _clean(existing)})
        return _error(405, "METHOD_NOT_ALLOWED", "Unsupported request method")
    except AuthorizationError as exc:
        return _error(403, "FORBIDDEN", str(exc))
    except workflow_helpers.HttpError as exc:
        return _response(exc.status, exc.payload)
    except ValueError as exc:
        return _error(400, "INVALID_REQUEST", str(exc))
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in {"ConditionalCheckFailedException", "UsernameExistsException", "TransactionCanceledException"}:
            return _error(409, "CONFLICT", "The record already exists or was changed. Refresh and try again.")
        logging.exception("AWS operation failed")
        return _error(502, "SERVICE_ERROR", "The account or data service could not complete the operation")
    except Exception:
        logging.exception("Domain operation failed")
        return _error(500, "INTERNAL_ERROR", "The request could not be processed")


def auth_handler(event, context):
    try:
        identity = active_identity(event)
        method, path, _ = _event_parts(event)
        if method == "GET" and path == "/auth/me":
            return _response(200, {"success": True, "data": {"sub": identity.cognito_sub or identity.user_id, "employeeId": identity.user_id, "orgId": identity.organization_id, "role": next(iter(identity.roles))}})
        return _error(404, "NOT_FOUND", "Unknown account operation")
    except AuthorizationError as exc:
        return _error(403, "FORBIDDEN", str(exc))
    except ValueError as exc:
        return _error(400, "INVALID_REQUEST", str(exc))
    except Exception:
        logging.exception("Session validation failed")
        return _error(503, "SERVICE_ERROR", "Account validation is temporarily unavailable")
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
def placeholder(event, context): return _error(500, "INVALID_HANDLER", "No Lambda handler was configured")
