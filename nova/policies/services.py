from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-services:%s'
DEPRECATED_SERVICE_POLICY = policy.DeprecatedRule('os_compute_api:os-services', base.RULE_ADMIN_API)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
services_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'list', check_str=base.SYSTEM_READER, description='List all running Compute services in a region.', operations=[{'method': 'GET', 'path': '/os-services'}], scope_types=['system'], deprecated_rule=DEPRECATED_SERVICE_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'update', check_str=base.SYSTEM_ADMIN, description='Update a Compute service.', operations=[{'method': 'PUT', 'path': '/os-services/{service_id}'}], scope_types=['system'], deprecated_rule=DEPRECATED_SERVICE_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'delete', check_str=base.SYSTEM_ADMIN, description='Delete a Compute service.', operations=[{'method': 'DELETE', 'path': '/os-services/{service_id}'}], scope_types=['system'], deprecated_rule=DEPRECATED_SERVICE_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0')]

@p.trace('list_rules')
def list_rules():
    return services_policies