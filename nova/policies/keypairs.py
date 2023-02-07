from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-keypairs:%s'
keypairs_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'index', check_str='(' + base.SYSTEM_READER + ') or user_id:%(user_id)s', description='List all keypairs', operations=[{'path': '/os-keypairs', 'method': 'GET'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'create', check_str='(' + base.SYSTEM_ADMIN + ') or user_id:%(user_id)s', description='Create a keypair', operations=[{'path': '/os-keypairs', 'method': 'POST'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'delete', check_str='(' + base.SYSTEM_ADMIN + ') or user_id:%(user_id)s', description='Delete a keypair', operations=[{'path': '/os-keypairs/{keypair_name}', 'method': 'DELETE'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'show', check_str='(' + base.SYSTEM_READER + ') or user_id:%(user_id)s', description='Show details of a keypair', operations=[{'path': '/os-keypairs/{keypair_name}', 'method': 'GET'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return keypairs_policies