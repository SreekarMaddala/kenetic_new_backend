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
    return data(call(api.finance_handler, "/payments", "POST", dict(projectId="P-A", vendorId=vendor["vendorId"], materialId=item["itemId"], amount=40, **extra)))


def test_catalog_blocks_duplicates_and_retries_are_idempotent(database):
    first = material("  TMT   Steel  ", "Kg", requestId="catalog-request-001")
    assert first["name"] == "TMT Steel"
    assert material("TMT Steel", "Kg", requestId="catalog-request-001")["itemId"] == first["itemId"]
    duplicate = call(api.supply_chain_handler, "/inventory", "POST", {"name": "tmt  STEEL", "unit": " kg ", "itemId": "bypass"})
    assert duplicate["statusCode"] == 409
    assert material("TMT Steel", "Tons")["itemId"] != first["itemId"]
    catalog = data(call(api.supply_chain_handler, "/inventory", query={"catalog": "true"}))
    assert len(catalog) == 2
    assert data(call(api.supply_chain_handler, "/inventory", query={"catalog": "true"}, sub="admin-b", org="ORG-B")) == []


def test_existing_catalog_ids_are_preserved_and_protected(database):
    put("INVENTORY_TABLE", "ORG#ORG-A", "INVENTORY-ITEM#legacy", orgId="ORG-A", itemId="legacy", name="Cement", unit="Bags", entityType="inventory-item")
    assert call(api.supply_chain_handler, "/inventory", "POST", {"name": "cement", "unit": "bags"})["statusCode"] == 409
    assert call(api.supply_chain_handler, "/inventory/legacy", "PATCH", {"name": "Sand"})["statusCode"] == 400
    assert call(api.supply_chain_handler, "/inventory/legacy", "DELETE")["statusCode"] == 400


def test_vendor_material_assignment_uses_catalog_ids(database):
    cement = material()
    vendor = supplier([cement["itemId"]])
    assert vendor["materialsSupplied"] == "Cement (Bags)"
    assert vendor["status"] == "Active"
    for ids in [[cement["itemId"], cement["itemId"]], ["missing"], "Cement"]:
        assert call(api.supply_chain_handler, "/vendors/" + vendor["vendorId"], "PATCH", {"materialIds": ids})["statusCode"] == 400
    assert call(api.supply_chain_handler, "/vendors", "POST", {"name": "Other", "materialsSupplied": "Cement"})["statusCode"] == 400
    foreign = data(call(api.supply_chain_handler, "/inventory", "POST", {"name": "Foreign", "unit": "Kg"}, sub="admin-b", org="ORG-B"))
    assert call(api.supply_chain_handler, "/vendors/" + vendor["vendorId"], "PATCH", {"materialIds": [foreign["itemId"]]})["statusCode"] == 400
    changed = data(call(api.supply_chain_handler, "/vendors/" + vendor["vendorId"], "PATCH", {"materialIds": []}))
    assert changed["materialsSupplied"] == ""


def test_payment_only_accepts_selected_vendors_catalog_material(database):
    cement, sand = material(), material("Sand", "Tons")
    vendor = supplier([cement["itemId"]])
    body = {"projectId": "P-A", "vendorId": vendor["vendorId"], "amount": 40}
    for changes in [{"material": "Cement"}, {"materialId": sand["itemId"]}, {"materialId": "missing"}]:
        assert call(api.finance_handler, "/payments", "POST", dict(body, **changes))["statusCode"] == 400
    paid = payment(vendor, cement, material="Forged", vendorName="Forged")
    assert (paid["material"], paid["materialUnit"], paid["vendorName"]) == ("Cement", "Bags", "Supplier")
    path = "/projects/P-A/payments/" + paid["paymentId"]
    assert call(api.finance_handler, path, "PATCH", {"materialId": sand["itemId"]})["statusCode"] == 400


@pytest.mark.parametrize("scoped", [False, True])
def test_approved_payment_cannot_be_reverted_or_change_amount_during_approval(database, scoped):
    cement = material()
    vendor = supplier([cement["itemId"]])
    bill = data(call(api.finance_handler, "/projects/P-A/bills", "POST", {"grossAmount": 100}))
    bill_path = "/projects/P-A/bills/" + bill["billId"]
    data(call(api.finance_handler, bill_path, "PATCH", {"status": "Approved"}))
    paid = payment(vendor, cement, billId=bill["billId"])
    path = ("/projects/P-A/payments/" if scoped else "/payments/") + paid["paymentId"]
    assert call(api.finance_handler, path, "PATCH", {"status": "Approved", "amount": 1000})["statusCode"] == 400
    data(call(api.finance_handler, path, "PATCH", {"status": "approved"}))
    for status in ["Pending", "pending", "Rejected", "Draft"]:
        assert call(api.finance_handler, path, "PATCH", {"status": status})["statusCode"] == 400
    data(call(api.finance_handler, path, "PATCH", {"status": "Approved"}))
    assert data(call(api.finance_handler, bill_path))["paidAmount"] == 40
    assert data(call(api.finance_handler, path))["status"] == "Approved"
