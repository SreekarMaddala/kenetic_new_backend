from common import model
# Canonical employee IDs are distinct from the signed login subjects.
ORG_A = model.new_id('organization', 'ORG-A')
ORG_B = model.new_id('organization', 'ORG-B')
PLATFORM_ORG = model.new_id('organization', 'PLATFORM')
PROJECT_A = model.new_id('project', 'P-A')
PROJECT_B = model.new_id('project', 'P-B')
ISSUE_A = model.new_id('issue', 'I-A')
ISSUE_B = model.new_id('issue', 'I-B')
VENDOR_ID = model.new_id('vendor', 'vendor-1')
CEMENT_ID = model.new_id('inventory-item', 'cement|bags')
ADMIN_A = model.new_id('user', 'admin-a')
ADMIN_B = model.new_id('user', 'admin-b')
SITE_A = model.new_id('user', 'site-a')
SITE_B = model.new_id('user', 'site-b')
SUPER_USER = model.new_id('user', 'super')
UNASSIGNED = model.new_id('user', 'unassigned')
NEW_USER = model.new_id('user', 'new-sub')
PROJECT_C = 'pro_' + 'c' * 32

SUBJECTS = {ADMIN_A: 'admin-a', ADMIN_B: 'admin-b', SITE_A: 'site-a', SITE_B: 'site-b', SUPER_USER: 'super', UNASSIGNED: 'unassigned'}
import json
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from common.authz import OPERATIONS_ADMIN, SUPER_ADMIN, SUPERVISOR
from common.dynamo import get_table
from handlers import consolidated as api


@pytest.fixture(autouse=True)
def database(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_test")
    with mock_aws():
        db = boto3.resource("dynamodb", region_name="us-east-1")
        from test_model import table_definitions
        for definition, env in table_definitions():
            definition = dict(definition, TableName=env)
            monkeypatch.setenv(env, env)
            db.meta.client.create_table(**definition)
        for org in (ORG_A, ORG_B):
            put("ORGANIZATIONS_TABLE", f"ORG#{org}", f"ORGANIZATION#{org}", orgId=org, entityType="organization", status="Active", name=org)
        for sub, role, org in [(ADMIN_A, OPERATIONS_ADMIN, ORG_A), (ADMIN_B, OPERATIONS_ADMIN, ORG_B), (SUPER_USER, SUPER_ADMIN, ORG_A), (SITE_A, SUPERVISOR, ORG_A), (SITE_B, SUPERVISOR, ORG_B), (UNASSIGNED, SUPERVISOR, ORG_A)]:
            put("USERS_TABLE", f"ORG#{org}", f"USER#{sub}", orgId=org, employeeId=sub, role=role, status="Active", cognitoUsername=SUBJECTS[sub], cognitoSub=SUBJECTS[sub], entityType="user")
        put("PROJECTS_TABLE", f"ORG#{ORG_A}", f"PROJECT#{PROJECT_A}", orgId=ORG_A, projectId=PROJECT_A, supervisorIds=[SITE_A], entityType="project", name="Site A", budget=100, spent=20, status="Active")
        put("PROJECTS_TABLE", f"ORG#{ORG_B}", f"PROJECT#{PROJECT_B}", orgId=ORG_B, projectId=PROJECT_B, supervisorIds=[SITE_B], entityType="project", name="Site B")
        put("SITE_CONTROL_TABLE", f"ORG#{ORG_A}#PROJECT#{PROJECT_A}", f"ISSUE#{ISSUE_A}", orgId=ORG_A, issueId=ISSUE_A, projectId=PROJECT_A, entityType="issue", status="Open")
        put("SITE_CONTROL_TABLE", f"ORG#{ORG_B}#PROJECT#{PROJECT_B}", f"ISSUE#{ISSUE_B}", orgId=ORG_B, issueId=ISSUE_B, projectId=PROJECT_B, entityType="issue", status="Open")
        yield


def put(table, pk, sk, **data):
    get_table(table).put_item(Item=model.index_item({"PK": pk, "SK": sk, **data}))


def event(path, method="GET", body=None, sub=ADMIN_A, role=OPERATIONS_ADMIN, org=ORG_A, query=None):
    return {"rawPath": path, "body": json.dumps(body or {}), "queryStringParameters": query,
            "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": {"sub": SUBJECTS.get(sub, sub), "custom:org_id": org, "cognito:groups": [role]}}}}}


def data(response):
    assert response["statusCode"] < 300, response
    return json.loads(response["body"])["data"]


