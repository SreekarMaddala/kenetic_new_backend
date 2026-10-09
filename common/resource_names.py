"""One naming convention for application resources and deployment environments."""
import re

PROJECT_NAME = 'kinetic-erp'
ENVIRONMENTS = ('dev', 'prod')
TABLE_FUNCTIONALITY = {
    'ORGANIZATIONS_TABLE': 'organizations',
    'USERS_TABLE': 'users',
    'PROJECTS_TABLE': 'projects',
    'PARTIES_TABLE': 'vendors-contractors',
    'WORKFORCE_TABLE': 'workforce',
    'FIELD_OPERATIONS_TABLE': 'site-activity',
    'SITE_CONTROL_TABLE': 'site-control',
    'MATERIALS_TABLE': 'materials',
    'TRANSPORT_TABLE': 'transport',
    'DOCUMENT_CONTROL_TABLE': 'documents',
    'INVENTORY_TABLE': 'inventory',
    'FINANCE_TABLE': 'finance',
    'PAYROLL_TABLE': 'payroll',
    'REPORTING_TABLE': 'reporting',
    'SETTINGS_TABLE': 'settings',
    'AUDIT_EVENTS_TABLE': 'audit-events',
}


def resource_name(functionality, environment='dev', project_name=PROJECT_NAME):
    if environment not in ENVIRONMENTS:
        raise ValueError('Environment must be dev or prod')
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,29}', project_name):
        raise ValueError('Project name must contain 2 to 30 lowercase letters, digits or hyphens')
    return f'{project_name}-{functionality}-{environment}'


def table_names(environment='dev', project_name=PROJECT_NAME):
    return {env: resource_name(functionality, environment, project_name)
            for env, functionality in TABLE_FUNCTIONALITY.items()}
