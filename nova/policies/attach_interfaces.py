from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-attach-interfaces'
POLICY_ROOT = 'os_compute_api:os-attach-interfaces:%s'
DEPRECATED_INTERFACES_POLICY = policy.DeprecatedRule(BASE_POLICY_NAME, base.RULE_ADMIN_OR_OWNER)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
attach_interfaces_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'list', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='List port interfaces attached to a server', operations=[{'method': 'GET', 'path': '/servers/{server_id}/os-interface'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_INTERFACES_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'show', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show details of a port interface attached to a server', operations=[{'method': 'GET', 'path': '/servers/{server_id}/os-interface/{port_id}'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_INTERFACES_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'create', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Attach an interface to a server', operations=[{'method': 'POST', 'path': '/servers/{server_id}/os-interface'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_INTERFACES_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'delete', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Detach an interface from a server', operations=[{'method': 'DELETE', 'path': '/servers/{server_id}/os-interface/{port_id}'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_INTERFACES_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0')]

@p.trace('list_rules')
def list_rules():
    return attach_interfaces_policies