@pytest.mark.parametrize("method,body", [("GET", None), ("PATCH", {"status": "Closed"}), ("DELETE", None)])
def test_cross_tenant_project_access_denied(method, body):
    result = api.site_control_handler(event(f"/projects/{PROJECT_B}/issues/{ISSUE_B}", method, body), None)
    assert result["statusCode"] == 403
    assert get_table("SITE_CONTROL_TABLE").get_item(Key={"PK": f"ORG#{ORG_B}#PROJECT#{PROJECT_B}", "SK": f"ISSUE#{ISSUE_B}"})["Item"]["status"] == "Open"


def test_cannot_choose_another_tenant_in_query_or_body():
    assert api.projects_handler(event("/projects", query={"orgId": ORG_B}), None)["statusCode"] == 403
    assert api.projects_handler(event("/projects", "POST", {"orgId": ORG_B, "name": "Attack"}), None)["statusCode"] == 400


def test_supervisor_only_lists_assigned_projects_and_cannot_create():
    args = {"sub": SITE_A, "role": SUPERVISOR}
    assert [p["projectId"] for p in data(api.projects_handler(event("/projects", **args), None))] == [PROJECT_A]
    assert data(api.projects_handler(event("/projects", sub=UNASSIGNED, role=SUPERVISOR), None)) == []
    assert api.projects_handler(event("/projects", "POST", {"name": "Bad"}, **args), None)["statusCode"] == 403
    assert api.site_control_handler(event(f"/projects/{PROJECT_A}/issues", sub=UNASSIGNED, role=SUPERVISOR), None)["statusCode"] == 403
    assert len(data(api.site_control_handler(event(f"/projects/{PROJECT_A}/issues", **args), None))) == 1


def test_project_create_get_update_use_same_partition_and_decimals():
    created = data(api.projects_handler(event("/projects", "POST", {"name": "New", "budget": 100.25}), None))
    path = f'/projects/{created["projectId"]}'
    assert data(api.projects_handler(event(path), None))["budget"] == 100.25
    assert data(api.projects_handler(event(path, "PUT", {"budget": 222.50}), None))["budget"] == 222.5
    assert any(p["projectId"] == created["projectId"] for p in data(api.projects_handler(event("/projects"), None)))


@pytest.mark.parametrize("field", ["PK", "SK", "orgId", "projectId", "createdAt", "createdBy", "version", "issueId"])
def test_updates_reject_protected_fields(field):
    response = api.site_control_handler(event(f"/projects/{PROJECT_A}/issues/{ISSUE_A}", "PATCH", {field: "tampered"}), None)
    assert response["statusCode"] in {400, 403}


def test_assignment_checks_same_tenant_and_active_supervisor():
    for ids in [[SITE_B], [ADMIN_A], ["unknown"]]:
        assert api.projects_handler(event(f"/projects/{PROJECT_A}", "PUT", {"supervisorIds": ids}), None)["statusCode"] == 400
    data(api.projects_handler(event(f"/projects/{PROJECT_A}", "PUT", {"supervisorIds": [UNASSIGNED]}), None))
    assert api.site_control_handler(event(f"/projects/{PROJECT_A}/issues", sub=SITE_A, role=SUPERVISOR), None)["statusCode"] == 403
    assert api.site_control_handler(event(f"/projects/{PROJECT_A}/issues", sub=UNASSIGNED, role=SUPERVISOR), None)["statusCode"] == 200


def test_disabled_account_and_suspended_org_block_valid_tokens():
    table = get_table("USERS_TABLE")
    table.update_item(Key={"PK": f"ORG#{ORG_A}", "SK": f"USER#{ADMIN_A}"}, UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "Disabled"})
    assert api.projects_handler(event("/projects"), None)["statusCode"] == 403
    get_table("ORGANIZATIONS_TABLE").update_item(Key={"PK": f"ORG#{ORG_B}", "SK": f"ORGANIZATION#{ORG_B}"}, UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "Suspended"})
    assert api.projects_handler(event("/projects", sub=ADMIN_B, org=ORG_B), None)["statusCode"] == 403


def test_token_role_must_match_profile():
    assert api.platform_admin_handler(event("/super-admin/organizations", role=SUPER_ADMIN), None)["statusCode"] == 403
    assert api.platform_admin_handler(event("/super-admin/organizations"), None)["statusCode"] == 403


def test_platform_organization_lifecycle_and_global_listing():
    args = {"sub": SUPER_USER, "role": SUPER_ADMIN}
    organization = data(api.platform_admin_handler(event("/super-admin/organizations", "POST", {"name": "Client"}, **args), None))
    assert organization["orgId"] != ORG_A
    path = f'/super-admin/organizations/{organization["orgId"]}'
    assert data(api.platform_admin_handler(event(path, **args), None))["name"] == "Client"
    assert len(data(api.platform_admin_handler(event("/super-admin/organizations", **args), None))) == 3
    assert data(api.platform_admin_handler(event(path + "/status", "PATCH", {"status": "Suspended"}, **args), None))["status"] == "Suspended"


