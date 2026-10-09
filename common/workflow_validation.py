"""Business validation and approval transitions."""
from common.authz import AuthorizationError, OPERATIONS_ADMIN, SUPERVISOR, SUPER_ADMIN, require_role
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from common.dynamo import get_table
from common.workflow_helpers import logistics_visible, now, number


def validate(identity, resource, method, path, body):
    if method == "GET":
        return
    if method == "DELETE" and resource in {"bill", "expense", "payment", "material", "dpr"}:
        raise ValueError("Keep submitted records for the audit trail")
    if resource == "subcontractor" and body.get("type") == "Procurement" and method == "POST" and body.get("status", "Pending") != "Pending":
        raise ValueError("New procurement requests must be Pending")
    if resource == "milestone" and "progress" in body:
        number(body["progress"], "Progress")
        if body["progress"] > 100:
            raise ValueError("Progress must not exceed 100")
    if resource == "logistics-trip":
        kind = body.get("tripType")
        if kind != "vehicle_registration":
            day = str(body.get("date", ""))
            if len(day) != 10:
                raise ValueError("A valid logistics date is required")
            date.fromisoformat(day)
            if not logistics_visible(identity, body):
                raise AuthorizationError("Supervisors can only record logistics for the current month")
        if kind == "vehicle_registration":
            require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
            if not str(body.get("vehicle", "")).strip():
                raise ValueError("Vehicle is required")
        elif kind == "vehicle":
            if number(body.get("endKm"), "End odometer") < number(body.get("startKm"), "Start odometer"):
                raise ValueError("End odometer cannot be below the start")
        elif kind == "fuel":
            body["total"] = number(body.get("liters"), "Liters", True) * number(body.get("rate"), "Rate")
        elif kind == "rental":
            body["total"] = number(body.get("rate"), "Hire charge") + number(body.get("helper", 0), "Helper charge")
        else:
            raise ValueError("Select a valid transport record type")
    if resource == "material":
        if "/stock" in path:
            raise ValueError("Stock is updated by receipts and issue vouchers")
        if SUPERVISOR in identity.roles:
            if "/indents" not in path or method != "POST":
                raise AuthorizationError("Only administrators can review material requests or receive stock")
            if str(body.get("status", "pending")).lower() != "pending":
                raise AuthorizationError("Requests must be submitted as pending")
        if method == "POST":
            if str(body.get("status", "pending")).lower() != "pending":
                raise ValueError("New requests must be pending")
            number(body.get("quantity", body.get("qty")), "Quantity", True)
            if not str(body.get("materialName", body.get("name", ""))).strip():
                raise ValueError("Material name is required")
    if resource in {"bill", "expense", "payment", "dpr"}:
        if method == "POST" and body.get("status", "Pending") not in {"Pending", "Draft"}:
            raise ValueError("New submissions must be Pending or Draft")
        if resource == "dpr" and SUPERVISOR in identity.roles and "status" in body and body["status"] != "Pending":
            raise AuthorizationError("Only administrators can review reports")
        if resource in {"bill", "expense", "payment"} and method == "POST":
            number(body.get("grossAmount") if resource == "bill" else body.get("amount"), "Amount", True)
    if SUPERVISOR in identity.roles and resource in {"inspection", "equipment"} and method != "GET":
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)


def validate_record(resource, body, existing=None):
    """Validate complete commercial state on both creates and partial updates."""
    merged = {**(existing or {}), **body}
    if resource == "boq":
        for field in ("code", "description", "category", "unit"):
            if not isinstance(merged.get(field), str) or not merged[field].strip():
                raise ValueError(f"{field} is required")
        number(merged.get("budgetedQty"), "Budget quantity", True)
        number(merged.get("rate"), "Unit rate")
        body["amount"] = merged["budgetedQty"] * merged["rate"]
    if resource in {"bill", "expense", "payment"}:
        number(merged.get("grossAmount") if resource == "bill" else merged.get("amount"), "Amount", True)
    if resource == "bill":
        deductions = sum(number(merged.get(field, 0), field) for field in ('retentionDeduction', 'tdsDeduction'))
        computed = (Decimal(merged['grossAmount']) - deductions).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if computed < 0 or ('netPayable' in body and Decimal(body['netPayable']) != computed):
            raise ValueError('Net payable must equal gross less retention and TDS deductions')
        body['netPayable'] = computed
        merged['netPayable'] = computed
        payable = number(merged.get("netPayable", merged["grossAmount"]), "Net payable")
        if payable > merged["grossAmount"]:
            raise ValueError("Net payable cannot exceed the gross bill amount")
        if payable < merged.get("paidAmount", 0):
            raise ValueError("Net payable cannot be below payments already approved")
    for field in ("date", "startDate", "endDate", "periodFrom", "periodTo", "requiredDate", "targetDate"):
        if field in body and body[field]:
            value = body[field]
            if not isinstance(value, str) or len(value) != 10:
                raise ValueError(f"{field} must use YYYY-MM-DD")
            date.fromisoformat(value)
    for start, end in (("periodFrom", "periodTo"), ("startDate", "endDate")):
        if merged.get(start) and merged.get(end) and merged[start] > merged[end]:
            raise ValueError(f"{end} cannot precede {start}")
    if resource == "project":
        if "progress" in merged and number(merged["progress"], "Progress") > 100:
            raise ValueError("Progress must not exceed 100")
        if merged.get("spent", 0) > merged.get("budget", 0):
            raise ValueError("Opening spending cannot exceed the project budget")


