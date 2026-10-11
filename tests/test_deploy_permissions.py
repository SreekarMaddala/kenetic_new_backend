"""Deployments must preserve routes without generating a policy per method."""
import importlib.util
import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_deploy', ROOT / 'scripts/prepare_deploy.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


def test_prepared_template_preserves_routes_auth_cors_and_functions():
    original = yaml.safe_load((ROOT / 'template.yaml').read_text(encoding='utf-8'))
    prepared = deployment.prepare(original)
    resources = prepared['Resources']
    paths = resources['KineticHttpApi']['Properties']['DefinitionBody']['paths']
    expected = {}
    for function_id, resource in original['Resources'].items():
        if resource['Type'] != 'AWS::Serverless::Function':
            continue
        for event in resource['Properties']['Events'].values():
            route = event['Properties']
            expected[route['Path'], route['Method'].lower()] = function_id
        assert 'Events' not in resources[function_id]['Properties']
        assert resources[function_id]['Properties'] == {
            key: value for key, value in resource['Properties'].items() if key != 'Events'
        }
    assert {(path, method) for path, operations in paths.items() for method in operations} == set(expected)
    for (path, method), function_id in expected.items():
        operation = paths[path][method]
        assert operation['security'] == [{'CognitoJwt': []}]
        integration = operation['x-amazon-apigateway-integration']
        assert '${' + function_id + '.Arn}' in integration['uri']['Fn::Sub']
        assert integration['type'] == 'aws_proxy'
        assert integration['payloadFormatVersion'] == '2.0'
    for key in ('CorsConfiguration', 'Auth', 'StageName'):
        assert resources['KineticHttpApi']['Properties'][key] == original['Resources']['KineticHttpApi']['Properties'][key]
    assert original['Resources']['FinanceFunction']['Properties']['Events']


def test_permissions_are_scoped_to_api_and_path_and_fit_policy_limit():
    template = yaml.safe_load((ROOT / 'template.yaml').read_text(encoding='utf-8'))
    resources = deployment.prepare(template)['Resources']
    policies = {}
    pairs = set()
    for logical_id, resource in resources.items():
        if resource['Type'] != 'AWS::Lambda::Permission':
            continue
        properties = resource['Properties']
        function = properties['FunctionName']['Ref']
        arn = properties['SourceArn']['Fn::Sub']
        assert '${AWS::AccountId}:${KineticHttpApi}/*/*/' in arn
        assert properties['Principal'] == 'apigateway.amazonaws.com'
        assert properties['Action'] == 'lambda:InvokeFunction'
        assert (function, arn) not in pairs
        pairs.add((function, arn))
        # Conservative realistic statement lengths, including long generated
        # CloudFormation statement IDs. Leave room for old policies on updates.
        policies.setdefault(function, []).append({
            'Sid': logical_id + '-' + 'x' * 100,
            'Effect': 'Allow', 'Principal': {'Service': properties['Principal']},
            'Action': properties['Action'],
            'Resource': 'arn:aws:lambda:ap-south-1:123456789012:function:' + 'x' * 64,
            'Condition': {'ArnLike': {'AWS:SourceArn': arn}},
        })
    for statements in policies.values():
        assert len(json.dumps({'Version': '2012-10-17', 'Statement': statements}).encode()) < 10000
    assert 'FinanceFunctionPaymentsPermission' in resources
    assert 'SupplyChainFunctionVendorsPermission' in resources
