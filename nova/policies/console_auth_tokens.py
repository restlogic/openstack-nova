from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-console-auth-tokens'
console_auth_tokens_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME, check_str=base.SYSTEM_READER, description='Show console connection information for a given console authentication token', operations=[{'method': 'GET', 'path': '/os-console-auth-tokens/{console_token}'}], scope_types=['system'])]

@p.trace('list_rules')
def list_rules():
    return console_auth_tokens_policies