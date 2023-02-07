from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-admin-password'
admin_password_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME, check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Change the administrative password for a server', operations=[{'path': '/servers/{server_id}/action (changePassword)', 'method': 'POST'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return admin_password_policies