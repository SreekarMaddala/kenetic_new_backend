# Cognito and access setup

Use **one Cognito user pool and one web app client** for all three roles. Each account belongs to exactly one group:

| Group | Purpose | Account management |
| --- | --- | --- |
| `super_admin` | Software provider; organization registry and platform accounts | Creates operations admins and supervisors in a selected organization |
| `operations_admin` | Customer organization administrator | Creates and disables supervisors in its own organization; assigns them to projects |
| `supervisor` | Assigned project field operations | No account administration or finance access |

The UI label “Operations Admin” means the customer admin. There is no `admin` group or client-selected role. Superadmin creation is an operator action, never a public registration endpoint. Existing membership in multiple application groups is rejected.

## Deploy and bootstrap

1. Review/deploy `cognito-template.yaml` to the intended account. If a pool already exists, update its owning stack instead of creating a second pool. Use the same pool ID and client ID in both the API parameters and frontend environment.
2. Ensure the twelve tables from `scripts/create_tables.py` exist. Deploy `template.yaml` with `CognitoUserPoolId`, `CognitoClientId`, and `FrontendOrigin` (the exact origin, e.g. `https://erp.example.com`). The SAM config's existing identifiers are not evidence of a tested deployment; pass the intended parameters explicitly. No deployment is performed by the code changes.
3. Bootstrap the first superadmin. This prints a plan and makes no AWS calls unless `--apply` is present:

```powershell
python scripts/bootstrap_account.py --pool-id YOUR_POOL_ID --email owner@example.com --name "Platform Owner" --org-id PLATFORM --org-name "Platform Operations"
```

After reviewing the pool, account and organization, repeat with `--apply --send-invitation`. This sends Cognito's temporary-password invitation. No permanent password is embedded in source code or printed. To bootstrap an operations admin, use the same pool ID with `--role operations_admin` and the customer's organization details. Ordinarily the superadmin creates this account through the UI instead.

4. Sign in with the temporary password and set a permanent password. Superadmins land on Organizations. Register the customer organization, then create an Operations Admin under User Accounts and click **Send invitation**.
5. The operations admin signs in, creates supervisor accounts, sends their invitations, creates a project, and selects supervisors under **Site Team & Attendance**. Supervisors see only assigned projects.

Existing users must have `custom:org_id`, exactly one group, and an active `USER#{sub}` profile in `ORG#{orgId}`. The bootstrap tool can add a missing profile for an existing account only when its immutable organization and group already match. It refuses to silently elevate or move an account. Users created without the immutable organization attribute require a reviewed reprovisioning/migration process.

## Enforcement and storage

- API Gateway validates the token; each handler also checks the current account profile, role and organization status. Disabled accounts and suspended organizations are rejected even if their JWT has not expired.
- Account organization and role cannot be changed through profile updates. Operations admins cannot manage peer admins or platform accounts.
- Project headers live in `ORG#{orgId}` / `PROJECT#{projectId}`. Child records live in `ORG#{orgId}#PROJECT#{projectId}`. Handlers verify the project header and current supervisor assignments before accessing children.
- If legacy child records exist under `PROJECT#{projectId}`, freeze legacy writes, run `python scripts/migrate_project_partitions.py` in dry-run mode, resolve conflicts, then repeat with `--apply` and reconcile counts before cutover. The tool copies records and leaves every source record intact. It refuses records without a matching organization-owned project header. Legacy project headers stored in the wrong partition must be reconciled separately.
- The frontend validates `/auth/me` on sign-in/session restoration and clears the query cache when accounts change. Cognito claims determine UI permissions; backend checks remain authoritative.
- Invitation creation is separated from delivery. If delivery fails, the account remains available for an explicit resend; creating a duplicate account is unnecessary. Confirmed users use password recovery instead of resending invitations.
- Attendance uses the authenticated user and server UTC date/time, with one check-in/check-out per project per UTC day. This flow does not certify GPS location or support overnight shifts crossing the UTC day boundary.

## Validation

```powershell
# backend
python -m pytest tests -q
sam validate --lint --template-file template.yaml
sam validate --lint --template-file cognito-template.yaml
# frontend
npm test
npm run typecheck
npm run build
```

Tests simulate DynamoDB/Cognito responses; they do not prove live email delivery or a live AWS configuration. Stage and verify all three roles in the intended AWS account before cutover.

Repository-wide ESLint currently has a substantial formatting and `any`-type backlog in the original screens. The new authentication/account screens pass scoped lint (the shared auth context retains the existing Fast Refresh export warning). Full lint cleanup is still required for a clean repository-wide quality gate.

## Remaining ERP release work

The access-control implementation does not make every existing ERP screen production complete. Several original modules still contain local/demo workflows, including labour, logistics, materials, payroll and warehouse screens. Production release also needs domain-specific financial approval rules, audit-event persistence, file upload/storage integration, operational alarms, load testing and backup/restore verification. Analytics currently paginate internal queries/scans; large tenants need indexed aggregation and client pagination. MFA is not enabled by this template. Establish and implement the required MFA policy before deploying privileged accounts to production.
