import json
import re
from pathlib import Path
import importlib.util

import boto3
import pytest
from moto import mock_aws
from common import model, workflows
from common.dynamo import get_table
from handlers import consolidated as api


def table_definitions():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('domain_setup', root / 'aws-setup/create_tables.py')
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    from common.resource_names import table_names
    mapping = {name: env for env, name in table_names().items()}
    return [(definition, mapping[definition['TableName']]) for definition in setup.definitions()]


@pytest.fixture
def app_database(monkeypatch):
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    with mock_aws():
        client = boto3.client('dynamodb')
        for definition, env in table_definitions():
            client.create_table(**definition)
            monkeypatch.setenv(env, definition['TableName'])
        org = model.new_id('organization')
        sub = '11111111-1111-4111-8111-111111111111'
        get_table('ORGANIZATIONS_TABLE').put_item(Item={'PK': f'ORG#{org}', 'SK': f'ORGANIZATION#{org}', 'orgId': org, 'status': 'Active'})
        emp = model.employee_id(sub)
        get_table('USERS_TABLE').put_item(Item={'PK': f'ORG#{org}', 'SK': f'USER#{emp}', 'orgId': org, 'employeeId': emp, 'cognitoSub': sub, 'role': 'operations_admin', 'status': 'Active', 'name': 'Admin'})
        def call(handler, path, method='GET', body=None, query=None):
            event = {'rawPath': path, 'body': json.dumps(body or {}), 'queryStringParameters': query,
                     'requestContext': {'http': {'method': method}, 'authorizer': {'jwt': {'claims': {'sub': sub, 'custom:org_id': org, 'cognito:groups': ['operations_admin']}}}}}
            return handler(event, None)
        yield call, org, emp, sub


def data(response):
    assert response['statusCode'] < 300, response
    return json.loads(response['body'])['data']


def test_prefixed_ids_identity_and_validation(app_database):
    call, org, emp, sub = app_database
    session = data(call(api.auth_handler, '/auth/me'))
    assert session['sub'] == sub and session['employeeId'] == emp
    project = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Site', 'budget': 10000, 'requestId': 'project-request-001'}))
    assert re.fullmatch('pro_[0-9a-f]{32}', project['projectId'])
    retry = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Site', 'budget': 10000, 'requestId': 'project-request-001'}))
    assert retry['projectId'] == project['projectId']
    assert call(api.projects_handler, '/projects', 'POST', {'name': 'Bad', 'projectId': 'bad/id'})['statusCode'] == 400


def test_workforce_payroll_and_summary_transactions(app_database):
    call, org, emp, sub = app_database
    project = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Site', 'budget': 10000}))
    pid = project['projectId']
    path = '/supervisor/labour/attendance'
    worker = data(call(api.workforce_handler, path, 'POST', {'projectId': pid, 'name': 'Worker', 'rate': 800}))
    assert worker['workerId'].startswith('wor_')
    body = {'projectId': pid, 'workerId': worker['workerId'], 'date': '2026-10-04', 'status': 'Half Day', 'nightShift': True}
    data(call(api.workforce_handler, path, 'POST', dict(body, operation='allocate')))
    paid = data(call(api.workforce_handler, path, 'POST', dict(body, paymentStatus='Paid')))
    assert paid['attendanceId'].startswith('att_') and paid['wage'] == 800
    data(call(api.workforce_handler, path, 'POST', dict(body, paymentStatus='Paid')))
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 800
    corrected = data(call(api.workforce_handler, path, 'POST', dict(body, paymentStatus='Not paid', expectedVersion=paid['version'], correctionReason='Payment not completed')))
    assert corrected['wage'] == 800
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 0
    cycle_path = '/projects/' + pid + '/payroll'
    cycle = data(call(api.governance_handler, cycle_path, 'POST', {'month': '2026-10'}))
    assert cycle['cycleId'].startswith('pyr_') and cycle['staff'][0]['gross'] == 800
    header = get_table('PAYROLL_TABLE').get_item(Key={'PK': f'ORG#{org}#PROJECT#{pid}', 'SK': 'PAYROLL#2026-10'})['Item']
    assert 'staff' not in header and header['entryCount'] == 1
    assert data(call(api.governance_handler, cycle_path))[0]['staff'] == cycle['staff']
    paid_cycle = data(call(api.governance_handler, cycle_path + '/disburse', 'POST', {'month': '2026-10', 'reference': 'BANK-001', 'expectedCreatedAt': cycle['createdAt']}))
    assert paid_cycle['staff'] == cycle['staff']
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 800
    data(call(api.governance_handler, cycle_path + '/disburse', 'POST', {'month': '2026-10', 'reference': 'BANK-001', 'expectedCreatedAt': cycle['createdAt']}))
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 800
    assert get_table('FIELD_OPERATIONS_TABLE').scan()['Count'] == 0
    assert get_table('SETTINGS_TABLE').scan()['Count'] == 0


