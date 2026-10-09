"""Worker registration, allocations, attendance and deductions."""
from common.authz import AuthorizationError, OPERATIONS_ADMIN, SUPERVISOR, SUPER_ADMIN, require_role
from collections import defaultdict
from decimal import Decimal
from boto3.dynamodb.conditions import Key
from common import audit, model, storage, worker_views
from datetime import date
from common.dynamo import get_table
from common.workflow_helpers import clean, daily_wage, now, number, re_id, rows


def labour_list(table, pk, day, roster=False, selected_workers=None):
    date.fromisoformat(day)
    org, pid = pk.removeprefix("ORG#").split("#PROJECT#", 1)
    if (selected_workers is not None):
        result = []
        keys = [key for worker in selected_workers for key in ({'PK': pk, 'SK': model.daily_key(worker['workerId'], day)}, {'PK': pk, 'SK': f"WORKER-MONTH#{day[:7]}#{worker['workerId']}"})]
        snapshots = worker_views.batch_rows(table, keys)
        for worker in selected_workers:
            wid = worker['workerId']
            current = snapshots.get((pk, model.daily_key(wid, day)), {})
            month = snapshots.get((pk, f'WORKER-MONTH#{day[:7]}#{wid}'), {})
            prefix = f'ALLOCATION#{wid}#'
            allocations = table.query(KeyConditionExpression=Key('PK').eq(f'ORG#{org}') & Key('SK').between(prefix, prefix + day),
                                      ScanIndexForward=False, Limit=1, ConsistentRead=True).get('Items', [])
            allocation = allocations[0] if allocations else {}
            allocated_project = allocation.get('projectId', worker.get('projectId'))
            if not roster and allocated_project != pid and not month:
                continue
            gross, paid = month.get('grossWages', 0), month.get('dailyPaid', 0)
            result.append(dict(clean(worker), status=current.get('status', 'Absent'), nightShift=current.get('nightShift', False),
                               attendanceRecorded=bool(current), paymentStatus=current.get('paymentStatus', 'Not paid'),
                               allocatedProjectId=allocated_project, allocationDate=allocation.get('date'),
                               allocationConfirmed=allocation.get('date') == day and allocated_project == pid,
                               allocationStarted=allocation.get('date') == day and allocation.get('attendanceStarted', False),
                               allocationVersion=allocation.get('version', 0) if allocation.get('date') == day else 0,
                               dailyWage=current.get('wage', 0), grossWages=gross, dailyPaid=paid, unpaidWages=max(0, gross - paid),
                               date=day, attendanceVersion=current.get('version', 0), daysPresent=month.get('daysPresent', 0),
                               nightShifts=month.get('nightShifts', 0), advanceDeductions=month.get('advanceDeductions', 0), deductions=[]))
        return result
    workers = (rows(table, f'ORG#{org}', 'WORKER#'))
    allocations = rows(table, f"ORG#{org}", "ALLOCATION#")
    daily = rows(table, pk, (f"DAILY#{day[:7]}"))
    debits = rows(table, pk, (f"DEBIT#{day[:7]}"))
    logs_by_worker = defaultdict(list)
    deductions_by_worker = defaultdict(list)
    latest_allocation = {}
    for record in daily:
        logs_by_worker[record['labourId']].append(record)
    for record in debits:
        deductions_by_worker[record['labourId']].append(record)
    for allocation in allocations:
        worker_id = allocation['labourId']
        if allocation['date'] <= day and allocation['date'] > latest_allocation.get(worker_id, {}).get('date', ''):
            latest_allocation[worker_id] = allocation
    result = []
    for w in workers:
        wid = w["labourAttendanceId"]
        logs = logs_by_worker[wid]
        current = next((r for r in logs if r["date"] == day), {})
        allocation = latest_allocation.get(wid, {})
        allocated_project = allocation.get("projectId", w.get("projectId"))
        if not roster and allocated_project != pid and not logs:
            continue
        deductions = deductions_by_worker[wid]
        gross = sum(r.get("wage", daily_wage(r.get("rate", w["rate"]), r["status"], bool(r.get("nightShift")))) for r in logs)
        paid = sum(r.get("wage", 0) for r in logs if r.get("paymentStatus") == "Paid")
        result.append(dict(clean(w), status=current.get("status", "Absent"), nightShift=current.get("nightShift", False),
                           attendanceRecorded=bool(current), paymentStatus=current.get("paymentStatus", "Not paid"),
                           allocatedProjectId=allocated_project, allocationDate=allocation.get("date"),
                           allocationConfirmed=allocation.get("date") == day and allocated_project == pid,
                           allocationStarted=allocation.get("date") == day and allocation.get("attendanceStarted", False),
                           dailyWage=current.get("wage", w["rate"] if current.get("status") == "Present" else 0),
                           grossWages=gross, dailyPaid=paid, unpaidWages=max(0, gross - paid),
                           date=day, attendanceVersion=current.get("version", 0), daysPresent=sum({"Present": 1, "Half Day": Decimal("0.5")}.get(r["status"], 0) for r in logs),
                           nightShifts=sum(bool(r.get("nightShift")) for r in logs),
                           advanceDeductions=sum(r["amount"] for r in deductions), deductions=[clean(r) for r in deductions]))
    return result


