from test_security_workflows import ADMIN_B, CEMENT_ID, ORG_A, ORG_B, PROJECT_A
import pytest

from test_security_workflows import database, event, data, put
from handlers import consolidated as api


def call(handler, path, method="GET", body=None, **kwargs):
    return handler(event(path, method, body, **kwargs), None)


def material(name="Cement", unit="Bags", **extra):
    return data(call(api.supply_chain_handler, "/inventory", "POST", dict(name=name, unit=unit, **extra)))


def supplier(ids):
    return data(call(api.supply_chain_handler, "/vendors", "POST", {"name": "Supplier", "materialIds": ids}))


def payment(vendor, item, **extra):
    body = dict(projectId=PROJECT_A, vendorId=vendor["vendorId"], materialId=item["itemId"], amount=40, mode="Cash", reference="cash-receipt", requestId="vendor-payment-001")
    return data(call(api.finance_handler, "/payments", "POST", {**body, **extra}))


def test_catalog_blocks_duplicates_and_retries_are_idempotent(database):
    first = material("  TMT   Steel  ", "Kg", requestId="catalog-request-001")
    assert first["name"] == "TMT Steel"
    assert material("TMT Steel", "Kg", requestId="catalog-request-001")["itemId"] == first["itemId"]
    duplicate = call(api.supply_chain_handler, "/inventory", "POST", {"name": "tmt  STEEL", "unit": " kg "})
    assert duplicate["statusCode"] == 409
    assert material("TMT Steel", "Tons")["itemId"] != first["itemId"]
    catalog = data(call(api.supply_chain_handler, "/inventory", query={"catalog": "true"}))
    assert len(catalog) == 2
    assert data(call(api.supply_chain_handler, "/inventory", query={"catalog": "true"}, sub=ADMIN_B, org=ORG_B)) == []


def test_current_catalog_ids_are_preserved_and_protected(database):
    put("INVENTORY_TABLE", f"ORG#{ORG_A}", f"INVENTORY-ITEM#{CEMENT_ID}", orgId=ORG_A, itemId=CEMENT_ID, name="Cement", unit="Bags", entityType="inventory-item")
    assert call(api.supply_chain_handler, "/inventory", "POST", {"name": "cement", "unit": "bags"})["statusCode"] == 409
    assert call(api.supply_chain_handler, f"/inventory/{CEMENT_ID}", "PATCH", {"name": "Sand"})["statusCode"] == 400
    assert call(api.supply_chain_handler, f"/inventory/{CEMENT_ID}", "DELETE")["statusCode"] == 400


def test_vendor_material_assignment_uses_catalog_ids(database):
    cement = material()
    vendor = supplier([cement["itemId"]])
    assert vendor["materialsSupplied"] == "Cement (Bags)"
    assert vendor["status"] == "Active"
    for ids in [[cement["itemId"], cement["itemId"]], ["missing"], "Cement"]:
        assert call(api.supply_chain_handler, "/vendors/" + vendor["vendorId"], "PATCH", {"materialIds": ids})["statusCode"] == 400
    manual = data(call(api.supply_chain_handler, "/vendors", "POST", {"name": "Other", "materialsSupplied": "  Cement, sand, TMT steel  "}))
    assert manual["materialsSupplied"] == "Cement, sand, TMT steel"
    assert manual["materialIds"] == []
    foreign = data(call(api.supply_chain_handler, "/inventory", "POST", {"name": "Foreign", "unit": "Kg"}, sub=ADMIN_B, org=ORG_B))
    assert call(api.supply_chain_handler, "/vendors/" + vendor["vendorId"], "PATCH", {"materialIds": [foreign["itemId"]]})["statusCode"] == 400
    changed = data(call(api.supply_chain_handler, "/vendors/" + vendor["vendorId"], "PATCH", {"materialIds": []}))
    assert changed["materialsSupplied"] == ""


def test_manual_vendor_materials_can_be_edited_without_losing_catalog_links(database):
    cement = material()
    vendor = supplier([cement["itemId"]])
    path = "/vendors/" + vendor["vendorId"]
    edited = data(call(api.supply_chain_handler, path, "PUT", {"name": "Updated Supplier", "materialsSupplied": "Cement, custom aggregates"}))
    assert edited["materialsSupplied"] == "Cement, custom aggregates"
    assert edited["materialIds"] == [cement["itemId"]]
    cleared = data(call(api.supply_chain_handler, path, "PATCH", {"materialsSupplied": ""}))
    assert cleared["materialsSupplied"] == ""
    assert cleared["materialIds"] == [cement["itemId"]]
    for invalid in [123, ["Cement"], "x" * 4001]:
        assert call(api.supply_chain_handler, path, "PATCH", {"materialsSupplied": invalid})["statusCode"] == 400


def test_payment_only_accepts_selected_vendors_catalog_material(database):
    cement, sand = material(), material("Sand", "Tons")
    vendor = supplier([cement["itemId"]])
    body = {"projectId": PROJECT_A, "vendorId": vendor["vendorId"], "amount": 40}
    for changes in [{"material": "Cement"}, {"materialId": sand["itemId"]}, {"materialId": "missing"}]:
        assert call(api.finance_handler, "/payments", "POST", dict(body, **changes))["statusCode"] == 400
    assert call(api.finance_handler, "/payments", "POST", dict(body, materialId=cement["itemId"], material="Forged", vendorName="Forged"))["statusCode"] == 400
    paid = payment(vendor, cement)
    assert (paid["material"], paid["materialUnit"], paid["vendorName"]) == ("Cement", "Bags", "Supplier")
    path = f"/projects/{PROJECT_A}/payments/" + paid["paymentId"]
    assert call(api.finance_handler, path, "PATCH", {"materialId": sand["itemId"]})["statusCode"] == 400


