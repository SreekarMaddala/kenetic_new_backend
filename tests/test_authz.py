import pytest

from common.authz import (
    AuthorizationError,
    OPERATIONS_ADMIN,
    SUPERVISOR,
    identity_from_event,
    require_organization,
    require_role,
)


def event_for(*roles, org_id="ORG-1"):
    return {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "user-1",
                        "custom:org_id": org_id,
                        "cognito:groups": list(roles),
                    }
                }
            }
        }
    }


def test_identity_reads_cognito_claims():
    identity = identity_from_event(event_for(OPERATIONS_ADMIN))
    assert identity.user_id == "user-1"
    assert identity.organization_id == "ORG-1"
    assert identity.roles == frozenset({OPERATIONS_ADMIN})


def test_role_and_tenant_checks_are_enforced():
    identity = identity_from_event(event_for(SUPERVISOR))
    require_role(identity, SUPERVISOR)
    require_organization(identity, "ORG-1")
    with pytest.raises(AuthorizationError):
        require_role(identity, OPERATIONS_ADMIN)
    with pytest.raises(AuthorizationError):
        require_organization(identity, "ORG-2")


def test_identity_rejects_missing_org_claim():
    with pytest.raises(AuthorizationError):
        identity_from_event({"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "user-1"}}}}})
