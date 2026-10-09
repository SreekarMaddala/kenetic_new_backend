from common import workflow_helpers
import json
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch
import pytest
from test_model import app_database, data
from common import audit, model, payroll_records, reconciliation, relationships, storage, workflows
from common.authz import Identity
from common.dynamo import get_table
from handlers import consolidated as api


def project(call):
    return data(call(api.projects_handler, '/projects', 'POST', {'name': 'Site', 'budget': 10000}))['projectId']


def expense(call, pid, request='expense-0001'):
    return data(call(api.finance_handler, f'/projects/{pid}/expenses', 'POST', {'description': 'Supplies', 'amount': 100, 'requestId': request}))


def test_assignment_names_use_batch_reads_for_bulk_supervisors(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = 0

        def batch_get_item(self, RequestItems):
            self.calls += 1
            assert RequestItems == {'USERS_TABLE': {'ConsistentRead': True, 'Keys': [
                {'PK': 'ORG#org-1', 'SK': 'USER#u-1'},
                {'PK': 'ORG#org-1', 'SK': 'USER#u-2'},
            ]}}
            return {'Responses': {'USERS_TABLE': [
                {'PK': 'ORG#org-1', 'SK': 'USER#u-1', 'name': 'Alice', 'role': 'supervisor', 'status': 'Active', 'orgId': 'org-1'},
                {'PK': 'ORG#org-1', 'SK': 'USER#u-2', 'name': 'Bob', 'role': 'supervisor', 'status': 'Active', 'orgId': 'org-1'},
            ]}}

    class FakeUsersTable:
        name = 'USERS_TABLE'
        def __init__(self):
            self.meta = type('Meta', (), {'client': FakeClient()})()

        def get_item(self, *args, **kwargs):
            raise AssertionError('Bulk assignment lookups must not loop with single-key reads')

    table = FakeUsersTable()
    monkeypatch.setattr(workflow_helpers, 'get_table', lambda name: table if name == 'USERS_TABLE' else None)
    assert workflow_helpers.assignment_names(['u-1', 'u-2'], 'org-1') == 'Alice, Bob'
    assert table.meta.client.calls == 1


@pytest.mark.parametrize('changes', [{'unexpected': 1}, {'amount': True}, {'amount': 1.001}, {'date': '2026-02-30'}, {'status': 'Paid'}, {'description': ''}])
def test_contract_rejects_invalid_writes_without_side_effects(app_database, changes):
    call, org, *_ = app_database
    pid = project(call)
    before = get_table('AUDIT_EVENTS_TABLE').scan()['Count']
    body = {'description': 'Supplies', 'amount': 100, 'requestId': 'invalid-0001', **changes}
    assert call(api.finance_handler, f'/projects/{pid}/expenses', 'POST', body)['statusCode'] == 400
    assert get_table('FINANCE_TABLE').scan()['Count'] == 0
    assert get_table('AUDIT_EVENTS_TABLE').scan()['Count'] == before


def test_archived_project_reference_and_race_are_rejected(app_database, monkeypatch):
    call, org, *_ = app_database
    pid = project(call)
    source = expense(call, pid)
    original = relationships.constraints
    def raced(*args, **kwargs):
        result = original(*args, **kwargs)
        get_table('PROJECTS_TABLE').update_item(Key={'PK': 'ORG#' + org, 'SK': 'PROJECT#' + pid},
                                               UpdateExpression='SET #s = :s', ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':s': 'Archived'})
        return result
    monkeypatch.setattr(relationships, 'constraints', raced)
    assert call(api.finance_handler, f"/expenses/{source['expenseId']}", 'PATCH', {'status': 'Approved'})['statusCode'] == 409
    assert data(call(api.finance_handler, f"/expenses/{source['expenseId']}"))['status'] == 'Pending'
    assert 'Item' not in get_table('REPORTING_TABLE').get_item(Key={'PK': 'ORG#' + org, 'SK': 'SPENDING#' + pid})


