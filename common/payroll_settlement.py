"""Group settlement receipts against an immutable monthly payroll register."""
from common import audit, gang_workforce, payroll_records, storage
from common.dynamo import get_table
from common.workflow_helpers import clean, now


def group_key(member):
    return 'staff' if member.get('salary') else member.get('gangId') or 'ungrouped'


def member_paid(cycle, member):
    return cycle.get('status') == 'Paid' or group_key(member) in cycle.get('paidGroups', {})


def settle(table, identity, cycle, body):
    expanded = payroll_records.expand(table, cycle)
    if body.get('expectedCreatedAt') != cycle['createdAt']:
        raise ValueError('Payroll was regenerated. Refresh and review the current cycle before recording payment.')
    reference = str(body.get('reference', '')).strip()
    if not reference:
        raise ValueError('An external payment reference is required')
    groups = {group_key(member) for member in expanded['staff']}
    selected = body.get('settlementGroup') or 'all'
    if selected != 'all' and selected not in groups:
        raise ValueError('Choose a group in this payroll register')
    receipt_key = {'PK': cycle['PK'], 'SK': f"PAYROLL-SETTLEMENT#{cycle['cycleId']}#{selected}"}
    previous = table.get_item(Key=receipt_key, ConsistentRead=True).get('Item')
    if previous:
        if previous['reference'] != reference:
            raise ValueError('This settlement has already been recorded with a different reference')
        return clean(expanded)
    if cycle.get('status') == 'Paid':
        if cycle.get('paymentReference') == reference:
            return clean(expanded)
        raise ValueError('This payroll is already settled')
    paid_groups = dict(cycle.get('paidGroups', {}))
    if selected != 'all' and selected in paid_groups:
        raise ValueError('This group is already settled')
    target_groups = groups - set(paid_groups) if selected == 'all' else {selected}
    members = [member for member in expanded['staff'] if group_key(member) in target_groups]
    amount = sum(member['net'] for member in members)
    stamp = now()
    for group in target_groups:
        paid_groups[group] = {'amount': sum(member['net'] for member in members if group_key(member) == group),
                              'reference': reference, 'paidAt': stamp}
    updated = dict(cycle, paidGroups=paid_groups, paidAmount=cycle.get('paidAmount', 0) + amount,
                   status='Paid' if set(paid_groups) == groups else 'Partially Paid',
                   settlementVersion=1, settlementRevision=cycle.get('settlementRevision', 0) + 1)
    if updated['status'] == 'Paid':
        updated.update(paymentReference=reference, paidAt=stamp, paidBy=identity.user_id)
    receipt = {**receipt_key, 'entityType': 'payroll-settlement', 'orgId': cycle['orgId'],
               'projectId': cycle['projectId'], 'cycleId': cycle['cycleId'], 'month': cycle['month'],
               'amount': amount, 'reference': reference, 'status': 'Paid', 'date': stamp[:10],
               'paidAt': stamp, 'paidBy': identity.user_id, 'settlementGroup': selected,
               'description': f"Payroll {cycle['month']} / {selected}", 'createdAt': stamp}
    condition = '#status = :status AND createdAt = :created'
    values = {':status': cycle['status'], ':created': cycle['createdAt']}
    if 'settlementRevision' in cycle:
        condition += ' AND settlementRevision = :revision'
        values[':revision'] = cycle['settlementRevision']
    else:
        condition += ' AND attribute_not_exists(settlementRevision)'
    storage.transact(table, [
        {'Put': {'TableName': table.name, 'Item': updated, 'ConditionExpression': condition,
                 'ExpressionAttributeNames': {'#status': 'status'}, 'ExpressionAttributeValues': values}},
        {'Put': {'TableName': table.name, 'Item': receipt, 'ConditionExpression': 'attribute_not_exists(PK)'}},
        audit.operation(identity, 'payroll.settled', before=cycle, after=updated),
        audit.operation(identity, 'payroll.group-payment-recorded', after=receipt),
    ] + gang_workforce.payroll_account_operations(get_table('WORKFORCE_TABLE'), identity, updated, members, True))
    return clean(payroll_records.expand(table, updated))
