import json
from common.dynamo import get_table
from handlers import consolidated as api
from test_model import app_database, data

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
    data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'attendance', 'status': 'Present', 'paymentStatus': 'Paid'}))
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
    data(call(api.workforce_handler, PATH, 'POST', {**body, 'projectId': other, 'operation': 'attendance', 'status': 'Present'}))
    assert call(api.workforce_handler, PATH, 'POST', {**body, 'expectedVersion': 2})['statusCode'] == 400


def test_profile_attendance_daily_payments_deductions_and_payroll(app_database):
    call, pid, wid = registered(app_database)
    for day, status, paid in [('2026-10-01', 'Present', 'Paid'), ('2026-10-02', 'Half Day', 'Not paid')]:
        body = {'workerId': wid, 'projectId': pid, 'date': day}
        data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'allocate'}))
        data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'attendance', 'status': status, 'paymentStatus': paid}))
    data(call(api.workforce_handler, PATH, 'POST', {**body, 'operation': 'debit', 'amount': 100, 'description': 'Advance', 'requestId': 'profile-advance-001'}))
    query = {'projectId': pid, 'month': '2026-10'}
    profile = data(call(api.workforce_handler, PATH + '/' + wid, query=query))
    assert len(profile['attendance']) == 2
    assert profile['summary'] == {'daysPresent': 1.5, 'grossWages': 1200, 'dailyPaid': 800, 'payrollPaid': 0, 'deductions': 100, 'outstanding': 300, 'payrollStatus': 'Not generated'}
    assert 'bankName' not in profile['worker'] and 'accNo' not in profile['worker']
    payroll = '/projects/' + pid + '/payroll'
    cycle = data(call(api.governance_handler, payroll, 'POST', {'month': '2026-10'}))
    data(call(api.governance_handler, payroll + '/disburse', 'POST', {'month': '2026-10', 'reference': 'profile-settlement-001', 'expectedCreatedAt': cycle['createdAt']}))
    settled = data(call(api.workforce_handler, PATH + '/' + wid, query=query))
    assert settled['summary']['outstanding'] == 0 and settled['summary']['payrollPaid'] == 300
    assert call(api.workforce_handler, PATH + '/' + wid, query={'month': 'bad'})['statusCode'] == 400


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
