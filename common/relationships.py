"""Tenant-owned references checked again by transaction conditions."""
from common import model
from common.dynamo import get_table


def constraints(record, before=None, action='', proposed=None):
    org = record.get('orgId')
    pid = record.get('projectId') or record.get('targetProjectId')
    kind = record.get('entityType')
    if not org or kind in {'audit-event', 'payroll-entry', 'payroll-build', 'payroll-history'}:
        return []
    reversal = action.endswith('.reversed') or action.endswith('-reversed')
    targets = []
    if kind != 'organization':
        targets.append(('ORGANIZATIONS_TABLE', {'PK': f'ORG#{org}', 'SK': f'ORGANIZATION#{org}'}, 'organization', 'status', {'Suspended', 'Archived'}))
    if pid and kind != 'project':
        targets.append(('PROJECTS_TABLE', {'PK': f'ORG#{org}', 'SK': f'PROJECT#{pid}'}, 'project', 'status', {'Archived'} if not reversal else set()))
    refs = {
        'vendorId': ('PARTIES_TABLE', f'ORG#{org}', 'VENDOR', 'vendor'),
        'materialId': ('INVENTORY_TABLE', f'ORG#{org}', 'INVENTORY-ITEM', 'inventory-item'),
        'itemId': ('INVENTORY_TABLE', f'ORG#{org}', 'INVENTORY-ITEM', 'inventory-item'),
        'documentId': ('DOCUMENT_CONTROL_TABLE', f'ORG#{org}#PROJECT#{pid}', 'DOCUMENT', 'document'),
        'registryId': ('PARTIES_TABLE', f'ORG#{org}', 'SUBCONTRACTOR', 'subcontractor'),
        'contractorId': ('PARTIES_TABLE', f'ORG#{org}#PROJECT#{pid}', 'SUBCONTRACTOR', 'subcontractor'),
        'workerId': ('WORKFORCE_TABLE', f'ORG#{org}', 'WORKER', 'worker'),
        'employeeId': ('USERS_TABLE', f'ORG#{org}', 'USER', 'user'),
        'supervisorId': ('USERS_TABLE', f'ORG#{org}', 'USER', 'user'),
        'billId': ('FINANCE_TABLE', f'ORG#{org}#PROJECT#{pid}', 'BILL', 'bill'),
    }
    for field, (table_env, pk, prefix, entity) in refs.items():
        identifier = record.get(field)
        if not identifier or (field == 'materialId' and kind == 'material') or (field == 'itemId' and kind == 'inventory-item') or (field == 'employeeId' and kind == 'user') or (field == 'workerId' and kind == 'worker'):
            continue
        model.validate_id(entity, identifier)
        targets.append((table_env, {'PK': pk, 'SK': f'{prefix}#{identifier}'}, entity, 'status', set() if reversal else {'Inactive', 'Disabled', 'Archived', 'Suspended'}))
    if kind == 'project':
        for identifier in record.get('supervisorIds', []):
            model.validate_id('user', identifier)
            targets.append(('USERS_TABLE', {'PK': f'ORG#{org}', 'SK': f'USER#{identifier}'}, 'user', 'status', {'Disabled'}))
    if kind == 'vendor':
        for identifier in record.get('materialIds', []):
            targets.append(('INVENTORY_TABLE', {'PK': f'ORG#{org}', 'SK': f'INVENTORY-ITEM#{identifier}'}, 'inventory-item', 'status', {'Inactive', 'Archived'}))
    result = []
    for env, key, entity, status_field, invalid_states in targets:
        if record.get('status') in {'Rejected', 'Inactive', 'Disabled', 'Archived'} and entity != 'organization':
            invalid_states = set()
        table = get_table(env)
        proposed_item = (proposed or {}).get((table.name, key['PK'], key['SK']))
        item = proposed_item or table.get_item(Key=key, ConsistentRead=True).get('Item')
        if not item or item.get('orgId') != org or item.get(status_field) in invalid_states:
            raise ValueError(f'Choose a valid {entity} in this organization')
        if entity == 'user' and kind in {'project', 'attendance', 'expense'} and item.get('role') != 'supervisor':
            raise ValueError('Choose an active supervisor')
        if kind == 'payment' and entity == 'bill':
            if not reversal and item.get('status') != 'Approved':
                raise ValueError('Choose an approved bill')
            if item.get('vendorId') and item['vendorId'] != record.get('vendorId'):
                raise ValueError('The bill belongs to a different vendor')
        names, values = {'#org': 'orgId'}, {':org': org}
        condition = '#org = :org'
        # Verify the exact state/version read, not just the presence of a reference.
        for field in ('version', 'status', 'role'):
            names['#' + field] = field
            if field in item:
                values[':' + field] = item[field]
                condition += f' AND #{field} = :{field}'
            else:
                condition += f' AND attribute_not_exists(#{field})'
        if proposed_item:
            continue  # The referenced row is conditionally created in this transaction.
        result.append({'ConditionCheck': {'TableName': table.name, 'Key': key, 'ConditionExpression': condition,
                                         'ExpressionAttributeNames': names, 'ExpressionAttributeValues': values}})
    return result
