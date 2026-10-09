"""Dispatch validated requests to focused business modules."""
from common.dynamo import get_table
from common import model
from common.workflow_helpers import HttpError, clean, rows
from common.workflow_validation import validate_subcontractor
from common.finance import payment_material
from common.inventory import catalog, stock
from common.workforce import labour
from common.payroll import payroll
from common.documents import files


def handle(identity, domain, resource, method, path, body, query, pk, pid, org, rid):
    if resource == "subcontractor":
        if not path.startswith("/projects/") and pid:
            raise ValueError("Use the project subcontractors route for assignments")
        if method == "DELETE":
            raise ValueError("Deactivate subcontractors to preserve project history")
        if method == "POST":
            validate_subcontractor(body, org, pk, pid)
    if resource in {"inventory-item", "vendor"}:
        result = catalog(identity, resource, method, body, query, pk, org, pid, rid)
        if result is not None:
            return result
    if resource == "payment" and method == "POST" and not path.endswith('/reverse'):
        payment_material(body, org, pk)
    if resource == "labour-attendance":
        return labour(identity, method, body, query, pk, org, pid)
    if resource == "payroll":
        return payroll(identity, method, path, body, pk, pid, org, query)
    if resource == "warehouse-event" or (resource == "material" and ("/stock" in path or "/grn" in path)):
        return stock(identity, method, path, body, pk, org, pid)
    if resource in {"document", "drawing"}:
        return files(identity, method, path, body, pk, org, pid, resource, rid)
    if resource in {"payment", "expense"} and not pid and (method == "GET" or rid):
        table = get_table("FINANCE_TABLE")
        if (rid):
            model.validate_id(resource, rid)
            locator = table.get_item(Key={'PK': f'ORG#{org}', 'SK': f'LOOKUP#{resource}#{rid}'}, ConsistentRead=True).get('Item')
            record = table.get_item(Key={'PK': locator['recordPK'], 'SK': locator['recordSK']}, ConsistentRead=True).get('Item') if locator else None
            found = [record] if record and record.get('orgId') == org and record.get('entityType') == resource else []
        else:
            found = [r for r in rows(table, prefix=(resource + '#'), org=org) if r.get("entityType") == resource and (not rid or r.get(resource + "Id") == rid)]
        if not rid:
            return [clean(i) for i in found]
        if len(found) != 1:
            raise ValueError("Payment not found")
        item = found[0]
        if method == "GET":
            return clean(item)
        # Re-enter the scoped handler so normal validation and version checks run.
        from handlers.consolidated import _domain_handler
        if item.get("projectId"):
            event = {"rawPath": path, "body": body, "queryStringParameters": {"projectId": item["projectId"], "orgId": org},
                     "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": {"sub": identity.cognito_sub or identity.user_id, "custom:org_id": identity.organization_id, "cognito:groups": list(identity.roles)}}}}}
            response = _domain_handler(domain, event, None)
            payload = __import__('json').loads(response["body"])
            if response["statusCode"] >= 300:
                raise HttpError(response['statusCode'], payload)
            return payload["data"]
    return None
