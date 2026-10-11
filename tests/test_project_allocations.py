import pytest
from common import model
from common.dynamo import get_table
from handlers import consolidated as api
from test_model import app_database, data


def project_and_supervisor(db):
    call, org, _, _ = db
    supervisor = model.new_id('user', 'phase-supervisor')
    get_table('USERS_TABLE').put_item(Item={
        'PK': 'ORG#' + org, 'SK': 'USER#' + supervisor, 'orgId': org,
        'employeeId': supervisor, 'entityType': 'user', 'name': 'Site Lead',
        'role': 'supervisor', 'status': 'Active',
    })
    project = data(call(api.projects_handler, '/projects', 'POST', {
        'name': 'Site', 'budget': 10000, 'startDate': '2026-07-01', 'endDate': '2027-11-10',
    }))
    return call, project, supervisor


def allocation():
    return {'name': 'Site Lead', 'phase': 'Structure pouring', 'start': '2026-10-22',
            'end': '2027-02-23', 'untilProjectEnd': False, 'status': 'Active'}


def test_phase_allocation_is_saved_and_returned_with_assignment(app_database):
    call, project, supervisor = project_and_supervisor(app_database)
    path = '/projects/' + project['projectId']
    body = {'supervisorsTimeline': [allocation()], 'supervisorIds': [supervisor], 'expectedVersion': project['version']}
    saved = data(call(api.projects_handler, path, 'PUT', body))
    assert saved['supervisorsTimeline'] == body['supervisorsTimeline']
    loaded = data(call(api.projects_handler, path))
    assert loaded['supervisorsTimeline'] == body['supervisorsTimeline']
    assert loaded['supervisorIds'] == [supervisor]
    assert call(api.projects_handler, path, 'PUT', body)['statusCode'] == 409
    through_end = {**allocation(), 'end': '2027-11-10', 'untilProjectEnd': True, 'status': 'Upcoming'}
    updated = data(call(api.projects_handler, path, 'PUT', {'supervisorsTimeline': [allocation(), through_end], 'expectedVersion': saved['version']}))
    assert len(updated['supervisorsTimeline']) == 2


@pytest.mark.parametrize('invalid', [
    'not a list', [None], [{**allocation(), 'unexpected': True}],
    [{**allocation(), 'name': ' '}], [{**allocation(), 'phase': ''}],
    [{**allocation(), 'start': 'Jul 2026'}], [{**allocation(), 'end': '2027-02-30'}],
    [{**allocation(), 'start': '2027-03-01'}], [{**allocation(), 'end': '2028-01-01'}],
    [{**allocation(), 'untilProjectEnd': True}], [{**allocation(), 'status': 'Unknown'}],
    [{**allocation(), 'untilProjectEnd': 'yes'}], [allocation()] * 101,
])
def test_invalid_phase_allocation_cannot_change_project(app_database, invalid):
    call, project, supervisor = project_and_supervisor(app_database)
    path = '/projects/' + project['projectId']
    response = call(api.projects_handler, path, 'PUT', {'supervisorsTimeline': invalid, 'supervisorIds': [supervisor], 'expectedVersion': project['version']})
    assert response['statusCode'] == 400, response
    loaded = data(call(api.projects_handler, path))
    assert loaded['version'] == project['version']
    assert not loaded.get('supervisorsTimeline')


def test_phase_allocation_does_not_bypass_supervisor_validation(app_database):
    call, project, _ = project_and_supervisor(app_database)
    response = call(api.projects_handler, '/projects/' + project['projectId'], 'PUT', {
        'supervisorsTimeline': [allocation()], 'supervisorIds': [app_database[2]], 'expectedVersion': project['version'],
    })
    assert response['statusCode'] == 400
