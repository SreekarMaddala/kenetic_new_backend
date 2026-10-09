"""Preview repairs by default; apply only to an explicitly verified AWS account."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boto3
from common import model, reconciliation, payroll_records
from common.authz import Identity
from common.dynamo import get_table
from common.resource_names import PROJECT_NAME, ENVIRONMENTS, table_names


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['reconcile-spending', 'reconcile-stock', 'reconcile-reports', 'backfill-directory', 'cleanup-payroll'])
    parser.add_argument('--org-id', required=True)
    parser.add_argument('--project-id')
    parser.add_argument('--cycle-id')
    parser.add_argument('--environment', choices=ENVIRONMENTS, default='dev')
    parser.add_argument('--project-name', default=PROJECT_NAME)
    parser.add_argument('--region', default='ap-south-1')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-account-id')
    parser.add_argument('--actor-id', help='Employee ID of the operator, recorded in repair audit events')
    args = parser.parse_args()
    os.environ['AWS_DEFAULT_REGION'] = args.region
    model.validate_id('organization', args.org_id)
    if args.operation in {'reconcile-spending', 'cleanup-payroll'}:
        if not args.project_id:
            parser.error('This operation requires --project-id')
        model.validate_id('project', args.project_id)
    for env, name in table_names(args.environment, args.project_name).items():
        os.environ[env] = name
    account = boto3.client('sts').get_caller_identity()['Account']
    if args.apply and (account != args.expected_account_id or not args.actor_id):
        parser.error('--apply requires the matching --expected-account-id and --actor-id')
    if args.actor_id:
        model.validate_id('user', args.actor_id)
        employee = get_table('USERS_TABLE').get_item(Key={'PK': 'ORG#' + args.org_id, 'SK': 'USER#' + args.actor_id}, ConsistentRead=True).get('Item')
        if not employee or employee.get('status') != 'Active' or employee.get('role') not in {'operations_admin', 'super_admin'}:
            parser.error('The audit actor must be an active administrator in this organization')
    identity = Identity(args.actor_id or 'preview', args.org_id, frozenset({'operations_admin'}))
    if args.operation == 'reconcile-spending':
        result = reconciliation.reconcile(identity, args.org_id, args.project_id, args.apply)
    elif args.operation == 'reconcile-stock':
        result = reconciliation.reconcile_stock(identity, args.org_id, args.apply)
    elif args.operation == 'reconcile-reports':
        result = reconciliation.reconcile_reports(identity, args.org_id, args.apply)
    elif args.operation == 'backfill-directory':
        result = reconciliation.backfill_directory(args.org_id, args.apply)
    else:
        if not args.cycle_id:
            parser.error('cleanup-payroll requires --cycle-id')
        model.validate_id('payroll', args.cycle_id)
        result = payroll_records.cleanup(get_table('PAYROLL_TABLE'), f'ORG#{args.org_id}#PROJECT#{args.project_id}', args.cycle_id, args.apply, identity)
    print(json.dumps({'account': account, **result}, default=str, indent=2))


if __name__ == '__main__':
    main()
