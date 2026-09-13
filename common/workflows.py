"""Shared persisted workflows. Authorization is performed before entering this module."""
import os
import uuid
import hashlib
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key, Attr
from common.dynamo import get_table
from common.authz import SUPERVISOR, OPERATIONS_ADMIN, SUPER_ADMIN, require_role, AuthorizationError


def now():
    return datetime.now(timezone.utc).isoformat()


def logistics_month():
    return datetime.fromisoformat(now()).astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m")


def logistics_visible(identity, item):
    if SUPERVISOR not in identity.roles or item.get("tripType") == "vehicle_registration":
        return True
    return str(item.get("date", ""))[:7] == logistics_month()


def rows(table, pk=None, prefix=None, org=None):
    args = ({"KeyConditionExpression": Key("PK").eq(pk) & Key("SK").begins_with(prefix or ""), "ConsistentRead": True}
            if pk else {"FilterExpression": Attr("orgId").eq(org)})
    result = []
    while True:
        page = table.query(**args) if pk else table.scan(**args)
        result.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return result
        args["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def clean(item):
    return {k: v for k, v in item.items() if k not in {"PK", "SK", "entityType"}}


def number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{label} must be a number")
    if not Decimal(value).is_finite() or value < 0 or (positive and value == 0):
        raise ValueError(f"{label} must be {'positive' if positive else 'non-negative'}")
    return value


def material_kind(item):
    # Old requests can be identified without exposing them as stock.
    return item.get("recordKind") or ("indents" if "materialName" in item or "requiredDate" in item else "grn")


def assignment_names(ids, org):
    table = get_table("USERS_TABLE")
    return ", ".join(table.get_item(Key={"PK": f"ORG#{org}", "SK": f"USER#{i}"}, ConsistentRead=True).get("Item", {}).get("name", i) for i in ids)


def validate(identity, resource, method, path, body):
    if method == "GET":
        return
    if method == "DELETE" and resource in {"bill", "expense", "payment", "material", "dpr"}:
        raise ValueError("Keep submitted records for the audit trail")
    if resource == "subcontractor" and body.get("type") == "Procurement" and method == "POST" and body.get("status", "Pending") != "Pending":
        raise ValueError("New procurement requests must be Pending")
    if resource == "milestone" and "progress" in body:
        number(body["progress"], "Progress")
        if body["progress"] > 100:
            raise ValueError("Progress must not exceed 100")
    if resource == "logistics-trip":
        kind = body.get("tripType")
        if kind != "vehicle_registration":
            day = str(body.get("date", ""))
            if len(day) != 10:
                raise ValueError("A valid logistics date is required")
            date.fromisoformat(day)
            if not logistics_visible(identity, body):
                raise AuthorizationError("Supervisors can only record logistics for the current month")
        if kind == "vehicle_registration":
            require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
            if not str(body.get("vehicle", "")).strip():
                raise ValueError("Vehicle is required")
        elif kind == "vehicle":
            if number(body.get("endKm"), "End odometer") < number(body.get("startKm"), "Start odometer"):
                raise ValueError("End odometer cannot be below the start")
        elif kind == "fuel":
            body["total"] = number(body.get("liters"), "Liters", True) * number(body.get("rate"), "Rate")
        elif kind == "rental":
            body["total"] = number(body.get("rate"), "Hire charge") + number(body.get("helper", 0), "Helper charge")
        else:
            raise ValueError("Select a valid transport record type")
    if resource == "material":
        if "/stock" in path:
            raise ValueError("Stock is updated by receipts and issue vouchers")
        if SUPERVISOR in identity.roles:
            if "/indents" not in path or method != "POST":
                raise AuthorizationError("Only administrators can review material requests or receive stock")
            if str(body.get("status", "pending")).lower() != "pending":
                raise AuthorizationError("Requests must be submitted as pending")
        if method == "POST":
            if str(body.get("status", "pending")).lower() != "pending":
                raise ValueError("New requests must be pending")
            number(body.get("quantity", body.get("qty")), "Quantity", True)
            if not str(body.get("materialName", body.get("name", ""))).strip():
                raise ValueError("Material name is required")
    if resource in {"bill", "expense", "payment", "dpr"}:
        if method == "POST" and body.get("status", "Pending") not in {"Pending", "Draft"}:
            raise ValueError("New submissions must be Pending or Draft")
        if resource == "dpr" and SUPERVISOR in identity.roles and "status" in body and body["status"] != "Pending":
            raise AuthorizationError("Only administrators can review reports")
        if resource in {"bill", "expense", "payment"} and method == "POST":
            number(body.get("grossAmount") if resource == "bill" else body.get("amount"), "Amount", True)
    if SUPERVISOR in identity.roles and resource in {"inspection", "equipment"} and method != "GET":
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)