def test_invoice_business_uniqueness_and_server_payable(app_database):
    call, org, *_ = app_database
    pid = project(call)
    path = f'/projects/{pid}/bills'
    body = {'billNumber': 'INV-001', 'clientOrContractor': 'Vendor', 'grossAmount': 1000, 'tdsDeduction': 100, 'retentionDeduction': 100, 'requestId': 'invoice-0001'}
    bill = data(call(api.finance_handler, path, 'POST', body))
    assert bill['netPayable'] == 800
    assert data(call(api.finance_handler, path, 'POST', body))['billId'] == bill['billId']
    assert call(api.finance_handler, path, 'POST', dict(body, billNumber=' inv-001 ', requestId='invoice-0002'))['statusCode'] == 409
    assert call(api.finance_handler, path, 'POST', dict(body, billNumber='INV-002', netPayable=900, requestId='invoice-0003'))['statusCode'] == 400


def test_payment_uniqueness_linked_bill_cap_and_reversal(app_database):
    call, org, *_ = app_database
    pid = project(call)
    item = data(call(api.supply_chain_handler, '/inventory', 'POST', {'name': 'Cement', 'unit': 'Bags'}))
    vendor = data(call(api.supply_chain_handler, '/vendors', 'POST', {'name': 'Supplier', 'materialIds': [item['itemId']]}))
    bill = data(call(api.finance_handler, f'/projects/{pid}/bills', 'POST', {'billNumber': 'INV-001', 'clientOrContractor': 'Supplier', 'vendorId': vendor['vendorId'], 'grossAmount': 150, 'requestId': 'invoice-0001'}))
    data(call(api.finance_handler, f"/projects/{pid}/bills/{bill['billId']}", 'PATCH', {'status': 'Approved'}))
    path = f'/projects/{pid}/payments'
    body = {'vendorId': vendor['vendorId'], 'materialId': item['itemId'], 'billId': bill['billId'], 'amount': 100, 'mode': 'NEFT', 'reference': 'BANK-001', 'requestId': 'payment-0001'}
    first = data(call(api.finance_handler, path, 'POST', body))
    assert call(api.finance_handler, path, 'POST', dict(body, requestId='payment-0002'))['statusCode'] == 409
    second = data(call(api.finance_handler, path, 'POST', dict(body, reference='BANK-002', requestId='payment-0003')))
    approved = data(call(api.finance_handler, path + '/' + first['paymentId'], 'PATCH', {'status': 'Approved'}))
    assert call(api.finance_handler, path + '/' + second['paymentId'], 'PATCH', {'status': 'Approved'})['statusCode'] == 409
    reverse = {'reason': 'Refunded', 'reference': 'REFUND-001', 'requestId': 'reverse-0001', 'expectedVersion': approved['version']}
    result = data(call(api.finance_handler, path + '/' + first['paymentId'] + '/reverse', 'POST', reverse))
    assert result['status'] == 'Reversed'
    data(call(api.finance_handler, path + '/' + first['paymentId'] + '/reverse', 'POST', reverse))
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 0
    assert data(call(api.finance_handler, f"/projects/{pid}/bills/{bill['billId']}"))['paidAmount'] == 0
    assert call(api.finance_handler, path + '/' + first['paymentId'], 'PATCH', {'amount': 50})['statusCode'] == 400


def test_missing_and_foreign_tenant_references(app_database):
    call, org, *_ = app_database
    pid = project(call)
    item = model.new_id('inventory-item')
    foreign = model.new_id('organization')
    get_table('INVENTORY_TABLE').put_item(Item={'PK': 'ORG#' + org, 'SK': 'INVENTORY-ITEM#' + item, 'itemId': item, 'orgId': foreign, 'name': 'Bad', 'unit': 'Bags'})
    assert call(api.supply_chain_handler, '/vendors', 'POST', {'name': 'Supplier', 'materialIds': [item]})['statusCode'] == 400
    assert call(api.site_control_handler, f'/projects/{pid}/issues', 'POST', {'title': 'Issue', 'description': 'Details', 'supervisorId': model.new_id('user')})['statusCode'] == 400


