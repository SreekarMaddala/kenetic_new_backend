from test_security_workflows import ADMIN_A, ADMIN_B, CEMENT_ID, ORG_A, ORG_B, PROJECT_A, PROJECT_B, SITE_A, VENDOR_ID
from test_security_workflows import database, event, data, put
from handlers import consolidated as api
from common.authz import SUPERVISOR
from common.dynamo import get_table
import pytest

@pytest.fixture(autouse=True)
def vendor(database):
    put("INVENTORY_TABLE", f"ORG#{ORG_A}", f"INVENTORY-ITEM#{CEMENT_ID}", orgId=ORG_A, itemId=CEMENT_ID, name="Cement", unit="Bags", entityType="inventory-item")
    put("PARTIES_TABLE", f"ORG#{ORG_A}", f"VENDOR#{VENDOR_ID}", orgId=ORG_A, vendorId=VENDOR_ID, name="Supplier", materialIds=[CEMENT_ID], entityType="vendor")

SITE = {"sub": SITE_A, "role": SUPERVISOR}


def call(handler, path, method="GET", body=None, **kwargs):
    return handler(event(path, method, body, **kwargs), None)


def test_supervisor_cannot_self_approve_or_reclassify_material():
    path = "/supervisor/materials/indents"
    body = {"projectId": PROJECT_A, "materialName": "Cement", "quantity": 5, "unit": "Bags"}
    assert call(api.field_operations_handler, path, "POST", dict(body, status="Approved"), **SITE)["statusCode"] == 403
    indent = data(call(api.field_operations_handler, path, "POST", body, **SITE))
    item_path = path + "/" + indent["materialId"]
    assert call(api.field_operations_handler, item_path, "PUT", {"status": "approved"}, query={"projectId": PROJECT_A}, **SITE)["statusCode"] == 403
    approved = data(call(api.field_operations_handler, item_path, "PATCH", {"status": "approved"}, query={"projectId": PROJECT_A}))
    assert approved["approvedBy"] == ADMIN_A
    assert call(api.field_operations_handler, item_path, "PATCH", {"status": "rejected"}, query={"projectId": PROJECT_A})["statusCode"] == 400
    assert data(call(api.field_operations_handler, "/supervisor/materials/stock", query={"projectId": PROJECT_A}, **SITE)) == []


def test_daily_attendance_is_idempotent_and_monthly_deductions_persist():
    path = "/supervisor/labour/attendance"
    worker = data(call(api.workforce_handler, path, "POST", {"projectId": PROJECT_A, "name": "Worker", "rate": 800}))
    wid = worker["labourAttendanceId"]
    body = {"projectId": PROJECT_A, "labourAttendanceId": wid, "date": "2026-09-01", "status": "Present"}
    data(call(api.workforce_handler, path, "POST", dict(body, operation="allocate")))
    for _ in range(2):
        data(call(api.workforce_handler, path, "POST", body, **SITE))
    data(call(api.workforce_handler, path, "POST", dict(body, date="2026-09-02", operation="allocate")))
    data(call(api.workforce_handler, path, "POST", dict(body, date="2026-09-02", status="Absent"), **SITE))
    data(call(api.workforce_handler, path, "POST", {"projectId": PROJECT_A, "operation": "debit", "labourAttendanceId": wid, "date": "2026-09-02", "amount": 100, "description": "Advance", "requestId": "test_feature_workflows-42"}))
    result = data(call(api.workforce_handler, path, query={"projectId": PROJECT_A, "date": "2026-09-02"}))[0]
    assert result["status"] == "Absent" and result["daysPresent"] == 1 and result["advanceDeductions"] == 100
    assert call(api.workforce_handler, path, "POST", dict(body, projectId=PROJECT_B), **SITE)["statusCode"] == 403
    assert "accNo" in worker and worker["accNo"] == ""


