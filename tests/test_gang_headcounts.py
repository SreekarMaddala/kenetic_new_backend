"""Headcounts, shared advances, and payroll must agree on the gang balance."""
import json
import pytest

from common import model, reconciliation, storage
from common.authz import Identity
from common.dynamo import get_table
from handlers import consolidated as api
from test_model import app_database, data, record_attendance

PATH = '/supervisor/labour/attendance'


def setup(db):
    call = db[0]
    pid = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Gang site', 'budget': 100000}))['projectId']
    gang = data(call(api.workforce_handler, PATH, 'POST', {'operation': 'gang_create', 'name': 'Masonry',
                'attendanceMode': 'headcount', 'size': 10, 'rate': 800, 'headcountFrom': '2026-10-01'}))
    return pid, gang['gangId']


def attend(db, pid, gid, day='2026-10-04', **values):
    data(db[0](api.workforce_handler, PATH, 'POST', {'operation': 'gang_allocate', 'projectId': pid,
              'gangId': gid, 'date': day, 'expectedVersion': 0}))
    return data(record_attendance(db, {'operation': 'gang_attendance', 'projectId': pid, 'gangId': gid,
                'date': day, 'present': 8, 'expectedVersion': 0, **values}))


def cash(db, pid, gid, amount, request='gang-cash-001', **values):
    return db[0](api.workforce_handler, PATH, 'POST', {'operation': 'gang_advance', 'projectId': pid,
                 'gangId': gid, 'date': '2026-10-04', 'amount': amount, 'description': 'Cash to gang leader',
                 'requestId': request, **values})


def details(db, gid):
    return data(db[0](api.workforce_handler, PATH, query={'gangRoster': 'true', 'gangId': gid}))[0]


def test_headcount_wages_correct_once_and_lock_after_payroll(app_database):
    pid, gid = setup(app_database)
    saved = attend(app_database, pid, gid, halfDay=1, nightCount=2)
    assert (saved['wage'], saved['absent'], saved['personDays']) == (8400, 1, 8.5)
    assert details(app_database, gid)['account']['earned'] == 8400
    correction = {'operation': 'gang_attendance', 'projectId': pid, 'gangId': gid, 'date': '2026-10-04',
                  'present': 7, 'halfDay': 1, 'nightCount': 2, 'expectedVersion': 1}
    assert record_attendance(app_database, correction)['statusCode'] == 400
    correction['correctionReason'] = 'One person left before work began'
    corrected = data(record_attendance(app_database, correction))
    assert corrected['wage'] == 7600 and details(app_database, gid)['account']['earned'] == 7600
    assert record_attendance(app_database, correction)['statusCode'] == 400
    endpoint = f'/projects/{pid}/payroll'
    cycle = data(app_database[0](api.governance_handler, endpoint, 'POST', {'month': '2026-10'}))
    assert len(cycle['staff']) == 1 and cycle['amount'] == 7600
    correction.update(present=6, expectedVersion=2)
    assert record_attendance(app_database, correction)['statusCode'] == 400
    assert app_database[0](api.workforce_handler, PATH, 'POST', dict(correction, expectedVersion=2))['statusCode'] == 403


@pytest.mark.parametrize('fields', [{'present': 11}, {'present': -1}, {'present': 1.5}, {'present': True}, {'present': 8, 'halfDay': 3}, {'present': 8, 'nightCount': 9}])
def test_invalid_counts_cannot_change_gang_balance(app_database, fields):
    pid, gid = setup(app_database)
    data(app_database[0](api.workforce_handler, PATH, 'POST', {'operation': 'gang_allocate', 'projectId': pid,
                       'gangId': gid, 'date': '2026-10-04', 'expectedVersion': 0}))
    response = record_attendance(app_database, {'operation': 'gang_attendance', 'projectId': pid, 'gangId': gid,
                'date': '2026-10-04', 'expectedVersion': 0, **fields})
    assert response['statusCode'] == 400
    assert details(app_database, gid)['account']['earned'] == 0


