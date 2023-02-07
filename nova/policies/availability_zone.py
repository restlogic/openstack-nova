from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-availability-zone:%s'
availability_zone_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'list', check_str=base.RULE_ANY, description='List availability zone information without host information', operations=[{'method': 'GET', 'path': '/os-availability-zone'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'detail', check_str=base.SYSTEM_READER, description='List detailed availability zone information with host information', operations=[{'method': 'GET', 'path': '/os-availability-zone/detail'}], scope_types=['system'])]

@p.trace('list_rules')
def list_rules():
    return availability_zone_policies