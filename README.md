# Kinetic ERP backend

The current backend uses twelve consolidated AWS SAM Lambda functions and twelve externally managed DynamoDB tables. The former catalog of 73 individual handlers described an earlier layout and does not represent the current implementation.

Authentication uses one Cognito user pool with `super_admin`, `operations_admin` and `supervisor` groups. API handlers enforce active account status, organization isolation and current project assignments. The frontend derives its permissions from the authenticated account.

See [ACCESS_SETUP.md](ACCESS_SETUP.md) for deployment parameters, first-account bootstrap, migration, the role matrix, validation commands and remaining release work. See [template.yaml](template.yaml) for deployed routes and IAM policies.

## Tests

Install `requirements-dev.txt`, then run `python -m pytest tests -q` from this directory. These tests use simulated AWS services and do not create live users, send emails or modify live tables.

## API CORS

`KineticHttpApi.CorsConfiguration` in `template.yaml` owns CORS for all API routes, including `/subcontractors` and project subcontractors. API Gateway answers browser preflight requests automatically; Lambda handlers do not handle OPTIONS or return CORS headers. Normal API requests retain the Cognito JWT authorizer. Keep explicit OPTIONS integrations and authenticated `$default` routes out of this API so they do not intercept automatic preflight handling. See the [AWS HTTP API CORS documentation](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-cors.html).

Set the stack parameter `FrontendOrigin` to the browser origin (scheme, hostname and port, without a path or trailing slash). Its existing `*` default supports both local development and deployed frontends using bearer tokens, without cookie credentials. The S3 upload bucket has its own service-level CORS configuration because uploads go directly to S3.

Deploy the updated SAM stack to apply the API configuration and remove old OPTIONS integrations; updating Lambda code alone does not apply this fix. `sam local start-api` is not proof of deployed HTTP API CORS behavior. After deployment, verify preflight without an Authorization token, supplying `Origin`, `Access-Control-Request-Method`, and `Access-Control-Request-Headers: authorization,content-type`. Expect a successful response with the configured allow-origin, methods and headers, then verify the authenticated request from the browser.
