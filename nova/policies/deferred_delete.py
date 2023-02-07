from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-deferred-delete:%s'
DEPRECATED_POLICY = policy.DeprecatedRule('os_compute_api:os-deferred-delete', base.RULE_ADMIN_OR_OWNER)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
deferred_delete_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'restore', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Restore a soft deleted server', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (restore)'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'force', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Force delete a server before deferred cleanup', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (forceDelete)'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0')]

@p.trace('list_rules')
def list_rules():
    return deferred_delete_policies