def test_payments_accept_saved_vendor_material_names_without_catalog_links(database):
    vendor = data(call(api.supply_chain_handler, '/vendors', 'POST', {
        'name': 'GD SAW MILL', 'materialsSupplied': 'PLYWOOD; Timber\nBoards'}))
    body = dict(projectId=PROJECT_A, vendorId=vendor['vendorId'], materialName='plywood',
                amount=40, mode='Cash', requestId='manual-vendor-payment')
    for scoped in (False, True):
        endpoint = f'/projects/{PROJECT_A}/payments' if scoped else '/payments'
        saved = data(call(api.finance_handler, endpoint, 'POST', body))
        assert saved['material'] == 'PLYWOOD' and saved['vendorName'] == 'GD SAW MILL'
        assert saved['materialUnit'] == '' and not saved.get('materialId')
    approved = data(call(api.finance_handler, f"/projects/{PROJECT_A}/payments/{saved['paymentId']}", 'PATCH', {'status': 'Approved'}))
    assert approved['status'] == 'Approved'
    for changes in ({'materialName': 'Steel'}, {'vendorId': supplier([])['vendorId']}, {'materialId': material()['itemId']}):
        assert call(api.finance_handler, '/payments', 'POST', {**body, **changes, 'requestId': 'invalid-manual-payment'})['statusCode'] == 400


def test_multiple_vendor_materials_are_available_for_payments(database):
    cement, sand = material(), material("Sand", "Tons")
    vendor = supplier([cement["itemId"], sand["itemId"]])
    assert vendor["materialIds"] == [cement["itemId"], sand["itemId"]]
    assert vendor["materialsSupplied"] == "Cement (Bags), Sand (Tons)"
    assert payment(vendor, cement)["materialId"] == cement["itemId"]
    assert payment(vendor, sand, requestId="vendor-payment-002")["materialId"] == sand["itemId"]
    edited = data(call(api.supply_chain_handler, "/vendors/" + vendor["vendorId"], "PUT", {"materialIds": [sand["itemId"]]}))
    assert edited["materialsSupplied"] == "Sand (Tons)"


@pytest.mark.parametrize("scoped", [False, True])
def test_cash_payment_needs_no_reference_and_can_be_approved(database, scoped):
    cement = material()
    vendor = supplier([cement["itemId"]])
    endpoint = f"/projects/{PROJECT_A}/payments" if scoped else "/payments"
    body = dict(projectId=PROJECT_A, vendorId=vendor["vendorId"], materialId=cement["itemId"], amount=40, mode="Cash", requestId="cash-without-reference")
    paid = data(call(api.finance_handler, endpoint, "POST", body))
    assert not paid.get("reference")
    approved = data(call(api.finance_handler, endpoint + "/" + paid["paymentId"], "PATCH", {"status": "Approved"}))
    assert approved["status"] == "Approved"
    assert data(call(api.finance_handler, endpoint, "POST", body))["paymentId"] == paid["paymentId"]


@pytest.mark.parametrize("mode", ["NEFT", "RTGS", "Cheque", "UPI"])
@pytest.mark.parametrize("reference", [None, "", "   "])
def test_non_cash_payment_requires_reference(database, mode, reference):
    cement = material()
    vendor = supplier([cement["itemId"]])
    body = dict(projectId=PROJECT_A, vendorId=vendor["vendorId"], materialId=cement["itemId"], amount=40, mode=mode, requestId="non-cash-reference")
    if reference is not None:
        body["reference"] = reference
    result = call(api.finance_handler, "/payments", "POST", body)
    assert result["statusCode"] == 400
    assert "reference" in result["body"]
    body["reference"] = "TXN-123"
    assert data(call(api.finance_handler, "/payments", "POST", body))["reference"] == "TXN-123"


@pytest.mark.parametrize("scoped", [False, True])
def test_approved_payment_cannot_be_reverted_or_change_amount_during_approval(database, scoped):
    cement = material()
    vendor = supplier([cement["itemId"]])
    bill = data(call(api.finance_handler, f"/projects/{PROJECT_A}/bills", "POST", {"grossAmount": 100, "billNumber": "VENDOR-001", "clientOrContractor": "Supplier", "requestId": "vendor-bill-001"}))
    bill_path = f"/projects/{PROJECT_A}/bills/" + bill["billId"]
    data(call(api.finance_handler, bill_path, "PATCH", {"status": "Approved"}))
    paid = payment(vendor, cement, billId=bill["billId"])
    path = (f"/projects/{PROJECT_A}/payments/" if scoped else "/payments/") + paid["paymentId"]
    assert call(api.finance_handler, path, "PATCH", {"status": "Approved", "amount": 1000})["statusCode"] == 400
    data(call(api.finance_handler, path, "PATCH", {"status": "Approved"}))
    for status in ["Pending", "pending", "Rejected", "Draft"]:
        assert call(api.finance_handler, path, "PATCH", {"status": status})["statusCode"] == 400
    data(call(api.finance_handler, path, "PATCH", {"status": "Approved"}))
    assert data(call(api.finance_handler, bill_path))["paidAmount"] == 40
    assert data(call(api.finance_handler, path))["status"] == "Approved"
