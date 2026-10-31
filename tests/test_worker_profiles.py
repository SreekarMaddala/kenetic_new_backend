import json
from common.dynamo import get_table
from handlers import consolidated as api
from test_model import app_database, data, record_attendance

PATH = '/supervisor/labour/attendance'


def registered(db):
    call = db[0]
    pid = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Site', 'budget': 10000}))['projectId']
    worker = data(call(api.workforce_handler, PATH, 'POST', {'name': 'Worker', 'rate': 800, 'bankName': 'Private bank', 'accNo': 'Private account'}))
    return call, pid, worker['workerId']


def test_copy_allocations_remain_daily_without_creating_wages(app_database):
    call, pid, wid = registered(app_database)
    body = {'workerId': wid, 'projectId': pid, 'date': '2026-10-03', 'operation': 'allocate', 'expectedVersion': 0}
    data(call(api.workforce_handler, PATH, 'POST', body))
    data(record_attendance(app_database, {**body, 'operation': 'attendance', 'status': 'Present', 'paymentStatus': 'Paid'}))
    next_day = {'projectId': pid, 'date': '2026-10-04', 'roster': 'true'}
    before = data(call(api.workforce_handler, PATH, query=next_day))[0]
    assert not before['allocationConfirmed'] and not before['attendanceRecorded']
    assert before['allocationVersion'] == 0
    data(call(api.workforce_handler, PATH, 'POST', {**body, 'date': next_day['date']}))
    after = data(call(api.workforce_handler, PATH, query=next_day))[0]
    assert after['allocationConfirmed'] and after['allocationVersion'] == 1
    assert not after['attendanceRecorded'] and after['dailyWage'] == 0
    assert after['paymentStatus'] == 'Not paid'
    assert 'bankName' not in after and 'accNo' not in after


def test_stale_allocation_preview_and_started_attendance_cannot_be_overwritten(app_database):
    call, pid, wid = registered(app_database)
    other = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Other site', 'budget': 10000}))['projectId']
    body = {'workerId': wid, 'projectId': pid, 'date': '2026-10-04', 'operation': 'allocate', 'expectedVersion': 0}
    data(call(api.workforce_handler, PATH, 'POST', body))
    assert call(api.workforce_handler, PATH, 'POST', {**body, 'targetProjectId': other})['statusCode'] == 400
    data(call(api.workforce_handler, PATH, 'POST', {**body, 'expectedVersion': 1, 'targetProjectId': other}))
    data(record_attendance(app_database, {**body, 'projectId': other, 'operation': 'attendance', 'status': 'Present'}))
    assert call(api.workforce_handler, PATH, 'POST', {**body, 'expectedVersion': 2})['statusCode'] == 400


def test_allocation_after_payroll_generation_does_not_change_wages_or_snapshots(app_database):
    call, pid, wid = registered(app_database)
    other = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Other site', 'budget': 10000}))['projectId']
    cycles = {}
    for project, day in [(pid, '2026-10-01'), (other, '2026-10-02')]:
        body = {'workerId': wid, 'projectId': project, 'date': day}
        data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'allocate'}))
        data(record_attendance(app_database, {**body, 'operation': 'attendance', 'status': 'Present'}))
    for project in (pid, other):
        cycles[project] = data(call(api.governance_handler, '/projects/' + project + '/payroll', 'POST', {'month': '2026-10'}))
    allocation = {'workerId': wid, 'projectId': pid, 'date': '2026-10-05', 'operation': 'allocate', 'expectedVersion': 0}
    first = data(call(api.workforce_handler, PATH, 'POST', allocation))
    assert first['projectId'] == pid
    moved = data(call(api.workforce_handler, PATH, 'POST', {**allocation, 'targetProjectId': other, 'expectedVersion': 1}))
    assert moved['projectId'] == other and moved['version'] == 2
    assert call(api.workforce_handler, PATH, 'POST', {**allocation, 'expectedVersion': 1})['statusCode'] == 400
    # Started attendance remains protected even when payroll exists.
    assert call(api.workforce_handler, PATH, 'POST', {**allocation, 'date': '2026-10-01', 'targetProjectId': other, 'expectedVersion': 1})['statusCode'] == 400
    for project in (pid, other):
        saved = data(call(api.governance_handler, '/projects/' + project + '/payroll', query={'month': '2026-10'}))[0]
        assert saved == cycles[project]
    rows = data(call(api.workforce_handler, PATH, query={'projectId': other, 'date': '2026-10-05'}))
    current = next(row for row in rows if row['workerId'] == wid)
    assert current['allocationConfirmed'] and not current['attendanceRecorded'] and current['dailyWage'] == 0