def test_collection_pages_are_complete_and_cursor_is_scoped(app_database):
    call, org, *_ = app_database
    table = get_table('PROJECTS_TABLE')
    for index in range(115):
        pid = model.new_id('project', str(index))
        table.put_item(Item={'PK': 'ORG#' + org, 'SK': 'PROJECT#' + pid, 'projectId': pid, 'orgId': org, 'entityType': 'project', 'name': str(index), 'budget': 100, 'supervisorIds': []})
    seen, cursor = [], None
    while True:
        query = {'limit': '30'}
        if cursor:
            query['cursor'] = cursor
        response = call(api.projects_handler, '/projects', query=query)
        seen.extend(data(response))
        cursor = json.loads(response['body'])['pagination']['nextCursor']
        if not cursor:
            break
        assert call(api.projects_handler, '/projects', query={'cursor': cursor, 'orgId': model.new_id('organization')})['statusCode'] in {400, 403}
    assert len(seen) == 115 and len({row['projectId'] for row in seen}) == 115
    assert call(api.projects_handler, '/projects', query={'limit': '101'})['statusCode'] == 400
    assert call(api.projects_handler, '/projects', query={'cursor': 'bad'})['statusCode'] == 400


def test_summary_repair_detects_drift_and_lease_blocks_posting(app_database):
    call, org, emp, _ = app_database
    pid = project(call)
    saved = expense(call, pid)
    data(call(api.finance_handler, f"/expenses/{saved['expenseId']}", 'PATCH', {'status': 'Approved'}))
    reporting = get_table('REPORTING_TABLE')
    key = {'PK': 'ORG#' + org, 'SK': 'SPENDING#' + pid}
    reporting.update_item(Key=key, UpdateExpression='SET amount = :amount', ExpressionAttributeValues={':amount': 999})
    identity = Identity(emp, org, frozenset({'operations_admin'}))
    preview = reconciliation.reconcile(identity, org, pid)
    assert preview['changes'][0]['after'] == 100
    assert reporting.get_item(Key=key)['Item']['amount'] == 999
    reconciliation.reconcile(identity, org, pid, True)
    assert reporting.get_item(Key=key)['Item']['amount'] == 100
    assert reconciliation.reconcile(identity, org, pid)['changes'] == []
    pending = expense(call, pid, 'expense-0002')
    reporting.update_item(Key={'PK': f'ORG#{org}#PROJECT#{pid}', 'SK': 'CONTROL'}, UpdateExpression='SET leaseUntil = :until', ExpressionAttributeValues={':until': int(time.time()) + 600})
    assert call(api.finance_handler, f"/expenses/{pending['expenseId']}", 'PATCH', {'status': 'Approved'})['statusCode'] == 409
    assert reporting.get_item(Key=key)['Item']['amount'] == 100


def test_payroll_staging_cleanup_and_publication_race(app_database):
    call, org, emp, _ = app_database
    pid = project(call)
    table = get_table('PAYROLL_TABLE')
    cycle = {'PK': f'ORG#{org}#PROJECT#{pid}', 'SK': 'PAYROLL#2026-10', 'cycleId': model.new_id('payroll'), 'month': '2026-10', 'orgId': org, 'projectId': pid, 'createdAt': workflow_helpers.now(),
             'staff': [{'labourId': model.new_id('worker', str(index)), 'gross': Decimal(100), 'deductions': Decimal(0), 'net': Decimal(100)} for index in range(125)]}
    build = payroll_records.save_entries(table, cycle)
    with pytest.raises(ValueError, match='active lease'):
        payroll_records.cleanup(table, cycle['PK'], cycle['cycleId'], True)
    key = {'PK': build['PK'], 'SK': build['SK']}
    table.update_item(Key=key, UpdateExpression='SET leaseUntil = :until', ExpressionAttributeValues={':until': 0})
    assert payroll_records.cleanup(table, cycle['PK'], cycle['cycleId'])['entries'] == 125
    assert payroll_records.cleanup(table, cycle['PK'], cycle['cycleId'], True)['deleted'] == 125
    assert payroll_records.cleanup(table, cycle['PK'], cycle['cycleId'], True)['deleted'] == 0
    with pytest.raises(Exception):
        table.meta.client.transact_write_items(TransactItems=[payroll_records.publish_operation(table, build)])


