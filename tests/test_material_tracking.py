from test_security_workflows import (
    ORG_A, ORG_B, PROJECT_A, PROJECT_B, SITE_A, ADMIN_B,
    database, event, data,
)
from common.authz import SUPERVISOR
from handlers import consolidated as api
from common import reconciliation
from common.authz import active_identity
from common.dynamo import get_table
import pytest
import json


def call(path, method='GET', body=None, handler=api.supply_chain_handler, **kwargs):
    return handler(event(path, method, body, **kwargs), None)


def receive():
    return data(call('/warehouse/grn', 'POST', {
        'item': 'Steel', 'unit': 'Kg', 'category': 'Reinforcement',
        'qty': 20, 'requestId': 'steel-receipt-001',
    }))


def move(kind, qty, request_id, **extra):
    return call('/warehouse/issue-vouchers', 'POST', {
        'item': 'Steel', 'unit': 'Kg', 'category': 'Reinforcement',
        'qty': qty, 'movementType': kind, 'targetProjectId': PROJECT_A,
        'requestId': request_id, **extra,
    })


def project_stock():
    return data(call('/supervisor/materials/stock', handler=api.field_operations_handler,
                     query={'projectId': PROJECT_A}, sub=SITE_A, role=SUPERVISOR))[0]


def test_received_used_returned_and_remaining_share_one_warehouse_balance(database):
    receive()
    data(move('issue', 12, 'steel-issue-001'))
    usage = data(move('usage', 5, 'steel-usage-001'))
    assert data(move('usage', 5, 'steel-usage-001'))['warehouseEventId'] == usage['warehouseEventId']
    data(move('return', 3, 'steel-return-001'))
    record = project_stock()
    assert (record['receivedQty'], record['usedQty'], record['returnedQty'], record['quantity']) == (12, 5, 3, 4)
    assert record['category'] == 'Reinforcement'
    material = data(call('/inventory'))[0]
    assert (material['centralStock'], material['deployedStock'], material['totalStock']) == (11, 4, 15)
    assert material['projectBalances'] == [{
        'projectId': PROJECT_A, 'receivedQty': 12, 'usedQty': 5, 'returnedQty': 3, 'quantity': 4,
    }]
    history = data(call('/warehouse/issue-vouchers'))
    assert sorted(row['movementType'] for row in history) == ['issue', 'return', 'usage']
    identity = active_identity(event('/inventory'))
    assert reconciliation.reconcile_stock(identity, ORG_A)['changes'] == []
    # Repair must restore both balances and the project material counters.
    table = get_table('INVENTORY_TABLE')
    table.update_item(Key={'PK': f'ORG#{ORG_A}#PROJECT#{PROJECT_A}', 'SK': 'BALANCE#' + material['itemId']},
                      UpdateExpression='SET quantity = :q, usedQty = :u', ExpressionAttributeValues={':q': 99, ':u': 0})
    reconciliation.reconcile_stock(identity, ORG_A, True)
    repaired = project_stock()
    assert (repaired['quantity'], repaired['usedQty'], repaired['returnedQty'], repaired['receivedQty']) == (4, 5, 3, 12)
    assert reconciliation.reconcile_stock(identity, ORG_A)['changes'] == []


@pytest.mark.parametrize('kind', ['usage', 'return'])
def test_project_movements_cannot_overdraw_or_reuse_another_movement_id(database, kind):
    receive()
    data(move('issue', 4, 'steel-issue-001'))
    overdraw = move(kind, 5, 'steel-overdraw-001')
    assert overdraw['statusCode'] == 409
    error = json.loads(overdraw['body'])['error']
    assert error['code'] == 'INSUFFICIENT_STOCK'
    assert 'this project has 4 Kg available' in error['message']
    assert project_stock()['quantity'] == 4
    assert len(data(call('/warehouse/issue-vouchers'))) == 1
    assert move(kind, 4, 'steel-issue-001')['statusCode'] == 400
    assert move(kind, 1, 'steel-foreign-001', targetProjectId=PROJECT_B)['statusCode'] == 403
    assert move(kind, 1, 'steel-no-project', targetProjectId='')['statusCode'] == 400
    assert move(kind, 1, 'steel-bad-category', category='Other')['statusCode'] == 400
    assert data(call('/inventory'))[0]['centralStock'] == 16


def test_zero_warehouse_stock_explains_how_to_receive_stock_first(database):
    data(call('/inventory', 'POST', {'name': 'Steel', 'unit': 'Kg', 'category': 'Reinforcement'}))
    response = move('issue', 9, 'zero-stock-issue-001')
    assert response['statusCode'] == 409
    error = json.loads(response['body'])['error']
    assert error['code'] == 'INSUFFICIENT_STOCK'
    assert 'central warehouse has 0 Kg available' in error['message']
    assert 'Record a warehouse receipt first' in error['message']
    assert data(call('/warehouse/issue-vouchers')) == []


def test_supervisor_catalog_is_project_authorized_and_requests_keep_catalog_identity(database):
    item = receive()
    catalog = data(call('/supervisor/materials/stock', handler=api.field_operations_handler,
                        query={'projectId': PROJECT_A, 'catalog': 'true'}, sub=SITE_A, role=SUPERVISOR))
    assert [row['itemId'] for row in catalog] == [item['itemId']]
    assert call('/supervisor/materials/stock', handler=api.field_operations_handler,
                query={'projectId': PROJECT_B, 'catalog': 'true'}, sub=SITE_A, role=SUPERVISOR)['statusCode'] == 403
    body = {'projectId': PROJECT_A, 'catalogItemId': item['itemId'], 'category': 'Reinforcement',
            'materialName': 'Steel', 'unit': 'Kg', 'quantity': 2, 'requiredDate': '2026-11-01'}
    request = data(call('/supervisor/materials/indents', 'POST', body,
                        handler=api.field_operations_handler, sub=SITE_A, role=SUPERVISOR))
    assert request['projectId'] == PROJECT_A and request['catalogItemId'] == item['itemId']
    assert call('/supervisor/materials/indents', 'POST', {**body, 'materialName': 'Forged'},
                handler=api.field_operations_handler, sub=SITE_A, role=SUPERVISOR)['statusCode'] == 400
    assert data(call('/inventory', sub=ADMIN_B, org=ORG_B)) == []
