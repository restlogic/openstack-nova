from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-networks:%s'
BASE_POLICY_NAME = 'os_compute_api:os-networks:view'
DEPRECATED_POLICY = policy.DeprecatedRule(BASE_POLICY_NAME, base.RULE_ADMIN_OR_OWNER)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
networks_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'list', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='List networks for the project.\n\nThis API is proxy calls to the Network service. This is deprecated.', operations=[{'method': 'GET', 'path': '/os-networks'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'show', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show network details.\n\nThis API is proxy calls to the Network service. This is deprecated.', operations=[{'method': 'GET', 'path': '/os-networks/{network_id}'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0')]

@p.trace('list_rules')
def list_rules():
    return networks_policies