def test_excess_advance_carries_forward_and_settlement_is_not_duplicated(app_database):
    pid, gid = setup(app_database)
    attend(app_database, pid, gid)
    first = data(cash(app_database, pid, gid, 10000))
    assert data(cash(app_database, pid, gid, 10000))['cashId'] == first['cashId']
    assert cash(app_database, pid, gid, 11000)['statusCode'] == 400
    endpoint = f'/projects/{pid}/payroll'
    cycle = data(app_database[0](api.governance_handler, endpoint, 'POST', {'month': '2026-10'}))
    assert (cycle['amount'], cycle['staff'][0]['advanceRecovery'], cycle['staff'][0]['carryForward']) == (0, 6400, 3600)
    assert details(app_database, gid)['account']['reservedAdvances'] == 6400
    # Advances added after the snapshot remain available for the next payment.
    data(cash(app_database, pid, gid, 1000, 'gang-cash-after-payroll'))
    payload = {'month': '2026-10', 'reference': 'Advance covers wages', 'settlementGroup': gid, 'expectedCreatedAt': cycle['createdAt']}
    data(app_database[0](api.governance_handler, endpoint + '/disburse', 'POST', payload))
    data(app_database[0](api.governance_handler, endpoint + '/disburse', 'POST', payload))
    account = details(app_database, gid)['account']
    assert (account['advanceBalance'], account['reservedAdvances'], account['carryForward']) == (4600, 0, 4600)
    attend(app_database, pid, gid, day='2026-11-01')
    november = data(app_database[0](api.governance_handler, endpoint, 'POST', {'month': '2026-11'}))
    assert (november['amount'], november['staff'][0]['advanceRecovery']) == (1800, 4600)
    data(app_database[0](api.governance_handler, endpoint + '/disburse', 'POST', dict(payload, month='2026-11', reference='Nov transfer', expectedCreatedAt=november['createdAt'])))
    account = details(app_database, gid)['account']
    assert (account['advanceBalance'], account['balanceToPay'], account['paid']) == (0, 0, 1800)
    assert data(app_database[0](api.projects_handler, '/projects/' + pid))['spent'] == 12800
    identity = Identity(app_database[2], app_database[1], frozenset({'operations_admin'}))
    assert reconciliation.reconcile(identity, app_database[1], pid)['changes'] == []


def test_reopen_releases_reserved_advance_and_returns_reduce_balance(app_database):
    pid, gid = setup(app_database)
    attend(app_database, pid, gid)
    data(cash(app_database, pid, gid, 2000))
    endpoint = f'/projects/{pid}/payroll'
    data(app_database[0](api.governance_handler, endpoint, 'POST', {'month': '2026-10'}))
    assert cash(app_database, pid, gid, 500, 'return-reserved', operation='gang_advance_return')['statusCode'] == 400
    data(app_database[0](api.governance_handler, endpoint + '/2026-10', 'DELETE'))
    assert details(app_database, gid)['account']['reservedAdvances'] == 0
    data(cash(app_database, pid, gid, 500, 'return-unreserved', operation='gang_advance_return'))
    assert details(app_database, gid)['account']['advanceBalance'] == 1500
    assert data(app_database[0](api.projects_handler, '/projects/' + pid))['spent'] == 1500
    regenerated = data(app_database[0](api.governance_handler, endpoint, 'POST', {'month': '2026-10'}))
    assert regenerated['amount'] == 4900


def test_one_gang_advance_is_shared_across_projects_without_double_recovery(app_database):
    pid, gid = setup(app_database)
    attend(app_database, pid, gid)
    second = data(app_database[0](api.projects_handler, '/projects', 'POST', {'name': 'Second site', 'budget': 100000}))['projectId']
    attend(app_database, second, gid, day='2026-10-05')
    data(cash(app_database, pid, gid, 10000))
    first_cycle = data(app_database[0](api.governance_handler, f'/projects/{pid}/payroll', 'POST', {'month': '2026-10'}))
    second_cycle = data(app_database[0](api.governance_handler, f'/projects/{second}/payroll', 'POST', {'month': '2026-10'}))
    assert first_cycle['staff'][0]['advanceRecovery'] == 6400
    assert second_cycle['staff'][0]['advanceRecovery'] == 3600
    assert second_cycle['amount'] == 2800
    assert details(app_database, gid)['account']['reservedAdvances'] == 10000


def test_supervisor_scope_and_migration_preserve_named_history(app_database):
    pid, gid = setup(app_database)
    # A named member cannot bypass headcount attendance or record personal gang advances.
    worker = data(app_database[0](api.workforce_handler, PATH, 'POST', {'operation': 'register', 'gangId': gid, 'name': 'Old member', 'rate': 800}))
    assert app_database[0](api.workforce_handler, PATH, 'POST', {'operation': 'allocate', 'workerId': worker['workerId'], 'projectId': pid, 'date': '2026-10-04'})['statusCode'] == 400
    attend(app_database, pid, gid)
    assert record_attendance(app_database, {'operation': 'gang_advance', 'projectId': pid, 'gangId': gid,
           'date': '2026-10-04', 'amount': 100, 'description': 'Unauthorized', 'requestId': 'supervisor-cash'})['statusCode'] == 403
    legacy = data(app_database[0](api.workforce_handler, PATH, 'POST', {'operation': 'gang_create', 'name': 'Legacy'}))
    member = data(app_database[0](api.workforce_handler, PATH, 'POST', {'operation': 'register', 'gangId': legacy['gangId'], 'name': 'Ravi', 'rate': 800}))
    base = {'workerId': member['workerId'], 'projectId': pid, 'date': '2026-10-04'}
    data(app_database[0](api.workforce_handler, PATH, 'POST', dict(base, operation='allocate')))
    data(record_attendance(app_database, dict(base, operation='attendance', status='Present')))
    config = {'operation': 'gang_configure', 'gangId': legacy['gangId'], 'size': 5, 'rate': 800, 'headcountFrom': '2026-10-04', 'expectedVersion': 0}
    assert app_database[0](api.workforce_handler, PATH, 'POST', config)['statusCode'] == 400
    data(app_database[0](api.workforce_handler, PATH, 'POST', dict(config, headcountFrom='2026-10-05')))


