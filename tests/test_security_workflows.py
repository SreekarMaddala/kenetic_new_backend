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
        for name in {config[0] for config in api.DOMAIN_CONFIG.values()} | {"USERS_TABLE", "PARTIES_TABLE", "MATERIALS_LOGISTICS_TABLE", "AUDIT_EVENTS_TABLE"}:
            monkeypatch.setenv(name, name)
            db.create_table(TableName=name, KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}], AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        for org in ("ORG-A", "ORG-B"):
            put("ORGANIZATIONS_TABLE", f"ORG#{org}", f"ORGANIZATION#{org}", orgId=org, entityType="organization", status="Active", name=org)
        for sub, role, org in [("admin-a", OPERATIONS_ADMIN, "ORG-A"), ("admin-b", OPERATIONS_ADMIN, "ORG-B"), ("super", SUPER_ADMIN, "ORG-A"), ("site-a", SUPERVISOR, "ORG-A"), ("site-b", SUPERVISOR, "ORG-B"), ("unassigned", SUPERVISOR, "ORG-A")]:
            put("USERS_TABLE", f"ORG#{org}", f"USER#{sub}", orgId=org, employeeId=sub, role=role, status="Active", cognitoUsername=sub, entityType="user")
        put("PROJECTS_TABLE", "ORG#ORG-A", "PROJECT#P-A", orgId="ORG-A", projectId="P-A", supervisorIds=["site-a"], entityType="project", name="Site A", budget=100, spent=20, status="Active")
        put("PROJECTS_TABLE", "ORG#ORG-B", "PROJECT#P-B", orgId="ORG-B", projectId="P-B", supervisorIds=["site-b"], entityType="project", name="Site B")
        put("SITE_CONTROL_TABLE", "ORG#ORG-A#PROJECT#P-A", "ISSUE#I-A", orgId="ORG-A", issueId="I-A", projectId="P-A", entityType="issue", status="Open")
        put("SITE_CONTROL_TABLE", "ORG#ORG-B#PROJECT#P-B", "ISSUE#I-B", orgId="ORG-B", issueId="I-B", projectId="P-B", entityType="issue", status="Open")
        yield


def put(table, pk, sk, **data):
    get_table(table).put_item(Item={"PK": pk, "SK": sk, **data})


def event(path, method="GET", body=None, sub="admin-a", role=OPERATIONS_ADMIN, org="ORG-A", query=None):
    return {"rawPath": path, "body": json.dumps(body or {}), "queryStringParameters": query,
            "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": {"sub": sub, "custom:org_id": org, "cognito:groups": [role]}}}}}


def data(response):
    assert response["statusCode"] < 300, response
    return json.loads(response["body"])["data"]


@pytest.mark.parametrize("method,body", [("GET", None), ("PATCH", {"status": "Closed"}), ("DELETE", None)])
def test_cross_tenant_project_access_denied(method, body):
    result = api.site_control_handler(event("/projects/P-B/issues/I-B", method, body), None)
    assert result["statusCode"] == 403
    assert get_table("SITE_CONTROL_TABLE").get_item(Key={"PK": "ORG#ORG-B#PROJECT#P-B", "SK": "ISSUE#I-B"})["Item"]["status"] == "Open"


def test_cannot_choose_another_tenant_in_query_or_body():
    assert api.projects_handler(event("/projects", query={"orgId": "ORG-B"}), None)["statusCode"] == 403
    assert api.projects_handler(event("/projects", "POST", {"orgId": "ORG-B", "name": "Attack"}), None)["statusCode"] == 403


def test_supervisor_only_lists_assigned_projects_and_cannot_create():
    args = {"sub": "site-a", "role": SUPERVISOR}
    assert [p["projectId"] for p in data(api.projects_handler(event("/projects", **args), None))] == ["P-A"]
    assert data(api.projects_handler(event("/projects", sub="unassigned", role=SUPERVISOR), None)) == []
    assert api.projects_handler(event("/projects", "POST", {"name": "Bad"}, **args), None)["statusCode"] == 403
    assert api.site_control_handler(event("/projects/P-A/issues", sub="unassigned", role=SUPERVISOR), None)["statusCode"] == 403
    assert len(data(api.site_control_handler(event("/projects/P-A/issues", **args), None))) == 1


