"""Provision the first operator account without exposing passwords.

Dry-run by default. Use the existing pool ID for both admins and superadmins.
This script is an operator tool, never an unauthenticated API route.
"""
import argparse
import json
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError


def bootstrap(args):
    client = boto3.client("cognito-idp", region_name=args.region)
    db = boto3.resource("dynamodb", region_name=args.region)
    users = db.Table(args.users_table)
    organizations = db.Table(args.organizations_table)
    org_key = {"PK": f"ORG#{args.org_id}", "SK": f"ORGANIZATION#{args.org_id}"}
    organization = organizations.get_item(Key=org_key, ConsistentRead=True).get("Item")
    if organization and organization.get("status") != "Active":
        raise ValueError("Cannot bootstrap an account in an inactive organization")
    now = datetime.now(timezone.utc).isoformat()
    if not organization:
        organizations.put_item(Item={**org_key, "orgId": args.org_id, "entityType": "organization", "name": args.org_name,
                                     "status": "Active", "createdAt": now, "updatedAt": now}, ConditionExpression="attribute_not_exists(PK)")
    created = False
    try:
        result = client.admin_create_user(UserPoolId=args.pool_id, Username=args.email, MessageAction="SUPPRESS", UserAttributes=[
            {"Name": "email", "Value": args.email}, {"Name": "email_verified", "Value": "true"},
            {"Name": "name", "Value": args.name}, {"Name": "custom:org_id", "Value": args.org_id}])["User"]
        created = True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "UsernameExistsException":
            raise
        existing = client.admin_get_user(UserPoolId=args.pool_id, Username=args.email)
        result = {"Username": existing["Username"], "Attributes": existing["UserAttributes"]}
    attributes = {a["Name"]: a["Value"] for a in result["Attributes"]}
    username = result["Username"]
    if attributes.get("custom:org_id") != args.org_id:
        raise ValueError("Existing account has a different or missing immutable organization; no permissions were changed")
    if not created:
        groups = client.admin_list_groups_for_user(UserPoolId=args.pool_id, Username=username)["Groups"]
        if {g["GroupName"] for g in groups} != {args.role}:
            raise ValueError("Existing account does not have exactly the requested role; refusing to elevate it")
    sub = attributes["sub"]
    key = {"PK": f"ORG#{args.org_id}", "SK": f"USER#{sub}"}
    profile = users.get_item(Key=key, ConsistentRead=True).get("Item")
    if profile and (profile.get("role") != args.role or profile.get("status") != "Active"):
        raise ValueError("Existing database profile conflicts with the requested account")
    try:
        if created:
            client.admin_add_user_to_group(UserPoolId=args.pool_id, Username=username, GroupName=args.role)
        if not profile:
            users.put_item(Item={**key, "entityType": "user", "employeeId": sub, "orgId": args.org_id,
                                 "name": args.name, "email": args.email, "role": args.role, "status": "Active",
                                 "cognitoUsername": username, "department": "", "phone": "", "location": "",
                                 "createdAt": now, "updatedAt": now, "invitationStatus": "Pending"}, ConditionExpression="attribute_not_exists(PK)")
    except Exception:
        if created:
            client.admin_delete_user(UserPoolId=args.pool_id, Username=username)
        raise
    if args.send_invitation:
        client.admin_create_user(UserPoolId=args.pool_id, Username=username, MessageAction="RESEND", DesiredDeliveryMediums=["EMAIL"])
        users.update_item(Key=key, UpdateExpression="SET invitationStatus = :sent", ExpressionAttributeValues={":sent": "Sent"})
    print(f"Provisioned {args.role} in organization {args.org_id}. No password was printed or stored by this script.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-id", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", choices=["super_admin", "operations_admin"], default="super_admin")
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--org-name", required=True)
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument("--users-table", default="kinetic-v2-users")
    parser.add_argument("--organizations-table", default="kinetic-v2-organizations")
    parser.add_argument("--apply", action="store_true", help="Actually create the account and database profile")
    parser.add_argument("--send-invitation", action="store_true", help="Send the Cognito temporary-password invitation email")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"mode": "dry-run", "pool": args.pool_id, "organization": args.org_id,
                          "role": args.role, "email": args.email, "sendInvitation": args.send_invitation}, indent=2))
        return
    bootstrap(args)


if __name__ == "__main__":
    main()