def test_catalog_stock_and_finance_indexes(app_database):
    call, org, *_ = app_database
    catalog = data(call(api.supply_chain_handler, '/inventory', 'POST', {'name': 'Cement', 'unit': 'Bags'}))
    assert catalog['itemId'].startswith('itm_')
    receipt = data(call(api.supply_chain_handler, '/warehouse/grn', 'POST', {'item': 'Cement', 'unit': 'Bags', 'qty': 10, 'requestId': 'receipt-0001'}))
    assert receipt['warehouseEventId'].startswith('war_') and receipt['itemId'] == catalog['itemId']
    assert data(call(api.supply_chain_handler, '/inventory'))[0]['centralStock'] == 10
    project = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Site', 'budget': 10000}))
    pid = project['projectId']
    expense = data(call(api.finance_handler, '/projects/' + pid + '/expenses', 'POST', {'description': 'Supplies', 'amount': 100, 'requestId': 'expense-0001'}))
    assert expense['expenseId'].startswith('exp_')
    assert data(call(api.finance_handler, '/expenses'))[0]['expenseId'] == expense['expenseId']
    data(call(api.finance_handler, '/expenses/' + expense['expenseId'], 'PATCH', {'status': 'Approved'}))
    assert data(call(api.projects_handler, '/projects/' + pid))['spent'] == 100
    assert sum(x['amount'] for x in data(call(api.projects_handler, '/projects/' + pid + '/spending'))) == 100


def test_payroll_entry_tampering_detected(app_database):
    call, org, emp, _ = app_database
    pid = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Site', 'budget': 10000}))['projectId']
    path = '/projects/' + pid + '/payroll'
    data(call(api.governance_handler, path + '/staff', 'POST', {'employeeId': emp, 'basic': 1000}))
    cycle = data(call(api.governance_handler, path, 'POST', {'month': '2026-10'}))
    table = get_table('PAYROLL_TABLE')
    key = {'PK': f'ORG#{org}#PROJECT#{pid}', 'SK': f"PAYROLL-ENTRY#{cycle['cycleId']}#{emp}"}
    table.update_item(Key=key, UpdateExpression='SET #member.gross = :amount', ExpressionAttributeNames={'#member': 'member'}, ExpressionAttributeValues={':amount': 2})
    response = call(api.governance_handler, path)
    assert response['statusCode'] == 400
    assert call(api.governance_handler, path + '/disburse', 'POST', {'month': '2026-10', 'reference': 'BANK-001', 'expectedCreatedAt': cycle['createdAt']})['statusCode'] == 400
    header = table.get_item(Key={'PK': f'ORG#{org}#PROJECT#{pid}', 'SK': 'PAYROLL#2026-10'})['Item']
    assert header['status'] == 'Pending'


def test_summary_failure_rolls_back_approval(app_database, monkeypatch):
    call, org, *_ = app_database
    pid = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Site', 'budget': 10000}))['projectId']
    path = '/projects/' + pid + '/expenses'
    expense = data(call(api.finance_handler, path, 'POST', {'description': 'Supplies', 'amount': 100, 'requestId': 'expense-0001'}))
    monkeypatch.setenv('REPORTING_TABLE', 'missing-reporting-table')
    assert call(api.finance_handler, path + '/' + expense['expenseId'], 'PATCH', {'status': 'Approved'})['statusCode'] == 502
    assert data(call(api.finance_handler, path + '/' + expense['expenseId']))['status'] == 'Pending'


def test_month_filter_and_personal_salary_read(app_database):
    call, org, emp, _ = app_database
    pid = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Site', 'budget': 10000}))['projectId']
    path = '/projects/' + pid + '/payroll'
    data(call(api.governance_handler, path + '/staff', 'POST', {'employeeId': emp, 'basic': 1000}))
    for month in ('2026-09', '2026-10'):
        data(call(api.governance_handler, path, 'POST', {'month': month}))
    assert [cycle['month'] for cycle in data(call(api.governance_handler, path, query={'month': '2026-10'}))] == ['2026-10']
    assert data(call(api.governance_handler, path, query={'month': '2026-08'})) == []
    salary = data(call(api.governance_handler, path + '/me'))
    assert len(salary['history']) == 2 and salary['history'][0]['gross'] == 1000


def test_account_creation_uses_employee_id_and_keeps_cognito_subject(app_database, monkeypatch):
    call, org, *_ = app_database
    from unittest.mock import MagicMock
    cognito = MagicMock()
    cognito.admin_create_user.return_value = {'User': {'Username': 'site@example.com', 'Attributes': [{'Name': 'sub', 'Value': '22222222-2222-4222-8222-222222222222'}]}}
    monkeypatch.setenv('COGNITO_USER_POOL_ID', 'us-east-1_test')
    monkeypatch.setattr('common.accounts.boto3.client', lambda service: cognito)
    account = data(call(api.platform_admin_handler, '/employees', 'POST', {'name': 'Supervisor', 'email': 'site@example.com', 'role': 'supervisor'}))
    assert account['employeeId'].startswith('emp_')
    profile = get_table('USERS_TABLE').get_item(Key={'PK': f'ORG#{org}', 'SK': f"USER#{account['employeeId']}"})['Item']
    assert model.employee_id(profile['cognitoSub']) == account['employeeId']
    assert profile['role'] == 'supervisor'