def test_project_create_get_update_use_same_partition_and_decimals():
    created = data(api.projects_handler(event("/projects", "POST", {"name": "New", "budget": 100.25}), None))
    path = f'/projects/{created["projectId"]}'
    assert data(api.projects_handler(event(path), None))["budget"] == 100.25
    assert data(api.projects_handler(event(path, "PUT", {"budget": 222.50}), None))["budget"] == 222.5
    assert any(p["projectId"] == created["projectId"] for p in data(api.projects_handler(event("/projects"), None)))


@pytest.mark.parametrize("field", ["PK", "SK", "orgId", "projectId", "createdAt", "createdBy", "version", "issueId"])
def test_updates_reject_protected_fields(field):
    response = api.site_control_handler(event("/projects/P-A/issues/I-A", "PATCH", {field: "tampered"}), None)
    assert response["statusCode"] in {400, 403}


def test_assignment_checks_same_tenant_and_active_supervisor():
    for ids in [["site-b"], ["admin-a"], ["unknown"]]:
        assert api.projects_handler(event("/projects/P-A", "PUT", {"supervisorIds": ids}), None)["statusCode"] == 400
    data(api.projects_handler(event("/projects/P-A", "PUT", {"supervisorIds": ["unassigned"]}), None))
    assert api.site_control_handler(event("/projects/P-A/issues", sub="site-a", role=SUPERVISOR), None)["statusCode"] == 403
    assert api.site_control_handler(event("/projects/P-A/issues", sub="unassigned", role=SUPERVISOR), None)["statusCode"] == 200


def test_disabled_account_and_suspended_org_block_valid_tokens():
    table = get_table("USERS_TABLE")
    table.update_item(Key={"PK": "ORG#ORG-A", "SK": "USER#admin-a"}, UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "Disabled"})
    assert api.projects_handler(event("/projects"), None)["statusCode"] == 403
    get_table("ORGANIZATIONS_TABLE").update_item(Key={"PK": "ORG#ORG-B", "SK": "ORGANIZATION#ORG-B"}, UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "Suspended"})
    assert api.projects_handler(event("/projects", sub="admin-b", org="ORG-B"), None)["statusCode"] == 403


def test_token_role_must_match_profile():
    assert api.platform_admin_handler(event("/super-admin/organizations", role=SUPER_ADMIN), None)["statusCode"] == 403
    assert api.platform_admin_handler(event("/super-admin/organizations"), None)["statusCode"] == 403


def test_platform_organization_lifecycle_and_global_listing():
    args = {"sub": "super", "role": SUPER_ADMIN}
    organization = data(api.platform_admin_handler(event("/super-admin/organizations", "POST", {"name": "Client"}, **args), None))
    assert organization["orgId"] != "ORG-A"
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
    assert account["employeeId"] == "new-sub"
    assert "cognitoUsername" not in account
    assert cognito.admin_create_user.call_args.kwargs["DesiredDeliveryMediums"] == ["EMAIL"]
    assert {"Name": "custom:org_id", "Value": "ORG-A"} in cognito.admin_create_user.call_args.kwargs["UserAttributes"]
    cognito.admin_add_user_to_group.assert_called_once_with(UserPoolId="us-east-1_test", Username="new-user", GroupName=SUPERVISOR)
    data(api.platform_admin_handler(event("/employees/new-sub/invitation", "POST"), None))
    assert cognito.admin_create_user.call_args.kwargs["MessageAction"] == "RESEND"
    for role in [SUPER_ADMIN, OPERATIONS_ADMIN]:
        assert api.platform_admin_handler(event("/employees", "POST", {**body, "role": role}), None)["statusCode"] == 403


