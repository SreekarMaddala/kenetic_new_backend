"""Audited reversal of approved project procurement requests."""
from common import audit, model
from common.authz import OPERATIONS_ADMIN, SUPER_ADMIN, require_role
from common.workflow_helpers import HttpError, now


def reverse_request(table, identity, pk, org, identifier, body):
    require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
    model.validate_id('subcontractor', identifier)
    record = table.get_item(Key={'PK': pk, 'SK': 'SUBCONTRACTOR#' + identifier}, ConsistentRead=True).get('Item')
    if not record or record.get('orgId') != org or record.get('type') != 'Procurement':
        raise ValueError('Procurement request not found')
    reason = body['reason'].strip()
    if not reason:
        raise ValueError('Enter a reason for reversing this procurement request')
    if record.get('status') == 'Reversed' and record.get('reversalRequestId') == body['requestId']:
        if record.get('reversalReason') != reason:
            raise ValueError('This request ID was already used for a different reversal')
        return record
    if record.get('status') != 'Approved':
        raise ValueError('Only approved procurement requests can be reversed')
    version = record.get('version', 0)
    if version != body['expectedVersion']:
        raise HttpError(409, {'success': False, 'error': {'code': 'CONFLICT', 'message': 'This request changed. Refresh before reversing it.'}})
    updated = {**record, 'status': 'Reversed', 'reversalReason': reason, 'reversedBy': identity.user_id,
               'reversedAt': now(), 'reversalRequestId': body['requestId'], 'updatedAt': now(), 'version': version + 1}
    condition = '#status = :approved AND ' + ('#version = :version' if 'version' in record else 'attribute_not_exists(#version)')
    values = {':approved': 'Approved'}
    if 'version' in record:
        values[':version'] = version
    audit.put(table, identity, updated, before=record, action='procurement.reversed',
              ConditionExpression=condition, ExpressionAttributeNames={'#status': 'status', '#version': 'version'},
              ExpressionAttributeValues=values)
    return updated
