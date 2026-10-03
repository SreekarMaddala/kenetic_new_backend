from test_security_workflows import database, event, data
from test_feature_workflows import call, SITE
from handlers import consolidated as api
from common.dynamo import get_table

PATH = "/supervisor/labour/attendance"


def test_register_without_project_and_allocate_later(database):
    worker = data(call(api.workforce_handler, PATH, "POST", {
        "operation": "register", "name": "Organization worker", "rate": 800,
    }))
    wid = worker["labourAttendanceId"]
    assert worker["projectId"] is None
    assert get_table("FIELD_OPERATIONS_TABLE").get_item(Key={
        "PK": "ORG#ORG-A", "SK": f"LABOUR-ATTENDANCE#{wid}",
    }).get("Item")
    assert [w["labourAttendanceId"] for w in data(call(api.workforce_handler, PATH))] == [wid]
    query = {"projectId": "P-A", "date": "2026-09-01"}
    assert data(call(api.workforce_handler, PATH, query=query, **SITE)) == []
    roster = data(call(api.workforce_handler, PATH, query=dict(query, roster="true")))
    assert roster[0]["labourAttendanceId"] == wid
    assert not roster[0]["allocationConfirmed"]
    body = dict(query, labourAttendanceId=wid)
    data(call(api.workforce_handler, PATH, "POST", dict(body, operation="allocate")))
    data(call(api.workforce_handler, PATH, "POST", dict(body, operation="attendance", status="Present"), **SITE))
    assert data(call(api.workforce_handler, PATH, query=query, **SITE))[0]["dailyWage"] == 800


def test_registry_is_organization_scoped_and_admin_only(database):
    body = {"operation": "register", "name": "Worker", "rate": 800}
    data(call(api.workforce_handler, PATH, "POST", body))
    assert data(call(api.workforce_handler, PATH, sub="admin-b", org="ORG-B")) == []
    assert call(api.workforce_handler, PATH, query={"orgId": "ORG-B"})["statusCode"] == 403
    assert call(api.workforce_handler, PATH, "POST", dict(body, orgId="ORG-B"))["statusCode"] == 403
    assert call(api.workforce_handler, PATH, **SITE)["statusCode"] >= 400
    assert call(api.workforce_handler, PATH, "POST", body, **SITE)["statusCode"] >= 400
    assert call(api.workforce_handler, PATH, "POST", {"operation": "allocate"})["statusCode"] == 400
