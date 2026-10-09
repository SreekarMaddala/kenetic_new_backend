"""Salary configuration, immutable payroll generation and settlement."""
from common.authz import AuthorizationError, OPERATIONS_ADMIN, SUPER_ADMIN, require_role
from common import audit, model, payroll_records, storage
from datetime import date
from common.dynamo import get_table
import uuid
from common.workflow_helpers import _batch_get, clean, now, number, rows
from common.workforce import labour_list


def payroll(identity, method, path, body, pk, pid, org, query=None):
    query = query or {}
    table = get_table(("PAYROLL_TABLE"))
    if path.endswith("/me"):
        if method != "GET":
            raise AuthorizationError("Personal salary is read-only")
        profile = table.get_item(Key={"PK": pk, "SK": f"SALARY#{identity.user_id}"}, ConsistentRead=True).get("Item")
        history = []
        for saved_cycle in rows(table, pk, "PAYROLL#"):
            cycle = dict(saved_cycle)
            member = payroll_records.employee_entry(table, cycle, identity.user_id)
            cycle['staff'] = [member] if member else []
            for member in cycle.get("staff", []):
                if member.get("labourId") == identity.user_id and member.get("salary"):
                    history.append({"month": cycle["month"], "status": cycle["status"],
                                    "gross": member["gross"], "deductions": member["deductions"],
                                    "net": member["net"], "paidAt": cycle.get("paidAt")})
        return {"profile": clean(profile) if profile else None, "history": sorted(history, key=lambda item: item["month"], reverse=True)}
    if method == "DELETE":
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        month = path.rsplit("/", 1)[-1]
        date.fromisoformat(month + "-01")
        key = {"PK": pk, "SK": f"PAYROLL#{month}"}
        old = table.get_item(Key=key, ConsistentRead=True).get("Item")
        if not old or old.get("status") != "Pending":
            raise ValueError("Only unpaid payroll cycles can be reopened")
        archived = dict(old, SK=f"PAYROLL-HISTORY#{month}#{uuid.uuid4().hex}", entityType="payroll-history", reopenedAt=now(), reopenedBy=identity.user_id)
        storage.transact(table, [
            {"Put": {"TableName": table.name, "Item": archived}},
            {"Delete": {"TableName": table.name, "Key": key, "ConditionExpression": "#s = :pending AND createdAt = :created", "ExpressionAttributeNames": {"#s": "status"}, "ExpressionAttributeValues": {":pending": "Pending", ":created": old["createdAt"]}}},
            audit.operation(identity, "payroll.reopened", old, archived),
        ])
        return {"reopened": True}
    if path.endswith("/staff"):
        if method == "GET":
            return [clean(i) for i in rows(table, pk, "SALARY#")]
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        if method != "POST":
            raise ValueError("Use POST to configure a salary")
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
        before = table.get_item(Key={"PK": pk, "SK": item["SK"]}, ConsistentRead=True).get("Item")
        version = (before or {}).get("version")
        item["version"] = (version or 0) + 1
        salary_put = {"TableName": table.name, "Item": item,
                      "ConditionExpression": "attribute_not_exists(#v)" if version is None else "#v = :v",
                      "ExpressionAttributeNames": {"#v": "version"}}
        if version is not None:
            salary_put["ExpressionAttributeValues"] = {":v": version}
        storage.transact(table, [
            {"Put": salary_put},
            {"Update": {"TableName": table.name, "Key": {"PK": pk, "SK": "SALARY-REVISION"}, "UpdateExpression": "ADD revision :one", "ExpressionAttributeValues": {":one": 1}}},
            audit.operation(identity, "salary.configured", before, item),
        ])
        return clean(item)
    if method == "GET":
        if query.get('month'):
            month = query['month']
            if len(month) != 7:
                raise ValueError('Month must use YYYY-MM')
            date.fromisoformat(month + '-01')
            item = table.get_item(Key={'PK': pk, 'SK': f'PAYROLL#{month}'}, ConsistentRead=True).get('Item')
            return [clean(payroll_records.expand(table, item))] if item else []
        return [clean(payroll_records.expand(table, i)) for i in rows(table, pk, "PAYROLL#")]
    require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
    month = str(body.get("month", ""))
    date.fromisoformat(month + "-01")
    if len(month) != 7:
        raise ValueError("Month must use YYYY-MM")
    key = {"PK": pk, "SK": f"PAYROLL#{month}"}
    old = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if path.endswith("/disburse"):
        if method != "POST":
            raise ValueError("Use POST to record payroll settlement")
        if not old:
            raise ValueError("Generate and review this payroll cycle first")
        payroll_records.expand(table, old)  # Verify the snapshot before recording money.
        if body.get("expectedCreatedAt") is not None and body["expectedCreatedAt"] != old["createdAt"]:
            raise ValueError("Payroll was regenerated. Refresh and review the current cycle before recording payment.")
        if old.get("status") == "Paid":
            return clean(payroll_records.expand(table, old))
        if not str(body.get("reference", "")).strip():
            raise ValueError("An external payment reference is required")
        updated = dict(old, status="Paid", paymentReference=body["reference"].strip(), paidAt=now(), paidBy=identity.user_id)
        audit.put(table, identity, updated, before=old, action="payroll.settled",
                  ConditionExpression="#s = :pending AND createdAt = :created", ExpressionAttributeNames={"#s": "status"},
                  ExpressionAttributeValues={":pending": "Pending", ":created": old["createdAt"]})
        return clean(payroll_records.expand(table, updated))
    if method != "POST":
        raise ValueError("Payroll cycles are immutable")
    if old:
        return clean(payroll_records.expand(table, old))
    workforce = get_table(("WORKFORCE_TABLE"))
    revision_key = {"PK": pk, "SK": f"MONTH#{month}"}
    revision = workforce.get_item(Key=revision_key, ConsistentRead=True).get("Item", {}).get("revision")
    salary_revision_key = {"PK": pk, "SK": "SALARY-REVISION"}
    salary_revision = table.get_item(Key=salary_revision_key, ConsistentRead=True).get("Item", {}).get("revision")
    workers = labour_list(workforce, pk, month + "-01")
    staff = []
    for w in workers:
        gross = w["grossWages"]
        if w["advanceDeductions"] + w["dailyPaid"] > gross:
            raise ValueError(f"Deductions exceed wages for {w['name']}; correct deductions before generating payroll")
        staff.append({"labourId": w["labourAttendanceId"], "name": w["name"], "type": w.get("type", "Labour"), "rate": w["rate"], "daysPresent": w["daysPresent"],
                      "nightShifts": w["nightShifts"], "gross": gross, "deductions": w["advanceDeductions"], "dailyPaid": w["dailyPaid"], "net": gross - w["advanceDeductions"] - w["dailyPaid"]})
    profiles = rows(table, pk, "SALARY#")
    employees = _batch_get(get_table("USERS_TABLE"), [
        {"PK": f"ORG#{org}", "SK": f"USER#{profile['employeeId']}"} for profile in profiles
    ])
    for profile in profiles:
        employee = employees.get((f"ORG#{org}", f"USER#{profile['employeeId']}"))
        if not employee or employee.get("status") != "Active":
            continue
        gross = sum(profile.get(f, 0) for f in ["basic", "hra", "conveyance", "medical"])
        deductions = sum(profile.get(f, 0) for f in ["pf", "esi", "tds"])
        staff.append({"labourId": profile["employeeId"], "name": profile["name"], "gross": gross, "deductions": deductions, "net": gross - deductions, "salary": clean(profile)})
    if not staff:
        raise ValueError("Register workers or configure staff salaries first")
    item = {**key, "cycleId": (model.new_id("payroll")), "month": month, "projectId": pid, "orgId": org, "entityType": "payroll", "status": "Pending",
            "staff": staff, "amount": sum(w["net"] for w in staff), "createdAt": now(), "createdBy": identity.user_id}
    build = (payroll_records.save_entries(table, item))
    check = {"TableName": workforce.name, "Key": revision_key, "ConditionExpression": "attribute_not_exists(revision)" if revision is None else "revision = :revision"}
    if revision is not None:
        check["ExpressionAttributeValues"] = {":revision": revision}
    salary_check = {"TableName": table.name, "Key": salary_revision_key,
                    "ConditionExpression": "attribute_not_exists(revision)" if salary_revision is None else "revision = :revision"}
    if salary_revision is not None:
        salary_check["ExpressionAttributeValues"] = {":revision": salary_revision}
    operations = [
        {"Put": {"TableName": table.name, "Item": item, "ConditionExpression": "attribute_not_exists(PK)"}},
        {"ConditionCheck": check},
        {"ConditionCheck": salary_check},
        audit.operation(identity, "payroll.generated", after=item),
    ]
    if build:
        operations.append(payroll_records.publish_operation(table, build))
    storage.transact(table, operations)
    return clean(payroll_records.expand(table, item))
