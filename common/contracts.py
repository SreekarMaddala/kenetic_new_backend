"""Authoritative request contracts. Exportable as JSON for clients/docs."""
from datetime import date
from decimal import Decimal
import re
import hashlib
import json
from common import model


def fields(text='', numbers='', dates='', ids=None, enums=None, booleans=''):
    result = {key: {'type': 'string', 'maxLength': 4000} for key in text.split()}
    result.update({key: {'type': 'number', 'minimum': 0, 'maximum': 10**12} for key in numbers.split()})
    result.update({key: {'type': 'string', 'format': 'date'} for key in dates.split()})
    result.update({key: {'type': 'string', 'entity': entity} for key, entity in (ids or {}).items()})
    result.update({key: {'type': 'string', 'enum': values} for key, values in (enums or {}).items()})
    result.update({key: {'type': 'boolean'} for key in booleans.split()})
    return result


SCHEMAS = {
    'organization': fields('name type plan taxId contactEmail contactPhone adminEmail adminName', enums={'status': ['Active', 'Suspended', 'Archived']}),
    'user': fields('name email department phone location', enums={'role': ['super_admin', 'operations_admin', 'supervisor'], 'status': ['Active', 'Disabled']}),
    'project': fields('name location phase image supervisor', 'budget spent progress', 'startDate endDate', enums={'status': ['Active', 'Planning', 'Planned', 'In Progress', 'On Hold', 'Completed', 'Archived']}),
    'boq': fields('code description category unit', 'budgetedQty rate amount', enums={'status': ['Active', 'Archived']}),
    'milestone': fields('title owner description', 'progress', 'targetDate', enums={'status': ['Pending', 'In Progress', 'Completed', 'Archived']}),
    'subcontractor': fields('name contactPerson phone email trade address scopeOfWork unit description', 'contractValue quantity', 'startDate endDate requiredDate', ids={'registryId': 'subcontractor', 'contractorId': 'subcontractor'}, enums={'type': ['Contractor', 'Procurement'], 'status': ['Active', 'Inactive', 'Draft', 'Pending', 'Approved', 'Rejected']}),
    'attendance': {'location': {'type': 'object', 'properties': fields(numbers='latitude longitude accuracy'), 'required': ['latitude', 'longitude', 'accuracy']}},
    'labour-attendance': fields('name type bankName accNo description correctionReason', 'rate amount', 'date', ids={'workerId': 'worker', 'labourAttendanceId': 'worker', 'targetProjectId': 'project'}, enums={'operation': ['register', 'allocate', 'attendance', 'debit'], 'status': ['Present', 'Half Day', 'Absent'], 'paymentStatus': ['Paid', 'Not paid']}, booleans='nightShift'),
    'dpr': fields('summary weather', 'labourCount', 'date', enums={'status': ['Draft', 'Pending', 'Approved', 'Rejected']}),
    'material': fields('materialName name unit priority description remarks', 'quantity qty', 'requiredDate date', enums={'status': ['pending', 'approved', 'rejected', 'issued', 'Pending', 'Approved', 'Rejected', 'Issued']}),
    'logistics-trip': fields('vehicle driver vendor material notes receipt operator registrationNumber type capacity', 'startKm endKm liters rate helper total', 'date', enums={'tripType': ['vehicle_registration', 'vehicle', 'fuel', 'rental'], 'status': ['Active', 'Inactive', 'Archived']}),
    'issue': fields('title description', enums={'priority': ['Low', 'Medium', 'High', 'Critical'], 'status': ['Open', 'In Progress', 'Resolved', 'Closed', 'Archived']}),
    'inspection': fields('title type inspector notes', dates='date', enums={'status': ['Pending', 'Passed', 'Failed', 'Archived']}),
    'equipment': fields('name code location category', enums={'status': ['Available', 'Deployed', 'Maintenance', 'Archived']}),
    'drawing': fields('title category revision filename storageKey contentType', 'size', enums={'status': ['Active', 'Archived']}),
    'document': fields('title category revision filename storageKey contentType', 'size', enums={'status': ['Active', 'Archived']}),
    'vendor': fields('name type email phone', ids={}, enums={'status': ['Active', 'Inactive']}),
    'inventory-item': fields('name category unit', 'reorderLevel', enums={'status': ['Active', 'Inactive']}),
    'warehouse-event': fields('item materialName unit vendor issuedTo', 'qty quantity rate', 'date', ids={'targetProjectId': 'project', 'indentId': 'material'}),
    'bill': fields('billNumber clientOrContractor type remarks adjustmentReason', 'grossAmount netPayable retentionDeduction tdsDeduction', 'date periodFrom periodTo', ids={'vendorId': 'vendor', 'documentId': 'document'}, enums={'status': ['Draft', 'Pending', 'Approved', 'Rejected']}),
    'expense': fields('category description submittedBy', 'amount', 'date', ids={'supervisorId': 'user'}, enums={'status': ['Draft', 'Pending', 'Approved', 'Rejected']}),
    'payment': fields('description reference', 'amount', 'date', ids={'vendorId': 'vendor', 'materialId': 'inventory-item', 'billId': 'bill'}, enums={'mode': ['NEFT', 'RTGS', 'Cheque', 'Cash', 'UPI'], 'status': ['Draft', 'Pending', 'Approved', 'Rejected']}),
    'payroll': fields('month reference expectedCreatedAt', ids={'employeeId': 'user'}, numbers='basic hra conveyance medical pf esi tds'),
    'setting': fields('companyName cin primaryEmail phone address plan timezone currency', booleans='emailNotifications pushNotifications approvalNotifications'),
}
SCHEMAS['project']['supervisorIds'] = {'type': 'array', 'entity': 'user', 'maxItems': 80}
SCHEMAS['vendor']['materialIds'] = {'type': 'array', 'entity': 'inventory-item', 'maxItems': 80}
# Geographic coordinates have their own bounds.
SCHEMAS['attendance']['location']['properties']['latitude'].update(minimum=-90, maximum=90)
SCHEMAS['attendance']['location']['properties']['longitude'].update(minimum=-180, maximum=180)
REQUIRED = {
    'organization': ['name'], 'user': ['name', 'email', 'role'], 'project': ['name', 'budget'],
    'boq': ['code', 'description', 'category', 'unit', 'budgetedQty', 'rate'],
    'milestone': ['title', 'targetDate', 'owner', 'progress'], 'attendance': ['location'],
    'dpr': ['summary', 'date'], 'material': ['materialName', 'quantity', 'unit'],
    'issue': ['title', 'description'], 'inspection': ['title', 'type', 'inspector', 'date'],
    'equipment': ['name', 'code', 'location', 'category'], 'drawing': ['title', 'storageKey'],
    'document': ['title', 'storageKey'], 'vendor': ['name', 'materialIds'],
    'inventory-item': ['name', 'unit'], 'warehouse-event': ['item', 'unit', 'qty', 'requestId'],
    'bill': ['billNumber', 'clientOrContractor', 'grossAmount', 'requestId'],
    'expense': ['description', 'amount', 'requestId'], 'payment': ['vendorId', 'materialId', 'amount', 'mode', 'reference', 'requestId'],
}
COMMON = fields(ids={'orgId': 'organization', 'projectId': 'project'})
COMMON['requestId'] = {'type': 'string', 'pattern': r'[A-Za-z0-9_-]{8,80}'}
COMMON['expectedVersion'] = {'type': 'integer', 'minimum': 0}
ID_FIELDS = {'organization': 'orgId', 'user': 'employeeId', 'project': 'projectId', 'milestone': 'milestoneId', 'boq': 'boqId', 'subcontractor': 'subcontractorId', 'attendance': 'attendanceId', 'dpr': 'dprId', 'material': 'materialId', 'logistics-trip': 'tripId', 'issue': 'issueId', 'inspection': 'inspectionId', 'equipment': 'equipmentId', 'drawing': 'drawingId', 'document': 'documentId', 'vendor': 'vendorId', 'inventory-item': 'itemId', 'bill': 'billId', 'expense': 'expenseId', 'payment': 'paymentId'}