def test_stock_receipt_transfer_is_atomic_and_idempotent():
    receive = {"item": "Cement", "unit": "Bags", "qty": 10, "rate": 50, "requestId": "receipt-0001"}
    data(call(api.supply_chain_handler, "/warehouse/grn", "POST", receive))
    data(call(api.supply_chain_handler, "/warehouse/grn", "POST", receive))
    issue = {"item": "Cement", "unit": "Bags", "qty": 4, "targetProjectId": PROJECT_A, "requestId": "issue-000001"}
    data(call(api.supply_chain_handler, "/warehouse/issue-vouchers", "POST", issue))
    data(call(api.supply_chain_handler, "/warehouse/issue-vouchers", "POST", issue))
    stock = data(call(api.field_operations_handler, "/supervisor/materials/stock", query={"projectId": PROJECT_A}, **SITE))
    assert stock[0]["stock"] == 4
    assert len(data(call(api.supply_chain_handler, "/warehouse/grn"))) == 1
    assert len(data(call(api.supply_chain_handler, "/warehouse/issue-vouchers"))) == 1
    catalog = data(call(api.supply_chain_handler, "/inventory"))[0]
    assert (catalog["centralStock"], catalog["deployedStock"], catalog["totalStock"]) == (6, 4, 10)
    assert call(api.supply_chain_handler, "/warehouse/issue-vouchers", "POST", dict(issue, qty=7, requestId="issue-000002"))["statusCode"] == 409
    assert data(call(api.supply_chain_handler, "/inventory"))[0]["centralStock"] == 6
    assert call(api.supply_chain_handler, "/warehouse/issue-vouchers", "POST", dict(issue, targetProjectId=PROJECT_B, requestId="issue-000003"))["statusCode"] == 403


def test_payment_approval_visible_globally_and_spending_not_double_counted():
    payment = data(call(api.finance_handler, "/payments", "POST", {"projectId": PROJECT_A, "vendorId": VENDOR_ID, "materialId": CEMENT_ID, "amount": 50, "mode": "UPI", "description": "Receipt", "requestId": "test_feature_workflows-68", "reference": "bank-reference-68"}))
    assert payment["status"] == "Pending"
    assert data(call(api.finance_handler, "/payments"))[0]["paymentId"] == payment["paymentId"]
    assert data(call(api.projects_handler, f"/projects/{PROJECT_A}"))["spent"] == 20
    for _ in range(2):
        data(call(api.finance_handler, "/payments/" + payment["paymentId"] + "/status", "PATCH", {"status": "Approved"}))
    assert data(call(api.projects_handler, f"/projects/{PROJECT_A}"))["spent"] == 70
    bill = data(call(api.finance_handler, f"/projects/{PROJECT_A}/bills", "POST", {"grossAmount": 50, "billNumber": "Invoice", "clientOrContractor": "Vendor", "requestId": "test_feature_workflows-75"}))
    data(call(api.finance_handler, f"/projects/{PROJECT_A}/bills/" + bill["billId"], "PATCH", {"status": "Approved"}))
    assert data(call(api.governance_handler, "/dashboard/analytics"))["totalSpent"] == 70
    assert data(call(api.finance_handler, "/payments", sub=ADMIN_B, org=ORG_B)) == []


def test_expenses_pending_queue_and_approval_rollup():
    expense = data(call(api.finance_handler, "/expenses", "POST", {"projectId": PROJECT_A, "amount": 25, "description": "Fuel", "category": "Travel", "requestId": "test_feature_workflows-82"}))
    stats = data(call(api.governance_handler, "/dashboard/analytics"))
    assert stats["pendingApprovals"] == 1 and stats["approvals"][0]["expenseId"] == expense["expenseId"]
    data(call(api.finance_handler, "/expenses/" + expense["expenseId"], "PATCH", {"status": "Approved"}))
    assert data(call(api.governance_handler, "/reports/executive"))["projectSummaries"][0]["spent"] == 45


def test_payroll_uses_attendance_and_posts_once():
    worker = data(call(api.workforce_handler, "/supervisor/labour/attendance", "POST", {"projectId": PROJECT_A, "name": "Worker", "rate": 800}))
    attendance = {"projectId": PROJECT_A, "labourAttendanceId": worker["labourAttendanceId"], "date": "2026-08-01", "status": "Present"}
    data(call(api.workforce_handler, "/supervisor/labour/attendance", "POST", dict(attendance, operation="allocate")))
    data(call(api.workforce_handler, "/supervisor/labour/attendance", "POST", attendance, **SITE))
    cycle = data(call(api.governance_handler, f"/projects/{PROJECT_A}/payroll", "POST", {"month": "2026-08"}))
    assert cycle["amount"] == 800 and cycle["status"] == "Pending"
    assert call(api.workforce_handler, "/supervisor/labour/attendance", "POST", attendance, **SITE)["statusCode"] == 400
    assert call(api.governance_handler, f"/projects/{PROJECT_A}/payroll/disburse", "POST", {"month": "2026-08"})["statusCode"] == 400
    for _ in range(2):
        result = data(call(api.governance_handler, f"/projects/{PROJECT_A}/payroll/disburse", "POST", {"month": "2026-08", "reference": "Bank receipt 123", "expectedCreatedAt": cycle["createdAt"]}))
        assert result["status"] == "Paid"
    assert data(call(api.projects_handler, f"/projects/{PROJECT_A}"))["spent"] == 820