def test_profile_attendance_daily_payments_deductions_and_payroll(app_database):
    call, pid, wid = registered(app_database)
    for day, status, paid in [('2026-10-01', 'Present', 'Paid'), ('2026-10-02', 'Half Day', 'Not paid')]:
        body = {'workerId': wid, 'projectId': pid, 'date': day}
        data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'allocate'}))
        data(record_attendance(app_database, {**body, 'operation': 'attendance', 'status': status, 'paymentStatus': paid}))
    data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'debit', 'amount': 100, 'description': 'Advance', 'requestId': 'profile-advance-001'}))
    query = {'projectId': pid, 'month': '2026-10'}
    profile = data(call(api.workforce_handler, PATH + '/' + wid, query=query))
    assert len(profile['attendance']) == 2
    assert profile['summary'] == {'daysPresent': 1.5, 'grossWages': 1200, 'dailyPaid': 800, 'payrollPaid': 0, 'deductions': 100, 'advanceRecovery': 0, 'outstanding': 300, 'payrollStatus': 'Not generated'}
    assert 'bankName' not in profile['worker'] and 'accNo' not in profile['worker']
    payroll = '/projects/' + pid + '/payroll'
    cycle = data(call(api.governance_handler, payroll, 'POST', {'month': '2026-10'}))
    data(call(api.governance_handler, payroll + '/disburse', 'POST', {'month': '2026-10', 'reference': 'profile-settlement-001', 'expectedCreatedAt': cycle['createdAt']}))
    settled = data(call(api.workforce_handler, PATH + '/' + wid, query=query))
    assert settled['summary']['outstanding'] == 0 and settled['summary']['payrollPaid'] == 300
    assert call(api.workforce_handler, PATH + '/' + wid, query={'month': 'bad'})['statusCode'] == 400


def test_recorded_advance_reduces_final_pay_and_locks_with_payroll(app_database):
    call, pid, wid = registered(app_database)
    body = {'workerId': wid, 'projectId': pid, 'date': '2026-10-04'}
    data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'allocate'}))
    data(record_attendance(app_database, {**body, 'operation': 'attendance', 'status': 'Present'}))
    advance = {**body, 'operation': 'advance', 'amount': 200, 'description': 'Personal advance', 'requestId': 'record-only-advance-001'}
    first = data(call(api.workforce_handler, PATH, 'POST', advance))
    repeated = data(call(api.workforce_handler, PATH, 'POST', advance))
    assert first['advanceId'] == repeated['advanceId']
    assert call(api.workforce_handler, PATH, 'POST', {**advance, 'amount': 300})['statusCode'] == 400
    query = {'projectId': pid, 'month': '2026-10'}
    profile = data(call(api.workforce_handler, PATH + '/' + wid, query=query))
    assert profile['recordedAdvances'] == 200 and len(profile['advances']) == 1
    assert profile['summary']['deductions'] == 200 and profile['summary']['outstanding'] == 600
    cycle = data(call(api.governance_handler, '/projects/' + pid + '/payroll', 'POST', {'month': '2026-10'}))
    assert cycle['staff'][0]['net'] == 600
    data(call(api.workforce_handler, PATH, 'POST', {**advance, 'amount': 500, 'requestId': 'record-only-after-payroll'}))
    after = data(call(api.workforce_handler, PATH + '/' + wid, query=query))
    assert after['recordedAdvances'] == 700 and len(after['advances']) == 2
    assert after['summary']['deductions'] == 200 and after['summary']['outstanding'] == 600
    assert data(call(api.workforce_handler, PATH + '/' + wid, query={**query, 'month': '2026-11'}))['recordedAdvances'] == 0
    other = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Other site', 'budget': 10000}))['projectId']
    assert data(call(api.workforce_handler, PATH + '/' + wid, query={**query, 'projectId': other}))['recordedAdvances'] == 0


