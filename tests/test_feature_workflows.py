from test_security_workflows import database, event, data, put
from handlers import consolidated as api
from common.authz import SUPERVISOR
from common.dynamo import get_table
import pytest

@pytest.fixture(autouse=True)
def vendor(database):
    put("PARTIES_TABLE", "ORG#ORG-A", "VENDOR#vendor-1", orgId="ORG-A", vendorId="vendor-1", name="Supplier", entityType="vendor")

SITE = {"sub": "site-a", "role": SUPERVISOR}


def call(handler, path, method="GET", body=None, **kwargs):
    return handler(event(path, method, body, **kwargs), None)


def test_supervisor_cannot_self_approve_or_reclassify_material():
    path = "/supervisor/materials/indents"
    body = {"projectId": "P-A", "materialName": "Cement", "quantity": 5, "unit": "Bags"}
    assert call(api.field_operations_handler, path, "POST", dict(body, status="Approved"), **SITE)["statusCode"] == 403
    indent = data(call(api.field_operations_handler, path, "POST", body, **SITE))
    item_path = path + "/" + indent["materialId"]
    assert call(api.field_operations_handler, item_path, "PUT", {"status": "approved"}, query={"projectId": "P-A"}, **SITE)["statusCode"] == 403
    approved = data(call(api.field_operations_handler, item_path, "PATCH", {"status": "approved"}, query={"projectId": "P-A"}))
    assert approved["approvedBy"] == "admin-a"
    assert call(api.field_operations_handler, item_path, "PATCH", {"status": "rejected"}, query={"projectId": "P-A"})["statusCode"] == 400
    assert data(call(api.field_operations_handler, "/supervisor/materials/stock", query={"projectId": "P-A"}, **SITE)) == []


def test_daily_attendance_is_idempotent_and_monthly_deductions_persist():
    path = "/supervisor/labour/attendance"
    worker = data(call(api.workforce_handler, path, "POST", {"projectId": "P-A", "name": "Worker", "rate": 800}))
    wid = worker["labourAttendanceId"]
    body = {"projectId": "P-A", "labourAttendanceId": wid, "date": "2026-09-01", "status": "Present"}
    data(call(api.workforce_handler, path, "POST", dict(body, operation="allocate")))
    for _ in range(2):
        data(call(api.workforce_handler, path, "POST", body, **SITE))
    data(call(api.workforce_handler, path, "POST", dict(body, date="2026-09-02", operation="allocate")))
    data(call(api.workforce_handler, path, "POST", dict(body, date="2026-09-02", status="Absent"), **SITE))
    data(call(api.workforce_handler, path, "POST", {"projectId": "P-A", "operation": "debit", "labourAttendanceId": wid, "date": "2026-09-02", "amount": 100, "description": "Advance"}))
    result = data(call(api.workforce_handler, path, query={"projectId": "P-A", "date": "2026-09-02"}))[0]
    assert result["status"] == "Absent" and result["daysPresent"] == 1 and result["advanceDeductions"] == 100
    assert call(api.workforce_handler, path, "POST", dict(body, projectId="P-B"), **SITE)["statusCode"] == 403
    assert "accNo" in worker and worker["accNo"] == ""


