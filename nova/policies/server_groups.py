from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-server-groups:%s'
server_groups_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'create', check_str=base.PROJECT_MEMBER, description='Create a new server group', operations=[{'path': '/os-server-groups', 'method': 'POST'}], scope_types=['project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'delete', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Delete a server group', operations=[{'path': '/os-server-groups/{server_group_id}', 'method': 'DELETE'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'index', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='List all server groups', operations=[{'path': '/os-server-groups', 'method': 'GET'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'index:all_projects', check_str=base.SYSTEM_READER, description='List all server groups for all projects', operations=[{'path': '/os-server-groups', 'method': 'GET'}], scope_types=['system']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'show', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show details of a server group', operations=[{'path': '/os-server-groups/{server_group_id}', 'method': 'GET'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return server_groups_policies