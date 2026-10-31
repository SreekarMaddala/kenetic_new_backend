from test_security_workflows import (
    ORG_A, ORG_B, PROJECT_A, PROJECT_C, ADMIN_B, SITE_A,
    database, event, data, put,
)
from test_subcontractors import call
from handlers import consolidated as api


def payment(project, contractor, amount, request_id, category=None, approve=True):
    body = dict(contractorId=contractor, amount=amount, mode='Cash', date='2026-10-31', requestId=request_id)
    if category:
        body['paymentType'] = category
    path = f'/projects/{project}/payments'
    saved = data(api.finance_handler(event(path, 'POST', body), None))
    if approve:
        saved = data(api.finance_handler(event(path + '/' + saved['paymentId'], 'PATCH', {'status': 'Approved'}), None))
    return saved


def test_one_subcontractor_has_separate_project_advances_and_payment_history(database):
    put('PROJECTS_TABLE', f'ORG#{ORG_A}', f'PROJECT#{PROJECT_C}', entityType='project', orgId=ORG_A,
        projectId=PROJECT_C, name='Site C', status='Active', budget=100000, spent=0)
    registry = data(call('/subcontractors', 'POST', {'name': 'Rajesh Shuttering', 'contactPerson': 'Rajesh', 'trade': 'Shuttering'}))
    identifier = registry['subcontractorId']
    for pid in (PROJECT_A, PROJECT_C):
        data(call(f'/projects/{pid}/subcontractors', 'POST', {'registryId': identifier, 'scopeOfWork': 'Slab work', 'contractValue': 2000}))
    payment(PROJECT_A, identifier, 100, 'advance-site-a-001', 'Advance')
    payment(PROJECT_C, identifier, 200, 'advance-site-c-001', 'Advance')
    payment(PROJECT_A, identifier, 50, 'work-payment-a-001', 'Payment')
    payment(PROJECT_A, identifier, 30, 'older-payment-a-001')
    payment(PROJECT_A, identifier, 80, 'pending-payment-a-001', 'Advance', approve=False)
    reversed_payment = payment(PROJECT_C, identifier, 500, 'reverse-advance-c-001', 'Advance')
    data(api.finance_handler(event(f"/projects/{PROJECT_C}/payments/{reversed_payment['paymentId']}/reverse", 'POST', {
        'reason': 'Refunded', 'reference': 'refund-proof', 'requestId': 'refund-site-c-001', 'expectedVersion': reversed_payment['version'],
    }), None))
    # A different company with the same name must stay separate.
    other = data(call('/subcontractors', 'POST', {'name': 'Rajesh Shuttering', 'contactPerson': 'Other Rajesh', 'trade': 'Shuttering'}))
    result = data(call('/subcontractors/analytics'))
    profile = next(r for r in result if r['subcontractorId'] == identifier)
    assert profile['projectCount'] == 2
    assert profile['contractValue'] == 4000
    assert profile['advancesPaid'] == 300
    assert profile['workPayments'] == 50
    assert profile['unclassifiedPaid'] == 30
    assert profile['totalPaid'] == 380
    assert profile['pendingPayments'] == 80
    by_project = {p['projectId']: p for p in profile['projects']}
    assert by_project[PROJECT_A]['advancesPaid'] == 100
    assert by_project[PROJECT_A]['totalPaid'] == 180
    assert by_project[PROJECT_C]['advancesPaid'] == 200
    assert by_project[PROJECT_C]['totalPaid'] == 200
    assert any(p['status'] == 'Reversed' for p in by_project[PROJECT_C]['payments'])
    assert next(r for r in result if r['subcontractorId'] == other['subcontractorId'])['totalPaid'] == 0


def test_subcontractor_analysis_is_admin_only_and_tenant_scoped(database):
    data(call('/subcontractors', 'POST', {'name': 'Private company', 'contactPerson': 'Rajesh', 'trade': 'Work'}))
    assert data(call('/subcontractors/analytics', sub=ADMIN_B, org=ORG_B)) == []
    assert call('/subcontractors/analytics', sub=SITE_A, role='supervisor')['statusCode'] == 403
    assert call('/subcontractors/analytics', query={'projectId': PROJECT_A})['statusCode'] == 400
