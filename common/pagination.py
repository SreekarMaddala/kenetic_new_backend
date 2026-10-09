"""Bounded DynamoDB collection pages and query-scoped continuation cursors."""
import base64
import hashlib
import json
import time
from boto3.dynamodb.conditions import Key


def page(table, query, scope, pk=None, prefix='', org=None, directory=None, approval=False):
    try:
        limit = int(query.get('limit', 50))
    except (ValueError, TypeError):
        raise ValueError('limit must be an integer from 1 to 100')
    if not 1 <= limit <= 100:
        raise ValueError('limit must be an integer from 1 to 100')
    fingerprint = hashlib.sha256(json.dumps([table.name, scope, pk, prefix, org, directory, approval], sort_keys=True).encode()).hexdigest()
    args = {'Limit': limit}
    if approval:
        args.update(IndexName='ApprovalIndex', KeyConditionExpression=Key('ApprovalPK').eq(f'ORG#{org}'))
    elif directory:
        args.update(IndexName='DirectoryIndex', KeyConditionExpression=Key('DirectoryPK').eq(directory))
    elif pk:
        args.update(KeyConditionExpression=Key('PK').eq(pk), ConsistentRead=True)
        if prefix:
            args['KeyConditionExpression'] &= Key('SK').begins_with(prefix)
    else:
        args.update(IndexName='OrgIndex', KeyConditionExpression=Key('OrgPK').eq(f'ORG#{org}'))
        if prefix:
            args['KeyConditionExpression'] &= Key('OrgSK').begins_with(prefix)
    if query.get('cursor'):
        try:
            if len(query['cursor']) > 3000:
                raise ValueError()
            cursor = json.loads(base64.urlsafe_b64decode(query['cursor'].encode()))
            key = cursor['key']
            if cursor['scope'] != fingerprint or not isinstance(key, dict) or not all(isinstance(v, str) for v in key.values()):
                raise ValueError()
            index_keys = {'ApprovalPK', 'ApprovalSK'} if approval else {'DirectoryPK', 'DirectorySK'} if directory else {'OrgPK', 'OrgSK'} if not pk else set()
            if set(key) != {'PK', 'SK'} | index_keys:
                raise ValueError()
            if pk and (key.get('PK') != pk or not key.get('SK', '').startswith(prefix)):
                raise ValueError()
            if org and not pk and key.get('ApprovalPK' if approval else 'OrgPK') != f'ORG#{org}':
                raise ValueError()
            if org and not pk and not approval and not key.get('OrgSK', '').startswith(prefix):
                raise ValueError()
            if directory and key.get('DirectoryPK') != directory:
                raise ValueError()
            args['ExclusiveStartKey'] = key
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ValueError('Invalid cursor for this collection')
    result = table.query(**args)
    entries = result.get('Items', [])
    if not pk and entries:
        pending = {table.name: {'Keys': [{k: entry[k] for k in ('PK', 'SK')} for entry in entries], 'ConsistentRead': True}}
        found = {}
        for attempt in range(6):
            batch = table.meta.client.batch_get_item(RequestItems=pending)
            found.update({(item['PK'], item['SK']): item for item in batch.get('Responses', {}).get(table.name, [])})
            pending = batch.get('UnprocessedKeys', {})
            if not pending:
                break
            if attempt == 5:
                raise ValueError('Database reads are throttled. Try again.')
            time.sleep(0.025 * 2**attempt)
        entries = [found[(item['PK'], item['SK'])] for item in entries if (item['PK'], item['SK']) in found]
    cursor = None
    if result.get('LastEvaluatedKey'):
        cursor = base64.urlsafe_b64encode(json.dumps({'scope': fingerprint, 'key': result['LastEvaluatedKey']}).encode()).decode()
    return entries, {'nextCursor': cursor, 'limit': limit}
