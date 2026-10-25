"""Subcontractor payments and group wages share the posted spending ledger."""
from common import model, storage, reconciliation
from common.authz import Identity
from common.dynamo import get_table
from handlers import consolidated as api
from test_model import app_database, data, record_attendance

PATH = '/supervisor/labour/attendance'
MONTH = '2026-10'


def project(db):
    return data(db[0](api.projects_handler, '/projects', 'POST', {'name': 'Site', 'budget': 100000}))['projectId']


def worker(db, pid, name, gang=None, advance=0, daily_paid=False):
    call = db[0]
    body = {'name': name, 'rate': 800}
    if gang:
        body['gangId'] = gang['gangId']
    saved = data(call(api.workforce_handler, PATH, 'POST', body))
    day = {'workerId': saved['workerId'], 'projectId': pid, 'date': '2026-10-04'}
    data(call(api.workforce_handler, PATH, 'POST', dict(day, operation='allocate')))
    data(record_attendance(db, dict(day, operation='attendance', status='Present', paymentStatus='Paid' if daily_paid else 'Not paid')))
    if advance:
        data(call(api.workforce_handler, PATH, 'POST', dict(day, operation='advance', amount=advance,
                  description='Cash advance', requestId='advance-' + saved['workerId'])))
    return saved


def test_group_settlement_deducts_advances_once_and_preserves_unpaid_groups(app_database):
    call, org, _, _ = app_database
    pid = project(app_database)
    gang = data(call(api.workforce_handler, PATH, 'POST', {'operation': 'gang_create', 'name': 'Masonry'}))
    first = worker(app_database, pid, 'Ravi', gang, advance=200)
    second = worker(app_database, pid, 'Sam', advance=100)
    worker(app_database, pid, 'Daily paid', daily_paid=True)
    endpoint = f'/projects/{pid}/payroll'
    cycle = data(call(api.governance_handler, endpoint, 'POST', {'month': MONTH}))
    member = next(member for member in cycle['staff'] if member['labourId'] == first['workerId'])
    assert (member['gross'], member['advanceRecovery'], member['net']) == (800, 200, 600)
    assert cycle['amount'] == 1300
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 1100
    payload = {'month': MONTH, 'reference': 'gang-transfer', 'expectedCreatedAt': cycle['createdAt'], 'settlementGroup': gang['gangId']}
    partial = data(call(api.governance_handler, endpoint + '/disburse', 'POST', payload))
    assert partial['status'] == 'Partially Paid' and partial['paidAmount'] == 600
    assert data(call(api.governance_handler, endpoint + '/disburse', 'POST', payload))['paidAmount'] == 600
    assert call(api.governance_handler, endpoint + '/disburse', 'POST', dict(payload, reference='different'))['statusCode'] == 400
    assert call(api.governance_handler, endpoint + '/' + MONTH, 'DELETE')['statusCode'] == 400
    assert call(api.governance_handler, endpoint + '/disburse', 'POST', dict(payload, settlementGroup='forged'))['statusCode'] == 400
    query = {'projectId': pid, 'month': MONTH}
    profile = data(call(api.workforce_handler, PATH + '/' + first['workerId'], query=query))
    assert profile['summary']['outstanding'] == 0 and profile['summary']['payrollPaid'] == 600
    assert profile['recordedAdvances'] == 0 and profile['advances'] == []
    unpaid = data(call(api.workforce_handler, PATH + '/' + second['workerId'], query=query))
    assert unpaid['summary']['outstanding'] == 700 and unpaid['recordedAdvances'] == 100
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 1700
    settled = data(call(api.governance_handler, endpoint + '/disburse', 'POST', dict(payload, reference='remaining-transfer', settlementGroup='all')))
    assert settled['status'] == 'Paid' and settled['paidAmount'] == 1300
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 2400
    ledger = data(call(api.projects_handler, f'/projects/{pid}/spending'))
    assert sum(row['amount'] for row in ledger) == 2400
    assert sum(row['amount'] for row in ledger if row['source'] == 'Worker Advance') == 300
    assert sum(row['amount'] for row in ledger if row['source'] == 'Payroll Settlement') == 1300
    identity = Identity(app_database[2], org, frozenset({'operations_admin'}))
    assert reconciliation.reconcile(identity, org, pid)['changes'] == []