def test_concurrent_expense_approval_never_double_counts(app_database):
    call, org, emp, _ = app_database
    pid = project(call)
    saved = expense(call, pid)
    table = get_table('FINANCE_TABLE')
    source = table.get_item(Key={'PK': f'ORG#{org}#PROJECT#{pid}', 'SK': 'EXPENSE#' + saved['expenseId']})['Item']
    identity = Identity(emp, org, frozenset({'operations_admin'}))
    updated = dict(source, status='Approved', version=2)
    def approve(_):
        try:
            audit.put(table, identity, dict(updated), before=source, ConditionExpression='#v = :v', ExpressionAttributeNames={'#v': 'version'}, ExpressionAttributeValues={':v': 1})
            return True
        except Exception:
            return False
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(approve, range(4)))
    assert sum(results) == 1
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 100


def test_stock_reconciliation_and_oversell_guard(app_database):
    call, org, emp, _ = app_database
    pid = project(call)
    receipt = data(call(api.supply_chain_handler, '/warehouse/grn', 'POST', {'item': 'Steel', 'unit': 'Kg', 'qty': 10, 'requestId': 'receipt-0001'}))
    item = receipt['itemId']
    assert data(call(api.supply_chain_handler, '/inventory'))[0]['itemId'] == item
    body = {'item': 'Steel', 'unit': 'Kg', 'qty': 8, 'targetProjectId': pid, 'requestId': 'issue-0001'}
    data(call(api.supply_chain_handler, '/warehouse/issue-vouchers', 'POST', body))
    assert call(api.supply_chain_handler, '/warehouse/issue-vouchers', 'POST', dict(body, requestId='issue-0002'))['statusCode'] == 409
    table = get_table('INVENTORY_TABLE')
    key = {'PK': 'ORG#' + org, 'SK': 'TOTAL#' + item}
    table.update_item(Key=key, UpdateExpression='SET quantity = :quantity', ExpressionAttributeValues={':quantity': 99})
    identity = Identity(emp, org, frozenset({'operations_admin'}))
    assert reconciliation.reconcile_stock(identity, org)['changes'][0]['after'] == 10
    reconciliation.reconcile_stock(identity, org, True)
    catalog = data(call(api.supply_chain_handler, '/inventory'))[0]
    assert (catalog['totalStock'], catalog['centralStock'], catalog['deployedStock']) == (10, 2, 8)
    assert reconciliation.reconcile_stock(identity, org)['changes'] == []


def test_worker_month_cache_correction_and_repair(app_database):
    call, org, emp, _ = app_database
    pid = project(call)
    path = '/supervisor/labour/attendance'
    worker = data(call(api.workforce_handler, path, 'POST', {'projectId': pid, 'name': 'Worker', 'rate': 800}))
    body = {'projectId': pid, 'workerId': worker['workerId'], 'date': '2026-10-04', 'status': 'Half Day', 'nightShift': True}
    data(call(api.workforce_handler, path, 'POST', dict(body, operation='allocate')))
    saved = data(call(api.workforce_handler, path, 'POST', dict(body, paymentStatus='Paid')))
    query = {'projectId': pid, 'date': '2026-10-04', 'roster': 'true'}
    roster = data(call(api.workforce_handler, path, query=query))[0]
    assert (roster['grossWages'], roster['dailyPaid'], roster['daysPresent'], roster['nightShifts']) == (800, 800, 0.5, 1)
    data(call(api.workforce_handler, path, 'POST', dict(body, status='Present', nightShift=False, paymentStatus='Not paid', expectedVersion=saved['version'], correctionReason='Corrected shift')))
    roster = data(call(api.workforce_handler, path, query=query))[0]
    assert (roster['grossWages'], roster['dailyPaid'], roster['daysPresent'], roster['nightShifts']) == (800, 0, 1, 0)
    key = {'PK': f'ORG#{org}#PROJECT#{pid}', 'SK': f"WORKER-MONTH#2026-10#{worker['workerId']}"}
    get_table('WORKFORCE_TABLE').update_item(Key=key, UpdateExpression='SET grossWages = :amount', ExpressionAttributeValues={':amount': 42})
    identity = Identity(emp, org, frozenset({'operations_admin'}))
    assert reconciliation.reconcile(identity, org, pid)['workerChanges']
    reconciliation.reconcile(identity, org, pid, True)
    assert data(call(api.workforce_handler, path, query=query))[0]['grossWages'] == 800


