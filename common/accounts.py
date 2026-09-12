"""One Cognito pool, one application role per account, immutable tenant membership."""
import logging
import os
from datetime import datetime, timezone

import boto3
from common.authz import AuthorizationError, SUPER_ADMIN, OPERATIONS_ADMIN, SUPERVISOR, require_organization
from common.dynamo import get_table

log = logging.getLogger(__name__)


def create_account(identity, data):
    allowed = {"name", "email", "role", "orgId", "department", "phone", "location"}
    if set(data) - allowed:
        raise ValueError("Unsupported account fields")
    role = data.get("role")
    allowed_roles = {OPERATIONS_ADMIN} if SUPER_ADMIN in identity.roles else {SUPERVISOR}
    if not identity.has_any_role({SUPER_ADMIN, OPERATIONS_ADMIN}) or role not in allowed_roles:
        raise AuthorizationError("You cannot create an account with this role")
    org_id = data.get("orgId") or identity.organization_id
    require_organization(identity, org_id)
    organization = get_table("ORGANIZATIONS_TABLE").get_item(
        Key={"PK": f"ORG#{org_id}", "SK": f"ORGANIZATION#{org_id}"}, ConsistentRead=True,
    ).get("Item")
    if not organization or organization.get("status") != "Active":
        raise ValueError("Choose an active organization")
    email = str(data.get("email", "")).strip().lower()
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 128 or len(email) > 254 or "@" not in email or " " in email:
        raise ValueError("A name and valid email address are required")
    client = boto3.client("cognito-idp")
    pool_id = os.environ["COGNITO_USER_POOL_ID"]
    # Suppress delivery until group and database provisioning have succeeded.
    result = client.admin_create_user(
        UserPoolId=pool_id, Username=email, MessageAction="SUPPRESS",
        UserAttributes=[{"Name": "email", "Value": email}, {"Name": "name", "Value": name},
                        {"Name": "email_verified", "Value": "true"}, {"Name": "custom:org_id", "Value": org_id}],
    )["User"]
    username = result["Username"]
    sub = next(a["Value"] for a in result["Attributes"] if a["Name"] == "sub")
    now = datetime.now(timezone.utc).isoformat()
    item = {"PK": f"ORG#{org_id}", "SK": f"USER#{sub}", "entityType": "user",
            "employeeId": sub, "orgId": org_id, "cognitoUsername": username,
            "name": name, "email": email, "role": role, "status": "Active",
            "department": str(data.get("department", "")), "phone": str(data.get("phone", "")),
            "location": str(data.get("location", "")), "createdAt": now, "updatedAt": now,
            "invitationStatus": "Pending"}
    try:
        client.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=role)
        get_table("USERS_TABLE").put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    except Exception:
        # This account was created by this request; never delete a pre-existing user.
        client.admin_delete_user(UserPoolId=pool_id, Username=username)
        raise
    return item


def resend_invitation(identity, item):
    require_managed_account(identity, item)
    if item.get("status") != "Active":
        raise ValueError("Enable the account before sending an invitation")
    client = boto3.client("cognito-idp")
    client.admin_create_user(UserPoolId=os.environ["COGNITO_USER_POOL_ID"],
                            Username=item["email"], MessageAction="RESEND",
                            DesiredDeliveryMediums=["EMAIL"])
    result = get_table("USERS_TABLE").update_item(
        Key={"PK": item["PK"], "SK": item["SK"]},
        UpdateExpression="SET invitationStatus = :sent",
        ExpressionAttributeValues={":sent": "Sent"}, ReturnValues="ALL_NEW",
    )
    return result["Attributes"]


def require_managed_account(identity, item):
    require_organization(identity, item["orgId"])
    if item.get("role") == SUPER_ADMIN or item["employeeId"] == identity.user_id:
        raise AuthorizationError("Platform accounts must be managed by the deployment operator")
    if OPERATIONS_ADMIN in identity.roles and item.get("role") != SUPERVISOR:
        raise AuthorizationError("Operations admins can only manage supervisors")


def update_account(identity, item, data):
    require_managed_account(identity, item)
    if set(data) - {"department", "phone", "location", "status"}:
        raise ValueError("Only department, phone, location and status can be changed; role and organization are immutable")
    if "status" in data and data["status"] not in {"Active", "Disabled"}:
        raise ValueError("Account status must be Active or Disabled")
    # Store the deny first. A failed Cognito call cannot leave a disabled user authorized.
    client = boto3.client("cognito-idp")
    args = {"UserPoolId": os.environ["COGNITO_USER_POOL_ID"], "Username": item["cognitoUsername"]}
    if data.get("status") == "Disabled":
        get_table("USERS_TABLE").update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression="SET #status = :disabled",
            ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues={":disabled": "Disabled"},
        )
        client.admin_disable_user(**args)
        client.admin_user_global_sign_out(**args)
    elif data.get("status") == "Active":
        client.admin_enable_user(**args)
    updates = {**data, "updatedAt": datetime.now(timezone.utc).isoformat()}
    result = get_table("USERS_TABLE").update_item(
        Key={"PK": item["PK"], "SK": item["SK"]},
        UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(updates))),
        ExpressionAttributeNames={f"#f{i}": name for i, name in enumerate(updates)},
        ExpressionAttributeValues={f":v{i}": value for i, value in enumerate(updates.values())}, ReturnValues="ALL_NEW",
    )
    return result["Attributes"]
