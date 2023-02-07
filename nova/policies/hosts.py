from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-hosts'
POLICY_NAME = 'os_compute_api:os-hosts:%s'
DEPRECATED_POLICY = policy.DeprecatedRule(BASE_POLICY_NAME, base.RULE_ADMIN_API)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
hosts_policies = [policy.DocumentedRuleDefault(name=POLICY_NAME % 'list', check_str=base.SYSTEM_READER, description='List physical hosts.\n\nThis API is deprecated in favor of os-hypervisors and os-services.', operations=[{'method': 'GET', 'path': '/os-hosts'}], scope_types=['system'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'show', check_str=base.SYSTEM_READER, description='Show physical host.\n\nThis API is deprecated in favor of os-hypervisors and os-services.', operations=[{'method': 'GET', 'path': '/os-hosts/{host_name}'}], scope_types=['system'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'update', check_str=base.SYSTEM_ADMIN, description='Update physical host.\n\nThis API is deprecated in favor of os-hypervisors and os-services.', operations=[{'method': 'PUT', 'path': '/os-hosts/{host_name}'}], scope_types=['system'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'reboot', check_str=base.SYSTEM_ADMIN, description='Reboot physical host.\n\nThis API is deprecated in favor of os-hypervisors and os-services.', operations=[{'method': 'GET', 'path': '/os-hosts/{host_name}/reboot'}], scope_types=['system'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'shutdown', check_str=base.SYSTEM_ADMIN, description='Shutdown physical host.\n\nThis API is deprecated in favor of os-hypervisors and os-services.', operations=[{'method': 'GET', 'path': '/os-hosts/{host_name}/shutdown'}], scope_types=['system'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'start', check_str=base.SYSTEM_ADMIN, description='Start physical host.\n\nThis API is deprecated in favor of os-hypervisors and os-services.', operations=[{'method': 'GET', 'path': '/os-hosts/{host_name}/startup'}], scope_types=['system'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0')]

@p.trace('list_rules')
def list_rules():
    return hosts_policies