def test_superadmin_can_provision_admin_in_another_org(cognito):
    response = api.platform_admin_handler(event("/employees", "POST", {"name": "Admin", "email": "admin@example.com", "role": OPERATIONS_ADMIN, "orgId": "ORG-B"}, sub="super", role=SUPER_ADMIN), None)
    assert data(response)["orgId"] == "ORG-B"
    assert get_table("USERS_TABLE").get_item(Key={"PK": "ORG#ORG-B", "SK": "USER#new-sub"})["Item"]["role"] == OPERATIONS_ADMIN


def test_partial_provision_failure_removes_new_cognito_account(cognito, monkeypatch):
    cognito.admin_add_user_to_group.side_effect = RuntimeError("Simulated failure")
    result = api.platform_admin_handler(event("/employees", "POST", {"name": "Site", "email": "site@example.com", "role": SUPERVISOR}), None)
    assert result["statusCode"] == 500
    cognito.admin_delete_user.assert_called_once()
    assert "Item" not in get_table("USERS_TABLE").get_item(Key={"PK": "ORG#ORG-A", "SK": "USER#new-sub"})


def test_disable_account_blocks_existing_token_even_if_cognito_fails(cognito):
    cognito.admin_disable_user.side_effect = RuntimeError("Simulated outage")
    assert api.platform_admin_handler(event("/employees/site-a", "PUT", {"status": "Disabled"}), None)["statusCode"] == 500
    assert api.projects_handler(event("/projects", sub="site-a", role=SUPERVISOR), None)["statusCode"] == 403
    assert api.platform_admin_handler(event("/employees/admin-a", "PUT", {"status": "Disabled"}), None)["statusCode"] == 403
    assert api.platform_admin_handler(event("/employees/site-b", "PUT", {"status": "Disabled"}, query={"orgId": "ORG-B"}), None)["statusCode"] == 403


def test_attendance_uses_authenticated_user_and_prevents_duplicates():
    args = {"sub": "site-a", "role": SUPERVISOR}
    body = {"projectId": "P-A", "supervisorId": "site-b"}
    checkin = data(api.workforce_handler(event("/supervisor/attendance/check-in", "POST", body, **args), None))
    assert checkin["supervisorId"] == "site-a"
    assert api.workforce_handler(event("/supervisor/attendance/check-in", "POST", body, **args), None)["statusCode"] == 409
    checkout = data(api.workforce_handler(event("/supervisor/attendance/check-out", "POST", body, **args), None))
    assert checkout["durationSeconds"] >= 0
    assert api.workforce_handler(event("/supervisor/attendance/check-out", "POST", body, **args), None)["statusCode"] == 409


def test_unknown_routes_and_invalid_payloads_fail_closed():
    assert api.projects_handler(event("/projects/P-A/anything"), None)["statusCode"] == 404
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
    put("PROJECTS_TABLE", "ORG#ORG-B", "PROJECT#P-A", orgId="ORG-B", projectId="P-A", supervisorIds=["site-b"], entityType="project", name="Different site")
    assert data(api.site_control_handler(event("/projects/P-A/issues", sub="admin-b", org="ORG-B"), None)) == []
    created = data(api.site_control_handler(event("/projects/P-A/issues", "POST", {"title": "Other tenant"}, sub="admin-b", org="ORG-B"), None))
    assert created["orgId"] == "ORG-B"
    assert [i["issueId"] for i in data(api.site_control_handler(event("/projects/P-A/issues"), None))] == ["I-A"]


def test_auth_me_requires_active_profile_and_rejects_unimplemented_routes():
    assert data(api.auth_handler(event("/auth/me"), None))["role"] == OPERATIONS_ADMIN
    assert api.auth_handler(event("/auth/invite", "POST"), None)["statusCode"] == 404
    assert api.auth_handler({"rawPath": "/auth/me"}, None)["statusCode"] == 403


