from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-rescue'
UNRESCUE_POLICY_NAME = 'os_compute_api:os-unrescue'
DEPRECATED_POLICY = policy.DeprecatedRule('os_compute_api:os-rescue', base.RULE_ADMIN_OR_OWNER)
DEPRECATED_REASON = '\nRescue/Unrescue API policies are made granular with new policy\nfor unrescue and keeping old policy for rescue.\n'
rescue_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME, check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Rescue a server', operations=[{'path': '/servers/{server_id}/action (rescue)', 'method': 'POST'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=UNRESCUE_POLICY_NAME, check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Unrescue a server', operations=[{'path': '/servers/{server_id}/action (unrescue)', 'method': 'POST'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0')]

@p.trace('list_rules')
def list_rules():
    return rescue_policies