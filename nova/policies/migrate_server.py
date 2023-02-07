from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-migrate-server:%s'
migrate_server_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'migrate', check_str=base.SYSTEM_ADMIN, description='Cold migrate a server to a host', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (migrate)'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'migrate_live', check_str=base.SYSTEM_ADMIN, description='Live migrate a server to a new host without a reboot', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (os-migrateLive)'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return migrate_server_policies