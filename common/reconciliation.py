"""Rebuild derived spending from strongly read source records, under a lease."""
import time
import uuid
from decimal import Decimal
from common import audit, storage
from common.dynamo import get_table


def reconcile(identity, org, pid, apply=False):
    from common.workflow_helpers import rows
    reporting = get_table('REPORTING_TABLE')
    pk = f'ORG#{org}#PROJECT#{pid}'
    control_key = {'PK': pk, 'SK': 'CONTROL'}
    control = reporting.get_item(Key=control_key, ConsistentRead=True).get('Item', {})
    revision = control.get('revision', 0)
    owner = uuid.uuid4().hex
    if apply:
        reporting.update_item(Key=control_key, UpdateExpression='SET leaseOwner = :owner, leaseUntil = :until',
                              ConditionExpression='(attribute_not_exists(leaseUntil) OR leaseUntil < :now) AND (revision = :revision OR (attribute_not_exists(revision) AND :revision = :zero))',
                              ExpressionAttributeValues={':owner': owner, ':until': int(time.time()) + 600, ':now': int(time.time()), ':revision': revision, ':zero': 0})
    try:
        expected = {('ORG#' + org, 'SPENDING#' + pid): Decimal(0)}
        worker_expected = {}
        source_count = 0
        for env in ('FINANCE_TABLE', 'WORKFORCE_TABLE', 'PAYROLL_TABLE'):
            for record in rows(get_table(env), pk, ''):
                worker_values = storage.worker_contribution(record) if env == 'WORKFORCE_TABLE' else {}
                if worker_values:
                    worker = record.get('workerId') or record.get('labourId')
                    sk = 'WORKER-MONTH#' + record['date'][:7] + '#' + worker
                    values = worker_expected.setdefault(sk, {})
                    for field, value in worker_values.items():
                        values[field] = values.get(field, Decimal(0)) + value
                amount = storage.contribution(record)
                if not amount:
                    continue
                day = str(record.get('date') or record.get('paidAt') or record.get('approvedAt') or record.get('createdAt') or '')[:10]
                if len(day) != 10:
                    raise ValueError('Source record has no valid posting date')
                source_count += 1
                for key in (('ORG#' + org, 'SPENDING#' + pid), (pk, 'MONTH#' + day[:7]), (pk, 'DAY#' + day)):
                    expected[key] = expected.get(key, Decimal(0)) + amount
            if apply:
                reporting.update_item(Key=control_key, UpdateExpression='SET leaseUntil = :until',
                                      ConditionExpression='leaseOwner = :owner AND leaseUntil >= :now',
                                      ExpressionAttributeValues={':owner': owner, ':until': int(time.time()) + 600, ':now': int(time.time())})
        actual = {(r['PK'], r['SK']): r.get('amount', 0) for r in rows(reporting, pk, '') if r['SK'].startswith(('MONTH#', 'DAY#'))}
        total_key = {'PK': 'ORG#' + org, 'SK': 'SPENDING#' + pid}
        actual[(total_key['PK'], total_key['SK'])] = reporting.get_item(Key=total_key, ConsistentRead=True).get('Item', {}).get('amount', 0)
        latest = reporting.get_item(Key=control_key, ConsistentRead=True).get('Item', {})
        if latest.get('revision', 0) != revision:
            raise ValueError('Spending changed during reconciliation; retry with a fresh snapshot')
        changes = [{'PK': key[0], 'SK': key[1], 'before': actual.get(key, 0), 'after': expected.get(key, 0)}
                   for key in sorted(set(expected) | set(actual)) if actual.get(key, 0) != expected.get(key, 0)]
        workforce = get_table('WORKFORCE_TABLE')
        worker_actual = {r['SK']: {field: r.get(field, 0) for field in ('grossWages', 'dailyPaid', 'daysPresent', 'nightShifts', 'advanceDeductions')}
                         for r in rows(workforce, pk, 'WORKER-MONTH#')}
        worker_changes = []
        for sk in sorted(set(worker_expected) | set(worker_actual)):
            after = {field: worker_expected.get(sk, {}).get(field, 0) for field in ('grossWages', 'dailyPaid', 'daysPresent', 'nightShifts', 'advanceDeductions')}
            if worker_actual.get(sk) != after:
                worker_changes.append({'PK': pk, 'SK': sk, 'before': worker_actual.get(sk), 'after': after})
        if apply:
            for start in range(0, len(changes), 75):
                batch = changes[start:start + 75]
                operations = [{'Put': {'TableName': reporting.name, 'Item': {'PK': row['PK'], 'SK': row['SK'],
                                           'orgId': org, 'projectId': pid, 'amount': row['after']}}} for row in batch]
                operations.append({'Update': {'TableName': reporting.name, 'Key': control_key,
                                              'UpdateExpression': 'SET leaseUntil = :until',
                                              'ConditionExpression': 'leaseOwner = :owner AND leaseUntil >= :now',
                                              'ExpressionAttributeValues': {':owner': owner, ':until': int(time.time()) + 600, ':now': int(time.time())}}})
                # Use direct transaction: repair writes must not create spending deltas.
                record = {**total_key, 'orgId': org, 'projectId': pid, 'changedRows': batch}
                operations.append(audit.operation(identity, 'reporting.reconciled', after=record, reason='Rebuilt from authoritative financial and wage records'))
                reporting.meta.client.transact_write_items(TransactItems=operations)
            for start in range(0, len(worker_changes), 75):
                batch = worker_changes[start:start + 75]
                operations = [{'Put': {'TableName': workforce.name, 'Item': {'PK': row['PK'], 'SK': row['SK'], **row['after']}}} for row in batch]
                operations.append({'Update': {'TableName': reporting.name, 'Key': control_key, 'UpdateExpression': 'SET leaseUntil = :until',
                                              'ConditionExpression': 'leaseOwner = :owner AND leaseUntil >= :now',
                                              'ExpressionAttributeValues': {':owner': owner, ':until': int(time.time()) + 600, ':now': int(time.time())}}})
                operations.append(audit.operation(identity, 'workforce.reconciled', after={**total_key, 'orgId': org, 'projectId': pid, 'changedRows': batch}))
                reporting.meta.client.transact_write_items(TransactItems=operations)
        return {'projectId': pid, 'sourceCount': source_count, 'revision': revision, 'applied': apply, 'changes': changes, 'workerChanges': worker_changes}
    finally:
        if apply:
            reporting.update_item(Key=control_key, UpdateExpression='REMOVE leaseOwner, leaseUntil',
                                  ConditionExpression='leaseOwner = :owner', ExpressionAttributeValues={':owner': owner})


