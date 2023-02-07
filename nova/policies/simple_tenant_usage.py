from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-simple-tenant-usage:%s'
simple_tenant_usage_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'show', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show usage statistics for a specific tenant', operations=[{'method': 'GET', 'path': '/os-simple-tenant-usage/{tenant_id}'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'list', check_str=base.SYSTEM_READER, description='List per tenant usage statistics for all tenants', operations=[{'method': 'GET', 'path': '/os-simple-tenant-usage'}], scope_types=['system'])]

@p.trace('list_rules')
def list_rules():
    return simple_tenant_usage_policies