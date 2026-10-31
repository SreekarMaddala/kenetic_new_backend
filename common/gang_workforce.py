"""Gang headcounts and one rolling advance account, shared across projects."""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from common import audit, model, storage
from common.authz import AuthorizationError, OPERATIONS_ADMIN, SUPER_ADMIN, SUPERVISOR, require_role
from common.dynamo import get_table
from common.workflow_helpers import clean, now, number, re_id, rows


def count(value, label, positive=False):
    number(value, label, positive)
    if value != int(value) or value > 10000:
        raise ValueError(f'{label} must be a whole number up to 10,000')
    return int(value)


def money(value, label):
    amount = Decimal(number(value, label, True))
    if amount != amount.quantize(Decimal('.01'), rounding=ROUND_HALF_UP):
        raise ValueError(f'{label} must have at most two decimal places')
    return amount


def settings(body, fallback_day):
    start = body.get('headcountFrom') or fallback_day
    date.fromisoformat(start)
    return {'attendanceMode': 'headcount', 'headcountFrom': start,
            'size': count(body.get('size'), 'Gang size', True), 'rate': money(body.get('rate'), 'Daily rate')}


def active_on(gang, day):
    return gang.get('attendanceMode') == 'headcount' and day >= gang['headcountFrom']


def read(table, key):
    return table.get_item(Key=key, ConsistentRead=True).get('Item', {})


def versioned_put(table, before, after):
    operation = {'TableName': table.name, 'Item': after,
                 'ConditionExpression': '#v = :v' if 'version' in before else 'attribute_not_exists(#v)',
                 'ExpressionAttributeNames': {'#v': 'version'}}
    if 'version' in before:
        operation['ExpressionAttributeValues'] = {':v': before['version']}
    return {'Put': operation}


def account(table, org, gid):
    return read(table, {'PK': f'ORG#{org}', 'SK': f'GANG-ACCOUNT#{gid}'})


def account_change(table, org, gid, before, **changes):
    updated = {**before, 'PK': f'ORG#{org}', 'SK': f'GANG-ACCOUNT#{gid}',
               'orgId': org, 'gangId': gid, 'entityType': 'gang-account',
               'version': before.get('version', 0) + 1, 'updatedAt': now()}
    for key, delta in changes.items():
        updated[key] = before.get(key, 0) + delta
    return versioned_put(table, before, updated)


def balance(saved):
    outstanding = saved.get('given', 0) - saved.get('returned', 0) - saved.get('recovered', 0)
    net = saved.get('earned', 0) - saved.get('paid', 0) - saved.get('given', 0) + saved.get('returned', 0)
    return {'earned': saved.get('earned', 0), 'advancesGiven': saved.get('given', 0),
            'advancesReturned': saved.get('returned', 0), 'advancesRecovered': saved.get('recovered', 0),
            'advanceBalance': outstanding, 'reservedAdvances': saved.get('reserved', 0),
            'paid': saved.get('paid', 0), 'balanceToPay': max(0, net), 'carryForward': max(0, -net)}


def roster(identity, table, org, pid, day, gid=None, all_gangs=False):
    date.fromisoformat(day)
    if all_gangs or not pid:
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
    gangs = [read(table, {'PK': f'ORG#{org}', 'SK': f'GANG#{gid}'})] if gid else rows(table, f'ORG#{org}', 'GANG#')
    result = []
    for gang in gangs:
        if not gang or gang.get('orgId') != org:
            continue
        identifier = gang['gangId']
        allocation = read(table, {'PK': f'ORG#{org}', 'SK': f'GANG-ALLOCATION#{identifier}#{day}'})
        assigned = bool(pid and allocation.get('projectId') == pid)
        if pid and not assigned and not all_gangs:
            continue
        entry = {**clean(gang), 'allocation': clean(allocation) if (assigned or all_gangs) and allocation else None}
        if pid:
            pk = f'ORG#{org}#PROJECT#{pid}'
            daily = read(table, {'PK': pk, 'SK': f'GANG-DAILY#{day}#{identifier}'})
            entry['attendance'] = clean(daily) if daily else None
            entry['monthSummary'] = clean(read(table, {'PK': pk, 'SK': f'GANG-MONTH#{day[:7]}#{identifier}'}))
            entry['payrollLocked'] = bool(read(get_table('PAYROLL_TABLE'), {'PK': pk, 'SK': f'PAYROLL#{day[:7]}'}))
        if SUPERVISOR not in identity.roles:
            entry['account'] = balance(account(table, org, identifier))
            if gid:
                entry['history'] = [clean(row) for row in rows(table, f'ORG#{org}', f'GANG-HISTORY#{identifier}#')][::-1]
        result.append(entry)
    return result


