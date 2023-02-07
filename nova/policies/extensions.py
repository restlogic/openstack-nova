from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:extensions'
extensions_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME, check_str=base.RULE_ANY, description='List available extensions and show information for an extension by alias', operations=[{'method': 'GET', 'path': '/extensions'}, {'method': 'GET', 'path': '/extensions/{alias}'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return extensions_policies