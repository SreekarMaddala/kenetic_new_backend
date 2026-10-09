from test_security_workflows import ORG_A, PROJECT_A
from test_security_workflows import database, event, data, put
from handlers import consolidated as api
from common import model, reconciliation
from common.authz import active_identity


def test_financial_report_filters_transactions_not_project_creation(database):
    for suffix, day, amount in [("one", "2026-09-01", 100), ("last", "2026-09-30", 200), ("older", "2026-08-31", 400)]:
        put("FINANCE_TABLE", f"ORG#{ORG_A}#PROJECT#{PROJECT_A}", f"PAYMENT#{suffix}", orgId=ORG_A, projectId=PROJECT_A, entityType="payment", status="Approved", date=day, amount=amount)
    worker = model.new_id('worker', 'report-worker')
    put("WORKFORCE_TABLE", f"ORG#{ORG_A}#PROJECT#{PROJECT_A}", model.daily_key(worker, '2026-09-02'), orgId=ORG_A, projectId=PROJECT_A, workerId=worker, labourId=worker, entityType="daily-wage", paymentStatus="Paid", status='Present', rate=800, date="2026-09-02", wage=800)
    reconciliation.reconcile(active_identity(event('/reports/executive')), ORG_A, PROJECT_A, apply=True)
    report = data(api.governance_handler(event("/reports/executive", query={"startDate": "2026-09-01", "endDate": "2026-09-30"}), None))
    assert report["projectSummaries"][0]["spent"] == 1100
    overall = data(api.governance_handler(event("/reports/executive"), None))
    assert overall["projectSummaries"][0]["spent"] == 1520


def test_invalid_report_ranges_are_rejected(database):
    for query in [{"startDate": "2026-09-01"}, {"startDate": "2026-09-30", "endDate": "2026-09-01"}, {"startDate": "2026-02-30", "endDate": "2026-03-01"}]:
        assert api.governance_handler(event("/reports/executive", query=query), None)["statusCode"] == 400
