"""Representative sequential request checks; timings use a mocked database."""
import json
import time
from decimal import Decimal
from unittest.mock import Mock

import pytest

from common import model, payroll, reporting, workflow_helpers, payroll_records
from common.dynamo import get_table
from handlers import consolidated as api
from test_model import app_database, data


def test_payroll_with_250_salary_profiles_reads_all_pages_and_batches_employees(app_database, monkeypatch, record_property):
    call, org, _, _ = app_database
    pid = data(call(api.projects_handler, '/projects', 'POST', {'name': 'Large payroll', 'budget': 10000000}))['projectId']
    pk = f'ORG#{org}#PROJECT#{pid}'
    tables = {env: get_table(env) for env in ('PAYROLL_TABLE', 'WORKFORCE_TABLE', 'USERS_TABLE')}
    with tables['PAYROLL_TABLE'].batch_writer() as salaries, tables['USERS_TABLE'].batch_writer() as users:
        for index in range(250):
            emp = model.new_id('user', f'employee-{index}')
            name = f'Employee {index} ' + 'x' * 3800
            salaries.put_item(Item={'PK': pk, 'SK': 'SALARY#' + emp, 'employeeId': emp, 'name': name,
                                   'basic': Decimal('1000.25'), 'hra': Decimal('100.25'), 'pf': Decimal('50.25')})
            users.put_item(Item={'PK': 'ORG#' + org, 'SK': 'USER#' + emp, 'employeeId': emp,
                                'orgId': org, 'status': 'Inactive' if index < 5 else 'Active'})
    monkeypatch.setattr(payroll, 'get_table', tables.__getitem__)
    table = tables['PAYROLL_TABLE']
    original_query = table.query
    query = Mock(side_effect=lambda **kwargs: original_query(**dict(kwargs, Limit=25)))
    monkeypatch.setattr(table, 'query', query)
    users = tables['USERS_TABLE']
    batch = Mock(wraps=users.meta.client.batch_get_item)
    monkeypatch.setattr(users.meta.client, 'batch_get_item', batch)
    employee_read = Mock(side_effect=AssertionError('Payroll must batch employee reads'))
    monkeypatch.setattr(users, 'get_item', employee_read)

    started = time.perf_counter()
    response = call(api.governance_handler, f'/projects/{pid}/payroll', 'POST', {'month': '2026-10'})
    elapsed = time.perf_counter() - started
    cycle = data(response)
    assert len(cycle['staff']) == 245
    assert Decimal(str(cycle['amount'])) == Decimal('1050.25') * 245
    assert all(member['net'] == 1050.25 for member in cycle['staff'])
    assert batch.call_count == 3
    assert all(len(c.kwargs['RequestItems'][users.name]['Keys']) <= 100 for c in batch.call_args_list)
    assert all(c.kwargs['RequestItems'][users.name]['ConsistentRead'] for c in batch.call_args_list)
    assert any('ExclusiveStartKey' in c.kwargs for c in query.call_args_list)
    header = table.get_item(Key={'PK': pk, 'SK': 'PAYROLL#2026-10'})['Item']
    assert header['entryCount'] == 245 and 'staff' not in header
    repeated = data(call(api.governance_handler, f'/projects/{pid}/payroll', 'POST', {'month': '2026-10'}))
    assert repeated['cycleId'] == cycle['cycleId']
    assert batch.call_count == 3
    record_property('payroll_profiles', 250)
    record_property('payroll_members', 245)
    record_property('payroll_response_bytes', len(response['body'].encode()))
    record_property('payroll_seconds_mocked', round(elapsed, 3))