def test_cannot_modify_account_role_or_org(cognito):
    assert api.platform_admin_handler(event("/employees/site-a", "PUT", {"role": SUPER_ADMIN}), None)["statusCode"] == 400
    assert api.platform_admin_handler(event("/employees/site-a", "PUT", {"orgId": "ORG-B"}), None)["statusCode"] == 403


def test_inviting_disabled_user_is_rejected(cognito):
    data(api.platform_admin_handler(event("/employees/site-a", "PUT", {"status": "Disabled"}), None))
    assert api.platform_admin_handler(event("/employees/site-a/invitation", "POST"), None)["statusCode"] == 400
    cognito.admin_create_user.assert_not_called()


def test_migration_copies_only_verified_records_and_is_idempotent():
    from scripts.migrate_project_partitions import migrate
    mapping = {"projects": "PROJECTS_TABLE", "parties": "PARTIES_TABLE", "field-operations": "FIELD_OPERATIONS_TABLE",
               "site-control": "SITE_CONTROL_TABLE", "materials-logistics": "MATERIALS_LOGISTICS_TABLE",
               "document-control": "DOCUMENT_CONTROL_TABLE", "inventory": "INVENTORY_TABLE", "finance": "FINANCE_TABLE", "settings": "SETTINGS_TABLE"}
    class Database:
        def Table(self, name):
            return get_table(mapping[name.removeprefix("test-")])
    put("SITE_CONTROL_TABLE", "PROJECT#P-A", "ISSUE#LEGACY", orgId="ORG-A", issueId="LEGACY")
    put("SITE_CONTROL_TABLE", "PROJECT#UNKNOWN", "ISSUE#ORPHAN", orgId="ORG-B")
    result = migrate(Database(), "test")
    assert result["pending"] == 1 and result["conflicts"] == 1
    assert "Item" not in get_table("SITE_CONTROL_TABLE").get_item(Key={"PK": "ORG#ORG-A#PROJECT#P-A", "SK": "ISSUE#LEGACY"})
    result = migrate(Database(), "test", apply=True)
    assert result["copied"] == 1
    assert "Item" in get_table("SITE_CONTROL_TABLE").get_item(Key={"PK": "PROJECT#P-A", "SK": "ISSUE#LEGACY"})
    assert migrate(Database(), "test", apply=True)["existing"] == 1


def test_bootstrap_uses_same_pool_and_creates_active_profile(cognito):
    from scripts.bootstrap_account import bootstrap
    from types import SimpleNamespace
    args = SimpleNamespace(region="us-east-1", users_table="USERS_TABLE", organizations_table="ORGANIZATIONS_TABLE",
                           org_id="PLATFORM", org_name="Platform", pool_id="us-east-1_test", email="owner@example.com",
                           name="Owner", role=SUPER_ADMIN, send_invitation=False)
    cognito.admin_create_user.return_value["User"]["Attributes"].append({"Name": "custom:org_id", "Value": "PLATFORM"})
    bootstrap(args)
    cognito.admin_add_user_to_group.assert_called_once_with(UserPoolId="us-east-1_test", Username="new-user", GroupName=SUPER_ADMIN)
    profile = get_table("USERS_TABLE").get_item(Key={"PK": "ORG#PLATFORM", "SK": "USER#new-sub"})["Item"]
    assert profile["status"] == "Active" and profile["role"] == SUPER_ADMIN
    assert cognito.admin_create_user.call_args.kwargs["MessageAction"] == "SUPPRESS"


def test_record_identifiers_cannot_change_resource_or_permissions():
    assert api.platform_admin_handler(event("/employees/organizations", "POST", {"name": "Bad"}), None)["statusCode"] != 201
    assert api._resource("/supervisor/dpr/organizations") == "dpr"
    assert api._resource("/projects/organizations/issues/employees") == "issue"
    assert api._item_id("/projects/issues/issues/actual-id", "issue") == "actual-id"
