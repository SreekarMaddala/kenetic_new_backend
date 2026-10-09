from test_security_workflows import ORG_B, PROJECT_A, SITE_A, SITE_B
import pytest
from test_security_workflows import database, event, data
from handlers import consolidated as api

SITE = {"sub": SITE_A, "role": "supervisor"}
LOCATION = {"latitude": 12.9716, "longitude": 77.5946, "accuracy": 18.5}

def submit(action, location=LOCATION, **identity):
    return api.workforce_handler(event(f"/supervisor/attendance/{action}", "POST", {"projectId": PROJECT_A, "location": location}, **identity), None)

def test_locations_persist_separately_and_history_remains_scoped(database):
    checkin = data(submit("check-in", **SITE))
    assert checkin["checkInLocation"]["latitude"] == LOCATION["latitude"]
    checkout = data(submit("check-out", {"latitude": 13, "longitude": 78, "accuracy": 10}, **SITE))
    assert checkout["checkInLocation"] == checkin["checkInLocation"]
    assert checkout["checkOutLocation"]["latitude"] == 13
    assert checkout["checkOutLocation"]["recordedAt"] == checkout["checkOut"]
    assert submit("check-in")["statusCode"] == 403
    history = "/supervisor/attendance/history"
    admin_rows = data(api.workforce_handler(event(history, query={"projectId": PROJECT_A}), None))
    assert len(admin_rows) == 1
    own = data(api.workforce_handler(event(history, query={"projectId": PROJECT_A}, **SITE), None))
    assert len(own) == 1 and own[0]["checkOutLocation"]["longitude"] == 78
    assert api.workforce_handler(event(history, query={"projectId": PROJECT_A}, sub=SITE_B, role="supervisor", org=ORG_B), None)["statusCode"] == 403

@pytest.mark.parametrize("location", [None, {}, {**LOCATION, "latitude": 91}, {**LOCATION, "longitude": -181}, {**LOCATION, "accuracy": -1}, {**LOCATION, "latitude": True}, {**LOCATION, "longitude": "77"}])
def test_invalid_location_cannot_create_attendance(database, location):
    assert submit("check-in", location, **SITE)["statusCode"] == 400

def test_failed_checkout_does_not_close_shift(database):
    data(submit("check-in", **SITE))
    assert submit("check-out", None, **SITE)["statusCode"] == 400
    data(submit("check-out", {"latitude": 0, "longitude": 0, "accuracy": 0}, **SITE))

@pytest.mark.parametrize("role", ["operations_admin", "super_admin"])
@pytest.mark.parametrize("action", ["check-in", "check-out"])
def test_admin_cannot_record_supervisor_attendance(database, role, action):
    assert submit(action, role=role)["statusCode"] == 403
