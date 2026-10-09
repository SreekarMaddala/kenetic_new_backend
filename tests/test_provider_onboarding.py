import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError


@pytest.fixture
def onboarding(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'aws-setup/onboard_super_admin.py'
    spec = importlib.util.spec_from_file_location('provider_onboarding', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    session = Mock()
    cognito = Mock()
    sts = Mock()
    sts.get_caller_identity.return_value = {'Account': '119618548962'}
    session.client.side_effect = lambda service: sts if service == 'sts' else cognito
    cognito.admin_get_user.side_effect = ClientError({'Error': {'Code': 'UserNotFoundException'}}, 'AdminGetUser')
    monkeypatch.setattr(module.boto3, 'Session', Mock(return_value=session))
    provision = Mock()
    monkeypatch.setattr(module, 'bootstrap', provision)
    setup = tmp_path / 'setup-results-dev.json'
    setup.write_text(json.dumps({'AccountId': '119618548962', 'Region': 'ap-south-1', 'CognitoUserPoolId': 'ap-south-1_testpool', 'ProjectName': 'kinetic-erp', 'Environment': 'dev'}), encoding='utf-8-sig')
    return module, setup, tmp_path / 'provider-super-admin-dev.json', sts, cognito, provision


def test_wrong_account_never_provisions_or_saves_state(onboarding):
    module, setup, state, sts, _, provision = onboarding
    sts.get_caller_identity.return_value = {'Account': '000000000000'}
    with pytest.raises(ValueError, match='Wrong AWS account'):
        module.onboard(setup, state, apply=True)
    assert not state.exists()
    provision.assert_not_called()


def test_preview_does_not_provision_or_send_invitation(onboarding):
    module, setup, state, _, _, provision = onboarding
    result = module.onboard(setup, state, send_invitation=True)
    assert result['Email'] == 'stromsree@gmail.com' and result['Role'] == 'super_admin'
    assert not state.exists()
    provision.assert_not_called()


def test_environment_mismatch_does_not_provision(onboarding):
    module, setup, state, _, _, provision = onboarding
    with pytest.raises(ValueError, match='different environment'):
        module.onboard(setup, state, apply=True, environment='prod')
    assert not state.exists()
    provision.assert_not_called()


def test_failed_onboarding_reuses_saved_organization_and_exact_identity(onboarding):
    module, setup, state, _, _, provision = onboarding
    provision.side_effect = [RuntimeError('temporary failure'), None]
    with pytest.raises(RuntimeError):
        module.onboard(setup, state, apply=True, send_invitation=True)
    original = json.loads(state.read_text())['OrganizationId']
    module.onboard(setup, state, apply=True, send_invitation=True)
    for invocation in provision.call_args_list:
        args = invocation.args[0]
        assert args.org_id == original
        assert args.name == 'Sreekar' and args.email == 'stromsree@gmail.com'
        assert args.role == 'super_admin' and args.send_invitation
        assert args.users_table == 'kinetic-erp-users-dev'
        assert invocation.kwargs['session'] is not None


def test_existing_lower_role_is_not_elevated(onboarding):
    module, setup, state, _, cognito, provision = onboarding
    cognito.admin_get_user.side_effect = None
    cognito.admin_get_user.return_value = {'Username': 'existing', 'Enabled': True}
    cognito.admin_list_groups_for_user.return_value = {'Groups': [{'GroupName': 'operations_admin'}]}
    with pytest.raises(ValueError, match='another role'):
        module.onboard(setup, state, apply=True)
    assert not state.exists()
    provision.assert_not_called()


def test_existing_activated_admin_is_reused_without_password_reset(onboarding):
    module, setup, state, _, cognito, provision = onboarding
    org = 'org_' + '1' * 32
    cognito.admin_get_user.side_effect = None
    cognito.admin_get_user.return_value = {'Username': 'existing', 'Enabled': True, 'UserStatus': 'CONFIRMED', 'UserAttributes': [{'Name': 'custom:org_id', 'Value': org}]}
    cognito.admin_list_groups_for_user.return_value = {'Groups': [{'GroupName': 'super_admin'}]}
    module.onboard(setup, state, apply=True, send_invitation=True)
    args = provision.call_args.args[0]
    assert args.org_id == org and not args.send_invitation