def test_unlimited_advances_after_payroll_generation_and_payment(app_database):
    call, pid, wid = registered(app_database)
    body = {'workerId': wid, 'projectId': pid, 'date': '2026-10-04'}
    data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'allocate'}))
    data(record_attendance(app_database, {**body, 'operation': 'attendance', 'status': 'Present'}))
    advance = {**body, 'operation': 'advance', 'amount': 200, 'description': 'Cash', 'requestId': 'before-payroll-advance'}
    data(call(api.workforce_handler, PATH, 'POST', advance))
    endpoint = '/projects/' + pid + '/payroll'
    cycle = data(call(api.governance_handler, endpoint, 'POST', {'month': '2026-10'}))
    # Backdating must not hide a new advance when the older snapshot is paid.
    large = {**advance, 'amount': 10**13, 'date': '2026-10-01', 'requestId': 'after-payroll-large-advance'}
    saved = data(call(api.workforce_handler, PATH, 'POST', large))
    assert data(call(api.workforce_handler, PATH, 'POST', large))['advanceId'] == saved['advanceId']
    data(call(api.governance_handler, endpoint + '/disburse', 'POST', {
        'month': '2026-10', 'reference': 'payroll-payment', 'expectedCreatedAt': cycle['createdAt']}))
    after_payment = {**advance, 'amount': 1000, 'requestId': 'after-paid-advance'}
    second = data(call(api.workforce_handler, PATH, 'POST', after_payment))
    profile = data(call(api.workforce_handler, PATH + '/' + wid, query={'projectId': pid, 'month': '2026-10'}))
    assert profile['recordedAdvances'] == 10**13 + 1000
    assert {entry['advanceId'] for entry in profile['advances']} == {saved['advanceId'], second['advanceId']}
    assert profile['summary']['payrollPaid'] == 600
    assert profile['summary']['outstanding'] == 0
    assert not profile['advancesHasMore']


def test_partial_clear_full_clear_and_delete_before_payroll(app_database):
    call, pid, wid = registered(app_database)
    body = {'workerId': wid, 'projectId': pid, 'date': '2026-10-04'}
    data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'allocate'}))
    data(record_attendance(app_database, {**body, 'operation': 'attendance', 'status': 'Present'}))
    advance = data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'advance', 'amount': 2000, 'description': 'Advance', 'requestId': 'advance-to-clear-001'}))
    query = {'projectId': pid, 'month': '2026-10'}
    clearing = {**body, 'operation': 'advance_clear', 'advanceId': advance['advanceId'], 'expectedVersion': 0, 'amount': 500, 'requestId': 'clear-advance-001'}
    for amount in (0, -100, 2001):
        assert call(api.workforce_handler, PATH, 'POST', {**clearing, 'amount': amount})['statusCode'] == 400
    first = data(call(api.workforce_handler, PATH, 'POST', clearing))
    assert first['remainingAmount'] == 1500 and first['clearedAmount'] == 500
    assert data(call(api.workforce_handler, PATH, 'POST', clearing))['remainingAmount'] == 1500
    assert call(api.workforce_handler, PATH, 'POST', {**clearing, 'amount': 100})['statusCode'] == 400
    assert call(api.workforce_handler, PATH, 'POST', {**clearing, 'requestId': 'stale-clear-advance-001'})['statusCode'] == 400
    profile = data(call(api.workforce_handler, PATH + '/' + wid, query=query))
    assert profile['recordedAdvances'] == 1500 and profile['advances'][0]['remainingAmount'] == 1500
    data(call(api.workforce_handler, PATH, 'POST', {**clearing, 'amount': 1500, 'expectedVersion': 1, 'requestId': 'full-clear-advance-001'}))
    profile = data(call(api.workforce_handler, PATH + '/' + wid, query=query))
    assert profile['recordedAdvances'] == 0 and profile['advances'] == []
    second = data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'advance', 'amount': 1000, 'description': 'Second advance', 'requestId': 'advance-to-delete-001'}))
    data(call(api.workforce_handler, PATH, 'POST', {**clearing, 'advanceId': second['advanceId'], 'amount': 200, 'requestId': 'partial-second-advance'}))
    deletion = {**body, 'operation': 'advance_delete', 'advanceId': second['advanceId'], 'expectedVersion': 1, 'requestId': 'delete-advance-001'}
    assert data(call(api.workforce_handler, PATH, 'POST', deletion))['status'] == 'Deleted'
    assert data(call(api.workforce_handler, PATH, 'POST', deletion))['status'] == 'Deleted'
    profile = data(call(api.workforce_handler, PATH + '/' + wid, query=query))
    assert profile['recordedAdvances'] == 0 and profile['advances'] == []
    assert profile['summary']['deductions'] == 0 and profile['summary']['outstanding'] == 800
    assert call(api.workforce_handler, PATH, 'POST', {**deletion, 'requestId': 'delete-again-advance'})['statusCode'] == 400
    other = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Other site', 'budget': 10000}))['projectId']
    assert call(api.workforce_handler, PATH, 'POST', {**deletion, 'projectId': other})['statusCode'] == 400
    cycle = data(call(api.governance_handler, '/projects/' + pid + '/payroll', 'POST', {'month': '2026-10'}))
    assert cycle['staff'][0]['net'] == 800


