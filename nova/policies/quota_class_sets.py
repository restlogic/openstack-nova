from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-quota-class-sets:%s'
quota_class_sets_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'show', check_str=base.SYSTEM_READER, description='List quotas for specific quota classs', operations=[{'method': 'GET', 'path': '/os-quota-class-sets/{quota_class}'}], scope_types=['system']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'update', check_str=base.SYSTEM_ADMIN, description='Update quotas for specific quota class', operations=[{'method': 'PUT', 'path': '/os-quota-class-sets/{quota_class}'}], scope_types=['system'])]

@p.trace('list_rules')
def list_rules():
    return quota_class_sets_policies