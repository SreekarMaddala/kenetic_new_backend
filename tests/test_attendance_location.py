import pytest
from test_security_workflows import database, event, data
from handlers import consolidated as api

SITE = {"sub": "site-a", "role": "supervisor"}
LOCATION = {"latitude": 12.9716, "longitude": 77.5946, "accuracy": 18.5}

def submit(action, location=LOCATION, **identity):
    return api.workforce_handler(event(f"/supervisor/attendance/{action}", "POST", {"projectId": "P-A", "location": location}, **identity), None)

def test_locations_persist_separately_and_history_remains_scoped(database):
    checkin = data(submit("check-in", **SITE))
    assert checkin["checkInLocation"]["latitude"] == LOCATION["latitude"]
    checkout = data(submit("check-out", {"latitude": 13, "longitude": 78, "accuracy": 10}, **SITE))
    assert checkout["checkInLocation"] == checkin["checkInLocation"]
    assert checkout["checkOutLocation"]["latitude"] == 13
    assert checkout["checkOutLocation"]["recordedAt"] == checkout["checkOut"]
    data(submit("check-in"))
    history = "/supervisor/attendance/history"
    admin_rows = data(api.workforce_handler(event(history, query={"projectId": "P-A"}), None))
    assert len(admin_rows) == 2
    own = data(api.workforce_handler(event(history, query={"projectId": "P-A"}, **SITE), None))
    assert len(own) == 1 and own[0]["checkOutLocation"]["longitude"] == 78
    assert api.workforce_handler(event(history, query={"projectId": "P-A"}, sub="site-b", role="supervisor", org="ORG-B"), None)["statusCode"] == 403

@pytest.mark.parametrize("location", [None, {}, {**LOCATION, "latitude": 91}, {**LOCATION, "longitude": -181}, {**LOCATION, "accuracy": -1}, {**LOCATION, "latitude": True}, {**LOCATION, "longitude": "77"}])
def test_invalid_location_cannot_create_attendance(database, location):
    assert submit("check-in", location, **SITE)["statusCode"] == 400

def test_failed_checkout_does_not_close_shift(database):
    data(submit("check-in", **SITE))
    assert submit("check-out", None, **SITE)["statusCode"] == 400
    data(submit("check-out", {"latitude": 0, "longitude": 0, "accuracy": 0}, **SITE))
