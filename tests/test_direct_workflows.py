import importlib
from pathlib import Path

import yaml

from common.dynamo import get_table
from handlers import consolidated as api
from test_model import app_database, data


def payroll_setup(db):
    call, org, emp, _ = db
    pid = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Site', 'budget': 10000}))['projectId']
    path = f'/projects/{pid}/payroll'
    data(call(api.governance_handler, path + '/staff', 'POST', {'employeeId': emp, 'basic': 1000}))
    return call, path, org, pid


def test_direct_payroll_returns_completed_cycle_and_prevents_duplicates(app_database):
    call, path, _, _ = payroll_setup(app_database)
    response = call(api.governance_handler, path, 'POST', {'month': '2026-10'})
    assert response['statusCode'] == 200
    cycle = data(response)
    assert cycle['staff'][0]['net'] == 1000
    repeated = data(call(api.governance_handler, path, 'POST', {'month': '2026-10'}))
    assert repeated['cycleId'] == cycle['cycleId']
    assert len(data(call(api.governance_handler, path))) == 1


def test_disabled_actor_cannot_generate_payroll(app_database):
    call, path, org, pid = payroll_setup(app_database)
    get_table('USERS_TABLE').update_item(
        Key={'PK': 'ORG#' + org, 'SK': 'USER#' + app_database[2]},
        UpdateExpression='SET #s = :s', ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':s': 'Inactive'})
    assert call(api.governance_handler, path, 'POST', {'month': '2026-10'})['statusCode'] == 403
    assert 'Item' not in get_table('PAYROLL_TABLE').get_item(
        Key={'PK': f'ORG#{org}#PROJECT#{pid}', 'SK': 'PAYROLL#2026-10'})


def test_retired_job_routes_do_not_generate_payroll(app_database):
    call, path, _, _ = payroll_setup(app_database)
    for suffix in ('/jobs', '/jobs/old-job', '/unknown'):
        assert call(api.governance_handler, path + suffix, 'POST', {'month': '2026-10'})['statusCode'] == 404
    assert data(call(api.governance_handler, path)) == []
    assert call(api.governance_handler, '/reports/exports', 'POST', {})['statusCode'] == 404


def test_direct_executive_report_and_date_validation(app_database):
    call, _, _, pid = payroll_setup(app_database)
    response = call(api.governance_handler, '/reports/executive', query={'startDate': '2026-10-01', 'endDate': '2026-10-31'})
    assert response['statusCode'] == 200
    projects = data(response)['projectSummaries']
    assert any(project['projectId'] == pid for project in projects)
    assert call(api.governance_handler, '/reports/executive', query={'startDate': 'bad'})['statusCode'] == 400


def test_template_wires_only_direct_request_handlers():
    root = Path(__file__).resolve().parents[2]
    template = yaml.safe_load((root / 'backend/template.yaml').read_text(encoding='utf-8'))
    resources = template['Resources']
    functions = [r['Properties'] for r in resources.values() if r['Type'] == 'AWS::Serverless::Function']
    assert len(functions) == 11
    assert not any(r['Type'].startswith(('AWS::SQS::', 'AWS::SNS::', 'AWS::Events::')) for r in resources.values())
    routes = {}
    for function in functions:
        module, name = function['Handler'].rsplit('.', 1)
        assert callable(getattr(importlib.import_module(module), name))
        for event in function['Events'].values():
            assert event['Type'] == 'HttpApi'
            properties = event['Properties']
            route = (properties['Path'], properties['Method'].upper())
            assert route not in routes
            routes[route] = function['Handler']
    for route in (('/projects/{projectId}/payroll', 'GET'),
                  ('/projects/{projectId}/payroll', 'POST'), ('/reports/executive', 'GET')):
        assert routes[route] == 'handlers.consolidated.governance_handler'
