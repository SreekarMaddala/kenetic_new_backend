import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

from common.resource_names import TABLE_FUNCTIONALITY, resource_name, table_names

ROOT = Path(__file__).resolve().parents[2]


def load_table_setup():
    spec = importlib.util.spec_from_file_location('table_setup', ROOT / 'aws-setup/create_tables.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve(value, environment):
    if isinstance(value, dict) and set(value) == {'Fn::Sub'}:
        return value['Fn::Sub'].replace('${ProjectName}', 'kinetic-erp').replace('${Environment}', environment)
    return value


def test_tables_and_functions_have_disjoint_environment_names():
    template = yaml.safe_load((ROOT / 'backend/template.yaml').read_text())
    setup = load_table_setup()
    deployments = {}
    for environment in ('dev', 'prod'):
        expected = table_names(environment)
        definitions = list(setup.definitions(environment))
        assert len(definitions) == len(expected) == 16
        assert {item['TableName'] for item in definitions} == set(expected.values())
        variables = template['Globals']['Function']['Environment']['Variables']
        assert {env: resolve(variables[env], environment) for env in TABLE_FUNCTIONALITY} == expected
        functions = [resolve(resource['Properties']['FunctionName'], environment)
                     for resource in template['Resources'].values() if resource['Type'] == 'AWS::Serverless::Function']
        assert len(functions) == len(set(functions)) == 11
        assert all(name.startswith('kinetic-erp-') and name.endswith('-' + environment) and len(name) <= 64 for name in functions)
        assert template['Resources']['KineticHttpApi']['Properties']['StageName'] == {'Ref': 'Environment'}
        assert '${Environment}' in template['Outputs']['KineticHttpApiUrl']['Value']['Fn::Sub']
        deployments[environment] = set(expected.values()) | set(functions)
        for definition in definitions:
            assert {'Key': 'Environment', 'Value': environment} in definition['Tags']
    assert deployments['dev'].isdisjoint(deployments['prod'])


def test_manifest_and_iam_permissions_target_the_named_tables():
    manifest = json.loads((ROOT / 'aws-setup/tables.json').read_text())
    assert {table['environmentVariable'] for table in manifest['tables']} == set(TABLE_FUNCTIONALITY)
    template = yaml.safe_load((ROOT / 'backend/template.yaml').read_text())
    names = set(table_names('prod').values())
    known_references = set(template['Parameters']) | set(template['Resources'])
    table_arns = []
    def inspect(node):
        if isinstance(node, dict):
            if 'TableName' in node:
                assert resolve(node['TableName'], 'prod') in names
            if 'Fn::Sub' in node:
                expression = node['Fn::Sub']
                assert isinstance(expression, str)
                for reference in re.findall(r'\$\{([^}]+)\}', expression):
                    assert reference.startswith('AWS::') or reference.split('.')[0] in known_references, reference
                if ':dynamodb:' in expression and ':table/' in expression:
                    table_name = resolve(node, 'prod').split(':table/', 1)[1].split('/')[0]
                    assert table_name in names
                    table_arns.append(table_name)
            for value in node.values():
                inspect(value)
        elif isinstance(node, list):
            for value in node:
                inspect(value)
    inspect(template)
    assert table_arns
    assert not any(name.endswith('Table') for name in template['Parameters'])


@pytest.mark.parametrize('environment', ['v1', 'v2', 'v3', 'staging', 'Prod', ''])
def test_invalid_environment_cannot_select_resources(environment):
    with pytest.raises(ValueError, match='dev or prod'):
        table_names(environment)


def test_custom_project_name_keeps_the_environment_suffix():
    assert resource_name('finance', 'prod', 'my-project') == 'my-project-finance-prod'
    with pytest.raises(ValueError, match='Project name'):
        table_names('dev', 'Invalid Project')