def validate_transition(identity, resource, old, body):
    approval = resource in {"bill", "expense", "payment", "dpr", "material"} or (resource == "subcontractor" and old.get("type") == "Procurement")
    if not approval:
        return
    if str(old.get("status", "Pending")).lower() == "approved" and set(body) - {"status"}:
        raise ValueError("Approved records are immutable")
    if "status" in body:
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        old_status = str(old.get("status", "Pending")).lower()
        new_status = str(body["status"]).lower()
        transitions = {"draft": {"pending"}, "pending": {"approved", "rejected"}, "approved": {"issued"} if resource == "material" else set(), "rejected": {"pending"}}
        if new_status != old_status and new_status not in transitions.get(old_status, set()):
            raise ValueError("Invalid approval transition")
        if new_status == "issued":
            raise ValueError("Issue stock from the warehouse to fulfill this request")
        if new_status != old_status:
            body["approvedBy"] = identity.user_id
            body["approvedAt"] = now()
    if str(old.get("status", "Pending")).lower() == "approved" and set(body) - {"status"}:
        raise ValueError("Approved records are immutable")


def report_period(query):
    start, end = query.get("startDate"), query.get("endDate")
    if not start and not end:
        return None
    if not start or not end:
        raise ValueError("Choose both a start and end date")
    if len(start) != 10 or len(end) != 10 or date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Choose a valid date range with start date before end date")
    return start, end


def in_report_period(item, period):
    if period is None:
        return True
    stamp = str(item.get("date") or item.get("paidAt") or item.get("approvedAt") or item.get("createdAt") or "")[:10]
    return period[0] <= stamp <= period[1]


def project_totals(projects, org, period=None):
    # Preserve explicit historical opening spending, then add posted transactions.
    finance = rows(get_table("FINANCE_TABLE"), org=org)
    payroll = rows(get_table("SETTINGS_TABLE"), org=org)
    daily_wages = [r for r in rows(get_table("FIELD_OPERATIONS_TABLE"), org=org) if r.get("entityType") == "daily-wage" and r.get("paymentStatus") == "Paid"]
    totals = {}
    for item in finance + payroll:
        if not in_report_period(item, period):
            continue
        kind, status = item.get("entityType"), str(item.get("status", "")).lower()
        if (kind in {"expense", "payment"} and status == "approved") or (kind == "payroll" and status == "paid"):
            pid = item.get("projectId")
            totals[pid] = totals.get(pid, 0) + item.get("amount", 0)
    for item in daily_wages:
        if not in_report_period(item, period):
            continue
        pid = item["projectId"]
        totals[pid] = totals.get(pid, 0) + item.get("wage", 0)
    return [dict(p, spent=(p.get("openingSpent", p.get("spent", 0)) if period is None else 0) + totals.get(p["projectId"], 0)) for p in projects]


def approval_queue(org, finance):
    candidates = finance + rows(get_table("MATERIALS_LOGISTICS_TABLE"), org=org) + rows(get_table("FIELD_OPERATIONS_TABLE"), org=org) + rows(get_table("PARTIES_TABLE"), org=org)
    result = []
    for item in candidates:
        resource = item.get("entityType")
        if resource not in {"payment", "bill", "expense", "material", "dpr", "subcontractor"}:
            continue
        if resource == "material" and material_kind(item) != "indents":
            continue
        if resource == "subcontractor" and item.get("type") != "Procurement":
            continue
        if str(item.get("status", "Pending")).lower() == "pending":
            result.append(dict(clean(item), resource=resource))
    return result


def approve_linked_payment(table, existing, body):
    bill_key = {"PK": existing["PK"], "SK": f"BILL#{existing['billId']}"}
    bill = table.get_item(Key=bill_key, ConsistentRead=True).get("Item")
    if not bill or bill.get("status") != "Approved":
        raise ValueError("The linked bill must be approved")
    remaining_limit = bill.get("netPayable", bill.get("grossAmount", 0)) - existing["amount"]
    if remaining_limit < 0:
        raise ValueError("Payment exceeds the bill amount")
    version = existing.get("version", 1)
    updated = dict(existing, **body)
    updated["version"] = version + 1
    updated["updatedAt"] = now()
    table.meta.client.transact_write_items(TransactItems=[
        {"Put": {"TableName": table.name, "Item": updated, "ConditionExpression": "#v = :v AND #s = :s", "ExpressionAttributeNames": {"#v": "version", "#s": "status"}, "ExpressionAttributeValues": {":v": version, ":s": existing["status"]}}},
        {"Update": {"TableName": table.name, "Key": bill_key, "UpdateExpression": "ADD paidAmount :amount", "ConditionExpression": "#s = :approved AND (attribute_not_exists(paidAmount) OR paidAmount <= :limit)", "ExpressionAttributeNames": {"#s": "status"}, "ExpressionAttributeValues": {":amount": existing["amount"], ":approved": "Approved", ":limit": remaining_limit}}},
    ])
    return updated


