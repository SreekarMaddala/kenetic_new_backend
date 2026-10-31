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


def prune_artifacts(build_dir):
    junk_names = {
        'docs', 'tests', 'scripts', '.samignore', 'cognito-template.yaml',
        'conftest.py', 'env.json.example', 'README.md', 'requirements-dev.txt',
        'samconfig.toml', 'template.yaml', '.pytest_cache', '__pycache__'
    }
    pruned = 0
    for func_dir in build_dir.iterdir():
        if func_dir.is_dir() and (func_dir / 'handlers').exists():
            for item in list(func_dir.iterdir()):
                if item.name in junk_names or item.suffix in {'.pyc', '.pyo'}:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
                    pruned += 1
            for cache in func_dir.rglob('__pycache__'):
                shutil.rmtree(cache, ignore_errors=True)
    if pruned:
        print(f'Pruned {pruned} non-runtime files/folders from Lambda packages.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--config-env', default='dev')
    args = parser.parse_args()
    sam = shutil.which('sam')
    if not sam:
        raise SystemExit('AWS SAM CLI is required')
    subprocess.run([sys.executable, str(ROOT / 'scripts/prepare_deploy.py')], cwd=ROOT, check=True)
    subprocess.run([sam, 'build', '--template-file', str(ROOT / '.aws-sam/prepared-template.yaml')], cwd=ROOT, check=True)
    build_dir = ROOT / '.aws-sam/build'
    prune_artifacts(build_dir)
    built = build_dir / 'template.yaml'
    validate_build(built)
    if not args.build_only:
        subprocess.run([sam, 'deploy', '--config-env', args.config_env,
                        '--config-file', str(ROOT / 'samconfig.toml'),
                        '--template-file', str(built)], cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
