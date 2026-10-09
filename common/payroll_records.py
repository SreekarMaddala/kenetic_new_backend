"""Immutable employee entries; only published cycle headers are visible."""
import hashlib
import json
import time
import uuid
from decimal import Decimal


def digest_members(staff):
    def canonical(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, Decimal)):
            return format(Decimal(value).normalize(), 'f')
        if isinstance(value, dict):
            return {key: canonical(member) for key, member in value.items()}
        if isinstance(value, list):
            return [canonical(member) for member in value]
        return value
    return hashlib.sha256(json.dumps(canonical(staff), sort_keys=True).encode()).hexdigest()


def save_entries(table, cycle):
    staff = sorted(cycle.pop('staff'), key=lambda member: member['labourId'])
    cycle['entryCount'] = len(staff)
    cycle['entriesDigest'] = digest_members(staff)
    build = {'PK': cycle['PK'], 'SK': f"PAYROLL-BUILD#{cycle['cycleId']}",
             'entityType': 'payroll-build', 'orgId': cycle['orgId'], 'projectId': cycle['projectId'],
             'cycleId': cycle['cycleId'], 'month': cycle['month'], 'state': 'Building',
             'owner': uuid.uuid4().hex, 'leaseUntil': int(time.time()) + 600,
             'entryCount': len(staff), 'entriesDigest': cycle['entriesDigest'], 'createdAt': cycle['createdAt']}
    table.put_item(Item=build, ConditionExpression='attribute_not_exists(PK)')
    operations = []
    byte_count = 0
    def flush():
        # Every chunk verifies the lease, so cleanup cannot race staging.
        table.meta.client.transact_write_items(TransactItems=operations + [lease_check(table, build)])
        table.update_item(Key={'PK': build['PK'], 'SK': build['SK']},
                          UpdateExpression='SET leaseUntil = :until', ConditionExpression='#state = :building AND #owner = :owner',
                          ExpressionAttributeNames={'#state': 'state', '#owner': 'owner'},
                          ExpressionAttributeValues={':until': int(time.time()) + 600, ':building': 'Building', ':owner': build['owner']})
    for member in staff:
        item = {
            'PK': cycle['PK'], 'SK': f"PAYROLL-ENTRY#{cycle['cycleId']}#{member['labourId']}",
            'orgId': cycle['orgId'], 'projectId': cycle['projectId'],
            'entityType': 'payroll-entry', 'cycleId': cycle['cycleId'], 'member': member,
            'memberDigest': digest_members([member]),
        }
        size = len(json.dumps(item, default=str).encode())
        if size > 250_000:
            raise ValueError('Payroll employee snapshot is too large')
        if operations and (len(operations) >= 80 or byte_count + size > 2_500_000):
            flush()
            operations = []
            byte_count = 0
        operations.append({'Put': {'TableName': table.name, 'Item': item, 'ConditionExpression': 'attribute_not_exists(PK)'}})
        byte_count += size
    if operations:
        flush()
    return build


def lease_check(table, build):
    return {'ConditionCheck': {'TableName': table.name, 'Key': {'PK': build['PK'], 'SK': build['SK']},
                              'ConditionExpression': '#state = :building AND #owner = :owner AND leaseUntil >= :now',
                              'ExpressionAttributeNames': {'#state': 'state', '#owner': 'owner'},
                              'ExpressionAttributeValues': {':building': 'Building', ':owner': build['owner'], ':now': int(time.time())}}}


def publish_operation(table, build):
    check = lease_check(table, build)['ConditionCheck']
    check.update(UpdateExpression='SET #state = :published REMOVE leaseUntil',
                 ExpressionAttributeValues={**check['ExpressionAttributeValues'], ':published': 'Published'})
    return {'Update': check}


