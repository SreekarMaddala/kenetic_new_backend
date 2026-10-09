"""Payment approval, reversal and material references."""
from common.authz import OPERATIONS_ADMIN, SUPER_ADMIN, require_role
from common import audit, storage
from common.dynamo import get_table
from common.workflow_helpers import now
from common.workflow_validation import validate_record
from common.inventory import catalog_material


def approve_linked_payment(table, existing, body, identity):
    bill_key = {"PK": existing["PK"], "SK": f"BILL#{existing['billId']}"}
    bill = table.get_item(Key=bill_key, ConsistentRead=True).get("Item")
    if not bill or bill.get("status") != "Approved":
        raise ValueError("The linked bill must be approved")
    validate_record("bill", {}, bill)
    remaining_limit = bill.get("netPayable", bill.get("grossAmount", 0)) - existing["amount"]
    if remaining_limit < 0:
        raise ValueError("Payment exceeds the bill amount")
    version = existing.get("version", 1)
    updated = dict(existing, **body)
    updated["version"] = version + 1
    updated["updatedAt"] = now()
    bill_values = {":amount": existing["amount"], ":approved": "Approved", ":limit": remaining_limit}
    bill_condition = "#s = :approved AND (attribute_not_exists(paidAmount) OR paidAmount <= :limit)"
    if "paidAmount" in bill:
        bill_condition += " AND paidAmount = :prior"
        bill_values[":prior"] = bill["paidAmount"]
    else:
        bill_condition += " AND attribute_not_exists(paidAmount)"
    storage.transact(table, [
        {"Put": {"TableName": table.name, "Item": updated, "ConditionExpression": "#v = :v AND #s = :s", "ExpressionAttributeNames": {"#v": "version", "#s": "status"}, "ExpressionAttributeValues": {":v": version, ":s": existing["status"]}}},
        {"Update": {"TableName": table.name, "Key": bill_key, "UpdateExpression": "ADD paidAmount :amount", "ConditionExpression": bill_condition, "ExpressionAttributeNames": {"#s": "status"}, "ExpressionAttributeValues": bill_values}},
        audit.operation(identity, "payment.approved", existing, updated),
        audit.operation(identity, "bill.payment-recorded", bill, dict(bill, paidAmount=bill.get("paidAmount", 0) + existing["amount"])),
    ])
    return updated


def reverse_financial(table, identity, resource, existing, body):
    require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
    if (resource not in {'expense', 'payment'}):
        raise ValueError('Only approved expenses and payments can be reversed')
    if existing.get('status') == 'Reversed':
        if existing.get('reversalRequestId') == body['requestId'] and existing.get('reversalReason') == body['reason'].strip() and existing.get('reversalReference') == body['reference'].strip():
            return existing
        raise ValueError('This transaction was already reversed')
    if existing.get('status') != 'Approved' or existing.get('version') != body['expectedVersion']:
        raise ValueError('Review the current approved transaction before reversing it')
    if not body['reason'].strip() or not body['reference'].strip():
        raise ValueError('A reversal reason and external reference are required')
    updated = dict(existing, status='Reversed', version=existing['version'] + 1, updatedAt=now(),
                   reversedAt=now(), reversedBy=identity.user_id, reversalReason=body['reason'].strip(),
                   reversalReference=body['reference'].strip(), reversalRequestId=body['requestId'])
    operations = [{'Put': {'TableName': table.name, 'Item': updated, 'ConditionExpression': '#v = :version AND #s = :approved',
                            'ExpressionAttributeNames': {'#v': 'version', '#s': 'status'},
                            'ExpressionAttributeValues': {':version': existing['version'], ':approved': 'Approved'}}},
                  audit.operation(identity, resource + '.reversed', existing, updated, body['reason'])]
    if resource == 'payment' and existing.get('billId'):
        key = {'PK': existing['PK'], 'SK': f"BILL#{existing['billId']}"}
        bill = table.get_item(Key=key, ConsistentRead=True).get('Item')
        if not bill or bill.get('paidAmount', 0) < existing['amount']:
            raise ValueError('The bill payment total requires reconciliation before reversal')
        operations.append({'Update': {'TableName': table.name, 'Key': key, 'UpdateExpression': 'ADD paidAmount :delta',
                                      'ConditionExpression': 'paidAmount = :prior',
                                      'ExpressionAttributeValues': {':delta': -existing['amount'], ':prior': bill['paidAmount']}}})
        operations.append(audit.operation(identity, 'bill.payment-reversed', bill, dict(bill, paidAmount=bill['paidAmount'] - existing['amount'])))
    storage.transact(table, operations)
    return updated


def payment_material(body, org, pk):
    vendor = get_table("PARTIES_TABLE").get_item(Key={"PK": f"ORG#{org}", "SK": f"VENDOR#{body.get('vendorId', '')}"}, ConsistentRead=True).get("Item")
    if not vendor or vendor.get("orgId") != org:
        raise ValueError("Choose a vendor in this organization")
    item = catalog_material(org, body.get("materialId"))
    if item["itemId"] not in vendor.get("materialIds", []):
        raise ValueError("Assign this material to the selected vendor first")
    body.update(vendorName=vendor["name"], material=item["name"], materialUnit=item["unit"])
    if body.get("billId"):
        bill = get_table("FINANCE_TABLE").get_item(Key={"PK": pk, "SK": f"BILL#{body['billId']}"}, ConsistentRead=True).get("Item")
        if not bill or bill.get("status") != "Approved":
            raise ValueError("Choose an approved bill in this project")