def labour_list(table, pk, day, roster=False):
    date.fromisoformat(day)
    org, pid = pk.removeprefix("ORG#").split("#PROJECT#", 1)
    workers = [w for w in rows(table, org=org) if w.get("entityType") == "worker"]
    allocations = rows(table, f"ORG#{org}", "ALLOCATION#")
    daily = rows(table, pk, "DAILY#")
    debits = rows(table, pk, "DEBIT#")
    result = []
    for w in workers:
        wid = w["labourAttendanceId"]
        logs = [r for r in daily if r["labourId"] == wid and r["date"].startswith(day[:7])]
        current = next((r for r in logs if r["date"] == day), {})
        history = sorted([a for a in allocations if a["labourId"] == wid and a["date"] <= day], key=lambda a: a["date"])
        allocation = history[-1] if history else {}
        allocated_project = allocation.get("projectId", w["projectId"])
        if not roster and allocated_project != pid and not logs:
            continue
        deductions = [r for r in debits if r["labourId"] == wid and r["date"].startswith(day[:7])]
        gross = sum(r.get("wage", w["rate"] * ({"Present": 1, "Half Day": Decimal("0.5")}.get(r["status"], 0) + bool(r.get("nightShift")))) for r in logs)
        paid = sum(r.get("wage", 0) for r in logs if r.get("paymentStatus") == "Paid")
        result.append(dict(clean(w), status=current.get("status", "Absent"), nightShift=current.get("nightShift", False),
                           attendanceRecorded=bool(current), paymentStatus=current.get("paymentStatus", "Not paid"),
                           allocatedProjectId=allocated_project, allocationDate=allocation.get("date"),
                           allocationConfirmed=allocation.get("date") == day and allocated_project == pid,
                           allocationStarted=allocation.get("date") == day and allocation.get("attendanceStarted", False),
                           dailyWage=current.get("wage", w["rate"] if current.get("status") == "Present" else 0),
                           grossWages=gross, dailyPaid=paid, unpaidWages=max(0, gross - paid),
                           date=day, daysPresent=sum({"Present": 1, "Half Day": Decimal("0.5")}.get(r["status"], 0) for r in logs),
                           nightShifts=sum(bool(r.get("nightShift")) for r in logs),
                           advanceDeductions=sum(r["amount"] for r in deductions), deductions=[clean(r) for r in deductions]))
    return result


