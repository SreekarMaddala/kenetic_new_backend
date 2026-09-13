import pytest
from test_security_workflows import database, event, data, put
from handlers import consolidated as api
from common import workflows

PATH = "/supervisor/logistics/trips"
SITE = {"sub": "site-a", "role": "supervisor"}

@pytest.fixture(autouse=True)
def month_clock(monkeypatch):
    monkeypatch.setattr(workflows, "now", lambda: "2026-09-13T12:00:00+00:00")

def create(day, **identity):
    return api.field_operations_handler(event(PATH, "POST", {"projectId": "P-A", "tripType": "vehicle", "date": day, "vehicle": "Truck", "startKm": 0, "endKm": 20}, **identity), None)

def test_supervisor_sees_current_month_and_vehicle_catalog_only(database):
    for day in ["2026-08-31", "2026-09-01", "2026-09-30", "2026-10-01"]:
        data(create(day))
    data(api.field_operations_handler(event(PATH, "POST", {"projectId": "P-A", "tripType": "vehicle_registration", "vehicle": "Truck"}), None))
    scoped = data(api.field_operations_handler(event(PATH, query={"projectId": "P-A", "month": "2026-08"}, **SITE), None))
    assert sorted(r["date"] for r in scoped if r["tripType"] != "vehicle_registration") == ["2026-09-01", "2026-09-30"]
    assert any(r["tripType"] == "vehicle_registration" for r in scoped)
    assert len(data(api.field_operations_handler(event(PATH, query={"projectId": "P-A"}), None))) == 5

@pytest.mark.parametrize("kind,fields", [("vehicle", {"startKm": 0, "endKm": 10}), ("fuel", {"liters": 5, "rate": 90}), ("rental", {"rate": 100, "helper": 20})])
def test_supervisor_cannot_submit_other_months(database, kind, fields):
    for day in ["2026-08-31", "2026-10-01"]:
        body = {"projectId": "P-A", "tripType": kind, "date": day, **fields}
        assert api.field_operations_handler(event(PATH, "POST", body, **SITE), None)["statusCode"] == 403
        assert api.field_operations_handler(event(PATH, "POST", body), None)["statusCode"] == 201
    assert create("2026-09-13", **SITE)["statusCode"] == 201

def test_direct_record_access_cannot_bypass_month_limit(database):
    old = data(create("2026-08-31"))
    path = PATH + "/" + old["tripId"]
    assert api.field_operations_handler(event(path, query={"projectId": "P-A"}, **SITE), None)["statusCode"] == 403
    body = {"tripType": "vehicle", "date": "2026-09-13", "startKm": 0, "endKm": 30}
    assert api.field_operations_handler(event(path, "PATCH", body, query={"projectId": "P-A"}, **SITE), None)["statusCode"] == 403
    assert api.field_operations_handler(event(path, query={"projectId": "P-A"}), None)["statusCode"] == 200

def test_month_boundary_uses_india_time(monkeypatch):
    monkeypatch.setattr(workflows, "now", lambda: "2026-08-31T18:30:00+00:00")
    assert workflows.logistics_month() == "2026-09"