@pytest.fixture
def cognito(monkeypatch):
    client = MagicMock()
    client.admin_create_user.return_value = {"User": {"Username": "new-user", "Attributes": [{"Name": "sub", "Value": "new-sub"}]}}
    original_client = boto3.client
    monkeypatch.setattr("common.accounts.boto3.client", lambda service, **kw: client if service == "cognito-idp" else original_client(service, **kw))
    return client


def test_admin_creates_only_supervisors_and_cognito_invitation_is_explicit(cognito):
    body = {"name": "Site User", "email": "site@example.com", "role": SUPERVISOR}
    account = data(api.platform_admin_handler(event("/employees", "POST", body), None))
    assert account["employeeId"] == model.employee_id("new-sub")
    assert "cognitoUsername" not in account
    assert cognito.admin_create_user.call_args.kwargs["DesiredDeliveryMediums"] == ["EMAIL"]
    assert {"Name": "custom:org_id", "Value": ORG_A} in cognito.admin_create_user.call_args.kwargs["UserAttributes"]
    cognito.admin_add_user_to_group.assert_called_once_with(UserPoolId="us-east-1_test", Username="new-user", GroupName=SUPERVISOR)
    data(api.platform_admin_handler(event(f"/employees/{NEW_USER}/invitation", "POST"), None))
    assert cognito.admin_create_user.call_args.kwargs["MessageAction"] == "RESEND"
    for role in [SUPER_ADMIN, OPERATIONS_ADMIN]:
        assert api.platform_admin_handler(event("/employees", "POST", {**body, "role": role}), None)["statusCode"] == 403


def test_superadmin_can_provision_admin_in_another_org(cognito):
    response = api.platform_admin_handler(event("/employees", "POST", {"name": "Admin", "email": "admin@example.com", "role": OPERATIONS_ADMIN, "orgId": ORG_B}, sub=SUPER_USER, role=SUPER_ADMIN), None)
    assert data(response)["orgId"] == ORG_B
    assert get_table("USERS_TABLE").get_item(Key={"PK": f"ORG#{ORG_B}", "SK": f"USER#{NEW_USER}"})["Item"]["role"] == OPERATIONS_ADMIN


def test_partial_provision_failure_removes_new_cognito_account(cognito, monkeypatch):
    cognito.admin_add_user_to_group.side_effect = RuntimeError("Simulated failure")
    result = api.platform_admin_handler(event("/employees", "POST", {"name": "Site", "email": "site@example.com", "role": SUPERVISOR}), None)
    assert result["statusCode"] == 500
    cognito.admin_delete_user.assert_called_once()
    assert "Item" not in get_table("USERS_TABLE").get_item(Key={"PK": f"ORG#{ORG_A}", "SK": f"USER#{NEW_USER}"})


def test_disable_account_blocks_existing_token_even_if_cognito_fails(cognito):
    cognito.admin_disable_user.side_effect = RuntimeError("Simulated outage")
    assert api.platform_admin_handler(event(f"/employees/{SITE_A}", "PUT", {"status": "Disabled"}), None)["statusCode"] == 500
    assert api.projects_handler(event("/projects", sub=SITE_A, role=SUPERVISOR), None)["statusCode"] == 403
    assert api.platform_admin_handler(event(f"/employees/{ADMIN_A}", "PUT", {"status": "Disabled"}), None)["statusCode"] == 403
    assert api.platform_admin_handler(event(f"/employees/{SITE_B}", "PUT", {"status": "Disabled"}, query={"orgId": ORG_B}), None)["statusCode"] == 403


def test_attendance_uses_authenticated_user_and_prevents_duplicates():
    args = {"sub": SITE_A, "role": SUPERVISOR}
    body = {"projectId": PROJECT_A, "supervisorId": SITE_B, "location": {"latitude": 12.97, "longitude": 77.59, "accuracy": 15}}
    assert api.workforce_handler(event("/supervisor/attendance/check-in", "POST", body, **args), None)["statusCode"] == 400
    body.pop("supervisorId")
    checkin = data(api.workforce_handler(event("/supervisor/attendance/check-in", "POST", body, **args), None))
    assert checkin["supervisorId"] == SITE_A
    assert api.workforce_handler(event("/supervisor/attendance/check-in", "POST", body, **args), None)["statusCode"] == 409
    checkout = data(api.workforce_handler(event("/supervisor/attendance/check-out", "POST", body, **args), None))
    assert checkout["durationSeconds"] >= 0
    assert api.workforce_handler(event("/supervisor/attendance/check-out", "POST", body, **args), None)["statusCode"] == 409