def handle(identity, body, pk, org, pid, day):
    table = get_table('WORKFORCE_TABLE')
    operation = body['operation']
    gid = model.validate_id('worker-gang', body.get('gangId'))
    gang = read(table, {'PK': f'ORG#{org}', 'SK': f'GANG#{gid}'})
    if not gang or gang.get('orgId') != org or gang.get('status') != 'Active':
        raise ValueError('Choose an active gang in this organization')
    if operation == 'gang_configure':
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        config = settings(body, day)
        if gang.get('attendanceMode') == 'headcount' and config['headcountFrom'] != gang['headcountFrom']:
            raise ValueError('The headcount start date cannot change after setup')
        if body.get('expectedVersion') != gang.get('version', 0):
            raise ValueError('Gang settings changed. Refresh before saving.')
        if gang.get('attendanceMode') != 'headcount':
            members = {worker['workerId'] for worker in rows(table, f'ORG#{org}', 'WORKER#') if worker.get('gangId') == gid}
            if any(row.get('workerId', row.get('labourId')) in members and row['date'] >= config['headcountFrom']
                   for row in rows(table, prefix='daily-wage#', org=org)):
                raise ValueError('Choose a headcount start date after the gang\'s last named-worker attendance')
        item = {**gang, **config, 'version': gang.get('version', 0) + 1, 'updatedAt': now()}
        storage.transact(table, [versioned_put(table, gang, item), audit.operation(identity, 'gang.configured', gang, item)])
        return clean(item)
    if not active_on(gang, day):
        raise ValueError('Set up the gang size and daily rate, and choose a date on or after its headcount start date')
    if not pid:
        raise ValueError('Choose a project for this gang entry')
    allocation_key = {'PK': f'ORG#{org}', 'SK': f'GANG-ALLOCATION#{gid}#{day}'}
    allocation = read(table, allocation_key)
    if operation == 'gang_allocate':
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        if allocation.get('attendanceStarted'):
            raise ValueError('Attendance has started; this date\'s gang allocation is locked')
        if body.get('expectedVersion') != allocation.get('version', 0):
            raise ValueError('Gang allocation changed. Refresh before assigning.')
        item = {**allocation_key, 'entityType': 'gang-allocation', 'orgId': org, 'projectId': pid,
                'gangId': gid, 'date': day, 'size': gang['size'], 'rate': gang['rate'],
                'version': allocation.get('version', 0) + 1, 'confirmedAt': now(), 'confirmedBy': identity.user_id}
        storage.transact(table, [versioned_put(table, allocation, item), audit.operation(identity, 'gang.allocated', allocation or None, item)])
        return clean(item)
    if operation == 'gang_attendance':
        require_role(identity, SUPERVISOR)
        if allocation.get('projectId') != pid:
            raise AuthorizationError('Ask the admin to assign this gang to this project and date first')
        present = count(body.get('present'), 'People present')
        half = count(body.get('halfDay', 0), 'Half days')
        night = count(body.get('nightCount', 0), 'Night shifts')
        if present + half > allocation['size'] or night > present:
            raise ValueError('Attendance cannot exceed gang size; night shifts must be among the people present for a full day')
        key = {'PK': pk, 'SK': f'GANG-DAILY#{day}#{gid}'}
        before = read(table, key)
        if body.get('expectedVersion') != before.get('version', 0):
            raise ValueError('Gang attendance changed. Refresh before saving.')
        reason = body.get('correctionReason', '').strip() if isinstance(body.get('correctionReason', ''), str) else ''
        if before and (not reason or len(reason) > 500):
            if all(before.get(field, 0) == value for field, value in [('present', present), ('halfDay', half), ('nightCount', night)]):
                return clean(before)
            raise ValueError('Enter a correction reason of up to 500 characters')
        person_days = present + Decimal('.5') * half
        wage = ((person_days + night) * allocation['rate']).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        item = {**key, 'entityType': 'gang-daily', 'gangId': gid, 'gangName': gang['name'], 'orgId': org, 'projectId': pid,
                'date': day, 'present': present, 'halfDay': half, 'nightCount': night, 'absent': allocation['size'] - present - half,
                'size': allocation['size'], 'rate': allocation['rate'], 'personDays': person_days, 'wage': wage,
                'version': before.get('version', 0) + 1, 'updatedAt': now(), 'updatedBy': identity.user_id}
        cycle_key = {'PK': pk, 'SK': f'PAYROLL#{day[:7]}'}
        if read(get_table('PAYROLL_TABLE'), cycle_key):
            raise ValueError('Payroll is already generated. Ask the admin to reopen the unpaid cycle before correcting attendance.')
        saved_account = account(table, org, gid)
        storage.transact(table, [
            versioned_put(table, before, item),
            {'Update': {'TableName': table.name, 'Key': allocation_key, 'UpdateExpression': 'SET attendanceStarted = :yes',
                        'ConditionExpression': '#v = :v AND projectId = :pid', 'ExpressionAttributeNames': {'#v': 'version'},
                        'ExpressionAttributeValues': {':v': allocation['version'], ':pid': pid, ':yes': True}}},
            {'ConditionCheck': {'TableName': get_table('PAYROLL_TABLE').name, 'Key': cycle_key, 'ConditionExpression': 'attribute_not_exists(PK)'}},
            {'Update': {'TableName': table.name, 'Key': {'PK': pk, 'SK': f'GANG-MONTH#{day[:7]}#{gid}'},
                        'UpdateExpression': 'SET gangId = :gid, gangName = :name ADD grossWages :wage, personDays :days, nightShifts :nights',
                        'ExpressionAttributeValues': {':gid': gid, ':name': gang['name'], ':wage': wage - before.get('wage', 0),
                                                      ':days': person_days - before.get('personDays', 0), ':nights': night - before.get('nightCount', 0)}}},
            {'Update': {'TableName': table.name, 'Key': {'PK': pk, 'SK': f'MONTH#{day[:7]}'},
                        'UpdateExpression': 'ADD revision :one', 'ExpressionAttributeValues': {':one': 1}}},
            account_change(table, org, gid, saved_account, earned=wage - before.get('wage', 0)),
            audit.operation(identity, 'gang.attendance-corrected' if before else 'gang.attendance-recorded', before or None, item, reason or None),
        ])
        return clean(item)
    if operation not in {'gang_advance', 'gang_advance_return'}:
        raise ValueError('Unknown gang operation')
    require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
    amount = money(body.get('amount'), 'Amount')
    description = body.get('description')
    if not isinstance(description, str) or not description.strip() or len(description.strip()) > 500:
        raise ValueError('Enter a reason or reference of up to 500 characters')
    request_id = body.get('requestId')
    if not isinstance(request_id, str) or not re_id(request_id):
        raise ValueError('A valid requestId is required')
    identifier = model.new_id('record', request_id)
    # The organization receipt binds retries across projects and dates.
    receipt_key = {'PK': f'ORG#{org}', 'SK': f'GANG-CASH-REQUEST#{identifier}'}
    action = {'gangId': gid, 'projectId': pid, 'date': day, 'operation': operation,
              'amount': amount, 'description': description.strip()}
    receipt = read(table, receipt_key)
    if receipt:
        if receipt['action'] != action:
            raise ValueError('Request ID already used for a different gang cash entry')
        return clean(receipt['result'])
    saved = account(table, org, gid)
    if operation == 'gang_advance_return' and amount > balance(saved)['advanceBalance'] - saved.get('reserved', 0):
        raise ValueError('Return cannot exceed the unreserved advance balance. Reopen unpaid payroll first if needed.')
    item = {'PK': pk, 'SK': f'GANG-CASH#{gid}#{day}#{identifier}', 'entityType': 'gang-cash',
            **action, 'cashId': identifier, 'gangName': gang['name'], 'orgId': org,
            'cashType': 'Advance given' if operation == 'gang_advance' else 'Advance returned',
            'createdAt': now(), 'createdBy': identity.user_id}
    history = {**item, 'PK': f'ORG#{org}', 'SK': f'GANG-HISTORY#{gid}#{item["createdAt"]}#{identifier}', 'entityType': 'gang-history'}
    changes = {'given': amount} if operation == 'gang_advance' else {'returned': amount}
    storage.transact(table, [
        {'Put': {'TableName': table.name, 'Item': item, 'ConditionExpression': 'attribute_not_exists(PK)'}},
        {'Put': {'TableName': table.name, 'Item': {**receipt_key, 'action': action, 'result': item}, 'ConditionExpression': 'attribute_not_exists(PK)'}},
        {'Put': {'TableName': table.name, 'Item': history, 'ConditionExpression': 'attribute_not_exists(PK)'}},
        account_change(table, org, gid, saved, **changes),
        audit.operation(identity, 'gang.cash-recorded', after=item),
    ])
    return clean(item)