def test_stock_receipt_transfer_is_atomic_and_idempotent():
    receive = {"item": "Cement", "unit": "Bags", "qty": 10, "rate": 50, "requestId": "receipt-0001"}
    data(call(api.supply_chain_handler, "/warehouse/grn", "POST", receive))
    data(call(api.supply_chain_handler, "/warehouse/grn", "POST", receive))
    issue = {"item": "Cement", "unit": "Bags", "qty": 4, "targetProjectId": "P-A", "requestId": "issue-000001"}
    data(call(api.supply_chain_handler, "/warehouse/issue-vouchers", "POST", issue))
    data(call(api.supply_chain_handler, "/warehouse/issue-vouchers", "POST", issue))
    stock = data(call(api.field_operations_handler, "/supervisor/materials/stock", query={"projectId": "P-A"}, **SITE))
    assert stock[0]["stock"] == 4
    assert len(data(call(api.supply_chain_handler, "/warehouse/grn"))) == 1
    assert len(data(call(api.supply_chain_handler, "/warehouse/issue-vouchers"))) == 1
    catalog = data(call(api.supply_chain_handler, "/inventory"))[0]
    assert (catalog["centralStock"], catalog["deployedStock"], catalog["totalStock"]) == (6, 4, 10)
    assert call(api.supply_chain_handler, "/warehouse/issue-vouchers", "POST", dict(issue, qty=7, requestId="issue-000002"))["statusCode"] == 409
    assert data(call(api.supply_chain_handler, "/inventory"))[0]["centralStock"] == 6
    assert call(api.supply_chain_handler, "/warehouse/issue-vouchers", "POST", dict(issue, targetProjectId="P-B", requestId="issue-000003"))["statusCode"] == 403


def test_payment_approval_visible_globally_and_spending_not_double_counted():
    payment = data(call(api.finance_handler, "/payments", "POST", {"projectId": "P-A", "vendorId": "vendor-1", "amount": 50, "mode": "UPI", "description": "Receipt"}))
    assert payment["status"] == "Pending"
    assert data(call(api.finance_handler, "/payments"))[0]["paymentId"] == payment["paymentId"]
    assert data(call(api.projects_handler, "/projects/P-A"))["spent"] == 20
    for _ in range(2):
        data(call(api.finance_handler, "/payments/" + payment["paymentId"] + "/status", "PATCH", {"status": "Approved"}))
    assert data(call(api.projects_handler, "/projects/P-A"))["spent"] == 70
    bill = data(call(api.finance_handler, "/projects/P-A/bills", "POST", {"grossAmount": 50, "billNumber": "Invoice", "clientOrContractor": "Vendor"}))
    data(call(api.finance_handler, "/projects/P-A/bills/" + bill["billId"], "PATCH", {"status": "Approved"}))
    assert data(call(api.governance_handler, "/dashboard/analytics"))["totalSpent"] == 70
    assert data(call(api.finance_handler, "/payments", sub="admin-b", org="ORG-B")) == []


def test_expenses_pending_queue_and_approval_rollup():
    expense = data(call(api.finance_handler, "/expenses", "POST", {"projectId": "P-A", "amount": 25, "description": "Fuel", "category": "Travel"}))
    stats = data(call(api.governance_handler, "/dashboard/analytics"))
    assert stats["pendingApprovals"] == 1 and stats["approvals"][0]["expenseId"] == expense["expenseId"]
    data(call(api.finance_handler, "/expenses/" + expense["expenseId"], "PATCH", {"status": "Approved"}))
    assert data(call(api.governance_handler, "/reports/executive"))["projectSummaries"][0]["spent"] == 45


def test_payroll_uses_attendance_and_posts_once():
    worker = data(call(api.workforce_handler, "/supervisor/labour/attendance", "POST", {"projectId": "P-A", "name": "Worker", "rate": 800}))
    attendance = {"projectId": "P-A", "labourAttendanceId": worker["labourAttendanceId"], "date": "2026-08-01", "status": "Present"}
    data(call(api.workforce_handler, "/supervisor/labour/attendance", "POST", dict(attendance, operation="allocate")))
    data(call(api.workforce_handler, "/supervisor/labour/attendance", "POST", attendance, **SITE))
    cycle = data(call(api.governance_handler, "/projects/P-A/payroll", "POST", {"month": "2026-08"}))
    assert cycle["amount"] == 800 and cycle["status"] == "Pending"
    assert call(api.workforce_handler, "/supervisor/labour/attendance", "POST", attendance, **SITE)["statusCode"] == 400
    assert call(api.governance_handler, "/projects/P-A/payroll/disburse", "POST", {"month": "2026-08"})["statusCode"] == 400
    for _ in range(2):
        result = data(call(api.governance_handler, "/projects/P-A/payroll/disburse", "POST", {"month": "2026-08", "reference": "Bank receipt 123"}))
        assert result["status"] == "Paid"
    assert data(call(api.projects_handler, "/projects/P-A"))["spent"] == 820