def test_worker_advance_retry_and_payroll_lock(app_database):
    call, pid, wid = registered(app_database)
    body = {'workerId': wid, 'projectId': pid, 'date': '2026-10-04'}
    advance = {**body, 'operation': 'debit', 'amount': 200, 'description': 'Personal advance', 'requestId': 'worker-advance-retry-001'}
    first = data(call(api.workforce_handler, PATH, 'POST', advance))
    repeated = data(call(api.workforce_handler, PATH, 'POST', advance))
    assert first['deductionId'] == repeated['deductionId']
    assert call(api.workforce_handler, PATH, 'POST', {**advance, 'amount': 300})['statusCode'] == 400
    data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'allocate'}))
    data(record_attendance(app_database, {**body, 'operation': 'attendance', 'status': 'Present'}))
    query = {'projectId': pid, 'month': '2026-10'}
    summary = data(call(api.workforce_handler, PATH + '/' + wid, query=query))['summary']
    assert summary['deductions'] == 200 and summary['outstanding'] == 600
    cycle = data(call(api.governance_handler, '/projects/' + pid + '/payroll', 'POST', {'month': '2026-10'}))
    assert cycle['staff'][0]['net'] == 600
    assert call(api.workforce_handler, PATH, 'POST', {**advance, 'requestId': 'worker-advance-after-payroll'})['statusCode'] == 400
    assert data(call(api.workforce_handler, PATH + '/' + wid, query=query))['summary']['deductions'] == 200


def test_supervisor_profiles_are_project_scoped_and_bank_fields_hidden(app_database):
    call, pid, wid = registered(app_database)
    _, org, emp, sub = app_database
    unrelated = data(call(api.workforce_handler, PATH, 'POST', {'name': 'Unrelated', 'rate': 800}))['workerId']
    data(call(api.workforce_handler, PATH, 'POST', {'workerId': wid, 'projectId': pid, 'date': '2026-10-04', 'operation': 'allocate'}))
    get_table('USERS_TABLE').update_item(Key={'PK': 'ORG#' + org, 'SK': 'USER#' + emp}, UpdateExpression='SET #r = :r', ExpressionAttributeNames={'#r': 'role'}, ExpressionAttributeValues={':r': 'supervisor'})
    get_table('PROJECTS_TABLE').update_item(Key={'PK': 'ORG#' + org, 'SK': 'PROJECT#' + pid}, UpdateExpression='SET supervisorIds = :ids', ExpressionAttributeValues={':ids': [emp]})
    def get(path, query):
        return api.workforce_handler({'rawPath': path, 'queryStringParameters': query, 'requestContext': {'http': {'method': 'GET'}, 'authorizer': {'jwt': {'claims': {'sub': sub, 'custom:org_id': org, 'cognito:groups': ['supervisor']}}}}}, None)
    query = {'projectId': pid, 'month': '2026-10', 'date': '2026-10-04'}
    profile = get(PATH + '/' + wid, query)
    assert profile['statusCode'] == 200
    assert 'Private' not in profile['body']
    assert 'Private' not in get(PATH, query)['body']
    assert get(PATH + '/' + unrelated, query)['statusCode'] == 403
    assert get(PATH + '/' + wid, {'month': '2026-10'})['statusCode'] in (400, 403)
