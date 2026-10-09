"""Bounded worker detail reads and attendance-safe response fields."""
import calendar
import re
import time
from datetime import date
from decimal import Decimal
from boto3.dynamodb.conditions import Key
from common import model, payroll_records
from common.authz import AuthorizationError, SUPERVISOR
from common.dynamo import get_table


def batch_rows(table, keys):
    found = {}
    for start in range(0, len(keys), 100):
        pending = {table.name: {'Keys': keys[start:start + 100], 'ConsistentRead': True}}
        for attempt in range(6):
            response = table.meta.client.batch_get_item(RequestItems=pending)
            found.update({(row['PK'], row['SK']): row for row in response.get('Responses', {}).get(table.name, [])})
            pending = response.get('UnprocessedKeys', {})
            if not pending:
                break
            if attempt == 5:
                raise ValueError('Worker history is temporarily unavailable; retry shortly')
            time.sleep(0.025 * 2 ** attempt)
    return found


def attendance_safe(row):
    return {key: value for key, value in row.items() if key not in {'bankName', 'accNo'}}


def profile(identity, table, org, pid, worker_id, query):
    model.validate_id('worker', worker_id)
    worker = table.get_item(Key={'PK': 'ORG#' + org, 'SK': 'WORKER#' + worker_id}, ConsistentRead=True).get('Item')
    if not worker or worker.get('orgId') != org:
        raise AuthorizationError('Worker is not accessible')
    month = str(query.get('month') or date.today().strftime('%Y-%m'))
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
        raise ValueError('Choose a month using YYYY-MM')
    first = date.fromisoformat(month + '-01')
    last = calendar.monthrange(first.year, first.month)[1]
    result = {'worker': {key: worker[key] for key in ('workerId', 'name', 'rate', 'type', 'createdAt') if key in worker},
              'month': month, 'projectId': pid, 'attendance': [], 'summary': None}
    if not pid:
        # _pk has already rejected project-less supervisor requests.
        return result
    pk = f'ORG#{org}#PROJECT#{pid}'
    keys = [{'PK': pk, 'SK': model.daily_key(worker_id, f'{month}-{day:02}')} for day in range(1, last + 1)]
    rows = sorted(batch_rows(table, keys).values(), key=lambda row: row['date'])
    allocation_prefix = 'ALLOCATION#' + worker_id + '#'
    allocations = table.query(KeyConditionExpression=Key('PK').eq('ORG#' + org) & Key('SK').between(allocation_prefix, allocation_prefix + f'{month}-{last:02}'),
                              ScanIndexForward=False, Limit=1, ConsistentRead=True).get('Items', [])
    if SUPERVISOR in identity.roles and not rows and (not allocations or allocations[0].get('projectId') != pid):
        raise AuthorizationError('Worker is not assigned to this project in the selected period')
    result['attendance'] = [{key: row[key] for key in ('date', 'status', 'nightShift', 'rate', 'wage', 'paymentStatus') if key in row} for row in rows]
    totals = table.get_item(Key={'PK': pk, 'SK': f'WORKER-MONTH#{month}#{worker_id}'}, ConsistentRead=True).get('Item', {})
    gross = sum(row['wage'] for row in rows)
    daily_paid = sum(row['wage'] for row in rows if row.get('paymentStatus') == 'Paid')
    deductions = totals.get('advanceDeductions', 0)
    payroll_paid = 0
    payroll = get_table('PAYROLL_TABLE')
    cycle = payroll.get_item(Key={'PK': pk, 'SK': 'PAYROLL#' + month}, ConsistentRead=True).get('Item')
    if cycle and cycle.get('status') == 'Paid':
        entry = payroll_records.employee_entry(payroll, cycle, worker_id)
        payroll_paid = (entry or {}).get('net', 0)
    result['summary'] = {'daysPresent': sum({'Present': 1, 'Half Day': Decimal('0.5')}.get(row['status'], 0) for row in rows),
                         'grossWages': gross, 'dailyPaid': daily_paid, 'payrollPaid': payroll_paid, 'deductions': deductions,
                         'outstanding': max(0, gross - daily_paid - deductions - payroll_paid),
                         'payrollStatus': cycle.get('status') if cycle else 'Not generated'}
    return result
