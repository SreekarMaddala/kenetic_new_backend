from test_security_workflows import ORG_A, PROJECT_A, PROJECT_B, PROJECT_C, UNASSIGNED
from test_security_workflows import database, event, data, put
from test_feature_workflows import call, SITE
from handlers import consolidated as api
from common.dynamo import get_table
import pytest

PATH = "/supervisor/labour/attendance"

def worker():
    item = data(call(api.workforce_handler, PATH, "POST", {"projectId": PROJECT_A, "name": "Worker", "rate": 800}))
    return {"projectId": PROJECT_A, "labourAttendanceId": item["labourAttendanceId"], "date": "2026-09-01", "status": "Present", "paymentStatus": "Not paid"}

def confirm(body):
    return data(call(api.workforce_handler, PATH, "POST", dict(body, operation="allocate")))

def report(day="2026-09-01", pid=PROJECT_A, **kwargs):
    return data(call(api.workforce_handler, PATH, query={"projectId": pid, "date": day}, **kwargs))

def test_only_admin_can_unlock_attendance_and_each_date_requires_confirmation(database):
    body = worker()
    assert call(api.workforce_handler, PATH, "POST", body, **SITE)["statusCode"] == 403
    assert call(api.workforce_handler, PATH, "POST", dict(body, operation="allocate"), **SITE)["statusCode"] == 403
    confirm(body)
    data(call(api.workforce_handler, PATH, "POST", body, **SITE))
    next_day = report("2026-09-02", **SITE)[0]
    assert next_day["allocatedProjectId"] == PROJECT_A
    assert not next_day["allocationConfirmed"] and not next_day["allocationStarted"]
    assert call(api.workforce_handler, PATH, "POST", dict(body, date="2026-09-02"), **SITE)["statusCode"] == 403
    confirm(dict(body, date="2026-09-02"))
    data(call(api.workforce_handler, PATH, "POST", dict(body, date="2026-09-02", status="Absent")))
    assert report("2026-09-02")[0]["daysPresent"] == 1

@pytest.mark.parametrize("changes", [{"paymentStatus": "Partial"}, {"status": "Absent", "paymentStatus": "Paid"}, {"status": "Absent", "nightShift": True}, {"nightShift": "yes"}])
def test_rejects_invalid_daily_options(database, changes):
    body = worker()
    confirm(body)
    assert call(api.workforce_handler, PATH, "POST", dict(body, **changes), **SITE)["statusCode"] == 400

def test_daily_payment_updates_are_idempotent_and_payroll_only_pays_balance(database):
    body = worker()
    confirm(body)
    data(call(api.workforce_handler, PATH, "POST", body, **SITE))
    assert report()[0]["unpaidWages"] == 800
    for _ in range(2):
        data(call(api.workforce_handler, PATH, "POST", dict(body, paymentStatus="Paid"), **SITE))
    row = report()[0]
    assert row["dailyPaid"] == 800 and row["unpaidWages"] == 0
    assert data(call(api.projects_handler, f"/projects/{PROJECT_A}"))["spent"] == 820
    second = dict(body, date="2026-09-02")
    confirm(second)
    data(call(api.workforce_handler, PATH, "POST", second, **SITE))
    cycle = data(call(api.governance_handler, f"/projects/{PROJECT_A}/payroll", "POST", {"month": "2026-09"}))
    assert cycle["amount"] == 800 and cycle["staff"][0]["dailyPaid"] == 800
    data(call(api.governance_handler, f"/projects/{PROJECT_A}/payroll/disburse", "POST", {"month": "2026-09", "reference": "balance receipt", "expectedCreatedAt": cycle["createdAt"]}))
    assert data(call(api.projects_handler, f"/projects/{PROJECT_A}"))["spent"] == 1620

def test_admin_can_transfer_worker_and_latest_allocation_is_default(database):
    put("PROJECTS_TABLE", f"ORG#{ORG_A}", f"PROJECT#{PROJECT_C}", orgId=ORG_A, projectId=PROJECT_C, supervisorIds=[UNASSIGNED], entityType="project", name="Site C")
    body = worker()
    confirm(dict(body, targetProjectId=PROJECT_C))
    assert report(**SITE) == []
    assert call(api.workforce_handler, PATH, "POST", body, **SITE)["statusCode"] == 403
    moved = dict(body, projectId=PROJECT_C)
    data(call(api.workforce_handler, PATH, "POST", moved, sub=UNASSIGNED, role="supervisor"))
    next_day = report("2026-09-02", PROJECT_C)[0]
    assert next_day["allocatedProjectId"] == PROJECT_C and not next_day["allocationConfirmed"]
    assert call(api.workforce_handler, PATH, "POST", dict(body, operation="allocate"))["statusCode"] == 400
    assert call(api.workforce_handler, PATH, "POST", dict(body, date="2026-09-02", operation="allocate", targetProjectId=PROJECT_B))["statusCode"] == 403
    assert report("2026-09-01", PROJECT_C)[0]["unpaidWages"] == 800
    assert report("2026-09-01", PROJECT_A) == []

def test_payment_correction_recalculates_amount_owed(database):
    body = worker()
    confirm(body)
    data(call(api.workforce_handler, PATH, "POST", dict(body, paymentStatus="Paid"), **SITE))
    data(call(api.workforce_handler, PATH, "POST", dict(body, expectedVersion=1, correctionReason="Incorrect payment recorded; no payment was made"), **SITE))
    row = report()[0]
    assert row["dailyPaid"] == 0 and row["unpaidWages"] == 800
    assert data(call(api.projects_handler, f"/projects/{PROJECT_A}"))["spent"] == 20
