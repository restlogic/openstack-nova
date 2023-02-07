from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
ROOT_POLICY = 'os_compute_api:os-multinic'
BASE_POLICY_NAME = 'os_compute_api:os-multinic:%s'
DEPRECATED_POLICY = policy.DeprecatedRule(ROOT_POLICY, base.RULE_ADMIN_OR_OWNER)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
multinic_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'add', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Add a fixed IP address to a server.\n\nThis API is proxy calls to the Network service. This is\ndeprecated.', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (addFixedIp)'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'remove', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Remove a fixed IP address from a server.\n\nThis API is proxy calls to the Network service. This is\ndeprecated.', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (removeFixedIp)'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0')]

@p.trace('list_rules')
def list_rules():
    return multinic_policies