def test_document_upload_and_download_are_project_scoped(monkeypatch):
    import boto3
    monkeypatch.setenv("DOCUMENTS_BUCKET", "test-project-files")
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-project-files")
    signed = data(call(api.document_control_handler, "/projects/P-A/documents/upload", "POST", {"name": "test.txt", "size": 4, "contentType": "text/plain"}))
    assert signed["storageKey"].startswith("ORG-A/P-A/")
    s3.put_object(Bucket="test-project-files", Key=signed["storageKey"], Body=b"test")
    record = data(call(api.document_control_handler, "/projects/P-A/documents", "POST", {"title": "Test", "storageKey": signed["storageKey"]}))
    assert "url" in data(call(api.document_control_handler, "/projects/P-A/documents/" + record["documentId"] + "/download"))
    assert call(api.document_control_handler, "/projects/P-A/documents", "POST", {"title": "Bad", "storageKey": "ORG-B/P-B/file"})["statusCode"] == 400

def test_approved_indent_issue_and_balances_commit_together():
    body = {"projectId": "P-A", "materialName": "Steel", "quantity": 3, "unit": "Tons"}
    indent = data(call(api.field_operations_handler, "/supervisor/materials/indents", "POST", body, **SITE))
    data(call(api.field_operations_handler, "/supervisor/materials/indents/" + indent["materialId"], "PATCH", {"status": "approved"}, query={"projectId": "P-A"}))
    issue = {"item": "Steel", "unit": "Tons", "qty": 3, "targetProjectId": "P-A", "indentId": indent["materialId"], "requestId": "indent-issue-001"}
    assert call(api.supply_chain_handler, "/warehouse/issue-vouchers", "POST", issue)["statusCode"] == 409
    assert data(call(api.field_operations_handler, "/supervisor/materials/indents", query={"projectId": "P-A"}))[0]["status"] == "approved"
    data(call(api.supply_chain_handler, "/warehouse/grn", "POST", {"item": "Steel", "unit": "Tons", "qty": 3, "requestId": "steel-receipt-001"}))
    data(call(api.supply_chain_handler, "/warehouse/issue-vouchers", "POST", issue))
    assert data(call(api.field_operations_handler, "/supervisor/materials/indents", query={"projectId": "P-A"}, **SITE))[0]["status"] == "issued"
    assert data(call(api.field_operations_handler, "/supervisor/materials/stock", query={"projectId": "P-A"}, **SITE))[0]["stock"] == 3


def test_staff_salary_snapshot_is_not_rewritten_by_profile_changes():
    profile = {"employeeId": "site-a", "basic": 1000, "hra": 100, "pf": 50}
    data(call(api.governance_handler, "/projects/P-A/payroll/staff", "POST", profile))
    cycle = data(call(api.governance_handler, "/projects/P-A/payroll", "POST", {"month": "2026-07"}))
    assert cycle["amount"] == 1050
    data(call(api.governance_handler, "/projects/P-A/payroll/staff", "POST", dict(profile, basic=2000)))
    assert data(call(api.governance_handler, "/projects/P-A/payroll"))[0]["amount"] == 1050


def test_repeated_financial_create_is_not_a_duplicate_payment():
    body = {"projectId": "P-A", "vendorId": "vendor-1", "amount": 20, "requestId": "same-payment-001"}
    first = data(call(api.finance_handler, "/payments", "POST", body))
    again = data(call(api.finance_handler, "/payments", "POST", body))
    assert first["paymentId"] == again["paymentId"]
    assert len(data(call(api.finance_handler, "/payments"))) == 1
    assert call(api.finance_handler, "/payments", "POST", dict(body, amount=30))["statusCode"] == 400