def labour(identity, method, body, query, pk, org, pid):
    table = get_table("FIELD_OPERATIONS_TABLE")
    day = str(body.get("date") or query.get("date") or now()[:10])
    date.fromisoformat(day)
    if method == "GET":
        roster = query.get("roster") == "true"
        if roster:
            require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        result = labour_list(table, pk, day, roster)
        if SUPERVISOR in identity.roles:
            result = [w for w in result if w["allocatedProjectId"] == pid]
        return result
    if method != "POST":
        raise ValueError("Use dated attendance submissions")
    from handlers.consolidated import _validate_data
    _validate_data("labour-attendance", body)
    operation = body.get("operation", "attendance" if body.get("labourAttendanceId") else "register")
    wid = body.get("labourAttendanceId") or body.get("requestId") or uuid.uuid4().hex
    key = {"PK": pk, "SK": f"LABOUR-ATTENDANCE#{wid}"}
    worker = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if operation == "register":
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        if not str(body.get("name", "")).strip():
            raise ValueError("Worker name is required")
        rate = number(body.get("rate"), "Daily rate", True)
        item = {**key, "labourAttendanceId": wid, "name": body["name"], "rate": rate, "type": body.get("type", "Skilled"),
                "bankName": body.get("bankName", ""), "accNo": body.get("accNo", ""), "entityType": "worker", "orgId": org,
                "projectId": pid, "createdAt": now(), "createdBy": identity.user_id}
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        return clean(item)
    if not worker:
        worker = next((w for w in rows(table, org=org) if w.get("entityType") == "worker" and w.get("labourAttendanceId") == wid), None)
    if not worker or worker.get("orgId") != org:
        raise ValueError("Worker does not exist in this organization")
    allocation_key = {"PK": f"ORG#{org}", "SK": f"ALLOCATION#{wid}#{day}"}
    if operation == "allocate":
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        target = str(body.get("targetProjectId") or pid)
        project = get_table("PROJECTS_TABLE").get_item(Key={"PK": f"ORG#{org}", "SK": f"PROJECT#{target}"}, ConsistentRead=True).get("Item")
        if not project or project.get("orgId") != org:
            raise AuthorizationError("Choose a project in this organization")
        target_pk = f"ORG#{org}#PROJECT#{target}"
        previous_logs = [r for r in rows(table, org=org) if r.get("labourId") == wid and r.get("date") == day and r.get("SK", "").startswith("DAILY#")]
        if any(r.get("projectId") != target for r in previous_logs):
            raise ValueError("This worker already has attendance at another project for this date")
        item = {**allocation_key, "entityType": "worker-allocation", "labourId": wid, "projectId": target,
                "orgId": org, "date": day, "confirmedBy": identity.user_id, "confirmedAt": now()}
        if previous_logs:
            item["attendanceStarted"] = True
        table.meta.client.transact_write_items(TransactItems=[
            {"ConditionCheck": {"TableName": get_table("SETTINGS_TABLE").name, "Key": {"PK": target_pk, "SK": f"PAYROLL#{day[:7]}"}, "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": table.name, "Item": item, "ConditionExpression": "attribute_not_exists(attendanceStarted)"}},
        ])
        return clean(item)
    cycle = get_table("SETTINGS_TABLE").get_item(Key={"PK": pk, "SK": f"PAYROLL#{day[:7]}"}, ConsistentRead=True).get("Item")
    if cycle:
        raise ValueError("Attendance and deductions are locked after the payroll cycle is generated")
    if operation == "debit":
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        amount = number(body.get("amount"), "Deduction", True)
        if not str(body.get("description", "")).strip():
            raise ValueError("Deduction description is required")
        item = {"PK": pk, "SK": f"DEBIT#{body.get('requestId') or uuid.uuid4().hex}", "labourId": wid, "amount": amount,
                "description": body["description"], "date": day, "orgId": org, "createdBy": identity.user_id}
        table.meta.client.transact_write_items(TransactItems=[
            {"ConditionCheck": {"TableName": get_table("SETTINGS_TABLE").name, "Key": {"PK": pk, "SK": f"PAYROLL#{day[:7]}"}, "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": table.name, "Item": item, "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Update": {"TableName": table.name, "Key": {"PK": pk, "SK": f"MONTH#{day[:7]}"}, "UpdateExpression": "ADD revision :one", "ExpressionAttributeValues": {":one": 1}}},
        ])
        return clean(item)
    if operation != "attendance" or body.get("status") not in {"Present", "Absent"}:
        raise ValueError("Select Present or Absent")
    if body.get("nightShift"):
        raise ValueError("Night shifts are no longer part of daily attendance")
    payment_status = body.get("paymentStatus", "Not paid")
    if payment_status not in {"Paid", "Not paid"}:
        raise ValueError("Select Paid or Not paid")
    if body["status"] == "Absent" and payment_status == "Paid":
        raise ValueError("An absent worker has no daily wage to mark paid")
    allocation = table.get_item(Key=allocation_key, ConsistentRead=True).get("Item")
    if not allocation or allocation.get("projectId") != pid:
        raise AuthorizationError("The admin must confirm this worker's project allocation for this date first")
    existing = table.get_item(Key={"PK": pk, "SK": f"DAILY#{wid}#{day}"}, ConsistentRead=True).get("Item", {})
    rate = existing.get("rate", worker["rate"])
    item = {"PK": pk, "SK": f"DAILY#{wid}#{day}", "labourId": wid, "date": day, "status": body["status"],
            "entityType": "daily-wage", "paymentStatus": payment_status, "rate": rate,
            "wage": rate if body["status"] == "Present" else 0,
            "nightShift": False, "orgId": org, "projectId": pid, "updatedAt": now(), "createdBy": identity.user_id}
    table.meta.client.transact_write_items(TransactItems=[
        {"Update": {"TableName": table.name, "Key": allocation_key, "UpdateExpression": "SET attendanceStarted = :yes", "ConditionExpression": "projectId = :pid", "ExpressionAttributeValues": {":yes": True, ":pid": pid}}},
        {"ConditionCheck": {"TableName": get_table("SETTINGS_TABLE").name, "Key": {"PK": pk, "SK": f"PAYROLL#{day[:7]}"}, "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Put": {"TableName": table.name, "Item": item}},
        {"Update": {"TableName": table.name, "Key": {"PK": pk, "SK": f"MONTH#{day[:7]}"}, "UpdateExpression": "ADD revision :one", "ExpressionAttributeValues": {":one": 1}}},
    ])
    return clean(item)


def stock(identity, method, path, body, pk, org, pid):
    table = get_table("INVENTORY_TABLE")
    central = f"ORG#{org}"
    if path.endswith("/stock"):
        if method != "GET":
            raise ValueError("Stock is maintained by receipts and issues")
        return [dict(clean(i), materialId=i["itemId"], stock=i.get("quantity", 0), totalStock=i.get("quantity", 0)) for i in rows(table, pk, "BALANCE#")]
    kind = "issue-vouchers" if "/issue-vouchers" in path else "grn"
    if method == "GET":
        return [clean(i) for i in rows(table, central, f"WAREHOUSE-{kind.upper()}#") if not path.startswith("/supervisor/") or i.get("targetProjectId") == pid]
    require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
    if method != "POST":
        raise ValueError("Stock movements cannot be edited; record a correcting movement")
    name = str(body.get("item", body.get("materialName", ""))).strip()
    unit = str(body.get("unit", "")).strip()
    if not name or not unit:
        raise ValueError("Item and unit are required")
    qty = number(body.get("qty", body.get("quantity")), "Quantity", True)
    item_id = hashlib.sha256(f"{name.casefold()}|{unit.casefold()}".encode()).hexdigest()[:24]
    request_id = body.get("requestId")
    if not isinstance(request_id, str) or not re_id(request_id):
        raise ValueError("A unique requestId is required")
    target = body.get("targetProjectId") or (pid if path.startswith("/supervisor/") else None)
    if kind == "issue-vouchers" and not target:
        raise ValueError("Destination project is required")
    if target:
        project = get_table("PROJECTS_TABLE").get_item(Key={"PK": central, "SK": f"PROJECT#{target}"}, ConsistentRead=True).get("Item")
        if not project or project.get("orgId") != org:
            raise AuthorizationError("Destination project is not accessible")
    event = {"PK": central, "SK": f"WAREHOUSE-{kind.upper()}#{request_id}", "warehouseEventId": request_id,
             "entityType": "warehouse-event", "recordKind": kind, "orgId": org, "targetProjectId": target,
             "itemId": item_id, "item": name, "unit": unit, "qty": qty, "rate": number(body.get("rate", 0), "Rate"),
             "vendor": str(body.get("vendor", "")), "issuedTo": str(body.get("issuedTo", "")), "createdAt": now(), "date": now()[:10], "createdBy": identity.user_id}
    event["amount"] = event["rate"] * qty
    existing = table.get_item(Key={"PK": central, "SK": event["SK"]}, ConsistentRead=True).get("Item")
    if existing:
        if any(existing.get(k) != event.get(k) for k in ("itemId", "qty", "targetProjectId", "rate")):
            raise ValueError("Request ID already used for a different movement")
        return clean(existing)
    operations = [{"Put": {"TableName": table.name, "Item": event, "ConditionExpression": "attribute_not_exists(PK)"}}]
    def balance(partition, delta, require_stock=False):
        values = {":q": delta, ":name": name, ":unit": unit, ":org": org, ":id": item_id}
        op = {"TableName": table.name, "Key": {"PK": partition, "SK": f"BALANCE#{item_id}"},
              "UpdateExpression": "SET #name = :name, #unit = :unit, orgId = :org, itemId = :id ADD quantity :q",
              "ExpressionAttributeNames": {"#name": "name", "#unit": "unit"}, "ExpressionAttributeValues": values}
        if require_stock:
            op["ConditionExpression"] = "quantity >= :required"
            values[":required"] = qty
        operations.append({"Update": op})
    if kind == "issue-vouchers":
        balance(central, -qty, True)
        balance(f"ORG#{org}#PROJECT#{target}", qty)
    else:
        balance(f"ORG#{org}#PROJECT#{target}" if target else central, qty)
    if body.get("indentId"):
        if kind != "issue-vouchers":
            raise ValueError("Only issue vouchers can fulfill material requests")
        request_table = get_table("MATERIALS_LOGISTICS_TABLE")
        request_key = {"PK": f"ORG#{org}#PROJECT#{target}", "SK": f"MATERIAL#{body['indentId']}"}
        indent = request_table.get_item(Key=request_key, ConsistentRead=True).get("Item")
        if not indent or material_kind(indent) != "indents" or str(indent.get("status", "")).lower() != "approved":
            raise ValueError("The material request must be approved first")
        if str(indent.get("materialName", "")).casefold() != name.casefold() or str(indent.get("unit", "")).casefold() != unit.casefold() or indent.get("quantity") != qty:
            raise ValueError("Issue the approved request's material, unit and full quantity")
        operations.append({"Update": {"TableName": request_table.name, "Key": request_key,
            "UpdateExpression": "SET #s = :issued, issuedAt = :at, issuedBy = :by",
            "ConditionExpression": "#s = :approved",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":issued": "issued", ":approved": indent["status"], ":at": now(), ":by": identity.user_id}}})
    # The resource client serializes native Decimal values, including transactions.
    table.meta.client.transact_write_items(TransactItems=operations)
    return clean(event)


def re_id(value):
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value))