def cleanup(table, pk, cycle_id, apply=False, identity=None):
    """Claim an abandoned build before deleting entries; safe to resume."""
    from common.workflow_helpers import rows
    key = {'PK': pk, 'SK': f'PAYROLL-BUILD#{cycle_id}'}
    build = table.get_item(Key=key, ConsistentRead=True).get('Item')
    if not build:
        raise ValueError('Payroll build does not exist')
    if build['state'] == 'Published':
        raise ValueError('Published and historical payroll entries must be retained')
    if build['state'] == 'Cleaned':
        return {'cycleId': cycle_id, 'state': 'Cleaned', 'deleted': 0}
    if int(build.get('leaseUntil', 0)) >= int(time.time()):
        raise ValueError('Payroll build still has an active lease')
    if not apply:
        return {'cycleId': cycle_id, 'state': build['state'], 'entries': len(rows(table, pk, f'PAYROLL-ENTRY#{cycle_id}#'))}
    owner = uuid.uuid4().hex
    table.update_item(Key=key, UpdateExpression='SET #state = :cleaning, #owner = :owner, leaseUntil = :until',
                      ConditionExpression='#state <> :published AND (attribute_not_exists(leaseUntil) OR leaseUntil < :now)',
                      ExpressionAttributeNames={'#state': 'state', '#owner': 'owner'},
                      ExpressionAttributeValues={':cleaning': 'Cleaning', ':published': 'Published', ':owner': owner, ':until': int(time.time()) + 600, ':now': int(time.time())})
    deleted = 0
    for start_page in range(100000):
        from boto3.dynamodb.conditions import Key
        entries = table.query(KeyConditionExpression=Key('PK').eq(pk) & Key('SK').begins_with(f'PAYROLL-ENTRY#{cycle_id}#'), ConsistentRead=True, Limit=80)['Items']
        if not entries:
            break
        operations = [{'Delete': {'TableName': table.name, 'Key': {k: entry[k] for k in ('PK', 'SK')}}} for entry in entries]
        operations.append({'Update': {'TableName': table.name, 'Key': key, 'UpdateExpression': 'SET leaseUntil = :until',
                                       'ConditionExpression': '#state = :cleaning AND #owner = :owner AND leaseUntil >= :now',
                                       'ExpressionAttributeNames': {'#state': 'state', '#owner': 'owner'},
                                       'ExpressionAttributeValues': {':cleaning': 'Cleaning', ':owner': owner, ':now': int(time.time()), ':until': int(time.time()) + 600}}})
        if identity:
            from common import audit
            operations.append(audit.operation(identity, 'payroll.build-cleanup', after={**key, 'orgId': build['orgId'], 'projectId': build['projectId'], 'cycleId': cycle_id, 'deletedEntries': len(entries)}))
        table.meta.client.transact_write_items(TransactItems=operations)
        deleted += len(entries)
    table.update_item(Key=key, UpdateExpression='SET #state = :cleaned REMOVE leaseUntil',
                      ConditionExpression='#owner = :owner AND #state = :cleaning',
                      ExpressionAttributeNames={'#state': 'state', '#owner': 'owner'},
                      ExpressionAttributeValues={':owner': owner, ':cleaned': 'Cleaned', ':cleaning': 'Cleaning'})
    return {'cycleId': cycle_id, 'state': 'Cleaned', 'deleted': deleted}


def expand(table, cycle):
    if not cycle:
        return cycle
    if 'entryCount' not in cycle or 'entriesDigest' not in cycle:
        raise ValueError('Payroll cycle is missing its immutable snapshot')
    from common.workflow_helpers import rows
    result = dict(cycle)
    result['staff'] = [entry['member'] for entry in rows(table, cycle['PK'], f"PAYROLL-ENTRY#{cycle['cycleId']}#")]
    if len(result['staff']) != cycle['entryCount']:
        raise ValueError('Payroll snapshot entries are incomplete')
    digest = digest_members(result['staff'])
    if digest != cycle['entriesDigest']:
        raise ValueError('Payroll snapshot integrity check failed')
    return result


def employee_entry(table, cycle, employee_id):
    item = table.get_item(Key={'PK': cycle['PK'], 'SK': f"PAYROLL-ENTRY#{cycle['cycleId']}#{employee_id}"}, ConsistentRead=True).get('Item')
    if not item:
        return None
    if digest_members([item['member']]) != item['memberDigest']:
        raise ValueError('Payroll employee entry integrity check failed')
    return item['member']
