import pytest

from common import model
from common.dynamo import get_table
from handlers import consolidated as api
from test_feature_workflows import SITE, call
from test_security_workflows import ADMIN_B, ORG_A, ORG_B, PROJECT_A, database, data

PATH = '/supervisor/labour/attendance'


def create_gang(name='Masonry gang', **identity):
    return data(call(api.workforce_handler, PATH, 'POST', {'operation': 'gang_create', 'name': name}, **identity))


def test_create_empty_gang_then_create_workers_inside_it(database):
    gang = create_gang()
    assert gang['gangId'].startswith('gng_')
    assert data(call(api.workforce_handler, PATH)) == []
    assert data(call(api.workforce_handler, PATH, query={'gangs': 'true'}))[0]['gangId'] == gang['gangId']
    body = {'operation': 'register', 'gangId': gang['gangId'], 'name': 'Ravi', 'rate': 800, 'requestId': 'gang-worker-ravi'}
    first = data(call(api.workforce_handler, PATH, 'POST', body))
    assert (first['gangId'], first['gangName']) == (gang['gangId'], gang['name'])
    assert data(call(api.workforce_handler, PATH, 'POST', body))['workerId'] == first['workerId']
    second = data(call(api.workforce_handler, PATH, 'POST', {**body, 'name': 'Suresh', 'requestId': 'gang-worker-suresh'}))
    workers = data(call(api.workforce_handler, PATH))
    assert {worker['workerId'] for worker in workers} == {first['workerId'], second['workerId']}
    assert all(worker['gangId'] == gang['gangId'] for worker in workers)
    # A gang is a registry grouping; allocations and attendance remain dated.
    allocation = {'projectId': PROJECT_A, 'workerId': first['workerId'], 'date': '2026-10-11'}
    data(call(api.workforce_handler, PATH, 'POST', dict(allocation, operation='allocate')))
    assert call(api.workforce_handler, PATH, 'POST', dict(allocation, status='Present'))['statusCode'] == 403
    data(call(api.workforce_handler, PATH, 'POST', dict(allocation, status='Present'), **SITE))


def test_gang_names_are_normalized_and_creation_retries_do_not_duplicate(database):
    first = create_gang('  Masonry   gang  ')
    assert first['name'] == 'Masonry gang'
    assert create_gang('masonry GANG')['gangId'] == first['gangId']
    assert len(data(call(api.workforce_handler, PATH, query={'gangs': 'true'}))) == 1
    assert create_gang('Steel gang')['gangId'] != first['gangId']


def test_gangs_and_members_are_organization_scoped_and_admin_only(database):
    gang = create_gang()
    assert data(call(api.workforce_handler, PATH, query={'gangs': 'true'}, sub=ADMIN_B, org=ORG_B)) == []
    assert call(api.workforce_handler, PATH, query={'gangs': 'true', 'projectId': PROJECT_A}, **SITE)['statusCode'] == 403
    assert call(api.workforce_handler, PATH, 'POST', {'operation': 'gang_create', 'name': 'Forbidden', 'projectId': PROJECT_A}, **SITE)['statusCode'] == 403
    assert call(api.workforce_handler, PATH, 'POST', {'operation': 'gang_create', 'name': 'Wrong scope', 'projectId': PROJECT_A})['statusCode'] == 400
    body = {'operation': 'register', 'name': 'Worker', 'rate': 800, 'gangId': gang['gangId']}
    assert call(api.workforce_handler, PATH, 'POST', body, sub=ADMIN_B, org=ORG_B)['statusCode'] == 400
    assert call(api.workforce_handler, PATH, 'POST', {**body, 'gangId': model.new_id('worker-gang')})['statusCode'] == 400
    assert call(api.workforce_handler, PATH, 'POST', {**body, 'gangId': 'forged'})['statusCode'] == 400
    assert get_table('WORKFORCE_TABLE').get_item(Key={'PK': f'ORG#{ORG_A}', 'SK': 'GANG#' + gang['gangId']})['Item']['name'] == gang['name']


@pytest.mark.parametrize('name', ['', '  ', 'x' * 129, 123])
def test_invalid_gang_names_are_rejected(database, name):
    assert call(api.workforce_handler, PATH, 'POST', {'operation': 'gang_create', 'name': name})['statusCode'] == 400
