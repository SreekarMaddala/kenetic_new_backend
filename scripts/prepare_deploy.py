"""Prepare explicit HTTP routes with one Lambda permission per path.

SAM HttpApi function events generate a permission for every method. Keeping
routes in the API definition instead lets methods share a path-scoped permission
without exceeding Lambda's 20 KB policy limit during an update.
"""
import argparse
from copy import deepcopy
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]


def prepare(template):
    result = deepcopy(template)
    resources = result['Resources']
    gateway = resources['KineticHttpApi']['Properties']
    if gateway.get('DefinitionBody') or gateway.get('DefinitionUri'):
        raise ValueError('Expected routes to be defined by HttpApi function events')
    authorizer = gateway['Auth']['DefaultAuthorizer']
    paths = {}
    for function_id, resource in list(resources.items()):
        if resource['Type'] != 'AWS::Serverless::Function':
            continue
        properties = resource['Properties']
        events = properties.get('Events', {})
        remaining = {}
        permissions = {}
        integration = {
            'httpMethod': 'POST',
            'type': 'aws_proxy',
            'uri': {'Fn::Sub': 'arn:${AWS::Partition}:apigateway:${AWS::Region}:lambda:path/2015-03-31/functions/${' + function_id + '.Arn}/invocations'},
            'payloadFormatVersion': '2.0',
        }
        for event_id, event in events.items():
            if event['Type'] != 'HttpApi':
                remaining[event_id] = event
                continue
            route = event['Properties']
            if set(route) - {'ApiId', 'Path', 'Method', 'Auth'}:
                raise ValueError(f'Unsupported route properties: {function_id}.{event_id}')
            if route['ApiId'] != {'Ref': 'KineticHttpApi'}:
                raise ValueError('All routes must use KineticHttpApi')
            path, method = route['Path'], route['Method'].lower()
            if method not in {'get', 'post', 'put', 'patch', 'delete'} or path == '$default':
                raise ValueError('Keep OPTIONS handled by gateway CORS')
            auth = route.get('Auth', {})
            if set(auth) - {'Authorizer', 'AuthorizationScopes'} or auth.get('Authorizer', authorizer) != authorizer:
                raise ValueError('Expected the configured default JWT authorizer')
            operations = paths.setdefault(path, {})
            if method in operations:
                raise ValueError(f'Duplicate route: {method} {path}')
            operations[method] = {
                'x-amazon-apigateway-integration': integration,
                'security': [{authorizer: auth.get('AuthorizationScopes', [])}],
                'responses': {},
            }
            if path not in permissions:
                # Reuse pre-method-split logical IDs when possible. This also
                # avoids unnecessary permission replacement during migration.
                base = re.sub(r'Method(Get|Post|Put|Patch|Delete)$', '', event_id)
                permission_id = function_id + base + 'Permission'
                if permission_id in resources:
                    raise ValueError(f'Permission ID collision: {permission_id}')
                arn_path = re.sub(r'\{[^}]+\}', '*', path)
                resources[permission_id] = {
                    'Type': 'AWS::Lambda::Permission',
                    'Properties': {
                        'Action': 'lambda:InvokeFunction',
                        'FunctionName': {'Ref': function_id},
                        'Principal': 'apigateway.amazonaws.com',
                        'SourceArn': {'Fn::Sub': 'arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${KineticHttpApi}/*/*' + arn_path},
                    },
                }
                permissions[path] = permission_id
        if remaining:
            properties['Events'] = remaining
        else:
            properties.pop('Events', None)
    gateway['DefinitionBody'] = {'openapi': '3.0.1', 'info': {'version': '1.0', 'title': {'Ref': 'AWS::StackName'}}, 'paths': paths}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.aws-sam/prepared-template.yaml')
    args = parser.parse_args()
    template = yaml.safe_load((ROOT / 'template.yaml').read_text(encoding='utf-8'))
    prepared = prepare(template)
    # The generated template lives in .aws-sam, so resolve code paths against
    # the source template before SAM build/package relocates it again.
    for properties in [prepared.get('Globals', {}).get('Function', {})] + [
        resource['Properties'] for resource in prepared['Resources'].values()
        if resource['Type'] == 'AWS::Serverless::Function'
    ]:
        if 'CodeUri' in properties:
            properties['CodeUri'] = str((ROOT / properties['CodeUri']).resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(prepared, sort_keys=False), encoding='utf-8')
    permissions = sum(r['Type'] == 'AWS::Lambda::Permission' for r in prepared['Resources'].values())
    routes = sum(len(methods) for methods in prepared['Resources']['KineticHttpApi']['Properties']['DefinitionBody']['paths'].values())
    print(f'Prepared {routes} HTTP routes with {permissions} path-scoped permissions: {args.output}')


if __name__ == '__main__':
    main()
