# Kinetic ERP v2 migration blueprint

## Safety rules

This is an additive migration.  The existing SAM stack remains deployed until
the v2 stack has passed staging verification, data reconciliation, and an
approved cutover.  Do not rename or delete existing DynamoDB tables in place.

## Deployment target

The v2 stack deploys twelve Lambda functions and twelve DynamoDB tables.
Routes remain REST-shaped; the deployment unit is consolidated by business
domain, not by individual CRUD operation.

| Lambda | Routes/features |
|---|---|
| auth | authenticated session status; Cognito handles password challenges |
| platform-admin | organizations, users, metrics |
| projects | projects and milestones |
| project-commercial | BOQ and subcontractors |
| workforce | supervisor and labour attendance |
| field-operations | DPR, material site actions, logistics |
| site-control | issues, inspections, equipment |
| document-control | drawings and document metadata |
| supply-chain | vendors, inventory, warehouse transactions |
| finance | bills, expenses, payments |
| governance | payroll, analytics, reports, settings |
| maintenance-jobs | scheduled reconciliation and retention jobs |

## Data target

| Table | Partition model |
|---|---|
| organizations | `ORG#{orgId}` |
| users | `ORG#{orgId}` / `USER#{userId}` |
| projects | `ORG#{orgId}` / `PROJECT#{projectId}` and `ORG#{orgId}#PROJECT#{projectId}` child items |
| parties | `ORG#{orgId}` / vendor or subcontractor item |
| field-operations | `ORG#{orgId}#PROJECT#{projectId}` / dated attendance and DPR items |
| site-control | `ORG#{orgId}#PROJECT#{projectId}` / issue, inspection, equipment items |
| materials-logistics | `ORG#{orgId}#PROJECT#{projectId}` / material or trip item |
| document-control | `ORG#{orgId}#PROJECT#{projectId}` / drawing or document item |
| inventory | `ORG#{orgId}` / stock and warehouse-event item |
| finance | `ORG#{orgId}` or `ORG#{orgId}#PROJECT#{projectId}` / financial item |
| settings | `ORG#{orgId}` / setting item |
| audit-events | `ORG#{orgId}` / timestamped immutable event |

Every table includes `orgId`.  APIs derive it from `custom:org_id` in the JWT;
clients never choose tenant scope.  Indexes are added only for demonstrated
queries (approval queues, project timelines, user attendance, and payments).

## Cutover checkpoints

1. Create Cognito groups and assign every existing user an `org_id` claim.
2. Deploy v2 resources under a new stack name and run automated authorization tests.
3. Backfill one table/domain at a time with idempotent migration jobs.
4. Reconcile counts and financial totals, then switch API routes by domain.
5. Keep legacy tables read-only for the rollback window; delete only after sign-off.

See [ACCESS_SETUP.md](ACCESS_SETUP.md) for the implemented role model, bootstrap and additive child-partition migration.
