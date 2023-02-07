from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-instance-usage-audit-log:%s'
DEPRECATED_POLICY = policy.DeprecatedRule('os_compute_api:os-instance-usage-audit-log', base.RULE_ADMIN_API)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
instance_usage_audit_log_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'list', check_str=base.SYSTEM_READER, description='List all usage audits.', operations=[{'method': 'GET', 'path': '/os-instance_usage_audit_log'}], scope_types=['system'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'show', check_str=base.SYSTEM_READER, description='List all usage audits occurred before a specified time for all servers on all compute hosts where usage auditing is configured', operations=[{'method': 'GET', 'path': '/os-instance_usage_audit_log/{before_timestamp}'}], scope_types=['system'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0')]

@p.trace('list_rules')
def list_rules():
    return instance_usage_audit_log_policies