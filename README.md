# Kinetic ERP backend

The current backend uses twelve consolidated AWS SAM Lambda functions and twelve externally managed DynamoDB tables. The former catalog of 73 individual handlers described an earlier layout and does not represent the current implementation.

Authentication uses one Cognito user pool with `super_admin`, `operations_admin` and `supervisor` groups. API handlers enforce active account status, organization isolation and current project assignments. The frontend derives its permissions from the authenticated account.

See [ACCESS_SETUP.md](ACCESS_SETUP.md) for deployment parameters, first-account bootstrap, migration, the role matrix, validation commands and remaining release work. See [template.yaml](template.yaml) for deployed routes and IAM policies.

## Tests

Install `requirements-dev.txt`, then run `python -m pytest tests -q` from this directory. These tests use simulated AWS services and do not create live users, send emails or modify live tables.
