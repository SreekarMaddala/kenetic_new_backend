"""Request identity and role-based authorization for HTTP API Lambda handlers.

The API Gateway JWT authorizer verifies a token.  This module is deliberately
responsible for the application-level checks that the authorizer cannot make:
role, tenant, and project scope.  New consolidated handlers must use it before
accessing DynamoDB.
"""

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


SUPER_ADMIN = "super_admin"
OPERATIONS_ADMIN = "operations_admin"
SUPERVISOR = "supervisor"

ALL_ROLES = frozenset({SUPER_ADMIN, OPERATIONS_ADMIN, SUPERVISOR})


class AuthorizationError(PermissionError):
    """Raised when a caller is not permitted to perform an API operation."""


@dataclass(frozen=True)
class Identity:
    user_id: str
    organization_id: str
    roles: frozenset[str]

    def has_any_role(self, roles: Iterable[str]) -> bool:
        return bool(self.roles.intersection(roles))


def identity_from_event(event: Mapping[str, Any]) -> Identity:
    """Build an identity from API Gateway HTTP API JWT claims.

    Cognito emits group membership in ``cognito:groups``.  The organization is
    supplied by the immutable ``custom:org_id`` user-pool attribute.  Support
    both a JSON array and Cognito's common string representation for tests and
    local API emulation.
    """
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    if not claims:
        raise AuthorizationError("Authentication is required")

    user_id = str(claims.get("sub") or "")
    organization_id = str(claims.get("custom:org_id") or "")
    raw_roles = claims.get("cognito:groups", [])
    if isinstance(raw_roles, str):
        raw_roles = raw_roles.strip("[]").replace('"', "").split(",")
    if not isinstance(raw_roles, (list, tuple)):
        raise AuthorizationError("Invalid role claims")
    roles = frozenset(str(role).strip() for role in raw_roles if str(role).strip())

    if not user_id or not organization_id:
        raise AuthorizationError("Token is missing required identity claims")
    unknown = roles.difference(ALL_ROLES)
    if unknown:
        raise AuthorizationError("Token contains an unsupported role")
    if not roles:
        raise AuthorizationError("Token does not include an application role")
    if len(roles) != 1:
        raise AuthorizationError("An account must have exactly one application role")
    return Identity(user_id=user_id, organization_id=organization_id, roles=roles)


def require_role(identity: Identity, *roles: str) -> None:
    if not identity.has_any_role(roles):
        raise AuthorizationError("You are not permitted to perform this operation")


def require_organization(identity: Identity, organization_id: str) -> None:
    if SUPER_ADMIN not in identity.roles and identity.organization_id != organization_id:
        raise AuthorizationError("You are not permitted to access this organization")


def active_identity(event):
    """Fail closed for disabled accounts, suspended tenants and stale role tokens."""
    from common.dynamo import get_table

    identity = identity_from_event(event)
    profile = get_table("USERS_TABLE").get_item(
        Key={"PK": f"ORG#{identity.organization_id}", "SK": f"USER#{identity.user_id}"},
        ConsistentRead=True,
    ).get("Item")
    if (not profile or profile.get("status") != "Active"
            or profile.get("orgId") != identity.organization_id
            or profile.get("role") not in identity.roles):
        raise AuthorizationError("Your account is inactive or its permissions have changed. Sign in again or contact your administrator.")
    organization = get_table("ORGANIZATIONS_TABLE").get_item(
        Key={"PK": f"ORG#{identity.organization_id}", "SK": f"ORGANIZATION#{identity.organization_id}"},
        ConsistentRead=True,
    ).get("Item")
    if not organization or organization.get("status") != "Active":
        raise AuthorizationError("Your organization is inactive")
    return identity