def payroll(identity, method, path, body, pk, pid, org):
    table = get_table("SETTINGS_TABLE")
    if method == "DELETE":
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        month = path.rsplit("/", 1)[-1]
        date.fromisoformat(month + "-01")
        key = {"PK": pk, "SK": f"PAYROLL#{month}"}
        old = table.get_item(Key=key, ConsistentRead=True).get("Item")
        if not old or old.get("status") != "Pending":
            raise ValueError("Only unpaid payroll cycles can be reopened")
        archived = dict(old, SK=f"PAYROLL-HISTORY#{month}#{uuid.uuid4().hex}", entityType="payroll-history", reopenedAt=now(), reopenedBy=identity.user_id)
        table.meta.client.transact_write_items(TransactItems=[
            {"Put": {"TableName": table.name, "Item": archived}},
            {"Delete": {"TableName": table.name, "Key": key, "ConditionExpression": "#s = :pending", "ExpressionAttributeNames": {"#s": "status"}, "ExpressionAttributeValues": {":pending": "Pending"}}},
        ])
        return {"reopened": True}
    if path.endswith("/staff"):
        if method == "GET":
            return [clean(i) for i in rows(table, pk, "SALARY#")]
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        employee_id = str(body.get("employeeId", ""))
        employee = get_table("USERS_TABLE").get_item(Key={"PK": f"ORG#{org}", "SK": f"USER#{employee_id}"}, ConsistentRead=True).get("Item")
        if not employee or employee.get("orgId") != org or employee.get("status") != "Active":
            raise ValueError("Choose an active employee in this organization")
        amounts = {field: number(body.get(field, 0), field) for field in ["basic", "hra", "conveyance", "medical", "pf", "esi", "tds"]}
        if amounts["basic"] <= 0:
            raise ValueError("Basic salary must be positive")
        if sum(amounts[f] for f in ["pf", "esi", "tds"]) > sum(amounts[f] for f in ["basic", "hra", "conveyance", "medical"]):
            raise ValueError("Salary deductions exceed gross salary")
        item = {"PK": pk, "SK": f"SALARY#{employee_id}", "employeeId": employee_id, "name": employee.get("name", employee_id),
                "orgId": org, "projectId": pid, "entityType": "salary", "updatedAt": now(), **amounts}
        table.put_item(Item=item)
        return clean(item)
    if method == "GET":
        return [clean(i) for i in rows(table, pk, "PAYROLL#")]
    require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
    month = str(body.get("month", ""))
    date.fromisoformat(month + "-01")
    if len(month) != 7:
        raise ValueError("Month must use YYYY-MM")
    key = {"PK": pk, "SK": f"PAYROLL#{month}"}
    old = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if path.endswith("/disburse"):
        if not old:
            raise ValueError("Generate and review this payroll cycle first")
        if old.get("status") == "Paid":
            return clean(old)
        if not str(body.get("reference", "")).strip():
            raise ValueError("An external payment reference is required")
        updated = table.update_item(Key=key, UpdateExpression="SET #s = :paid, paymentReference = :ref, paidAt = :at, paidBy = :by",
            ConditionExpression="#s = :pending", ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":paid": "Paid", ":pending": "Pending", ":ref": body["reference"], ":at": now(), ":by": identity.user_id}, ReturnValues="ALL_NEW")
        return clean(updated["Attributes"])
    if method != "POST":
        raise ValueError("Payroll cycles are immutable")
    if old:
        return clean(old)
    workforce = get_table("FIELD_OPERATIONS_TABLE")
    revision_key = {"PK": pk, "SK": f"MONTH#{month}"}
    revision = workforce.get_item(Key=revision_key, ConsistentRead=True).get("Item", {}).get("revision")
    workers = labour_list(workforce, pk, month + "-01")
    staff = []
    for w in workers:
        gross = w["grossWages"]
        if w["advanceDeductions"] + w["dailyPaid"] > gross:
            raise ValueError(f"Deductions exceed wages for {w['name']}; correct deductions before generating payroll")
        staff.append({"labourId": w["labourAttendanceId"], "name": w["name"], "rate": w["rate"], "daysPresent": w["daysPresent"],
                      "nightShifts": w["nightShifts"], "gross": gross, "deductions": w["advanceDeductions"], "dailyPaid": w["dailyPaid"], "net": gross - w["advanceDeductions"] - w["dailyPaid"]})
    for profile in rows(table, pk, "SALARY#"):
        employee = get_table("USERS_TABLE").get_item(Key={"PK": f"ORG#{org}", "SK": f"USER#{profile['employeeId']}"}, ConsistentRead=True).get("Item")
        if not employee or employee.get("status") != "Active":
            continue
        gross = sum(profile.get(f, 0) for f in ["basic", "hra", "conveyance", "medical"])
        deductions = sum(profile.get(f, 0) for f in ["pf", "esi", "tds"])
        staff.append({"labourId": profile["employeeId"], "name": profile["name"], "gross": gross, "deductions": deductions, "net": gross - deductions, "salary": clean(profile)})
    if not staff:
        raise ValueError("Register workers or configure staff salaries first")
    item = {**key, "cycleId": month, "month": month, "projectId": pid, "orgId": org, "entityType": "payroll", "status": "Pending",
            "staff": staff, "amount": sum(w["net"] for w in staff), "createdAt": now(), "createdBy": identity.user_id}
    check = {"TableName": workforce.name, "Key": revision_key, "ConditionExpression": "attribute_not_exists(revision)" if revision is None else "revision = :revision"}
    if revision is not None:
        check["ExpressionAttributeValues"] = {":revision": revision}
    table.meta.client.transact_write_items(TransactItems=[
        {"Put": {"TableName": table.name, "Item": item, "ConditionExpression": "attribute_not_exists(PK)"}},
        {"ConditionCheck": check},
    ])
    return clean(item)


