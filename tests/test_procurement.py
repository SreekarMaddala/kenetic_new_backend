import json
import pytest
from test_security_workflows import ADMIN_A, ORG_A, PROJECT_A, PROJECT_B, SITE_A, database, event, data
from test_subcontractors import call, onboard
from common.dynamo import get_table

SITE = {'sub': SITE_A, 'role': 'supervisor'}
ENDPOINT = f'/projects/{PROJECT_A}/subcontractors'


def setup_request():
    company = onboard()
    assignment = data(call(ENDPOINT, 'POST', {'registryId': company['subcontractorId'], 'scopeOfWork': 'Work'}))
    body = {'type': 'Procurement', 'name': 'Plywood', 'contractorId': assignment['subcontractorId'], 'quantity': 5, 'unit': 'pcs', 'requiredDate': '2026-11-02'}
    request = data(call(ENDPOINT, 'POST', body, **SITE))
    return request, body


def test_supervisor_submits_and_admin_reviews_but_cannot_request(database):
    request, body = setup_request()
    assert request['status'] == 'Pending'
    assert request['createdBy'] == SITE_A and request['supervisorId'] == SITE_A
    assert call(ENDPOINT, 'POST', body)['statusCode'] == 403
    assert call(ENDPOINT, 'POST', {**body, 'status': 'Approved'}, **SITE)['statusCode'] == 400
    path = ENDPOINT + '/' + request['subcontractorId']
    assert call(path, 'PATCH', {'status': 'Approved'}, **SITE)['statusCode'] == 403
    approved = data(call(path, 'PATCH', {'status': 'Approved'}))
    assert approved['approvedBy'] == ADMIN_A
    assert any(r['subcontractorId'] == request['subcontractorId'] for r in data(call(ENDPOINT, **SITE)))
    assert call('/subcontractors', **SITE)['statusCode'] == 403
    assert call(f'/projects/{PROJECT_B}/subcontractors', **SITE)['statusCode'] == 403
    assert call(f'/projects/{PROJECT_A}/boq', **SITE)['statusCode'] == 403
    assert call(ENDPOINT, 'POST', {'registryId': body['contractorId'], 'scopeOfWork': 'Work'}, **SITE)['statusCode'] == 403


def test_reverse_keeps_approval_and_audits_reason_and_actor_once(database):
    request, _ = setup_request()
    path = ENDPOINT + '/' + request['subcontractorId']
    approved = data(call(path, 'PATCH', {'status': 'Approved'}))
    body = {'reason': 'Order cancelled by site', 'requestId': 'procurement-reverse-001', 'expectedVersion': approved['version']}
    assert call(path + '/reverse', 'POST', body, **SITE)['statusCode'] == 403
    assert call(path + '/reverse', 'POST', {**body, 'expectedVersion': 0})['statusCode'] == 409
    assert call(path + '/reverse', 'POST', {**body, 'reason': '  '})['statusCode'] == 400
    reversed_request = data(call(path + '/reverse', 'POST', body))
    assert reversed_request['status'] == 'Reversed'
    assert reversed_request['reversalReason'] == body['reason']
    assert reversed_request['reversedBy'] == ADMIN_A
    assert reversed_request['approvedAt'] == approved['approvedAt']
    assert reversed_request['approvedBy'] == approved['approvedBy']
    assert reversed_request['version'] == approved['version'] + 1
    assert data(call(path + '/reverse', 'POST', body))['version'] == reversed_request['version']
    assert call(path + '/reverse', 'POST', {**body, 'reason': 'Changed reason'})['statusCode'] == 400
    assert call(path, 'PATCH', {'status': 'Pending'})['statusCode'] == 400
    assert call(path, 'PATCH', {'quantity': 99})['statusCode'] == 400
    from common.workflow_helpers import rows
    reversals = [r for r in rows(get_table('AUDIT_EVENTS_TABLE'), f'ORG#{ORG_A}', 'AUDIT#') if r['action'] == 'procurement.reversed']
    assert len(reversals) == 1
    assert reversals[0]['before']['status'] == 'Approved' and reversals[0]['after']['status'] == 'Reversed'


def test_only_approved_procurement_can_be_reversed(database):
    request, body = setup_request()
    path = ENDPOINT + '/' + request['subcontractorId']
    reversal = {'reason': 'Cancel', 'requestId': 'procurement-reverse-002', 'expectedVersion': request['version']}
    assert call(path + '/reverse', 'POST', reversal)['statusCode'] == 400
    assignment_path = ENDPOINT + '/' + body['contractorId']
    assert call(assignment_path + '/reverse', 'POST', reversal)['statusCode'] == 400