def test_salaried_staff_can_settle_separately_from_zero_net_worker_group(app_database):
    call, _, employee_id, _ = app_database
    pid = project(app_database)
    saved = worker(app_database, pid, 'Worker', advance=800)
    endpoint = f'/projects/{pid}/payroll'
    data(call(api.governance_handler, endpoint + '/staff', 'POST', {'employeeId': employee_id, 'basic': 1000}))
    cycle = data(call(api.governance_handler, endpoint, 'POST', {'month': MONTH}))
    payload = {'month': MONTH, 'reference': 'staff-transfer', 'settlementGroup': 'staff', 'expectedCreatedAt': cycle['createdAt']}
    partial = data(call(api.governance_handler, endpoint + '/disburse', 'POST', payload))
    assert partial['status'] == 'Partially Paid' and partial['paidAmount'] == 1000
    history = data(call(api.governance_handler, endpoint + '/me'))['history']
    assert history[0]['status'] == 'Paid' and history[0]['net'] == 1000 and history[0]['paidAt']
    final = data(call(api.governance_handler, endpoint + '/disburse', 'POST', dict(payload, reference='advance-fully-covers-wages', settlementGroup='ungrouped')))
    assert final['status'] == 'Paid' and final['paidAmount'] == 1000
    assert final['paidGroups']['ungrouped']['amount'] == 0
    profile = data(call(api.workforce_handler, PATH + '/' + saved['workerId'], query={'projectId': pid, 'month': MONTH}))
    assert profile['recordedAdvances'] == 0 and profile['summary']['outstanding'] == 0
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 1800


def test_advances_cannot_produce_negative_payroll(app_database):
    call = app_database[0]
    pid = project(app_database)
    worker(app_database, pid, 'Worker', advance=900)
    endpoint = f'/projects/{pid}/payroll'
    response = call(api.governance_handler, endpoint, 'POST', {'month': MONTH})
    assert response['statusCode'] == 400 and 'Deductions exceed wages' in response['body']
    assert data(call(api.governance_handler, endpoint, query={'month': MONTH})) == []


def test_advance_corrections_require_reopening_unpaid_payroll(app_database):
    call = app_database[0]
    pid = project(app_database)
    saved = worker(app_database, pid, 'Ravi', advance=200)
    endpoint = f'/projects/{pid}/payroll'
    cycle = data(call(api.governance_handler, endpoint, 'POST', {'month': MONTH}))
    body = {'workerId': saved['workerId'], 'projectId': pid, 'date': '2026-10-04',
            'operation': 'advance', 'amount': 100, 'description': 'Cash', 'requestId': 'advance-after-snapshot'}
    assert call(api.workforce_handler, PATH, 'POST', body)['statusCode'] == 400
    profile = data(call(api.workforce_handler, PATH + '/' + saved['workerId'], query={'projectId': pid, 'month': MONTH}))
    entry = profile['advances'][0]
    deletion = {key: body[key] for key in ('workerId', 'projectId', 'date')}
    deletion.update(operation='advance_delete', advanceId=entry['advanceId'], expectedVersion=entry['version'], requestId='delete-locked-advance')
    assert call(api.workforce_handler, PATH, 'POST', deletion)['statusCode'] == 400
    data(call(api.governance_handler, endpoint + '/' + MONTH, 'DELETE'))
    data(call(api.workforce_handler, PATH, 'POST', deletion))
    regenerated = data(call(api.governance_handler, endpoint, 'POST', {'month': MONTH}))
    assert regenerated['amount'] == 800 and regenerated['cycleId'] != cycle['cycleId']
    assert call(api.governance_handler, endpoint + '/disburse', 'POST', {
        'month': MONTH, 'reference': 'stale', 'expectedCreatedAt': cycle['createdAt']})['statusCode'] == 400


