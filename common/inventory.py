"""Catalog materials, receipts and stock transfers."""
from common.authz import AuthorizationError, OPERATIONS_ADMIN, SUPER_ADMIN, require_role
from common import audit, model, storage
from common.dynamo import get_table
import time
from common.workflow_helpers import clean, material_kind, now, number, re_id, rows


def stock(identity, method, path, body, pk, org, pid):
    table = get_table("INVENTORY_TABLE")
    central = f"ORG#{org}"
    if path.endswith("/stock"):
        if method != "GET":
            raise ValueError("Stock is maintained by receipts and issues")
        return [dict(clean(i), materialId=i["itemId"], stock=i.get("quantity", 0), totalStock=i.get("quantity", 0)) for i in rows(table, pk, "BALANCE#")]
    kind = "issue-vouchers" if "/issue-vouchers" in path else "grn"
    if method == "GET":
        return [clean(i) for i in rows(table, central, f"WAREHOUSE-{kind.upper()}#") if not path.startswith("/supervisor/") or i.get("targetProjectId") == pid]
    require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
    if method != "POST":
        raise ValueError("Stock movements cannot be edited; record a correcting movement")
    name = " ".join(str(body.get("item", body.get("materialName", ""))).split())
    unit = " ".join(str(body.get("unit", "")).split())
    if not name or not unit:
        raise ValueError("Item and unit are required")
    qty = number(body.get("qty", body.get("quantity")), "Quantity", True)
    item_id = (model.new_id('inventory-item', f"{name.casefold()}|{unit.casefold()}"))
    request_id = body.get("requestId")
    if not isinstance(request_id, str) or not re_id(request_id):
        raise ValueError("A unique requestId is required")
    target = body.get("targetProjectId") or (pid if path.startswith("/supervisor/") else None)
    if kind == "issue-vouchers" and not target:
        raise ValueError("Destination project is required")
    if target:
        project = get_table("PROJECTS_TABLE").get_item(Key={"PK": central, "SK": f"PROJECT#{target}"}, ConsistentRead=True).get("Item")
        if not project or project.get("orgId") != org:
            raise AuthorizationError("Destination project is not accessible")
    movement_id = (model.new_id('warehouse-event', f'{kind}|{request_id}'))
    event = {"PK": central, "SK": f"WAREHOUSE-{kind.upper()}#{movement_id}", "warehouseEventId": movement_id,
             "entityType": "warehouse-event", "recordKind": kind, "orgId": org, "targetProjectId": target,
             "itemId": item_id, "item": name, "unit": unit, "qty": qty, "rate": number(body.get("rate", 0), "Rate"),
             "vendor": str(body.get("vendor", "")), "issuedTo": str(body.get("issuedTo", "")), "createdAt": now(), "date": body.get('date') or now()[:10], "createdBy": identity.user_id, 'requestId': request_id}
    event["amount"] = event["rate"] * qty
    existing = table.get_item(Key={"PK": central, "SK": event["SK"]}, ConsistentRead=True).get("Item")
    if existing:
        if any(existing.get(k) != event.get(k) for k in ("itemId", "qty", "targetProjectId", "rate", "vendor", "issuedTo")) or (body.get('date') and existing.get('date') != body['date']):
            raise ValueError("Request ID already used for a different movement")
        return clean(existing)
    operations = [{"Put": {"TableName": table.name, "Item": event, "ConditionExpression": "attribute_not_exists(PK)"}}]
    catalog_key = {'PK': central, 'SK': f'INVENTORY-ITEM#{item_id}'}
    catalog = table.get_item(Key=catalog_key, ConsistentRead=True).get('Item')
    if not catalog:
        if kind != 'grn':
            raise ValueError('Register the catalog material before issuing stock')
        catalog = {**catalog_key, 'entityType': 'inventory-item', 'itemId': item_id, 'orgId': org,
                   'name': name, 'unit': unit, 'category': 'General', 'status': 'Active',
                   'createdAt': now(), 'createdBy': identity.user_id, 'version': 1}
        operations.append({'Put': {'TableName': table.name, 'Item': catalog, 'ConditionExpression': 'attribute_not_exists(PK)'}})
        operations.append(audit.operation(identity, 'inventory-item.created', after=catalog))
    def balance(partition, delta, require_stock=False):
        values = {":q": delta, ":name": name, ":unit": unit, ":org": org, ":id": item_id}
        op = {"TableName": table.name, "Key": {"PK": partition, "SK": f"BALANCE#{item_id}"},
              "UpdateExpression": "SET #name = :name, #unit = :unit, orgId = :org, itemId = :id ADD quantity :q",
              "ExpressionAttributeNames": {"#name": "name", "#unit": "unit"}, "ExpressionAttributeValues": values}
        op['UpdateExpression'] = 'SET #name = :name, #unit = :unit, orgId = :org, itemId = :id, entityType = :kind, OrgPK = :opk, OrgSK = :osk ADD quantity :q'
        values.update({':kind': 'stock-balance', ':opk': f'ORG#{org}', ':osk': f'stock-balance##{partition}#{item_id}'})
        if require_stock:
            op["ConditionExpression"] = "quantity >= :required"
            values[":required"] = qty
        operations.append({"Update": op})
    if kind == "issue-vouchers":
        balance(central, -qty, True)
        balance(f"ORG#{org}#PROJECT#{target}", qty)
    else:
        balance(f"ORG#{org}#PROJECT#{target}" if target else central, qty)
        operations.append({'Update': {'TableName': table.name, 'Key': {'PK': central, 'SK': f'TOTAL#{item_id}'},
                                       'UpdateExpression': 'ADD quantity :quantity', 'ExpressionAttributeValues': {':quantity': qty}}})
    if body.get("indentId"):
        if kind != "issue-vouchers":
            raise ValueError("Only issue vouchers can fulfill material requests")
        request_table = get_table(("MATERIALS_TABLE"))
        request_key = {"PK": f"ORG#{org}#PROJECT#{target}", "SK": f"MATERIAL#{body['indentId']}"}
        indent = request_table.get_item(Key=request_key, ConsistentRead=True).get("Item")
        if not indent or material_kind(indent) != "indents" or str(indent.get("status", "")).lower() != "approved":
            raise ValueError("The material request must be approved first")
        if str(indent.get("materialName", "")).casefold() != name.casefold() or str(indent.get("unit", "")).casefold() != unit.casefold() or indent.get("quantity") != qty:
            raise ValueError("Issue the approved request's material, unit and full quantity")
        operations.append({"Update": {"TableName": request_table.name, "Key": request_key,
            "UpdateExpression": "SET #s = :issued, issuedAt = :at, issuedBy = :by",
            "ConditionExpression": "#s = :approved",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":issued": "issued", ":approved": indent["status"], ":at": now(), ":by": identity.user_id}}})
    # The resource client serializes native Decimal values, including transactions.
    operations.append({'Update': {'TableName': table.name, 'Key': {'PK': central, 'SK': 'CONTROL#STOCK'},
                                   'UpdateExpression': 'ADD revision :one',
                                   'ConditionExpression': 'attribute_not_exists(leaseUntil) OR leaseUntil < :now',
                                   'ExpressionAttributeValues': {':one': 1, ':now': int(time.time())}}})
    operations.append(audit.operation(identity, "stock.received" if kind == "grn" else "stock.issued", after=event))
    storage.transact(table, operations)
    return clean(event)


