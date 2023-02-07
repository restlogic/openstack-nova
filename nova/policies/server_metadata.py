from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:server-metadata:%s'
server_metadata_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'index', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='List all metadata of a server', operations=[{'path': '/servers/{server_id}/metadata', 'method': 'GET'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'show', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show metadata for a server', operations=[{'path': '/servers/{server_id}/metadata/{key}', 'method': 'GET'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'create', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Create metadata for a server', operations=[{'path': '/servers/{server_id}/metadata', 'method': 'POST'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'update_all', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Replace metadata for a server', operations=[{'path': '/servers/{server_id}/metadata', 'method': 'PUT'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'update', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Update metadata from a server', operations=[{'path': '/servers/{server_id}/metadata/{key}', 'method': 'PUT'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'delete', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Delete metadata from a server', operations=[{'path': '/servers/{server_id}/metadata/{key}', 'method': 'DELETE'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return server_metadata_policies