def test_document_cannot_be_repointed_to_another_tenant(monkeypatch):
    import boto3
    monkeypatch.setenv("DOCUMENTS_BUCKET", "test-files")
    s3=boto3.client("s3",region_name="us-east-1");s3.create_bucket(Bucket="test-files")
    s3.put_object(Bucket="test-files",Key="ORG-A/P-A/file",Body=b"test")
    record=data(call(api.document_control_handler,"/projects/P-A/documents","POST",{"title":"Test","storageKey":"ORG-A/P-A/file"}))
    assert call(api.document_control_handler,"/projects/P-A/documents/"+record["documentId"],"PATCH",{"storageKey":"ORG-B/P-B/file"})["statusCode"]==400

def test_linked_bill_cannot_be_overpaid_by_multiple_approvals():
    bill=data(call(api.finance_handler,"/projects/P-A/bills","POST",{"grossAmount":100,"billNumber":"B1"}))
    data(call(api.finance_handler,"/projects/P-A/bills/"+bill["billId"],"PATCH",{"status":"Approved"}))
    payment={"projectId":"P-A","vendorId":"vendor-1","amount":60,"billId":bill["billId"]}
    first=data(call(api.finance_handler,"/payments","POST",payment))
    second=data(call(api.finance_handler,"/payments","POST",payment))
    data(call(api.finance_handler,"/projects/P-A/payments/"+first["paymentId"],"PATCH",{"status":"Approved"}))
    assert call(api.finance_handler,"/projects/P-A/payments/"+second["paymentId"],"PATCH",{"status":"Approved"})["statusCode"]==409
    assert data(call(api.finance_handler,"/projects/P-A/bills/"+bill["billId"]))["paidAmount"]==60


def test_unpaid_payroll_can_be_reopened_but_paid_cannot():
    data(call(api.governance_handler,"/projects/P-A/payroll/staff","POST",{"employeeId":"site-a","basic":1000}))
    data(call(api.governance_handler,"/projects/P-A/payroll","POST",{"month":"2026-06"}))
    assert data(call(api.governance_handler,"/projects/P-A/payroll/2026-06","DELETE"))["reopened"]
    data(call(api.governance_handler,"/projects/P-A/payroll","POST",{"month":"2026-06"}))
    data(call(api.governance_handler,"/projects/P-A/payroll/disburse","POST",{"month":"2026-06","reference":"receipt"}))
    assert call(api.governance_handler,"/projects/P-A/payroll/2026-06","DELETE")["statusCode"]==400

def test_invoice_extraction_returns_suggestions_without_creating_a_bill(monkeypatch):
    import boto3
    from unittest.mock import MagicMock
    monkeypatch.setenv("DOCUMENTS_BUCKET","test-invoices")
    real_client=boto3.client
    s3=real_client("s3",region_name="us-east-1");s3.create_bucket(Bucket="test-invoices")
    s3.put_object(Bucket="test-invoices",Key="ORG-A/P-A/invoice",Body=b"image",ContentType="image/jpeg")
    document=data(call(api.document_control_handler,"/projects/P-A/documents","POST",{"title":"Invoice","storageKey":"ORG-A/P-A/invoice"}))
    textract=MagicMock();textract.analyze_expense.return_value={"ExpenseDocuments":[{"SummaryFields":[{"Type":{"Text":"VENDOR_NAME"},"ValueDetection":{"Text":"Supplier"}},{"Type":{"Text":"TOTAL"},"ValueDetection":{"Text":"1,000.50"}}]}]}
    monkeypatch.setattr(boto3,"client",lambda name,*args,**kwargs:textract if name=="textract" else real_client(name,*args,**kwargs))
    result=data(call(api.document_control_handler,"/projects/P-A/documents/"+document["documentId"]+"/extract","POST"))
    assert result["vendor"]=="Supplier" and result["total"]=="1,000.50"
    assert data(call(api.finance_handler,"/projects/P-A/bills"))==[]
    textract.analyze_expense.assert_called_once()
