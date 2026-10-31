# Gang attendance and advances

Create a gang with its size, common daily rate per person, and headcount start date. Existing gangs can switch using `gang_configure`. The start date must follow any existing named-worker attendance; historical records and personal advances remain intact.

Admins assign a gang to one project for each work date. An assignment saves the agreed size and rate. Supervisors enter full-day workers, optional half-day workers, and optional night shifts. Night shifts are a subset of full-day workers and add one daily wage each. Attendance corrections require a reason. Saved attendance locks the assignment; generated payroll locks attendance for that project/month.

Gang earnings are calculated by the server:

`daily wages = rate × (full-day workers + half-day workers / 2 + night shifts)`

Gang advances use one organization-wide account shared across projects. Each cash entry identifies the project against which cash spending is posted. Given advances and cash returned are immutable entries with retry identifiers. Returns cannot exceed the unrecovered, unreserved balance. Historical personal advances are never silently transferred to the gang account.

Payroll includes one headcount entry per gang/project/month. It reserves at most that month's earned wages from the available gang advance, and pays the remaining wages. Excess advances carry forward. Each published payroll reserves its recovery in the same transaction as publishing, so different projects/months cannot recover the same money twice. Reopening an entirely unpaid cycle releases its reservations. Settlement records the final payment and recovery atomically; repeated settlements do not post twice. Advances given after a snapshot are considered by later payrolls, or by reopening and preparing the unpaid cycle again.

The gang account displays:

- Balance to pay: earned headcount wages − advances given + cash returned − final wage payments, with a minimum of zero.
- Advance still to recover: advances given − cash returned − advances recovered by settled payroll.
- Advance carried forward: cash paid to the gang above its earned wages, with a minimum of zero.

Supervisors can read assigned project attendance and record headcounts. Gang setup, cash records, organization-wide money balances, and payroll settlement are restricted to administrators. Conditional versions protect attendance, assignments, accounts, and payroll against concurrent edits.
