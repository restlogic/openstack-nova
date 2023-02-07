from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-floating-ip-pools'
floating_ip_pools_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME, check_str=base.RULE_ANY, description='List floating IP pools. This API is deprecated.', operations=[{'method': 'GET', 'path': '/os-floating-ip-pools'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return floating_ip_pools_policies