def check_value(key, value, spec):
    kind = spec['type']
    if kind in {'number', 'integer'}:
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or not Decimal(value).is_finite():
            raise ValueError(f'{key} must be a finite number')
        if kind == 'integer' and value != int(value):
            raise ValueError(f'{key} must be an integer')
        if value < spec.get('minimum', 0) or value > spec.get('maximum', 10**12):
            raise ValueError(f'{key} is outside its permitted range')
    elif kind == 'string':
        if not isinstance(value, str) or len(value) > spec.get('maxLength', 4000):
            raise ValueError(f'{key} must be a string within the permitted length')
        if value and spec.get('entity'):
            model.validate_id(spec['entity'], value)
        if spec.get('enum') and value not in spec['enum']:
            raise ValueError(f'Invalid {key}')
        if value and spec.get('format') == 'date':
            if len(value) != 10:
                raise ValueError(f'{key} must use YYYY-MM-DD')
            date.fromisoformat(value)
        if spec.get('pattern') and not re.fullmatch(spec['pattern'], value):
            raise ValueError(f'Invalid {key}')
        if value and ('email' in key.lower()) and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError(f'Invalid {key}')
    elif kind == 'boolean' and not isinstance(value, bool):
        raise ValueError(f'{key} must be boolean')
    elif kind == 'array':
        if not isinstance(value, list) or len(value) > spec.get('maxItems', 100) or any(not isinstance(x, str) for x in value) or len(set(value)) != len(value):
            raise ValueError(f'{key} must contain unique IDs within its item limit')
        for entry in value:
            model.validate_id(spec['entity'], entry)
    elif kind == 'object':
        if not isinstance(value, dict) or set(value) - set(spec['properties']):
            raise ValueError(f'Invalid {key} object')
        for name in spec.get('required', []):
            if name not in value:
                raise ValueError(f'{key}.{name} is required')
        for name, member in value.items():
            check_value(f'{key}.{name}', member, spec['properties'][name])


