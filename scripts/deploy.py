"""Build and deploy the prepared template, rejecting per-method permissions."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]


def validate_build(path):
    template = yaml.safe_load(path.read_text(encoding='utf-8'))
    resources = template['Resources']
    for name, resource in resources.items():
        if resource['Type'] == 'AWS::Serverless::Function':
            if any(event.get('Type') == 'HttpApi' for event in resource.get('Properties', {}).get('Events', {}).values()):
                raise ValueError(f'{name} still generates per-method permissions; refusing deployment')
    permissions = sum(resource['Type'] == 'AWS::Lambda::Permission' for resource in resources.values())
    paths = resources['KineticHttpApi']['Properties']['DefinitionBody']['paths']
    if permissions != len(paths):
        raise ValueError('Expected one invocation permission per API path')
    print(f'Checked deployment: {permissions} path permissions; no per-method permissions.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--config-env', default='dev')
    args = parser.parse_args()
    sam = shutil.which('sam')
    if not sam:
        raise SystemExit('AWS SAM CLI is required')
    subprocess.run([sys.executable, str(ROOT / 'scripts/prepare_deploy.py')], cwd=ROOT, check=True)
    subprocess.run([sam, 'build', '--cached', '--template-file', str(ROOT / '.aws-sam/prepared-template.yaml')], cwd=ROOT, check=True)
    built = ROOT / '.aws-sam/build/template.yaml'
    validate_build(built)
    if not args.build_only:
        subprocess.run([sam, 'deploy', '--config-env', args.config_env,
                        '--config-file', str(ROOT / 'samconfig.toml'),
                        '--template-file', str(built)], cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
