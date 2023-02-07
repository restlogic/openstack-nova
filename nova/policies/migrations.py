from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-migrations:%s'
migrations_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'index', check_str=base.SYSTEM_READER, description='List migrations', operations=[{'method': 'GET', 'path': '/os-migrations'}], scope_types=['system'])]

@p.trace('list_rules')
def list_rules():
    return migrations_policies