def test_account_race_cannot_double_post_cash(app_database, monkeypatch):
    pid, gid = setup(app_database)
    data(cash(app_database, pid, gid, 2000))
    original = storage.transact
    def race(table, operations):
        if any(operation.get('Put', {}).get('Item', {}).get('entityType') == 'gang-cash' for operation in operations):
            table.update_item(Key={'PK': f'ORG#{app_database[1]}', 'SK': f'GANG-ACCOUNT#{gid}'},
                              UpdateExpression='ADD #v :one', ExpressionAttributeNames={'#v': 'version'}, ExpressionAttributeValues={':one': 1})
        return original(table, operations)
    monkeypatch.setattr(storage, 'transact', race)
    assert cash(app_database, pid, gid, 500, 'raced-gang-advance')['statusCode'] == 409
    assert details(app_database, gid)['account']['advancesGiven'] == 2000
    assert data(app_database[0](api.projects_handler, '/projects/' + pid))['spent'] == 2000
    assert len(details(app_database, gid)['history']) == 1


def test_supervisor_only_sees_assigned_project_and_cannot_access_gang_money(app_database):
    pid, gid = setup(app_database)
    attend(app_database, pid, gid)
    second = data(app_database[0](api.projects_handler, '/projects', 'POST', {'name': 'Other site', 'budget': 10000}))['projectId']
    subject = '22222222-2222-4222-8222-222222222222'
    context = {'http': {'method': 'GET'}, 'authorizer': {'jwt': {'claims': {'sub': subject, 'custom:org_id': app_database[1], 'cognito:groups': ['supervisor']}}}}
    query = {'gangRoster': 'true', 'projectId': pid, 'date': '2026-10-04'}
    event = {'rawPath': PATH, 'requestContext': context, 'queryStringParameters': query}
    result = data(api.workforce_handler(event, None))
    assert len(result) == 1 and result[0]['gangId'] == gid
    assert 'account' not in result[0] and 'history' not in result[0]
    assert api.workforce_handler(dict(event, queryStringParameters=dict(query, roster='true')), None)['statusCode'] == 403
    assert api.workforce_handler(dict(event, queryStringParameters=dict(query, projectId=second)), None)['statusCode'] == 403
    assert api.workforce_handler(dict(event, queryStringParameters={'gangRoster': 'true', 'gangId': gid}), None)['statusCode'] >= 400
    # A gang cannot move after attendance has been saved on that date.
    assert app_database[0](api.workforce_handler, PATH, 'POST', {'operation': 'gang_allocate', 'projectId': second,
        'gangId': gid, 'date': '2026-10-04', 'expectedVersion': 1})['statusCode'] == 400


def test_gang_rate_change_applies_only_to_new_allocations(app_database):
    pid, gid = setup(app_database)
    attend(app_database, pid, gid)
    data(app_database[0](api.workforce_handler, PATH, 'POST', {'operation': 'gang_configure', 'gangId': gid,
              'size': 12, 'rate': 1000, 'headcountFrom': '2026-10-01', 'expectedVersion': 0}))
    correction = data(record_attendance(app_database, {'operation': 'gang_attendance', 'projectId': pid,
        'gangId': gid, 'date': '2026-10-04', 'present': 7, 'expectedVersion': 1, 'correctionReason': 'Correct headcount'}))
    assert correction['rate'] == 800 and correction['size'] == 10 and correction['wage'] == 5600
    later = attend(app_database, pid, gid, day='2026-10-05', present=10)
    assert later['rate'] == 1000 and later['size'] == 12 and later['wage'] == 10000
    rows = data(app_database[0](api.workforce_handler, PATH, query={'projectId': pid, 'date': '2026-10-05', 'gangRoster': 'true', 'roster': 'true'}))
    assert rows[0]['monthSummary']['grossWages'] == 15600