def payroll_entries(table, org, pk, month):
    entries, operations = [], []
    for totals in rows(table, pk, f'GANG-MONTH#{month}#'):
        gid = totals['gangId']
        saved = account(table, org, gid)
        available = balance(saved)['advanceBalance'] - saved.get('reserved', 0)
        recovery = min(available, totals.get('grossWages', 0))
        entries.append({'labourId': gid, 'gangId': gid, 'gangName': totals['gangName'], 'name': totals['gangName'],
                        'headcountGang': True, 'type': 'Gang headcount', 'daysPresent': totals.get('personDays', 0),
                        'nightShifts': totals.get('nightShifts', 0), 'gross': totals.get('grossWages', 0),
                        'deductions': recovery, 'advanceRecovery': recovery, 'dailyPaid': 0,
                        'carryForward': available - recovery, 'net': totals.get('grossWages', 0) - recovery})
        # Even a zero recovery checks the balance used for the snapshot.
        operations.append(account_change(table, org, gid, saved, reserved=recovery))
    return entries, operations


def payroll_account_operations(table, identity, cycle, members, settling):
    operations = []
    for member in members:
        if not member.get('headcountGang'):
            continue
        gid = member['gangId']
        saved = account(table, cycle['orgId'], gid)
        recovery = member.get('advanceRecovery', 0)
        if saved.get('reserved', 0) < recovery:
            raise ValueError('Gang advance reservation is missing. Refresh and review payroll.')
        changes = {'reserved': -recovery}
        if settling:
            changes.update(recovered=recovery, paid=member['net'])
        operations.append(account_change(table, cycle['orgId'], gid, saved, **changes))
        if settling:
            stamp = now()
            history = {'PK': f'ORG#{cycle["orgId"]}', 'SK': f'GANG-HISTORY#{gid}#{stamp}#{cycle["cycleId"]}',
                       'entityType': 'gang-history', 'orgId': cycle['orgId'], 'projectId': cycle['projectId'], 'gangId': gid,
                       'date': stamp[:10], 'cashType': 'Wages paid', 'amount': member['net'], 'advanceRecovery': recovery,
                       'description': f'{cycle["month"]} payroll · {cycle.get("paidGroups", {}).get(gid, {}).get("reference", "")}',
                       'createdAt': stamp, 'createdBy': identity.user_id}
            operations.append({'Put': {'TableName': table.name, 'Item': history, 'ConditionExpression': 'attribute_not_exists(PK)'}})
    return operations
