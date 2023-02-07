from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-flavor-manage:%s'
flavor_manage_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'create', check_str=base.SYSTEM_ADMIN, description='Create a flavor', operations=[{'method': 'POST', 'path': '/flavors'}], scope_types=['system']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'update', check_str=base.SYSTEM_ADMIN, description='Update a flavor', operations=[{'method': 'PUT', 'path': '/flavors/{flavor_id}'}], scope_types=['system']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'delete', check_str=base.SYSTEM_ADMIN, description='Delete a flavor', operations=[{'method': 'DELETE', 'path': '/flavors/{flavor_id}'}], scope_types=['system'])]

@p.trace('list_rules')
def list_rules():
    return flavor_manage_policies