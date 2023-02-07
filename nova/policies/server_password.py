from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-server-password:%s'
DEPRECATED_POLICY = policy.DeprecatedRule('os_compute_api:os-server-password', base.RULE_ADMIN_OR_OWNER)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
server_password_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'show', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show the encrypted administrative password of a server', operations=[{'method': 'GET', 'path': '/servers/{server_id}/os-server-password'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'clear', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Clear the encrypted administrative password of a server', operations=[{'method': 'DELETE', 'path': '/servers/{server_id}/os-server-password'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0')]

@p.trace('list_rules')
def list_rules():
    return server_password_policies