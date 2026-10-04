import pytest
from decimal import Decimal
from test_security_workflows import database, data, put
from test_feature_workflows import call, SITE, vendor
from test_daily_wages import worker, confirm, report, PATH
from common.dynamo import get_table
from handlers import consolidated as api


def audit_events():
    return get_table("AUDIT_EVENTS_TABLE").scan()["Items"]


@pytest.mark.parametrize("status,night,wage", [("Present", False, 800), ("Half Day", False, 400), ("Present", True, 1600), ("Half Day", True, 800)])
def test_half_day_and_night_shift_use_the_saved_wage(database, status, night, wage):
    body = worker()
    confirm(body)
    saved = data(call(api.workforce_handler, PATH, "POST", dict(body, status=status, nightShift=night), **SITE))
    assert saved["wage"] == wage
    assert report()[0]["grossWages"] == wage
    assert data(call(api.governance_handler, "/projects/P-A/payroll", "POST", {"month": "2026-09"}))["amount"] == wage


def test_paid_wage_correction_preserves_history_and_rejects_stale_writes(database):
    body = worker()
    confirm(body)
    paid = data(call(api.workforce_handler, PATH, "POST", dict(body, paymentStatus="Paid"), **SITE))
    before = len(audit_events())
    data(call(api.workforce_handler, PATH, "POST", dict(body, paymentStatus="Paid"), **SITE))
    assert len(audit_events()) == before
    assert call(api.workforce_handler, PATH, "POST", body, **SITE)["statusCode"] == 400
    corrected = data(call(api.workforce_handler, PATH, "POST", dict(body, expectedVersion=paid["version"], correctionReason="Payment was entered for the wrong worker"), **SITE))
    assert corrected["version"] == paid["version"] + 1
    history = [item for item in audit_events() if item["action"] == "daily-wage.corrected"]
    assert len(history) == 1
    assert history[0]["before"]["paymentStatus"] == "Paid"
    assert history[0]["after"]["paymentStatus"] == "Not paid"
    assert history[0]["actorId"] == "site-a"
    assert history[0]["reason"]
    assert call(api.workforce_handler, PATH, "POST", dict(body, paymentStatus="Paid", expectedVersion=paid["version"]), **SITE)["statusCode"] == 400
    assert report()[0]["dailyPaid"] == 0


@pytest.mark.parametrize("changes", [{"budgetedQty": -1}, {"rate": -1}, {"budgetedQty": True}, {"rate": "20"}])
def test_boq_validation_covers_create_and_partial_update(database, changes):
    body = {"code": "C", "description": "Concrete", "category": "Structure", "unit": "m3", "budgetedQty": 10, "rate": 100}
    assert call(api.project_commercial_handler, "/projects/P-A/boq", "POST", dict(body, **changes))["statusCode"] == 400
    boq = data(call(api.project_commercial_handler, "/projects/P-A/boq", "POST", body))
    assert boq["amount"] == 1000
    path = "/projects/P-A/boq/" + boq["boqId"]
    assert call(api.project_commercial_handler, path, "PATCH", changes)["statusCode"] == 400
    assert data(call(api.project_commercial_handler, path, "PATCH", {"rate": 50}))["amount"] == 500


def test_bill_caps_are_validated_at_creation_update_and_approval(database, vendor):
    path = "/projects/P-A/bills"
    assert call(api.finance_handler, path, "POST", {"grossAmount": 100, "netPayable": 1000})["statusCode"] == 400
    bill = data(call(api.finance_handler, path, "POST", {"grossAmount": 100, "netPayable": 80}))
    bill_path = path + "/" + bill["billId"]
    assert call(api.finance_handler, bill_path, "PATCH", {"grossAmount": 70})["statusCode"] == 400
    data(call(api.finance_handler, bill_path, "PATCH", {"status": "Approved"}))
    payment = data(call(api.finance_handler, "/projects/P-A/payments", "POST", {"amount": 81, "vendorId": "vendor-1", "materialId": "cement", "billId": bill["billId"]}))
    assert call(api.finance_handler, "/projects/P-A/payments/" + payment["paymentId"], "PATCH", {"status": "Approved"})["statusCode"] == 400
    assert not any(event["action"] == "payment.approved" for event in audit_events())