def labour(identity, method, body, query, pk, org, pid):
    table = get_table(("WORKFORCE_TABLE"))
    day = str(body.get("date") or query.get("date") or now()[:10])
    date.fromisoformat(day)
    if not pid:
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        if method == "GET":
            workers = (rows(table, f'ORG#{org}', 'WORKER#'))
            return [clean(w) for w in workers]
        operation = body.get("operation", "attendance" if body.get("workerId") or body.get("labourAttendanceId") else "register")
        if method != "POST" or operation != "register":
            raise ValueError("A projectId is required for allocation and attendance")
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
    if body.get('workerId') and body.get('labourAttendanceId') and body['workerId'] != body['labourAttendanceId']:
        raise ValueError('Conflicting worker identifiers')
    if body.get('requestId') and not re_id(str(body['requestId'])):
        raise ValueError('Invalid requestId')
    operation = body.get("operation", "attendance" if body.get("workerId") or body.get("labourAttendanceId") else "register")
    wid = body.get('workerId') or body.get('labourAttendanceId') or ((model.new_id('worker', body.get('requestId'))))
    model.validate_id('worker', wid)
    key = {"PK": (f'ORG#{org}'), "SK": (f'WORKER#{wid}')}
    worker = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if operation == "register":
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        if not str(body.get("name", "")).strip():
            raise ValueError("Worker name is required")
        rate = number(body.get("rate"), "Daily rate", True)
        item = {**key, "labourAttendanceId": wid, "workerId": wid, "name": body["name"], "rate": rate, "type": body.get("type", "Skilled"),
                "bankName": body.get("bankName", ""), "accNo": body.get("accNo", ""), "entityType": "worker", "orgId": org,
                "projectId": pid, "createdAt": now(), "createdBy": identity.user_id}
        if (worker):
            if any(worker.get(field) != item.get(field) for field in ('name', 'rate', 'type', 'bankName', 'accNo', 'projectId')):
                raise ValueError('Worker ID already used for different data')
            return clean(worker)
        audit.put(table, identity, item, ConditionExpression="attribute_not_exists(PK)")
        return clean(item)
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
        item = {**allocation_key, "entityType": "worker-allocation", "labourId": wid, "projectId": target,
                "orgId": org, "date": day, "confirmedBy": identity.user_id, "confirmedAt": now()}
        allocation_put = {"TableName": table.name, "Item": item, "ConditionExpression": "attribute_not_exists(attendanceStarted)"}
        prior = table.get_item(Key=allocation_key, ConsistentRead=True).get('Item', {})
        version = prior.get('version', 0)
        if prior.get('attendanceStarted'):
            raise ValueError('Attendance has already started for this worker and date; the allocation is locked')
        if body.get('expectedVersion') is not None and body['expectedVersion'] != version:
            raise ValueError('Allocation changed. Refresh the worker list before confirming.')
        item['version'] = version + 1
        allocation_put['ExpressionAttributeNames'] = {'#version': 'version'}
        if 'version' in prior:
            allocation_put['ConditionExpression'] += ' AND #version = :version'
            allocation_put['ExpressionAttributeValues'] = {':version': version}
        else:
            allocation_put['ConditionExpression'] += ' AND attribute_not_exists(#version)'
        storage.transact(table, [
            {"ConditionCheck": {"TableName": get_table(("PAYROLL_TABLE")).name, "Key": {"PK": target_pk, "SK": f"PAYROLL#{day[:7]}"}, "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": allocation_put},
            audit.operation(identity, "worker.allocated", before=(prior), after=item),
        ])
        return clean(item)
    cycle = get_table(("PAYROLL_TABLE")).get_item(Key={"PK": pk, "SK": f"PAYROLL#{day[:7]}"}, ConsistentRead=True).get("Item")
    if cycle:
        raise ValueError("Attendance and deductions are locked after the payroll cycle is generated")
    if operation == "debit":
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        amount = number(body.get("amount"), "Deduction", True)
        if not str(body.get("description", "")).strip():
            raise ValueError("Deduction description is required")
        deduction_id = (model.new_id("deduction", body.get("requestId")))
        item = {"PK": pk, "SK": model.deduction_key(day, deduction_id), "entityType": "deduction", "deductionId": deduction_id, "projectId": pid, "workerId": wid, "labourId": wid, "amount": amount,
                "description": body["description"], "date": day, "orgId": org, "createdBy": identity.user_id, "requestId": body.get('requestId')}
        prior = table.get_item(Key={'PK': pk, 'SK': item['SK']}, ConsistentRead=True).get('Item')
        if prior:
            if any(prior.get(field) != item.get(field) for field in ('workerId', 'amount', 'description', 'date')):
                raise ValueError('Request ID already used for a different deduction')
            return clean(prior)
        storage.transact(table, [
            {"ConditionCheck": {"TableName": get_table(("PAYROLL_TABLE")).name, "Key": {"PK": pk, "SK": f"PAYROLL#{day[:7]}"}, "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": table.name, "Item": item, "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Update": {"TableName": table.name, "Key": {"PK": pk, "SK": f"MONTH#{day[:7]}"}, "UpdateExpression": "ADD revision :one", "ExpressionAttributeValues": {":one": 1}}},
            audit.operation(identity, "worker.deduction-created", after=item),
        ])
        return clean(item)
    if operation != "attendance" or body.get("status") not in {"Present", "Half Day", "Absent"}:
        raise ValueError("Select Present, Half Day, or Absent")
    night_shift = body.get("nightShift", False)
    if not isinstance(night_shift, bool):
        raise ValueError("Night shift must be true or false")
    if body["status"] == "Absent" and night_shift:
        raise ValueError("An absent worker cannot have a night shift")
    payment_status = body.get("paymentStatus", "Not paid")
    if payment_status not in {"Paid", "Not paid"}:
        raise ValueError("Select Paid or Not paid")
    if body["status"] == "Absent" and payment_status == "Paid":
        raise ValueError("An absent worker has no daily wage to mark paid")
    allocation = table.get_item(Key=allocation_key, ConsistentRead=True).get("Item")
    if not allocation or allocation.get("projectId") != pid:
        raise AuthorizationError("The admin must confirm this worker's project allocation for this date first")
    existing = table.get_item(Key={"PK": pk, "SK": model.daily_key(wid, day)}, ConsistentRead=True).get("Item", {})
    version = existing.get("version", 0)
    expected = body.get("expectedVersion")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, (int, Decimal))):
        raise ValueError("Attendance changed. Refresh before saving your correction.")
    rate = existing.get("rate", worker["rate"])
    item = {"PK": pk, "SK": model.daily_key(wid, day), "labourId": wid, "date": day, "status": body["status"],
            "entityType": "daily-wage", "workerId": wid, "attendanceId": (model.new_id("attendance", f"{org}|{wid}|{day}")), "paymentStatus": payment_status, "rate": rate,
            "wage": daily_wage(rate, body["status"], night_shift),
            "nightShift": night_shift, "orgId": org, "projectId": pid, "updatedAt": now(),
            "createdAt": existing.get("createdAt", now()), "createdBy": existing.get("createdBy", identity.user_id),
            "updatedBy": identity.user_id, "version": version + 1}
    if existing and all(existing.get(field, False if field == "nightShift" else None) == item[field]
                        for field in ("status", "paymentStatus", "wage", "nightShift")):
        return clean(existing)
    if expected is not None and expected != version:
        raise ValueError("Attendance changed. Refresh before saving your correction.")
    reason = body.get("correctionReason")
    if existing.get("paymentStatus") == "Paid":
        if expected is None or not isinstance(reason, str) or not reason.strip():
            raise ValueError("A reason and current attendance version are required to correct a paid wage")
        if len(reason.strip()) > 500:
            raise ValueError("Correction reason must be at most 500 characters")
    put_operation = {"TableName": table.name, "Item": item,
                     "ConditionExpression": "attribute_not_exists(#v)" if "version" not in existing else "#v = :v",
                     "ExpressionAttributeNames": {"#v": "version"}}
    if "version" in existing:
        put_operation["ExpressionAttributeValues"] = {":v": version}
    storage.transact(table, [
        {"Update": {"TableName": table.name, "Key": allocation_key, "UpdateExpression": "SET attendanceStarted = :yes", "ConditionExpression": "projectId = :pid", "ExpressionAttributeValues": {":yes": True, ":pid": pid}}},
        {"ConditionCheck": {"TableName": get_table(("PAYROLL_TABLE")).name, "Key": {"PK": pk, "SK": f"PAYROLL#{day[:7]}"}, "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Put": put_operation},
        {"Update": {"TableName": table.name, "Key": {"PK": pk, "SK": f"MONTH#{day[:7]}"}, "UpdateExpression": "ADD revision :one", "ExpressionAttributeValues": {":one": 1}}},
        audit.operation(identity, "daily-wage.corrected" if existing else "daily-wage.created", existing or None, item, reason.strip() if isinstance(reason, str) else None),
    ])
    return clean(item)
