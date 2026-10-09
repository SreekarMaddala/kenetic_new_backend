"""Period filtering, spending summaries and report data."""
from decimal import Decimal
from boto3.dynamodb.conditions import Key
from common.authz import OPERATIONS_ADMIN, SUPER_ADMIN, require_role
from datetime import date
from common.dynamo import get_table
from common import pagination, storage
from common.workflow_helpers import _batch_get, clean, material_kind, rows


def report_period(query):
    start, end = query.get("startDate"), query.get("endDate")
    if not start and not end:
        return None
    if not start or not end:
        raise ValueError("Choose both a start and end date")
    if len(start) != 10 or len(end) != 10 or date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Choose a valid date range with start date before end date")
    return start, end


def project_totals(projects, org, period=None):
    table = get_table('REPORTING_TABLE')
    summaries = _batch_get(table, [{'PK': f'ORG#{org}', 'SK': f"SPENDING#{project['projectId']}"} for project in projects]) if period is None else {}
    result = []
    for project in projects:
        pid = project['projectId']
        if period is None:
            summary = summaries.get((f'ORG#{org}', f'SPENDING#{pid}'), {})
            spent = summary.get('amount', 0) + project.get('openingSpent', project.get('spent', 0))
        else:
            pages = {'KeyConditionExpression': Key('PK').eq(f'ORG#{org}#PROJECT#{pid}') & Key('SK').between(f'DAY#{period[0]}', f'DAY#{period[1]}'), 'ConsistentRead': True}
            spent = 0
            while True:
                page = table.query(**pages)
                spent += sum(row.get('amount', 0) for row in page.get('Items', []))
                if not page.get('LastEvaluatedKey'):
                    break
                pages['ExclusiveStartKey'] = page['LastEvaluatedKey']
        result.append(dict(project, spent=spent))
    return result


def spending_ledger(identity, org, pid):
    require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
    result = []
    labels = {"expense": "Direct Expense", "payment": "Vendor Payment", "payroll": "Payroll Settlement", "daily-wage": "Daily Wage Payment"}
    sources = ([item for env in ('FINANCE_TABLE', 'WORKFORCE_TABLE', 'PAYROLL_TABLE') for item in rows(get_table(env), f'ORG#{org}#PROJECT#{pid}', '') if storage.contribution(item)])
    for item in sources:
        if item.get("projectId") != pid:
            continue
        kind = item["entityType"]
        result.append({"id": item["SK"], "source": labels[kind], "date": str(item.get("date") or item.get("paidAt") or item.get("approvedAt") or item.get("createdAt") or "")[:10],
                       "amount": item.get("wage", 0) if kind == "daily-wage" else item.get("amount", 0),
                       "description": item.get("description") or item.get("vendorName") or item.get("month") or item.get("labourId") or labels[kind],
                       "status": "Paid" if kind in {"payroll", "daily-wage"} else "Approved", "projectId": pid})
    project = get_table("PROJECTS_TABLE").get_item(Key={"PK": f"ORG#{org}", "SK": f"PROJECT#{pid}"}, ConsistentRead=True)["Item"]
    opening = project.get("openingSpent", project.get("spent", 0))
    if opening:
        result.append({"id": "opening", "source": "Opening Balance", "date": "", "amount": opening, "description": "Historical opening spending", "status": "Posted", "projectId": pid})
    return result


def approval_queue(org):
    candidates = [item for env in ('FINANCE_TABLE', 'MATERIALS_TABLE', 'FIELD_OPERATIONS_TABLE', 'PARTIES_TABLE')
                  for item in pagination.page(get_table(env), {'limit': '25'}, ['approval-preview', org, env], org=org, approval=True)[0]]
    result = []
    for item in candidates:
        if item.get('orgId') != org:
            continue
        resource = item.get("entityType")
        if resource not in {"payment", "bill", "expense", "material", "dpr", "subcontractor"}:
            continue
        if resource == "material" and material_kind(item) != "indents":
            continue
        if resource == "subcontractor" and item.get("type") != "Procurement":
            continue
        if str(item.get("status", "Pending")).lower() == "pending":
            result.append(dict(clean(item), resource=resource))
    return result


def expense_total(org, period=None):
    table = get_table('REPORTING_TABLE')
    if period is None:
        return table.get_item(Key={'PK': 'ORG#' + org, 'SK': 'SUMMARY#EXPENSES'}, ConsistentRead=True).get('Item', {}).get('amount', 0)
    args = {'KeyConditionExpression': Key('PK').eq('ORG#' + org + '#EXPENSES') & Key('SK').between('DAY#' + period[0], 'DAY#' + period[1]), 'ConsistentRead': True}
    result = Decimal(0)
    while True:
        page = table.query(**args)
        result += sum(row.get('amount', 0) for row in page['Items'])
        if not page.get('LastEvaluatedKey'):
            return result
        args['ExclusiveStartKey'] = page['LastEvaluatedKey']