def test_unknown_routes_and_invalid_payloads_fail_closed():
    assert api.projects_handler(event(f"/projects/{PROJECT_A}/anything"), None)["statusCode"] == 404
    bad = event("/projects", "POST")
    bad["body"] = "[]"
    assert api.projects_handler(bad, None)["statusCode"] == 400


def test_settings_round_trip_and_analytics_are_real():
    assert data(api.governance_handler(event("/settings", "PUT", {"companyName": "Example"}), None))["companyName"] == "Example"
    assert data(api.governance_handler(event("/settings"), None))["companyName"] == "Example"
    stats = data(api.governance_handler(event("/dashboard/analytics"), None))
    assert stats["totalProjects"] == 1
    assert stats["totalBudget"] == 100
    assert stats["totalSpent"] == 20


def test_identical_project_ids_in_different_tenants_do_not_share_records():
    put("PROJECTS_TABLE", f"ORG#{ORG_B}", f"PROJECT#{PROJECT_A}", orgId=ORG_B, projectId=PROJECT_A, supervisorIds=[SITE_B], entityType="project", name="Different site")
    assert data(api.site_control_handler(event(f"/projects/{PROJECT_A}/issues", sub=ADMIN_B, org=ORG_B), None)) == []
    created = data(api.site_control_handler(event(f"/projects/{PROJECT_A}/issues", "POST", {"title": "Other tenant", "description": "Independent tenant issue"}, sub=ADMIN_B, org=ORG_B), None))
    assert created["orgId"] == ORG_B
    assert [i["issueId"] for i in data(api.site_control_handler(event(f"/projects/{PROJECT_A}/issues"), None))] == [ISSUE_A]


def test_auth_me_requires_active_profile_and_rejects_unimplemented_routes():
    assert data(api.auth_handler(event("/auth/me"), None))["role"] == OPERATIONS_ADMIN
    assert api.auth_handler(event("/auth/invite", "POST"), None)["statusCode"] == 404
    assert api.auth_handler({"rawPath": "/auth/me"}, None)["statusCode"] == 403


def test_cannot_modify_account_role_or_org(cognito):
    assert api.platform_admin_handler(event(f"/employees/{SITE_A}", "PUT", {"role": SUPER_ADMIN}), None)["statusCode"] == 400
    assert api.platform_admin_handler(event(f"/employees/{SITE_A}", "PUT", {"orgId": ORG_B}), None)["statusCode"] == 400


def test_inviting_disabled_user_is_rejected(cognito):
    data(api.platform_admin_handler(event(f"/employees/{SITE_A}", "PUT", {"status": "Disabled"}), None))
    assert api.platform_admin_handler(event(f"/employees/{SITE_A}/invitation", "POST"), None)["statusCode"] == 400
    cognito.admin_create_user.assert_not_called()




def test_bootstrap_uses_same_pool_and_creates_active_profile(cognito):
    from scripts.bootstrap_account import bootstrap
    from types import SimpleNamespace
    args = SimpleNamespace(region="us-east-1", users_table="USERS_TABLE", organizations_table="ORGANIZATIONS_TABLE",
                           org_id=PLATFORM_ORG, org_name="Platform", pool_id="us-east-1_test", email="owner@example.com",
                           name="Owner", role=SUPER_ADMIN, send_invitation=False)
    cognito.admin_create_user.return_value["User"]["Attributes"].append({"Name": "custom:org_id", "Value": PLATFORM_ORG})
    bootstrap(args)
    cognito.admin_add_user_to_group.assert_called_once_with(UserPoolId="us-east-1_test", Username="new-user", GroupName=SUPER_ADMIN)
    profile = get_table("USERS_TABLE").get_item(Key={"PK": f"ORG#{PLATFORM_ORG}", "SK": f"USER#{NEW_USER}"})["Item"]
    assert profile["status"] == "Active" and profile["role"] == SUPER_ADMIN
    assert cognito.admin_create_user.call_args.kwargs["MessageAction"] == "SUPPRESS"


def test_record_identifiers_cannot_change_resource_or_permissions():
    assert api.platform_admin_handler(event("/employees/organizations", "POST", {"name": "Bad"}), None)["statusCode"] != 201
    assert api._resource("/supervisor/dpr/organizations") == "dpr"
    assert api._resource("/projects/organizations/issues/employees") == "issue"
    assert api._item_id("/projects/issues/issues/actual-id", "issue") == "actual-id"