def catalog_identity(item):
    return tuple(" ".join(str(item.get(key, "")).split()).casefold() for key in ("name", "unit"))


def catalog_material(org, material_id):
    if not isinstance(material_id, str) or not material_id:
        raise ValueError("Select a material from the Inventory Catalog")
    item = get_table("INVENTORY_TABLE").get_item(Key={"PK": f"ORG#{org}", "SK": f"INVENTORY-ITEM#{material_id}"}, ConsistentRead=True).get("Item")
    if not item or item.get("orgId") != org:
        raise ValueError("Select a material from this organization's Inventory Catalog")
    return item


def vendor_materials(body, org):
    ids = body.get("materialIds", [])
    if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) or not i for i in ids):
        raise ValueError("Select up to 100 catalog materials")
    if len(set(ids)) != len(ids):
        raise ValueError("Each material can only be selected once")
    items = [catalog_material(org, i) for i in ids]
    body["materialIds"] = ids
    body["materialsSupplied"] = ", ".join(f"{i['name']} ({i['unit']})" for i in items)


def catalog(identity, resource, method, body, query, pk, org, pid, rid):
    if resource in {"inventory-item", "vendor"} and pid:
        raise ValueError("Manage catalog materials and vendors at organization level")
    if resource == "inventory-item" and method == "DELETE":
        raise ValueError("Keep catalog materials referenced by vendors and payments")
    if resource == "inventory-item" and method == "POST":
        for field in ("name", "unit"):
            if not isinstance(body.get(field), str) or not body[field].strip():
                raise ValueError("Material name and unit are required")
            body[field] = " ".join(body[field].split())
        # A deterministic key plus conditional put prevents concurrent duplicates.
        canonical_id = model.new_id('inventory-item', f"{body['name'].casefold()}|{body['unit'].casefold()}")
        existing = get_table('INVENTORY_TABLE').get_item(Key={'PK': pk, 'SK': 'INVENTORY-ITEM#' + canonical_id}, ConsistentRead=True).get('Item')
        body["itemId"] = existing["itemId"] if existing else ((model.new_id('inventory-item', f"{body['name'].casefold()}|{body['unit'].casefold()}")))
    if resource == "inventory-item" and method in {"POST", "PUT", "PATCH"}:
        if "reorderLevel" in body:
            number(body["reorderLevel"], "Reorder level")
        if set(body) & {"quantity", "totalStock", "centralStock", "deployedStock"}:
            raise ValueError("Stock balances are maintained by receipts and issue vouchers")
    if resource == "vendor" and method in {"POST", "PUT", "PATCH"}:
        if "materialsSupplied" in body and "materialIds" not in body:
            raise ValueError("Select catalog materials instead of entering material names")
        if method == "POST" or "materialIds" in body:
            vendor_materials(body, org)
        if method == "POST":
            body.setdefault("status", "Active")
    if resource == "inventory-item" and method == "GET" and not rid:
        table = get_table("INVENTORY_TABLE")
        if (query.get('catalog') == 'true'):
            return [clean(i) for i in rows(table, f'ORG#{org}', 'INVENTORY-ITEM#')]
        all_rows = rows(table, org=org)
        catalog = {((i['itemId'])): i for i in all_rows if i.get("entityType") == "inventory-item"}
        balances = [i for i in all_rows if i.get("SK", "").startswith("BALANCE#")]
        for i in balances:
            catalog.setdefault(i["itemId"], dict(i, category="General"))
        result = []
        for key, item in catalog.items():
            central = sum(i.get("quantity", 0) for i in balances if i["itemId"] == key and i["PK"] == f"ORG#{org}")
            deployed = sum(i.get("quantity", 0) for i in balances if i["itemId"] == key and i["PK"] != f"ORG#{org}")
            result.append(dict(clean(item), totalStock=central + deployed, centralStock=central, deployedStock=deployed))
        return result
    return None