def files(identity, method, path, body, pk, org, pid, resource, rid):
    bucket = os.environ.get("DOCUMENTS_BUCKET")
    if not bucket:
        raise ValueError("Document storage is not configured. Deploy the updated backend template.")
    s3 = boto3.client("s3")
    if method == "POST" and rid == "upload":
        size = number(body.get("size"), "File size", True)
        if size > 20 * 1024 * 1024:
            raise ValueError("Files must be at most 20 MB")
        filename = str(body.get("name", "")).strip()
        if not filename:
            raise ValueError("Filename is required")
        content_type = str(body.get("contentType") or "application/octet-stream")
        key = f"{org}/{pid}/{uuid.uuid4().hex}"
        post = s3.generate_presigned_post(bucket, key, Fields={"Content-Type": content_type},
            Conditions=[["content-length-range", 1, 20 * 1024 * 1024], {"Content-Type": content_type}], ExpiresIn=300)
        return {"upload": post, "storageKey": key}
    if method == "POST" and path.endswith("/extract"):
        record = get_table("DOCUMENT_CONTROL_TABLE").get_item(Key={"PK": pk, "SK": f"{resource.upper()}#{rid}"}, ConsistentRead=True).get("Item")
        if not record or not str(record.get("storageKey", "")).startswith(f"{org}/{pid}/"):
            raise ValueError("Upload the invoice to this project first")
        metadata = s3.head_object(Bucket=bucket, Key=record["storageKey"])
        if metadata.get("ContentLength", 0) > 5 * 1024 * 1024 or metadata.get("ContentType") not in {"image/jpeg", "image/png"}:
            raise ValueError("Invoice extraction accepts JPEG or PNG images up to 5 MB")
        analysis = boto3.client("textract").analyze_expense(Document={"S3Object": {"Bucket": bucket, "Name": record["storageKey"]}})
        documents = analysis.get("ExpenseDocuments", [])
        if not documents:
            raise ValueError("No invoice fields were detected. Enter the bill manually.")
        fields = {f.get("Type", {}).get("Text"): f.get("ValueDetection", {}).get("Text", "") for f in documents[0].get("SummaryFields", [])}
        return {"vendor": fields.get("VENDOR_NAME", ""), "invoice": fields.get("INVOICE_RECEIPT_ID", ""),
                "total": fields.get("TOTAL", ""), "date": fields.get("INVOICE_RECEIPT_DATE", ""), "tax": fields.get("TAX", ""),
                "documentId": rid}
    if method == "GET" and path.endswith("/download"):
        table = get_table("DOCUMENT_CONTROL_TABLE")
        record = table.get_item(Key={"PK": pk, "SK": f"{resource.upper()}#{rid}"}, ConsistentRead=True).get("Item")
        if not record or record.get("orgId") != org or not record.get("storageKey"):
            raise ValueError("No file is attached to this record")
        if not str(record["storageKey"]).startswith(f"{org}/{pid}/"):
            raise AuthorizationError("File is not in this project")
        return {"url": s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": record["storageKey"], "ResponseContentDisposition": "attachment"}, ExpiresIn=300)}
    if method == "POST" and not rid:
        key = body.get("storageKey", "")
        if not isinstance(key, str) or not key.startswith(f"{org}/{pid}/") or ".." in key:
            raise ValueError("Upload a file to this project first")
        s3.head_object(Bucket=bucket, Key=key)
    return None


def handle(identity, domain, resource, method, path, body, query, pk, pid, org, rid):
    if resource == "payment" and method == "POST":
        vendor = get_table("PARTIES_TABLE").get_item(Key={"PK": f"ORG#{org}", "SK": f"VENDOR#{body.get('vendorId', '')}"}, ConsistentRead=True).get("Item")
        if not vendor or vendor.get("orgId") != org:
            raise ValueError("Choose a vendor in this organization")
        body["vendorName"] = vendor["name"]
        if body.get("billId"):
            bill = get_table("FINANCE_TABLE").get_item(Key={"PK": pk, "SK": f"BILL#{body['billId']}"}, ConsistentRead=True).get("Item")
            if not bill or bill.get("status") != "Approved":
                raise ValueError("Choose an approved bill in this project")
    if resource == "inventory-item" and method == "GET" and not rid:
        table = get_table("INVENTORY_TABLE")
        all_rows = rows(table, org=org)
        catalog = {hashlib.sha256(f"{str(i['name']).casefold()}|{str(i.get('unit','')).casefold()}".encode()).hexdigest()[:24]: i for i in all_rows if i.get("entityType") == "inventory-item"}
        balances = [i for i in all_rows if i.get("SK", "").startswith("BALANCE#")]
        for i in balances:
            catalog.setdefault(i["itemId"], dict(i, category="General"))
        result = []
        for key, item in catalog.items():
            central = sum(i.get("quantity", 0) for i in balances if i["itemId"] == key and i["PK"] == f"ORG#{org}")
            deployed = sum(i.get("quantity", 0) for i in balances if i["itemId"] == key and i["PK"] != f"ORG#{org}")
            result.append(dict(clean(item), totalStock=central + deployed, centralStock=central, deployedStock=deployed))
        return result
    if resource == "labour-attendance":
        return labour(identity, method, body, query, pk, org, pid)
    if resource == "payroll":
        return payroll(identity, method, path, body, pk, pid, org)
    if resource == "warehouse-event" or (resource == "material" and ("/stock" in path or "/grn" in path)):
        return stock(identity, method, path, body, pk, org, pid)
    if resource in {"document", "drawing"}:
        return files(identity, method, path, body, pk, org, pid, resource, rid)
    if resource in {"payment", "expense"} and not pid and (method == "GET" or rid):
        table = get_table("FINANCE_TABLE")
        found = [r for r in rows(table, org=org) if r.get("entityType") == resource and (not rid or r.get(resource + "Id") == rid)]
        if not rid:
            return [clean(i) for i in found]
        if len(found) != 1:
            raise ValueError("Payment not found")
        item = found[0]
        if method == "GET":
            return clean(item)
        # Re-enter the scoped handler so normal validation and version checks run.
        from handlers.consolidated import _domain_handler
        if item.get("projectId"):
            event = {"rawPath": path, "body": body, "queryStringParameters": {"projectId": item["projectId"], "orgId": org},
                     "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": {"sub": identity.user_id, "custom:org_id": identity.organization_id, "cognito:groups": list(identity.roles)}}}}}
            response = _domain_handler(domain, event, None)
            payload = __import__('json').loads(response["body"])
            if response["statusCode"] >= 300:
                raise ValueError(payload["error"]["message"])
            return payload["data"]
    return None
