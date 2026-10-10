"""CORS belongs to the HTTP API, including routes added in future changes."""
from pathlib import Path

import pytest
import yaml

from common.response import success_response, error_response
from handlers import consolidated as api


ROOT = Path(__file__).resolve().parents[1]


def test_all_http_routes_use_gateway_cors_without_lambda_preflight():
    template = yaml.safe_load((ROOT / "template.yaml").read_text(encoding="utf-8"))
    resources = template["Resources"]
    gateway = resources["KineticHttpApi"]["Properties"]
    cors = gateway["CorsConfiguration"]
    assert cors["AllowOrigins"] == [{"Ref": "FrontendOrigin"}, "http://localhost:5173"]
    assert resources["DocumentsBucket"]["Properties"]["CorsConfiguration"]["CorsRules"][0]["AllowedOrigins"] == cors["AllowOrigins"]
    assert set(cors["AllowMethods"]) >= {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
    assert {header.lower() for header in cors["AllowHeaders"]} >= {"authorization", "content-type", "x-amz-date", "x-api-key", "x-amz-security-token"}
    assert gateway["Auth"]["DefaultAuthorizer"] == "CognitoJwt"
    routes = set()
    for resource in resources.values():
        if resource["Type"] != "AWS::Serverless::Function":
            continue
        for event in resource["Properties"].get("Events", {}).values():
            if event["Type"] != "HttpApi":
                continue
            properties = event["Properties"]
            assert properties["ApiId"] == {"Ref": "KineticHttpApi"}
            # ANY captures preflight and applies JWT auth before gateway CORS.
            # Explicit business methods leave OPTIONS to API Gateway.
            assert properties["Method"].upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            assert properties.get("Path") not in (None, "$default")
            assert properties.get("Auth", {}).get("Authorizer", "CognitoJwt") == "CognitoJwt"
            routes.add(properties["Path"])
    assert routes >= {"/subcontractors", "/subcontractors/{proxy+}",
                      "/projects/{projectId}/subcontractors", "/projects/{projectId}/subcontractors/{proxy+}"}


@pytest.mark.parametrize("response", [
    api._response(200, {"success": True}),
    api._error(403, "FORBIDDEN", "Denied"),
    api._error(500, "INTERNAL_ERROR", "Failed"),
    success_response({"ok": True}),
    error_response("Denied", 403),
])
def test_lambda_responses_do_not_set_cors_headers(response):
    assert response["headers"]["Content-Type"] == "application/json"
    assert not any(header.lower().startswith("access-control-") for header in response["headers"])


@pytest.mark.parametrize("handler", [api.auth_handler, api.project_commercial_handler])
def test_lambda_does_not_bypass_authentication_for_options(handler):
    # Gateway owns preflight. A direct Lambda invocation has no special bypass.
    event = {"rawPath": "/subcontractors", "requestContext": {"http": {"method": "OPTIONS"}}}
    response = handler(event, None)
    assert response["statusCode"] == 403