def test_document_upload_and_download_are_project_scoped(monkeypatch):
    import boto3
    monkeypatch.setenv("DOCUMENTS_BUCKET", "test-project-files")
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-project-files")
    signed = data(call(api.document_control_handler, f"/projects/{PROJECT_A}/documents/upload", "POST", {"name": "test.txt", "size": 4, "contentType": "text/plain"}))
    assert signed["storageKey"].startswith(f"{ORG_A}/{PROJECT_A}/")
    s3.put_object(Bucket="test-project-files", Key=signed["storageKey"], Body=b"test")
    record = data(call(api.document_control_handler, f"/projects/{PROJECT_A}/documents", "POST", {"title": "Test", "storageKey": signed["storageKey"]}))
    assert "url" in data(call(api.document_control_handler, f"/projects/{PROJECT_A}/documents/" + record["documentId"] + "/download"))
    assert call(api.document_control_handler, f"/projects/{PROJECT_A}/documents", "POST", {"title": "Bad", "storageKey": f"{ORG_B}/{PROJECT_B}/file"})["statusCode"] == 400

def test_approved_indent_issue_and_balances_commit_together():
    body = {"projectId": PROJECT_A, "materialName": "Steel", "quantity": 3, "unit": "Tons"}
    indent = data(call(api.field_operations_handler, "/supervisor/materials/indents", "POST", body, **SITE))
    data(call(api.field_operations_handler, "/supervisor/materials/indents/" + indent["materialId"], "PATCH", {"status": "approved"}, query={"projectId": PROJECT_A}))
    issue = {"item": "Steel", "unit": "Tons", "qty": 3, "targetProjectId": PROJECT_A, "indentId": indent["materialId"], "requestId": "indent-issue-001"}
    assert call(api.supply_chain_handler, "/warehouse/issue-vouchers", "POST", issue)["statusCode"] == 400
    assert data(call(api.field_operations_handler, "/supervisor/materials/indents", query={"projectId": PROJECT_A}))[0]["status"] == "approved"
    data(call(api.supply_chain_handler, "/warehouse/grn", "POST", {"item": "Steel", "unit": "Tons", "qty": 3, "requestId": "steel-receipt-001"}))
    data(call(api.supply_chain_handler, "/warehouse/issue-vouchers", "POST", issue))
    assert data(call(api.field_operations_handler, "/supervisor/materials/indents", query={"projectId": PROJECT_A}, **SITE))[0]["status"] == "issued"
    assert data(call(api.field_operations_handler, "/supervisor/materials/stock", query={"projectId": PROJECT_A}, **SITE))[0]["stock"] == 3


def test_staff_salary_snapshot_is_not_rewritten_by_profile_changes():
    profile = {"employeeId": SITE_A, "basic": 1000, "hra": 100, "pf": 50}
    data(call(api.governance_handler, f"/projects/{PROJECT_A}/payroll/staff", "POST", profile))
    cycle = data(call(api.governance_handler, f"/projects/{PROJECT_A}/payroll", "POST", {"month": "2026-07"}))
    assert cycle["amount"] == 1050
    data(call(api.governance_handler, f"/projects/{PROJECT_A}/payroll/staff", "POST", dict(profile, basic=2000)))
    assert data(call(api.governance_handler, f"/projects/{PROJECT_A}/payroll"))[0]["amount"] == 1050


def test_repeated_financial_create_is_not_a_duplicate_payment():
    body = {"projectId": PROJECT_A, "vendorId": VENDOR_ID, "materialId": CEMENT_ID, "amount": 20, "requestId": "same-payment-001", "mode": "Cash", "reference": "cash-receipt"}
    first = data(call(api.finance_handler, "/payments", "POST", body))
    again = data(call(api.finance_handler, "/payments", "POST", body))
    assert first["paymentId"] == again["paymentId"]
    assert len(data(call(api.finance_handler, "/payments"))) == 1
    assert call(api.finance_handler, "/payments", "POST", dict(body, amount=30))["statusCode"] == 400