def reconcile_stock(identity, org, apply=False):
    from common.workflow_helpers import rows
    table = get_table('INVENTORY_TABLE')
    pk = 'ORG#' + org
    key = {'PK': pk, 'SK': 'CONTROL#STOCK'}
    revision = table.get_item(Key=key, ConsistentRead=True).get('Item', {}).get('revision', 0)
    owner = uuid.uuid4().hex
    if apply:
        table.update_item(Key=key, UpdateExpression='SET leaseOwner = :owner, leaseUntil = :until',
                          ConditionExpression='(attribute_not_exists(leaseUntil) OR leaseUntil < :now) AND (revision = :revision OR (attribute_not_exists(revision) AND :revision = :zero))',
                          ExpressionAttributeValues={':owner': owner, ':until': int(time.time()) + 600, ':now': int(time.time()), ':revision': revision, ':zero': 0})
    try:
        expected = {}
        events = rows(table, pk, 'WAREHOUSE-')
        for event in events:
            identifier = event['itemId']
            destination = pk + '#PROJECT#' + event['targetProjectId'] if event.get('targetProjectId') else pk
            changes = [(destination, 'BALANCE#' + identifier, event['qty'])]
            if event['recordKind'] == 'issue-vouchers':
                changes.append((pk, 'BALANCE#' + identifier, -event['qty']))
            else:
                changes.append((pk, 'TOTAL#' + identifier, event['qty']))
            for partition, sk, delta in changes:
                row = expected.setdefault((partition, sk), {'PK': partition, 'SK': sk, 'orgId': org, 'itemId': identifier,
                                                              'name': event['item'], 'unit': event['unit'], 'quantity': Decimal(0)})
                row['quantity'] += delta
        partitions = {pk} | {row['PK'] for row in expected.values()}
        partitions |= {pk + '#PROJECT#' + project['projectId'] for project in rows(get_table('PROJECTS_TABLE'), pk, 'PROJECT#')}
        actual = {}
        for partition in partitions:
            actual.update({(row['PK'], row['SK']): row for row in rows(table, partition, 'BALANCE#')})
            if apply:
                table.update_item(Key=key, UpdateExpression='SET leaseUntil = :until', ConditionExpression='leaseOwner = :owner AND leaseUntil >= :now',
                                  ExpressionAttributeValues={':owner': owner, ':until': int(time.time()) + 600, ':now': int(time.time())})
        actual.update({(row['PK'], row['SK']): row for row in rows(table, pk, 'TOTAL#')})
        if table.get_item(Key=key, ConsistentRead=True).get('Item', {}).get('revision', 0) != revision:
            raise ValueError('Stock changed during reconciliation; retry')
        if any(row['quantity'] < 0 for row in expected.values()):
            raise ValueError('Movement records imply negative stock; review the ledger before repair')
        changes = []
        for record_key in sorted(set(expected) | set(actual)):
            before = actual.get(record_key, {}).get('quantity', 0)
            row = expected.get(record_key) or dict(actual[record_key], quantity=Decimal(0))
            if before != row['quantity']:
                changes.append({'row': row, 'before': before, 'after': row['quantity']})
        if apply:
            for start in range(0, len(changes), 75):
                batch = changes[start:start + 75]
                operations = []
                for change in batch:
                    row = dict(change['row'])
                    if row['SK'].startswith('BALANCE#'):
                        row.update(entityType='stock-balance', OrgPK=pk, OrgSK=f"stock-balance##{row['PK']}#{row['itemId']}")
                    operations.append({'Put': {'TableName': table.name, 'Item': row}})
                operations.append({'Update': {'TableName': table.name, 'Key': key, 'UpdateExpression': 'SET leaseUntil = :until',
                                              'ConditionExpression': 'leaseOwner = :owner AND leaseUntil >= :now',
                                              'ExpressionAttributeValues': {':owner': owner, ':until': int(time.time()) + 600, ':now': int(time.time())}}})
                details = [{k: change[k] for k in ('before', 'after')} | {k: change['row'][k] for k in ('PK', 'SK')} for change in batch]
                operations.append(audit.operation(identity, 'inventory.reconciled', after={**key, 'orgId': org, 'changedRows': details}))
                table.meta.client.transact_write_items(TransactItems=operations)
        return {'orgId': org, 'sourceCount': len(events), 'applied': apply, 'changes': [{'PK': change['row']['PK'], 'SK': change['row']['SK'], 'before': change['before'], 'after': change['after']} for change in changes]}
    finally:
        if apply:
            table.update_item(Key=key, UpdateExpression='REMOVE leaseOwner, leaseUntil', ConditionExpression='leaseOwner = :owner', ExpressionAttributeValues={':owner': owner})


