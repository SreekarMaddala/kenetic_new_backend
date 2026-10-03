from test_security_workflows import database, event, data
from handlers import consolidated as api


def call(path, method="GET", body=None, **kwargs):
    return api.project_commercial_handler(event(path, method, body, **kwargs), None)


def onboard(**kwargs):
    return data(call("/subcontractors", "POST", {"name": "ABC Works", "contactPerson": "Sam", "trade": "Electrical"}, **kwargs))


def test_registry_and_project_assignment_are_separate(database):
    company = onboard()
    endpoint = "/projects/P-A/subcontractors"
    body = {"registryId": company["subcontractorId"], "scopeOfWork": "Install wiring", "name": "Forged"}
    assigned = data(call(endpoint, "POST", body))
    assert assigned["name"] == "ABC Works"
    assert assigned["registryId"] == company["subcontractorId"]
    assert "projectId" not in data(call("/subcontractors"))[0]
    assert len(data(call(endpoint))) == 1
    assert call(endpoint, "POST", body)["statusCode"] == 409
    request = data(call(endpoint, "POST", {"type": "Procurement", "contractorId": assigned["subcontractorId"], "name": "Cable", "quantity": 2}))
    assert request["contractorName"] == "ABC Works"
    request_path = endpoint + "/" + request["subcontractorId"]
    data(call(request_path, "PATCH", {"status": "Approved"}))
    data(call(request_path, "PATCH", {"status": "Approved"}))
    assert call(endpoint, "POST", {"name": "Inline company"})["statusCode"] == 400
    assert call(endpoint, "POST", {"type": "Procurement", "contractorId": "missing", "quantity": 2})["statusCode"] == 400


def test_assignment_rejects_foreign_and_inactive_companies(database):
    foreign = onboard(sub="admin-b", org="ORG-B")
    endpoint = "/projects/P-A/subcontractors"
    assert call(endpoint, "POST", {"registryId": foreign["subcontractorId"], "scopeOfWork": "Work"})["statusCode"] == 400
    company = onboard()
    path = "/subcontractors/" + company["subcontractorId"]
    data(call(path, "PATCH", {"status": "Inactive"}))
    assert call(endpoint, "POST", {"registryId": company["subcontractorId"], "scopeOfWork": "Work"})["statusCode"] == 400
    assert call(path, "DELETE")["statusCode"] == 400
    assert data(call("/subcontractors", sub="admin-b", org="ORG-B"))[0]["subcontractorId"] == foreign["subcontractorId"]


def test_assignment_dates_links_and_retries(database):
    company = onboard()
    endpoint = "/projects/P-A/subcontractors"
    body = {"registryId": company["subcontractorId"], "scopeOfWork": "Wiring", "requestId": "assignment-request-001"}
    assert call(endpoint, "POST", dict(body, startDate="2026-10-05", endDate="2026-10-04"))["statusCode"] == 400
    assignment = data(call(endpoint, "POST", body))
    assert data(call(endpoint, "POST", body))["subcontractorId"] == assignment["subcontractorId"]
    path = endpoint + "/" + assignment["subcontractorId"]
    assert call(path, "PATCH", {"registryId": "other"})["statusCode"] == 400
    data(call(path, "PATCH", {"status": "Inactive"}))
    assert call(endpoint, "POST", {"type": "Procurement", "contractorId": assignment["subcontractorId"], "quantity": 1})["statusCode"] == 400
