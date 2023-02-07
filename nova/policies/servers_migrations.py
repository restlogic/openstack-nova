from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:servers:migrations:%s'
servers_migrations_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'show', check_str=base.SYSTEM_READER, description='Show details for an in-progress live migration for a given server', operations=[{'method': 'GET', 'path': '/servers/{server_id}/migrations/{migration_id}'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'force_complete', check_str=base.SYSTEM_ADMIN, description='Force an in-progress live migration for a given server to complete', operations=[{'method': 'POST', 'path': '/servers/{server_id}/migrations/{migration_id}/action (force_complete)'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'delete', check_str=base.SYSTEM_ADMIN, description='Delete(Abort) an in-progress live migration', operations=[{'method': 'DELETE', 'path': '/servers/{server_id}/migrations/{migration_id}'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'index', check_str=base.SYSTEM_READER, description='Lists in-progress live migrations for a given server', operations=[{'method': 'GET', 'path': '/servers/{server_id}/migrations'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return servers_migrations_policies