# Backend structure

The backend uses 11 direct HTTP handlers and 16 domain tables. Requests follow
`handlers/consolidated.py` → authorization and validation → domain workflows →
database writes and audit history.

- `handlers/`: request routing and response boundaries.
- `common/workflows.py`: small dispatcher for domain operations.
- `common/payroll.py`: salary setup, payroll generation and settlement.
- `common/finance.py`: payment approval, reversals and material references.
- `common/inventory.py`: catalog, receipts and stock transfers.
- `common/reporting.py`: date ranges, spending totals and report data.
- `common/workforce.py`: workers, allocations, attendance and deductions.
- `common/workflow_helpers.py` and `workflow_validation.py`: shared reads,
  values, validation and approval transitions.
- Other `common/` modules: access checks, records and database integrity.
- `tests/`: business rules, tenant isolation and request regression tests.

Payroll generation is `POST /projects/{projectId}/payroll` with a `month`.
It returns the completed cycle; repeating the same month returns the existing
cycle. Salary snapshots and settlement checks remain in place.

Subcontractors can be edited in the global registry and their scope, value and
dates can be edited on the project assignment. Project payments accept either a
vendor/material pair or an active assigned `contractorId`. Subcontractor
payments use the same approval, reversal, audit and spending rules as vendor
payments.

New payroll registers deduct the selected project/month's remaining worker
advances as well as existing deductions and daily wages already paid. Advances
post cash spending when recorded; payroll settlement posts only the remaining
wages. Repaid or deleted advances adjust that spending. Advances cannot be
changed after generation; reopen a completely unpaid cycle to correct them.
Payroll rejects deductions that exceed earned wages. Advances are scoped to
their recorded month and project; they do not automatically roll forward.

The payroll disbursement request accepts `settlementGroup`: a saved gang ID,
`ungrouped`, `staff`, or `all` (default). `all` pays only groups still unpaid.
Each settlement saves a receipt and updates the cycle atomically. Partially paid
cycles cannot be reopened. The monthly CSV includes groups, advance recovery,
daily payments, final wages and each group's payment status/reference. The
logistics page exports dated transport, mileage and fuel records for its
selected period.

Existing payroll snapshots keep their original figures. Reopen and regenerate
unpaid registers to include automatic advance recovery. Before relying on
spending summaries after upgrading a populated environment, preview and apply
`scripts/maintain_data.py reconcile-spending` for each affected project using
the existing account/actor checks; old advance records were not previously
included in derived spending. This change requires both backend and frontend
deployment, but no new tables or API Gateway routes.

Financial reports use `GET /reports/executive` with optional `startDate` and
`endDate`. The frontend creates the CSV download from that response.
There are no queue workers, scheduled dispatchers or job polling endpoints.
Audit records remain in the database.

The runtime uses one domain schema: prefixed IDs, separate workforce and payroll
tables, sparse indexes and immutable payroll entries. The table definitions in
`aws-setup/tables.json` remain the authoritative schema.

Resource names follow `kinetic-erp-<functionality>-<dev|prod>`. For example,
`kinetic-erp-payroll-dev` is a development table and
`kinetic-erp-payroll-reporting-settings-prod` is the production Lambda for those
routes. `common/resource_names.py` defines table names used by setup, bootstrap
and maintenance scripts. The backend template derives the same names from its
`ProjectName` and `Environment` parameters. Dev and prod use separate tables,
Lambdas, API stages, Cognito stacks and setup files.

Request-size regression tests cover 250 salary profiles (including inactive
accounts) and reports for 100 projects backed by 10,000 posted transactions.
They force small database pages to verify complete pagination, check batches
stay within 100 keys, and verify retries never return incomplete results.
Employee and overall project-summary reads are batched. Monthly workforce
records are grouped by worker before calculating payroll.

These checks use a mocked database. Their timings describe local verification,
not live service latency or a guaranteed maximum supported workload.

Run backend tests from this directory with `python -m pytest tests -q`.
Run frontend tests from `frontend/` with `npm test`; use `npm run typecheck`
and `npm run build` to check the application build.

For deployment, prepare the template before building:

```powershell
python scripts/deploy.py
```

The source template keeps explicit HTTP methods so API Gateway handles OPTIONS
preflight without JWT authentication. The prepared template preserves those
routes in the API definition and shares one Lambda permission per path across
methods. This avoids Lambda's 20 KB resource policy limit, including temporary
overlap with old permissions during updates. Generate it again after route
changes; do not build the source template directly for deployment.
The deploy config path is relative to the built template's directory, so
`../../samconfig.toml` loads the backend configuration even when the template
is explicitly specified.
