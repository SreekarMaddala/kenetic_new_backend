"""Transactional writes, sparse indexes and reconciliable spending summaries."""
from decimal import Decimal
import hashlib
import threading
import time
from common import model, relationships
from common.dynamo import get_table

_TRANSACT_LOCK = threading.RLock()


def contribution(record):
    record = record or {}
    kind = record.get('entityType')
    status = str(record.get('status', '')).lower()
    if kind == 'daily-wage' and record.get('paymentStatus') == 'Paid':
        return Decimal(record.get('wage', 0))
    if (kind in {'expense', 'payment'} and status == 'approved') or (kind == 'payroll' and status == 'paid'):
        return Decimal(record.get('amount', 0))
    return Decimal(0)


def transact(table, operations):
    with _TRANSACT_LOCK:
        deltas = {}
        locators = []
        references = []
        worker_deltas = {}
        touched_projects = set()
        organization_deltas = {}
        proposed = {(op['Put']['TableName'], op['Put']['Item']['PK'], op['Put']['Item']['SK']): op['Put']['Item'] for op in operations if 'Put' in op}
        for op in operations:
            put = op.get('Put')
            if not put:
                continue
            item = put['Item']
            if item.get('entityType') != 'audit-event':
                model.index_item(item)
                kind = item.get('entityType')
                if kind in {'expense', 'payment'}:
                    identifier = item[kind + 'Id']
                    locators.append({'Put': {'TableName': put['TableName'], 'Item': {
                        'PK': f"ORG#{item['orgId']}", 'SK': f'LOOKUP#{kind}#{identifier}',
                        'orgId': item['orgId'], 'recordPK': item['PK'], 'recordSK': item['SK'],
                    }, 'ConditionExpression': 'attribute_not_exists(PK) OR (recordPK = :pk AND recordSK = :sk)',
                        'ExpressionAttributeValues': {':pk': item['PK'], ':sk': item['SK']}}})
                continue
            for sign, record in ((-1, item.get('before')), (1, item.get('after'))):
                if sign == 1 and record:
                    references.extend(relationships.constraints(record, item.get('before'), item.get('action', ''), proposed))
                    if not item.get('before'):
                        kind = record.get('entityType')
                        if record.get('requestId'):
                            request_key = hashlib.sha256(str(record['requestId']).encode()).hexdigest()
                            source = next((op['Put']['TableName'] for op in operations if 'Put' in op and op['Put']['Item'].get('PK') == record['PK'] and op['Put']['Item'].get('SK') == record['SK']), None)
                            if source:
                                locators.append({'Put': {'TableName': source, 'Item': {
                                    'PK': f"ORG#{record['orgId']}", 'SK': f'REQUEST#{kind}#{request_key}',
                                    'recordPK': record['PK'], 'recordSK': record['SK'],
                                }, 'ConditionExpression': 'attribute_not_exists(PK)'}})
                        unique = None
                        if kind == 'bill':
                            party = record.get('vendorId') or str(record.get('clientOrContractor', '')).strip().casefold()
                            unique = 'invoice|' + party + '|' + str(record.get('billNumber', '')).strip().casefold()
                        elif kind == 'payment' and record.get('mode') != 'Cash':
                            unique = 'payment|' + str(record.get('mode', '')).casefold() + '|' + str(record.get('reference', '')).strip().casefold()
                        if unique:
                            key = hashlib.sha256(unique.encode()).hexdigest()
                            locators.append({'Put': {'TableName': get_table('FINANCE_TABLE').name, 'Item': {
                                'PK': f"ORG#{record['orgId']}", 'SK': 'UNIQUE#' + key,
                                'orgId': record['orgId'], 'recordPK': record['PK'], 'recordSK': record['SK'],
                            }, 'ConditionExpression': 'attribute_not_exists(PK)'}})
                if record and record.get('orgId'):
                    org = record['orgId']
                    if model.pending(record):
                        key = ('ORG#' + org, 'SUMMARY#APPROVALS')
                        organization_deltas[key] = organization_deltas.get(key, 0) + sign
                    if record.get('entityType') == 'expense' and contribution(record):
                        day = str(record.get('date') or record.get('approvedAt') or record['createdAt'])[:10]
                        for key in (('ORG#' + org, 'SUMMARY#EXPENSES'), ('ORG#' + org + '#EXPENSES', 'DAY#' + day)):
                            organization_deltas[key] = organization_deltas.get(key, 0) + sign * contribution(record)
                if not record or not record.get('projectId'):
                    continue
                if record.get('entityType') in {'daily-wage', 'deduction'}:
                    touched_projects.add((record['orgId'], record['projectId']))
                    worker = record.get('workerId') or record.get('labourId')
                    key = (record['PK'], 'WORKER-MONTH#' + record['date'][:7] + '#' + worker)
                    delta = worker_deltas.setdefault(key, {})
                    values = worker_contribution(record)
                    for field, value in values.items():
                        delta[field] = delta.get(field, Decimal(0)) + sign * value
                amount = sign * contribution(record)
                if not amount:
                    continue
                day = str(record.get('date') or record.get('paidAt') or record.get('approvedAt') or record.get('createdAt') or '')[:10]
                if not day:
                    raise ValueError('Posted transactions need a posting date')
                org, pid = record['orgId'], record['projectId']
                for pk, sk in ((f'ORG#{org}', f'SPENDING#{pid}'),
                               (f'ORG#{org}#PROJECT#{pid}', f'MONTH#{day[:7]}'),
                               (f'ORG#{org}#PROJECT#{pid}', f'DAY#{day}')):
                    key = (pk, sk, org, pid)
                    deltas[key] = deltas.get(key, Decimal(0)) + amount
        reporting = get_table('REPORTING_TABLE')
        operations.extend(locators)
        keys = {(op[next(iter(op))]['TableName'], op[next(iter(op))].get('Key', op[next(iter(op))].get('Item', {})).get('PK'), op[next(iter(op))].get('Key', op[next(iter(op))].get('Item', {})).get('SK')) for op in operations}
        for reference in references:
            check = reference['ConditionCheck']
            key = (check['TableName'], check['Key']['PK'], check['Key']['SK'])
            if key not in keys:
                operations.append(reference)
                keys.add(key)
        for (pk, sk, org, pid), amount in deltas.items():
            if amount:
                operations.append({'Update': {
                    'TableName': reporting.name, 'Key': {'PK': pk, 'SK': sk},
                    'UpdateExpression': 'SET orgId = :org, projectId = :pid ADD amount :delta',
                    'ExpressionAttributeValues': {':org': org, ':pid': pid, ':delta': amount},
                }})
        # A repair lease blocks posting while rebuilding derived totals. Revision
        # checks let dry-run reconciliation detect a changing source snapshot.
        for (pk, sk), values in worker_deltas.items():
            values = {key: value for key, value in values.items() if value}
            if values:
                operations.append({'Update': {'TableName': get_table('WORKFORCE_TABLE').name, 'Key': {'PK': pk, 'SK': sk},
                                              'UpdateExpression': 'ADD ' + ', '.join('#' + field + ' :' + field for field in values),
                                              'ExpressionAttributeNames': {'#' + field: field for field in values},
                                              'ExpressionAttributeValues': {':' + field: value for field, value in values.items()}}})
        for (pk, sk), delta in organization_deltas.items():
            if delta:
                operations.append({'Update': {'TableName': reporting.name, 'Key': {'PK': pk, 'SK': sk},
                                              'UpdateExpression': 'ADD amount :delta', 'ExpressionAttributeValues': {':delta': delta}}})
        organizations = {pk.split('#')[1] for (pk, _), delta in organization_deltas.items() if delta}
        for org in organizations:
            operations.append({'Update': {'TableName': reporting.name, 'Key': {'PK': 'ORG#' + org, 'SK': 'CONTROL#REPORTS'},
                                           'UpdateExpression': 'ADD revision :one',
                                           'ConditionExpression': 'attribute_not_exists(leaseUntil) OR leaseUntil < :now',
                                           'ExpressionAttributeValues': {':one': 1, ':now': int(time.time())}}})
        projects = {(org, pid) for (_, _, org, pid), amount in deltas.items() if amount} | touched_projects
        for org, pid in projects:
            operations.append({'Update': {
                'TableName': reporting.name, 'Key': {'PK': f'ORG#{org}#PROJECT#{pid}', 'SK': 'CONTROL'},
                'UpdateExpression': 'ADD revision :one',
                'ConditionExpression': 'attribute_not_exists(leaseUntil) OR leaseUntil < :now',
                'ExpressionAttributeValues': {':one': 1, ':now': int(time.time())},
            }})
        if len(operations) > 100:
            raise ValueError('The operation exceeds the transaction limit')
        return table.meta.client.transact_write_items(TransactItems=operations)


def worker_contribution(record):
    if record.get('entityType') == 'deduction':
        return {'advanceDeductions': Decimal(record['amount'])}
    if record.get('entityType') == 'daily-wage':
        return {'grossWages': Decimal(record.get('wage', 0)),
                'dailyPaid': Decimal(record.get('wage', 0)) if record.get('paymentStatus') == 'Paid' else Decimal(0),
                'daysPresent': {'Present': Decimal(1), 'Half Day': Decimal('0.5')}.get(record['status'], Decimal(0)),
                'nightShifts': Decimal(int(bool(record.get('nightShift'))))}
    return {}
