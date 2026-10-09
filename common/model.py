"""Stable application identities and domain key conventions."""
import hashlib
import re
import uuid

PREFIXES = {
    'organization': 'org', 'user': 'emp', 'project': 'pro', 'worker': 'wor',
    'milestone': 'mil', 'boq': 'boq', 'subcontractor': 'sub', 'attendance': 'att',
    'labour-attendance': 'wor', 'daily-wage': 'att', 'dpr': 'dpr', 'material': 'mat',
    'logistics-trip': 'tri', 'issue': 'iss', 'inspection': 'ins', 'equipment': 'equ',
    'drawing': 'dra', 'document': 'doc', 'vendor': 'ven', 'inventory-item': 'itm',
    'warehouse-event': 'war', 'bill': 'bil', 'expense': 'exp', 'payment': 'pay',
    'payroll': 'pyr', 'audit-event': 'aud', 'deduction': 'ded', 'record': 'rec',
}
APPROVAL_TYPES = {'bill', 'expense', 'payment', 'material', 'dpr', 'subcontractor'}


def pending(item):
    return item.get('entityType') in APPROVAL_TYPES and str(item.get('status', '')).lower() == 'pending' and (item.get('entityType') != 'subcontractor' or item.get('type') == 'Procurement')


def new_id(entity, seed=None):
    prefix = PREFIXES[entity]
    suffix = uuid.uuid4().hex if seed is None else hashlib.sha256(str(seed).encode()).hexdigest()[:32]
    return f'{prefix}_{suffix}'


def validate_id(entity, value):
    if not isinstance(value, str) or not re.fullmatch(PREFIXES[entity] + r'_[0-9a-f]{32}', value):
        raise ValueError(f'{entity} ID must use {PREFIXES[entity]}_ followed by 32 lowercase hexadecimal characters')
    return value


def employee_id(sub):
    # Stable across role changes, while Cognito's signed subject stays untouched.
    return (new_id('user', sub))


def daily_key(worker, day):
    return (f'DAILY#{day}#{worker}')


def deduction_key(day, identifier):
    return (f'DEBIT#{day}#{identifier}')


def index_item(item):
    if (not item.get('orgId') or not item.get('entityType')):
        return item
    if item['entityType'] in {'audit-event', 'payroll-entry', 'payroll-history-entry'}:
        return item
    if item['entityType'] in {'organization', 'user'}:
        item['DirectoryPK'] = 'DIRECTORY#' + item['entityType']
        item['DirectorySK'] = f"ORG#{item['orgId']}#{item['SK']}"
    if pending(item):
        item['ApprovalPK'] = 'ORG#' + item['orgId']
        item['ApprovalSK'] = item['entityType'] + '#' + str(item.get('createdAt', '')) + '#' + item['SK']
    else:
        item.pop('ApprovalPK', None)
        item.pop('ApprovalSK', None)
    stamp = str(item.get('date') or item.get('month') or item.get('createdAt') or item.get('updatedAt') or '')[:10]
    item['OrgPK'] = f"ORG#{item['orgId']}"
    item['OrgSK'] = f"{item['entityType']}#{stamp}#{item.get('projectId', '')}#{item['SK']}"
    return item