def backfill_directory(org=None, apply=False):
    """Explicit offline scan for existing profiles; normal APIs use indexes."""
    from common import model
    count = 0
    for env in ('ORGANIZATIONS_TABLE', 'USERS_TABLE'):
        table = get_table(env)
        args = {}
        while True:
            result = table.scan(**args)
            for row in result['Items']:
                if row.get('entityType') not in {'organization', 'user'} or (org and row.get('orgId') != org):
                    continue
                indexed = model.index_item(dict(row))
                if row.get('DirectoryPK') == indexed['DirectoryPK'] and row.get('DirectorySK') == indexed['DirectorySK']:
                    continue
                count += 1
                if apply:
                    table.update_item(Key={k: row[k] for k in ('PK', 'SK')}, UpdateExpression='SET DirectoryPK = :pk, DirectorySK = :sk',
                                      ConditionExpression='attribute_exists(PK) AND orgId = :org',
                                      ExpressionAttributeValues={':pk': indexed['DirectoryPK'], ':sk': indexed['DirectorySK'], ':org': row['orgId']})
            if not result.get('LastEvaluatedKey'):
                break
            args['ExclusiveStartKey'] = result['LastEvaluatedKey']
    return {'indexedRecords': count, 'applied': apply}


def reconcile_reports(identity, org, apply=False):
    from common import model
    from common.workflow_helpers import rows
    table = get_table('REPORTING_TABLE')
    pk = 'ORG#' + org
    control_key = {'PK': pk, 'SK': 'CONTROL#REPORTS'}
    revision = table.get_item(Key=control_key, ConsistentRead=True).get('Item', {}).get('revision', 0)
    owner = uuid.uuid4().hex
    if apply:
        table.update_item(Key=control_key, UpdateExpression='SET leaseOwner = :owner, leaseUntil = :until',
                          ConditionExpression='(attribute_not_exists(leaseUntil) OR leaseUntil < :now) AND (revision = :revision OR (attribute_not_exists(revision) AND :revision = :zero))',
                          ExpressionAttributeValues={':owner': owner, ':until': int(time.time()) + 600, ':now': int(time.time()), ':revision': revision, ':zero': 0})
    try:
        partitions = {pk} | {pk + '#PROJECT#' + project['projectId'] for project in rows(get_table('PROJECTS_TABLE'), pk, 'PROJECT#')}
        expected = {(pk, 'SUMMARY#APPROVALS'): Decimal(0), (pk, 'SUMMARY#EXPENSES'): Decimal(0)}
        index_changes = []
        source_count = 0
        for env in ('FINANCE_TABLE', 'MATERIALS_TABLE', 'FIELD_OPERATIONS_TABLE', 'PARTIES_TABLE'):
            source_table = get_table(env)
            for partition in partitions:
                for row in rows(source_table, partition, ''):
                    if row.get('entityType') not in model.APPROVAL_TYPES:
                        continue
                    source_count += 1
                    if model.pending(row):
                        expected[(pk, 'SUMMARY#APPROVALS')] += 1
                    if row.get('entityType') == 'expense' and storage.contribution(row):
                        amount = storage.contribution(row)
                        expected[(pk, 'SUMMARY#EXPENSES')] += amount
                        day = str(row.get('date') or row.get('approvedAt') or row['createdAt'])[:10]
                        day_key = (pk + '#EXPENSES', 'DAY#' + day)
                        expected[day_key] = expected.get(day_key, Decimal(0)) + amount
                    indexed = model.index_item(dict(row))
                    if any(row.get(field) != indexed.get(field) for field in ('ApprovalPK', 'ApprovalSK')):
                        index_changes.append((source_table.name, row, indexed))
                if apply:
                    table.update_item(Key=control_key, UpdateExpression='SET leaseUntil = :until', ConditionExpression='leaseOwner = :owner AND leaseUntil >= :now',
                                      ExpressionAttributeValues={':owner': owner, ':until': int(time.time()) + 600, ':now': int(time.time())})
        actual = {(row['PK'], row['SK']): row.get('amount', 0) for row in rows(table, pk + '#EXPENSES', 'DAY#')}
        for sk in ('SUMMARY#APPROVALS', 'SUMMARY#EXPENSES'):
            actual[(pk, sk)] = table.get_item(Key={'PK': pk, 'SK': sk}, ConsistentRead=True).get('Item', {}).get('amount', 0)
        if table.get_item(Key=control_key, ConsistentRead=True).get('Item', {}).get('revision', 0) != revision:
            raise ValueError('Reports changed during reconciliation; retry')
        changes = [{'PK': key[0], 'SK': key[1], 'before': actual.get(key, 0), 'after': expected.get(key, 0)} for key in sorted(set(expected) | set(actual)) if actual.get(key, 0) != expected.get(key, 0)]
        if apply:
            operations = []
            for row in changes:
                operations.append({'Put': {'TableName': table.name, 'Item': {'PK': row['PK'], 'SK': row['SK'], 'amount': row['after'], 'orgId': org}}})
            for source_table_name, row, indexed in index_changes:
                operation = {'TableName': source_table_name, 'Key': {k: row[k] for k in ('PK', 'SK')},
                             'ConditionExpression': '#version = :version' if 'version' in row else 'attribute_not_exists(#version)',
                             'ExpressionAttributeNames': {'#version': 'version'}}
                if 'version' in row:
                    operation['ExpressionAttributeValues'] = {':version': row['version']}
                if indexed.get('ApprovalPK'):
                    operation['UpdateExpression'] = 'SET ApprovalPK = :pk, ApprovalSK = :sk'
                    operation.setdefault('ExpressionAttributeValues', {}).update({':pk': indexed['ApprovalPK'], ':sk': indexed['ApprovalSK']})
                else:
                    operation['UpdateExpression'] = 'REMOVE ApprovalPK, ApprovalSK'
                operations.append({'Update': operation})
            for start in range(0, len(operations), 75):
                batch = operations[start:start + 75]
                batch.append({'Update': {'TableName': table.name, 'Key': control_key, 'UpdateExpression': 'SET leaseUntil = :until',
                                         'ConditionExpression': 'leaseOwner = :owner AND leaseUntil >= :now',
                                         'ExpressionAttributeValues': {':owner': owner, ':until': int(time.time()) + 600, ':now': int(time.time())}}})
                batch.append(audit.operation(identity, 'reports.reconciled', after={**control_key, 'orgId': org, 'changedRecords': len(batch) - 1, 'repairId': owner}))
                table.meta.client.transact_write_items(TransactItems=batch)
        return {'orgId': org, 'sourceCount': source_count, 'applied': apply, 'changes': changes, 'approvalIndexChanges': len(index_changes)}
    finally:
        if apply:
            table.update_item(Key=control_key, UpdateExpression='REMOVE leaseOwner, leaseUntil', ConditionExpression='leaseOwner = :owner', ExpressionAttributeValues={':owner': owner})
