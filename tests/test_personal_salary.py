from test_security_workflows import database, event, data, put
from handlers import consolidated as api
from common.authz import SUPERVISOR


def test_personal_salary_is_private_and_read_only(database):
    pk = "ORG#ORG-A#PROJECT#P-A"
    for employee in ("site-a", "someone-else"):
        put("SETTINGS_TABLE", pk, f"SALARY#{employee}", employeeId=employee, basic=25000)
    put("SETTINGS_TABLE", pk, "PAYROLL#2026-09", month="2026-09", status="Paid", amount=50000,
        staff=[{"labourId": employee, "salary": {"basic": 25000}, "gross": 25000, "deductions": 1000, "net": 24000}
               for employee in ("site-a", "someone-else")])
    args = {"sub": "site-a", "role": SUPERVISOR}
    path = "/projects/P-A/payroll/me"
    result = data(api.governance_handler(event(path, **args), None))
    assert result["profile"]["employeeId"] == "site-a"
    assert result["history"] == [{"month": "2026-09", "status": "Paid", "gross": 25000, "deductions": 1000, "net": 24000, "paidAt": None}]
    for suffix in ("", "/staff"):
        assert api.governance_handler(event("/projects/P-A/payroll" + suffix, **args), None)["statusCode"] == 403
    for method in ("POST", "PATCH", "DELETE"):
        assert api.governance_handler(event(path, method, {}, **args), None)["statusCode"] == 403
    assert api.governance_handler(event(path, sub="unassigned", role=SUPERVISOR), None)["statusCode"] == 403
    assert api.governance_handler(event("/projects/P-B/payroll/me", **args), None)["statusCode"] == 403


def test_personal_salary_can_be_unconfigured(database):
    result = data(api.governance_handler(event("/projects/P-A/payroll/me", sub="site-a", role=SUPERVISOR), None))
    assert result == {"profile": None, "history": []}
