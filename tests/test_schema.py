import importlib.util
from pathlib import Path
import boto3
import pytest
from unittest.mock import Mock
from botocore.exceptions import ClientError
from moto import mock_aws


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_provisioning_adds_both_missing_indexes_and_is_repeatable(monkeypatch):
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    root = Path(__file__).resolve().parents[2]
    setup = module(root / 'aws-setup/create_tables.py', 'schema_setup')
    with mock_aws():
        client = boto3.client('dynamodb')
        finance = next(item for item in setup.definitions() if item['TableName'].endswith('-finance-dev'))
        finance.pop('GlobalSecondaryIndexes')
        finance['AttributeDefinitions'] = [item for item in finance['AttributeDefinitions'] if item['AttributeName'] in {'PK', 'SK'}]
        finance['BillingMode'] = 'PROVISIONED'
        finance['ProvisionedThroughput'] = {'ReadCapacityUnits': 1, 'WriteCapacityUnits': 1}
        client.create_table(**finance)
        audit = next(item for item in setup.definitions() if item['TableName'].endswith('-audit-events-dev'))
        client.create_table(**audit)
        setup.provision(client)
        setup.provision(client)
        names = client.list_tables()['TableNames']
        assert len(names) == 16
        assert all(client.describe_table(TableName=name)['Table']['BillingModeSummary']['BillingMode'] == 'PAY_PER_REQUEST' for name in names)
        indexes = sum(len(client.describe_table(TableName=name)['Table'].get('GlobalSecondaryIndexes', [])) for name in names)
        assert indexes == 16
        audit_table = client.describe_table(TableName=audit['TableName'])['Table']
        assert not audit_table.get('StreamSpecification', {}).get('StreamEnabled')
        assert client.describe_continuous_backups(TableName=finance['TableName'])['ContinuousBackupsDescription']['PointInTimeRecoveryDescription']['PointInTimeRecoveryStatus'] == 'ENABLED'


def test_generated_contracts_match_the_authoritative_schemas():
    root = Path(__file__).resolve().parents[2]
    generator = module(root / 'backend/scripts/export_contracts.py', 'contract_generator')
    for path, expected in generator.artifacts().items():
        assert path.read_text(encoding='utf-8') == expected


def test_backup_initialization_retries_then_succeeds(monkeypatch):
    setup = module(Path(__file__).resolve().parents[2] / 'aws-setup/create_tables.py', 'backup_setup')
    error = ClientError({'Error': {'Code': 'ContinuousBackupsUnavailableException', 'Message': 'Not ready'}}, 'UpdateContinuousBackups')
    client = Mock()
    client.update_continuous_backups.side_effect = [error, error, {}]
    sleep = Mock()
    monkeypatch.setattr(setup.time, 'sleep', sleep)
    setup.enable_point_in_time_recovery(client, 'kinetic-erp-projects-dev')
    assert client.update_continuous_backups.call_count == 3
    assert sleep.call_count == 2


def test_backup_initialization_timeout_is_bounded(monkeypatch):
    setup = module(Path(__file__).resolve().parents[2] / 'aws-setup/create_tables.py', 'backup_timeout_setup')
    client = Mock()
    client.update_continuous_backups.side_effect = ClientError({'Error': {'Code': 'ContinuousBackupsUnavailableException'}}, 'UpdateContinuousBackups')
    monkeypatch.setattr(setup.time, 'monotonic', Mock(side_effect=[0, 1200]))
    with pytest.raises(TimeoutError, match='rerun setup'):
        setup.enable_point_in_time_recovery(client, 'kinetic-erp-projects-dev')


def test_backup_permission_errors_are_not_retried(monkeypatch):
    setup = module(Path(__file__).resolve().parents[2] / 'aws-setup/create_tables.py', 'backup_permission_setup')
    client = Mock()
    client.update_continuous_backups.side_effect = ClientError({'Error': {'Code': 'AccessDeniedException'}}, 'UpdateContinuousBackups')
    sleep = Mock()
    monkeypatch.setattr(setup.time, 'sleep', sleep)
    with pytest.raises(ClientError):
        setup.enable_point_in_time_recovery(client, 'kinetic-erp-projects-dev')
    sleep.assert_not_called()