def test_existing_directory_profiles_can_be_backfilled(app_database):
    call, org, emp, _ = app_database
    table = get_table('USERS_TABLE')
    table.update_item(Key={'PK': 'ORG#' + org, 'SK': 'USER#' + emp}, UpdateExpression='SET entityType = :kind', ExpressionAttributeValues={':kind': 'user'})
    assert reconciliation.backfill_directory(org)['indexedRecords'] == 1
    assert 'DirectoryPK' not in table.get_item(Key={'PK': 'ORG#' + org, 'SK': 'USER#' + emp})['Item']
    reconciliation.backfill_directory(org, True)
    assert table.get_item(Key={'PK': 'ORG#' + org, 'SK': 'USER#' + emp})['Item']['DirectoryPK'] == 'DIRECTORY#user'
    assert reconciliation.backfill_directory(org)['indexedRecords'] == 0


def test_project_spending_cannot_be_edited_as_an_opening_balance(app_database):
    call, org, *_ = app_database
    pid = project(call)
    assert call(api.projects_handler, '/projects/' + pid, 'PATCH', {'spent': 100})['statusCode'] == 400


def test_retry_id_cannot_be_reused_in_a_second_project(app_database):
    call, org, *_ = app_database
    first, second = project(call), project(call)
    saved = expense(call, first)
    assert call(api.finance_handler, f'/projects/{second}/expenses', 'POST', {'description': 'Supplies', 'amount': 100, 'requestId': 'expense-0001'})['statusCode'] == 409
    assert data(call(api.finance_handler, '/expenses'))[0]['projectId'] == first


def test_dashboard_approval_preview_and_report_summary_repair(app_database):
    call, org, emp, _ = app_database
    pid = project(call)
    for index in range(30):
        expense(call, pid, f'expense-{index:04}')
    dashboard = data(call(api.governance_handler, '/dashboard/analytics'))
    assert dashboard['pendingApprovals'] == 30
    assert len(dashboard['approvals']) == 25 and dashboard['approvalsLimited']
    first = data(call(api.finance_handler, '/expenses'))[0]
    data(call(api.finance_handler, '/expenses/' + first['expenseId'], 'PATCH', {'status': 'Approved'}))
    assert data(call(api.governance_handler, '/reports/executive'))['totalExpenses'] == 100
    table = get_table('REPORTING_TABLE')
    table.update_item(Key={'PK': 'ORG#' + org, 'SK': 'SUMMARY#APPROVALS'}, UpdateExpression='SET amount = :amount', ExpressionAttributeValues={':amount': 999})
    finance = get_table('FINANCE_TABLE')
    second = data(call(api.finance_handler, '/expenses'))[1]
    finance.update_item(Key={'PK': f'ORG#{org}#PROJECT#{pid}', 'SK': 'EXPENSE#' + second['expenseId']}, UpdateExpression='REMOVE ApprovalPK, ApprovalSK')
    identity = Identity(emp, org, frozenset({'operations_admin'}))
    preview = reconciliation.reconcile_reports(identity, org)
    assert preview['approvalIndexChanges'] == 1
    assert preview['changes'][0]['after'] == 29
    reconciliation.reconcile_reports(identity, org, True)
    assert data(call(api.governance_handler, '/dashboard/analytics'))['pendingApprovals'] == 29
    assert reconciliation.reconcile_reports(identity, org)['changes'] == []


def test_supplied_financial_id_cannot_replace_another_projects_locator(app_database):
    call, org, *_ = app_database
    first, second = project(call), project(call)
    saved = expense(call, first)
    response = call(api.finance_handler, f'/projects/{second}/expenses', 'POST',
                    {'expenseId': saved['expenseId'], 'description': 'Other', 'amount': 50, 'requestId': 'expense-0002'})
    assert response['statusCode'] == 409
    assert data(call(api.finance_handler, '/expenses/' + saved['expenseId']))['projectId'] == first