def test_executive_report_100_projects_10000_transactions_uses_complete_summaries(app_database, monkeypatch, record_property):
    call, org, _, _ = app_database
    projects = get_table('PROJECTS_TABLE')
    finance = get_table('FINANCE_TABLE')
    summaries = get_table('REPORTING_TABLE')
    # Seed a posted ledger and its summaries as a database snapshot. The report
    # must read those summaries, independent of the number of financial records.
    with projects.batch_writer() as project_writer, finance.batch_writer() as ledger, summaries.batch_writer() as summary_writer:
        for index in range(100):
            pid = model.new_id('project', f'large-report-{index}')
            pk = f'ORG#{org}#PROJECT#{pid}'
            project_writer.put_item(Item={'PK': 'ORG#' + org, 'SK': 'PROJECT#' + pid,
                                         'orgId': org, 'projectId': pid, 'name': f'Project {index}',
                                         'status': 'Active', 'budget': 100000, 'openingSpent': 50})
            for transaction in range(100):
                identifier = model.new_id('expense', f'{index}-{transaction}')
                ledger.put_item(Item={'PK': pk, 'SK': 'EXPENSE#' + identifier, 'expenseId': identifier,
                                      'orgId': org, 'projectId': pid, 'entityType': 'expense',
                                      'date': f'2026-10-{transaction % 20 + 1:02}', 'amount': 10, 'status': 'Approved'})
            summary_writer.put_item(Item={'PK': 'ORG#' + org, 'SK': 'SPENDING#' + pid, 'amount': 1000})
            for day in range(1, 21):
                summary_writer.put_item(Item={'PK': pk, 'SK': f'DAY#2026-10-{day:02}', 'amount': 50})
        summary_writer.put_item(Item={'PK': 'ORG#' + org, 'SK': 'SUMMARY#EXPENSES', 'amount': 100000})
        for day in range(1, 21):
            summary_writer.put_item(Item={'PK': 'ORG#' + org + '#EXPENSES', 'SK': f'DAY#2026-10-{day:02}', 'amount': 5000})

    original_get_table = api.get_table
    monkeypatch.setattr(api, 'get_table', lambda env: projects if env == 'PROJECTS_TABLE' else original_get_table(env))
    monkeypatch.setattr(reporting, 'get_table', lambda env: summaries if env == 'REPORTING_TABLE' else pytest.fail('Report read raw financial records'))
    original_project_query = projects.query
    project_query = Mock(side_effect=lambda **kwargs: original_project_query(**dict(kwargs, Limit=17)))
    monkeypatch.setattr(projects, 'query', project_query)
    original_summary_query = summaries.query
    summary_query = Mock(side_effect=lambda **kwargs: original_summary_query(**dict(kwargs, Limit=7)))
    monkeypatch.setattr(summaries, 'query', summary_query)
    batch = Mock(wraps=summaries.meta.client.batch_get_item)
    monkeypatch.setattr(summaries.meta.client, 'batch_get_item', batch)

    started = time.perf_counter()
    overall = data(call(api.governance_handler, '/reports/executive'))
    overall_seconds = time.perf_counter() - started
    assert len(overall['projectSummaries']) == 100
    assert sum(p['spent'] for p in overall['projectSummaries']) == 105000
    assert overall['totalExpenses'] == 100000
    assert batch.call_count == 1
    assert summary_query.call_count == 0
    started = time.perf_counter()
    selected = data(call(api.governance_handler, '/reports/executive', query={'startDate': '2026-10-01', 'endDate': '2026-10-10'}))
    selected_seconds = time.perf_counter() - started
    assert len(selected['projectSummaries']) == 100
    assert all(p['spent'] == 500 for p in selected['projectSummaries'])
    assert selected['totalExpenses'] == 50000
    assert summary_query.call_count == 202  # Two pages per project plus two expense pages.
    assert any('ExclusiveStartKey' in c.kwargs for c in project_query.call_args_list)
    assert any('ExclusiveStartKey' in c.kwargs for c in summary_query.call_args_list)
    record_property('report_projects', 100)
    record_property('posted_transactions', 10000)
    record_property('report_overall_seconds_mocked', round(overall_seconds, 3))
    record_property('report_period_seconds_mocked', round(selected_seconds, 3))


def test_batch_reads_retry_unprocessed_keys_and_fail_without_partial_results(monkeypatch):
    keys = [{'PK': 'ORG#test', 'SK': f'USER#{index}'} for index in range(101)]
    class Client:
        def __init__(self):
            self.calls = []
        def batch_get_item(self, RequestItems):
            self.calls.append(RequestItems)
            requested = RequestItems['users']['Keys']
            if len(self.calls) == 1:
                return {'Responses': {'users': requested[:-1]}, 'UnprocessedKeys': {'users': {'Keys': requested[-1:], 'ConsistentRead': True}}}
            return {'Responses': {'users': requested}}
    client = Client()
    table = type('Table', (), {'name': 'users', 'meta': type('Meta', (), {'client': client})()})()
    monkeypatch.setattr(workflow_helpers.time, 'sleep', lambda _: None)
    assert len(workflow_helpers._batch_get(table, keys + [keys[0]])) == 101
    assert len(client.calls) == 3
    monkeypatch.setattr(client, 'batch_get_item', lambda RequestItems: {'UnprocessedKeys': RequestItems})
    with pytest.raises(ValueError, match='throttled'):
        workflow_helpers._batch_get(table, keys)


def test_payroll_header_without_immutable_snapshot_is_rejected():
    with pytest.raises(ValueError, match='immutable snapshot'):
        payroll_records.expand(None, {'cycleId': model.new_id('payroll'), 'staff': []})
