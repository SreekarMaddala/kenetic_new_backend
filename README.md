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