def test_concurrent_group_payment_does_not_post_cash_or_overwrite_receipts(app_database, monkeypatch):
    call, org, _, _ = app_database
    pid = project(app_database)
    worker(app_database, pid, 'Ravi')
    endpoint = f'/projects/{pid}/payroll'
    cycle = data(call(api.governance_handler, endpoint, 'POST', {'month': MONTH}))
    original = storage.transact
    def race(table, operations):
        if any(op.get('Put', {}).get('Item', {}).get('entityType') == 'payroll-settlement' for op in operations):
            table.update_item(Key={'PK': f'ORG#{org}#PROJECT#{pid}', 'SK': 'PAYROLL#' + MONTH},
                              UpdateExpression='SET settlementRevision = :one', ExpressionAttributeValues={':one': 1})
        return original(table, operations)
    monkeypatch.setattr(storage, 'transact', race)
    response = call(api.governance_handler, endpoint + '/disburse', 'POST', {
        'month': MONTH, 'reference': 'concurrent', 'expectedCreatedAt': cycle['createdAt']})
    assert response['statusCode'] == 409
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 0
    assert data(call(api.projects_handler, f'/projects/{pid}/spending')) == []


def test_subcontractor_edit_payment_approval_and_reversal(app_database):
    call = app_database[0]
    pid = project(app_database)
    company = data(call(api.project_commercial_handler, '/subcontractors', 'POST', {
        'name': 'ABC Works', 'contactPerson': 'Sam', 'trade': 'Electrical'}))
    registry_path = '/subcontractors/' + company['subcontractorId']
    updated = data(call(api.project_commercial_handler, registry_path, 'PATCH', {
        'phone': '123456', 'expectedVersion': company['version']}))
    assert updated['phone'] == '123456'
    assert call(api.project_commercial_handler, registry_path, 'PATCH', {'phone': 'other', 'expectedVersion': company['version']})['statusCode'] == 409
    assigned = data(call(api.project_commercial_handler, f'/projects/{pid}/subcontractors', 'POST', {
        'registryId': company['subcontractorId'], 'scopeOfWork': 'Install wiring', 'contractValue': 5000}))
    path = f'/projects/{pid}/subcontractors/' + assigned['subcontractorId']
    edited = data(call(api.project_commercial_handler, path, 'PATCH', {'scopeOfWork': 'Wire all floors', 'contractValue': 7000, 'expectedVersion': assigned['version']}))
    assert edited['scopeOfWork'] == 'Wire all floors' and edited['contractValue'] == 7000
    endpoint = f'/projects/{pid}/payments'
    payload = {'contractorId': assigned['subcontractorId'], 'amount': 1000, 'mode': 'UPI',
               'date': '2026-10-05', 'reference': 'contractor-upi-001', 'requestId': 'contractor-payment-001'}
    paid = data(call(api.finance_handler, endpoint, 'POST', payload))
    assert paid['contractorName'] == 'ABC Works'
    assert data(call(api.finance_handler, endpoint, 'POST', payload))['paymentId'] == paid['paymentId']
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 0
    payment_path = endpoint + '/' + paid['paymentId']
    approved = data(call(api.finance_handler, payment_path, 'PATCH', {'status': 'Approved'}))
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 1000
    assert data(call(api.projects_handler, f'/projects/{pid}/spending'))[0]['source'] == 'Subcontractor Payment'
    assert call(api.finance_handler, payment_path, 'PATCH', {'amount': 2000})['statusCode'] == 400
    reversed_payment = data(call(api.finance_handler, payment_path + '/reverse', 'POST', {
        'reason': 'Refunded', 'reference': 'refund-receipt', 'expectedVersion': approved['version'], 'requestId': 'contractor-reversal-001'}))
    assert reversed_payment['status'] == 'Reversed'
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 0
    other_project = project(app_database)
    assert call(api.finance_handler, f'/projects/{other_project}/payments', 'POST', dict(payload, requestId='contractor-wrong-project'))['statusCode'] == 400
    assert call(api.finance_handler, endpoint, 'POST', dict(payload, vendorId=model.new_id('vendor'), requestId='mixed-payment-links'))['statusCode'] == 400