def request_schema(resource, path, method):
    if resource == 'material' and ('/grn' in path or '/stock' in path):
        resource = 'warehouse-event'
    props = {**COMMON, **SCHEMAS[resource]}
    if resource in ID_FIELDS:
        props[ID_FIELDS[resource]] = {'type': 'string', 'entity': resource}
    required = list(REQUIRED.get(resource, [])) if method == 'POST' else []
    if path.endswith('/upload'):
        return fields('name contentType', 'size'), ['name', 'size', 'contentType']
    if path.endswith('/extract') or path.endswith('/invitation'):
        return {}, []
    if path.endswith('/reverse'):
        return {**fields('reason reference'), **{k: COMMON[k] for k in ('requestId', 'expectedVersion')}}, ['reason', 'reference', 'requestId', 'expectedVersion']
    if resource == 'labour-attendance':
        required = ['name', 'rate'] if not (path and method == 'POST') else []
    if resource == 'payroll':
        required = ['employeeId', 'basic'] if path.endswith('/staff') and method == 'POST' else (['month'] if method == 'POST' else [])
        if path.endswith('/disburse'):
            required += ['reference', 'expectedCreatedAt']
    if resource == 'user' and method in {'PUT', 'PATCH'}:
        props = {key: props[key] for key in ('department', 'phone', 'location', 'status', 'expectedVersion')}
    return props, required


def validate(resource, path, method, body, existing=None):
    if (method not in {'POST', 'PUT', 'PATCH'}):
        return
    props, required = request_schema(resource, path, method)
    unknown = set(body) - set(props)
    if unknown:
        raise ValueError('Unsupported fields: ' + ', '.join(sorted(unknown)))
    for key, value in body.items():
        check_value(key, value, props[key])
    state = {**(existing or {}), **body}
    if existing and resource != 'user':
        required = [key for key in REQUIRED.get(resource, []) if key != 'requestId']
    for key in required:
        if key not in state or state[key] is None or (isinstance(state[key], str) and not state[key].strip()):
            raise ValueError(f'{key} is required')
    if resource == 'labour-attendance':
        operation = body.get('operation', 'attendance' if body.get('workerId') or body.get('labourAttendanceId') else 'register')
        necessary = {'register': ['name', 'rate'], 'allocate': ['date'], 'attendance': ['date', 'status'], 'debit': ['date', 'amount', 'description', 'requestId']}.get(operation)
        if necessary is None:
            raise ValueError('Invalid workforce operation')
        for key in necessary:
            if key not in body or body[key] == '':
                raise ValueError(f'{key} is required')
    if resource == 'logistics-trip' and method == 'POST':
        kind = body.get('tripType')
        necessary = {'vehicle_registration': ['vehicle'], 'vehicle': ['vehicle', 'date', 'startKm', 'endKm'], 'fuel': ['vehicle', 'date', 'liters', 'rate'], 'rental': ['vendor', 'material', 'date', 'rate']}.get(kind)
        if necessary is None or any(key not in body or body[key] == '' for key in necessary):
            raise ValueError('Required transport fields are missing')
    if existing and resource in {'bill', 'payment'}:
        immutable = ('billNumber', 'vendorId', 'clientOrContractor') if resource == 'bill' else ('vendorId', 'materialId', 'billId', 'mode', 'reference')
        if any(key in body and body[key] != existing.get(key) for key in immutable):
            raise ValueError('Financial identity fields cannot be changed; reject or reverse the record')
    if existing and existing.get('status') == 'Archived' and set(body) - {'status', 'expectedVersion'}:
        raise ValueError('Restore an archived record before editing it')
    if resource in {'expense', 'payment', 'bill'}:
        for key in ('amount', 'grossAmount', 'netPayable', 'retentionDeduction', 'tdsDeduction'):
            if key in body and Decimal(body[key]) != Decimal(body[key]).quantize(Decimal('0.01')):
                raise ValueError(f'{key} must have at most two decimal places')
    if resource == 'payroll':
        for key in ('basic', 'hra', 'conveyance', 'medical', 'pf', 'esi', 'tds'):
            if key in body and Decimal(body[key]) != Decimal(body[key]).quantize(Decimal('0.01')):
                raise ValueError(f'{key} must have at most two decimal places')


def export():
    return {resource: {'type': 'object', 'additionalProperties': False, 'properties': request_schema(resource, '', 'POST')[0], 'required': REQUIRED.get(resource, [])} for resource in SCHEMAS}


def fingerprint(body):
    def canonical(value):
        if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
            return format(Decimal(value).normalize(), 'f')
        if isinstance(value, dict):
            return {key: canonical(member) for key, member in value.items()}
        if isinstance(value, list):
            return [canonical(member) for member in value]
        return value
    return hashlib.sha256(json.dumps(canonical(body), sort_keys=True).encode()).hexdigest()
