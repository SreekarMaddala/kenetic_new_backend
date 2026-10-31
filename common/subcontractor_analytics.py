"""Company-wide subcontractor records joined by registry and project IDs."""
from decimal import Decimal
from common.dynamo import get_table
from common.workflow_helpers import rows


def totals():
    return dict(contractValue=Decimal(0), advancesPaid=Decimal(0), workPayments=Decimal(0),
                unclassifiedPaid=Decimal(0), totalPaid=Decimal(0), pendingPayments=Decimal(0))


def report(org):
    central = f'ORG#{org}'
    contractors = [row for row in rows(get_table('PARTIES_TABLE'), org=org, prefix='subcontractor#')
                   if row.get('orgId') == org and row.get('type') != 'Procurement']
    projects = {row['projectId']: row for row in rows(get_table('PROJECTS_TABLE'), central, 'PROJECT#')}
    profiles = {}
    for row in contractors:
        if row.get('projectId'):
            continue
        identifier = row['subcontractorId']
        profiles[identifier] = dict(subcontractorId=identifier, name=row.get('name', ''),
            contactPerson=row.get('contactPerson', ''), trade=row.get('trade', ''), status=row.get('status', ''),
            projects=[], **totals())
    assignments = {}
    for row in contractors:
        pid = row.get('projectId')
        if not pid:
            continue
        identifier = row.get('registryId') or row['subcontractorId']
        if identifier not in profiles:
            profiles[identifier] = dict(subcontractorId=identifier, name=row.get('name', ''),
                contactPerson=row.get('contactPerson', ''), trade=row.get('trade', ''), status=row.get('status', ''),
                projects=[], **totals())
        detail = dict(projectId=pid, projectName=projects.get(pid, {}).get('name', pid),
            scopeOfWork=row.get('scopeOfWork', ''), startDate=row.get('startDate', ''), endDate=row.get('endDate', ''),
            assignmentStatus=row.get('status', ''), payments=[], **totals())
        detail['contractValue'] = row.get('contractValue', Decimal(0))
        profiles[identifier]['projects'].append(detail)
        # The same contractor ID can be assigned in several projects.
        assignments[(pid, row['subcontractorId'])] = detail
    for payment in rows(get_table('FINANCE_TABLE'), org=org, prefix='payment#'):
        if payment.get('orgId') != org or not payment.get('contractorId'):
            continue
        detail = assignments.get((payment.get('projectId'), payment['contractorId']))
        if detail is None:
            continue
        amount = payment.get('amount', Decimal(0))
        status = str(payment.get('status', '')).lower()
        category = payment.get('paymentType')
        history = {key: payment.get(key, '') for key in
            ('paymentId', 'date', 'amount', 'paymentType', 'status', 'mode', 'reference', 'description')}
        history['date'] = history['date'] or ''
        detail['payments'].append(history)
        if status == 'approved':
            detail['totalPaid'] += amount
            field = 'advancesPaid' if category == 'Advance' else 'workPayments' if category == 'Payment' else 'unclassifiedPaid'
            detail[field] += amount
        elif status in {'draft', 'pending'}:
            detail['pendingPayments'] += amount
    for profile in profiles.values():
        for detail in profile['projects']:
            for field in totals():
                profile[field] += detail[field]
            detail['payments'].sort(key=lambda p: str(p.get('date') or ''), reverse=True)
        profile['projects'].sort(key=lambda p: p['projectName'].casefold())
        profile['projectCount'] = len(profile['projects'])
    return sorted(profiles.values(), key=lambda profile: profile['name'].casefold())
