from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:limits'
OTHER_PROJECT_LIMIT_POLICY_NAME = 'os_compute_api:limits:other_project'
DEPRECATED_POLICY = policy.DeprecatedRule('os_compute_api:os-used-limits', base.RULE_ADMIN_API)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
limits_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME, check_str=base.RULE_ANY, description='Show rate and absolute limits for the current user project', operations=[{'method': 'GET', 'path': '/limits'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=OTHER_PROJECT_LIMIT_POLICY_NAME, check_str=base.SYSTEM_READER, description='Show rate and absolute limits of other project.\n\nThis policy only checks if the user has access to the requested\nproject limits. And this check is performed only after the check\nos_compute_api:limits passes', operations=[{'method': 'GET', 'path': '/limits'}], scope_types=['system'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0')]

@p.trace('list_rules')
def list_rules():
    return limits_policies