def test_document_cannot_be_repointed_to_another_tenant(monkeypatch):
    import boto3
    monkeypatch.setenv("DOCUMENTS_BUCKET", "test-files")
    s3=boto3.client("s3",region_name="us-east-1");s3.create_bucket(Bucket="test-files")
    s3.put_object(Bucket="test-files",Key=f"{ORG_A}/{PROJECT_A}/file",Body=b"test")
    record=data(call(api.document_control_handler,f"/projects/{PROJECT_A}/documents","POST",{"title":"Test","storageKey":f"{ORG_A}/{PROJECT_A}/file"}))
    assert call(api.document_control_handler,f"/projects/{PROJECT_A}/documents/"+record["documentId"],"PATCH",{"storageKey":f"{ORG_B}/{PROJECT_B}/file"})["statusCode"]==400

def test_linked_bill_cannot_be_overpaid_by_multiple_approvals():
    bill=data(call(api.finance_handler,f"/projects/{PROJECT_A}/bills","POST",{"grossAmount":100,"billNumber":"B1", "clientOrContractor": "Supplier", "requestId": "test_feature_workflows-156"}))
    data(call(api.finance_handler,f"/projects/{PROJECT_A}/bills/"+bill["billId"],"PATCH",{"status":"Approved"}))
    payment={"projectId":PROJECT_A,"vendorId":VENDOR_ID,"materialId":CEMENT_ID,"amount":60,"billId":bill["billId"], "mode": "Cash", "reference": "cash-receipt", "requestId": "test_feature_workflows-158"}
    first=data(call(api.finance_handler,"/payments","POST",payment))
    second=data(call(api.finance_handler,"/payments","POST",dict(payment, requestId="second-bill-payment")))
    data(call(api.finance_handler,f"/projects/{PROJECT_A}/payments/"+first["paymentId"],"PATCH",{"status":"Approved"}))
    assert call(api.finance_handler,f"/projects/{PROJECT_A}/payments/"+second["paymentId"],"PATCH",{"status":"Approved"})["statusCode"]==409
    assert data(call(api.finance_handler,f"/projects/{PROJECT_A}/bills/"+bill["billId"]))["paidAmount"]==60


def test_unpaid_payroll_can_be_reopened_but_paid_cannot():
    data(call(api.governance_handler,f"/projects/{PROJECT_A}/payroll/staff","POST",{"employeeId":SITE_A,"basic":1000}))
    cycle = data(call(api.governance_handler,f"/projects/{PROJECT_A}/payroll","POST",{"month":"2026-06"}))
    assert data(call(api.governance_handler,f"/projects/{PROJECT_A}/payroll/2026-06","DELETE"))["reopened"]
    cycle = data(call(api.governance_handler,f"/projects/{PROJECT_A}/payroll","POST",{"month":"2026-06"}))
    data(call(api.governance_handler,f"/projects/{PROJECT_A}/payroll/disburse","POST",{"month":"2026-06","reference":"receipt", "expectedCreatedAt": cycle["createdAt"]}))
    assert call(api.governance_handler,f"/projects/{PROJECT_A}/payroll/2026-06","DELETE")["statusCode"]==400

def test_invoice_extraction_returns_suggestions_without_creating_a_bill(monkeypatch):
    import boto3
    from unittest.mock import MagicMock
    monkeypatch.setenv("DOCUMENTS_BUCKET","test-invoices")
    real_client=boto3.client
    s3=real_client("s3",region_name="us-east-1");s3.create_bucket(Bucket="test-invoices")
    s3.put_object(Bucket="test-invoices",Key=f"{ORG_A}/{PROJECT_A}/invoice",Body=b"image",ContentType="image/jpeg")
    document=data(call(api.document_control_handler,f"/projects/{PROJECT_A}/documents","POST",{"title":"Invoice","storageKey":f"{ORG_A}/{PROJECT_A}/invoice"}))
    textract=MagicMock();textract.analyze_expense.return_value={"ExpenseDocuments":[{"SummaryFields":[{"Type":{"Text":"VENDOR_NAME"},"ValueDetection":{"Text":"Supplier"}},{"Type":{"Text":"TOTAL"},"ValueDetection":{"Text":"1,000.50"}}]}]}
    monkeypatch.setattr(boto3,"client",lambda name,*args,**kwargs:textract if name=="textract" else real_client(name,*args,**kwargs))
    result=data(call(api.document_control_handler,f"/projects/{PROJECT_A}/documents/"+document["documentId"]+"/extract","POST"))
    assert result["vendor"]=="Supplier" and result["total"]=="1,000.50"
    assert data(call(api.finance_handler,f"/projects/{PROJECT_A}/bills"))==[]
    textract.analyze_expense.assert_called_once()