def test_posted_ledger_agrees_with_portfolio_totals_and_excludes_pending(database):
    expense = data(call(api.finance_handler, "/projects/P-A/expenses", "POST", {"amount": 100, "status": "Pending"}))
    assert len(data(call(api.projects_handler, "/projects/P-A/spending"))) == 1  # opening
    data(call(api.finance_handler, "/projects/P-A/expenses/" + expense["expenseId"], "PATCH", {"status": "Approved"}))
    body = worker()
    confirm(body)
    data(call(api.workforce_handler, PATH, "POST", dict(body, paymentStatus="Paid"), **SITE))
    ledger = data(call(api.projects_handler, "/projects/P-A/spending"))
    assert sum(entry["amount"] for entry in ledger) == data(call(api.projects_handler, "/projects/P-A"))["spent"] == 920
    assert call(api.projects_handler, "/projects/P-A/spending", **SITE)["statusCode"] == 403
    assert call(api.projects_handler, "/projects/P-B/spending")["statusCode"] == 403


def test_financial_partial_updates_reject_zero_and_invalid_dates(database):
    expense = data(call(api.finance_handler, "/projects/P-A/expenses", "POST", {"amount": 100}))
    path = "/projects/P-A/expenses/" + expense["expenseId"]
    for changes in ({"amount": 0}, {"date": "2026-02-30"}, {"date": 20261004}):
        assert call(api.finance_handler, path, "PATCH", changes)["statusCode"] == 400


def test_audit_failure_rolls_back_the_financial_write(database, monkeypatch):
    from common import audit
    original = audit.operation
    def invalid(*args, **kwargs):
        entry = original(*args, **kwargs)
        entry["Put"]["ConditionExpression"] = "attribute_exists(PK)"
        return entry
    monkeypatch.setattr(audit, "operation", invalid)
    assert call(api.finance_handler, "/projects/P-A/expenses", "POST", {"amount": 100})["statusCode"] == 409
    assert data(call(api.finance_handler, "/projects/P-A/expenses")) == []
    assert audit_events() == []


def test_regenerated_payroll_cannot_be_settled_from_an_old_review(database):
    body = worker()
    confirm(body)
    data(call(api.workforce_handler, PATH, "POST", body, **SITE))
    cycle = data(call(api.governance_handler, "/projects/P-A/payroll", "POST", {"month": "2026-09"}))
    data(call(api.governance_handler, "/projects/P-A/payroll/2026-09", "DELETE"))
    data(call(api.governance_handler, "/projects/P-A/payroll", "POST", {"month": "2026-09"}))
    result = call(api.governance_handler, "/projects/P-A/payroll/disburse", "POST", {"month": "2026-09", "reference": "Bank 1", "expectedCreatedAt": cycle["createdAt"]})
    assert result["statusCode"] == 400


def test_spending_route_is_allowed_but_unknown_routes_still_fail(database):
    assert call(api.projects_handler, "/projects/P-A/spending")["statusCode"] == 200
    assert call(api.projects_handler, "/projects/P-A/spending", "POST")["statusCode"] == 404
    assert call(api.projects_handler, "/projects/P-A/not-spending")["statusCode"] == 404


def test_stale_supervisor_assignment_cannot_remove_a_newer_assignment(database):
    project = data(call(api.projects_handler, "/projects/P-A"))
    first = data(call(api.projects_handler, "/projects/P-A", "PUT", {"supervisorIds": ["site-a", "unassigned"], "expectedVersion": project.get("version", 0)}))
    stale = call(api.projects_handler, "/projects/P-A", "PUT", {"supervisorIds": ["site-a"], "expectedVersion": project.get("version", 0)})
    assert stale["statusCode"] == 409
    assert data(call(api.projects_handler, "/projects/P-A"))["supervisorIds"] == ["site-a", "unassigned"]
    assert any(entry["action"] == "project.updated" and entry["after"]["supervisorIds"] == first["supervisorIds"] for entry in audit_events())


def test_salary_change_during_generation_rejects_the_inconsistent_snapshot(database, monkeypatch):
    from common import workflows
    salary = {"employeeId": "site-a", "basic": 1000}
    data(call(api.governance_handler, "/projects/P-A/payroll/staff", "POST", salary))
    original = workflows.rows
    def change_after_read(table, pk=None, prefix=None, org=None):
        result = original(table, pk, prefix, org)
        if prefix == "SALARY#":
            data(call(api.governance_handler, "/projects/P-A/payroll/staff", "POST", dict(salary, basic=2000)))
        return result
    monkeypatch.setattr(workflows, "rows", change_after_read)
    assert call(api.governance_handler, "/projects/P-A/payroll", "POST", {"month": "2026-09"})["statusCode"] == 409
    assert "Item" not in get_table("SETTINGS_TABLE").get_item(Key={"PK": "ORG#ORG-A#PROJECT#P-A", "SK": "PAYROLL#2026-09"})


def test_half_day_currency_rounding_is_saved_consistently(database):
    from common.workflows import daily_wage
    assert daily_wage(Decimal("800.01"), "Half Day") == Decimal("400.01")
