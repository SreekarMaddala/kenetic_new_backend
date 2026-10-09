from test_security_workflows import ORG_A, PROJECT_A, PROJECT_B, SITE_A, UNASSIGNED
from test_security_workflows import database, event, data, put
from handlers import consolidated as api
from common.authz import SUPERVISOR
from common import model, payroll_records
from common.dynamo import get_table


def test_personal_salary_is_private_and_read_only(database):
    pk = f"ORG#{ORG_A}#PROJECT#{PROJECT_A}"
    for employee in (SITE_A, "someone-else"):
        put("PAYROLL_TABLE", pk, f"SALARY#{employee}", employeeId=employee, basic=25000)
    table = get_table('PAYROLL_TABLE')
    cycle = {'PK': pk, 'SK': 'PAYROLL#2026-09', 'month': '2026-09', 'status': 'Paid', 'amount': 48000,
             'orgId': ORG_A, 'projectId': PROJECT_A,
             'cycleId': model.new_id('payroll'), 'createdAt': '2026-09-30T00:00:00+00:00',
             'staff': [{'labourId': employee, 'salary': {'basic': 25000}, 'gross': 25000, 'deductions': 1000, 'net': 24000}
                       for employee in (SITE_A, model.new_id('user', 'other'))]}
    build = payroll_records.save_entries(table, cycle)
    table.put_item(Item=cycle)
    table.update_item(Key={'PK': pk, 'SK': build['SK']}, UpdateExpression='SET #s = :s',
                      ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':s': 'Published'})
    args = {"sub": SITE_A, "role": SUPERVISOR}
    path = f"/projects/{PROJECT_A}/payroll/me"
    result = data(api.governance_handler(event(path, **args), None))
    assert result["profile"]["employeeId"] == SITE_A
    assert result["history"] == [{"month": "2026-09", "status": "Paid", "gross": 25000, "deductions": 1000, "net": 24000, "paidAt": None}]
    for suffix in ("", "/staff"):
        assert api.governance_handler(event(f"/projects/{PROJECT_A}/payroll" + suffix, **args), None)["statusCode"] == 403
    for method in ("POST", "PATCH", "DELETE"):
        assert api.governance_handler(event(path, method, {}, **args), None)["statusCode"] == 403
    assert api.governance_handler(event(path, sub=UNASSIGNED, role=SUPERVISOR), None)["statusCode"] == 403
    assert api.governance_handler(event(f"/projects/{PROJECT_B}/payroll/me", **args), None)["statusCode"] == 403


def test_personal_salary_can_be_unconfigured(database):
    result = data(api.governance_handler(event(f"/projects/{PROJECT_A}/payroll/me", sub=SITE_A, role=SUPERVISOR), None))
    assert result == {"profile": None, "history": []}