def validate_transition(identity, resource, old, body):
    approval = resource in {"bill", "expense", "payment", "dpr", "material"} or (resource == "subcontractor" and old.get("type") == "Procurement")
    if not approval:
        return
    if "status" in body:
        require_role(identity, OPERATIONS_ADMIN, SUPER_ADMIN)
        old_status = str(old.get("status", "Pending")).lower()
        new_status = str(body["status"]).lower()
        transitions = {"draft": {"pending"}, "pending": {"approved", "rejected"}, "approved": {"issued"} if resource == "material" else set(), "rejected": {"pending"}}
        if new_status != old_status and new_status not in transitions.get(old_status, set()):
            raise ValueError("Invalid approval transition")
        if new_status == "issued":
            raise ValueError("Issue stock from the warehouse to fulfill this request")
        if new_status != old_status:
            body["approvedBy"] = identity.user_id
            body["approvedAt"] = now()
    if str(old.get("status", "Pending")).lower() in {'approved', 'reversed', 'issued'} and set(body) - {"status", "approvedBy", "approvedAt"}:
        raise ValueError("Approved records are immutable")


def validate_subcontractor(body, org, pk, pid, existing=None):
    merged = {**(existing or {}), **body}
    kind = merged.get("type", "Contractor")
    if existing and ("type" in body or "registryId" in body or "contractorId" in body):
        for field in ("type", "registryId", "contractorId"):
            if field in body and body[field] != existing.get(field):
                raise ValueError("Subcontractor links and record type cannot be changed")
    if kind not in {"Contractor", "Procurement"}:
        raise ValueError("Invalid subcontractor record type")
    if not pid:
        if kind != "Contractor":
            raise ValueError("Create procurement requests inside a project")
        for field in ("name", "contactPerson", "trade"):
            if not isinstance(merged.get(field), str) or not merged[field].strip():
                raise ValueError(f"{field} is required")
            if field in body:
                body[field] = body[field].strip()
    elif kind == "Contractor" and (not existing or merged.get("registryId")):
        registry = get_table("PARTIES_TABLE").get_item(Key={"PK": f"ORG#{org}", "SK": f"SUBCONTRACTOR#{merged.get('registryId', '')}"}, ConsistentRead=True).get("Item")
        if not registry or registry.get("orgId") != org or registry.get("type") == "Procurement":
            raise ValueError("Select an onboarded subcontractor in this organization")
        if not existing and registry.get("status") != "Active":
            raise ValueError("Select an active subcontractor")
        if not str(merged.get("scopeOfWork", "")).strip():
            raise ValueError("Scope of work is required")
        for field in ("name", "contactPerson", "phone", "email", "trade"):
            body[field] = registry.get(field, "")
        if not existing:
            body["subcontractorId"] = registry["subcontractorId"]
    elif kind == "Procurement" and (not existing or ("contractorId" in merged and set(body) - {"status"})):
        contractor = get_table("PARTIES_TABLE").get_item(Key={"PK": pk, "SK": f"SUBCONTRACTOR#{merged.get('contractorId', '')}"}, ConsistentRead=True).get("Item")
        if not contractor or contractor.get("type") == "Procurement" or contractor.get("status") != "Active":
            raise ValueError("Select an active subcontractor assigned to this project")
        body["contractorName"] = contractor["name"]
        number(merged.get("quantity"), "Quantity", True)
    if kind == "Contractor":
        if merged.get("status", "Active") not in {"Active", "Inactive"}:
            raise ValueError("Choose Active or Inactive status")
        if not existing:
            body.setdefault("type", "Contractor")
            body.setdefault("status", "Active")
        if "contractValue" in merged:
            number(merged["contractValue"], "Contract value")
        for field in ("startDate", "endDate"):
            if merged.get(field):
                date.fromisoformat(merged[field])
        if merged.get("startDate") and merged.get("endDate") and merged["endDate"] < merged["startDate"]:
            raise ValueError("End date must be on or after start date")
