from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-console-output'
console_output_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME, check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Show console output for a server', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (os-getConsoleOutput)'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return console_output_policies