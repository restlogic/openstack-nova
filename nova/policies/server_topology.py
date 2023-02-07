from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'compute:server:topology:%s'
server_topology_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'index', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show the NUMA topology data for a server', operations=[{'method': 'GET', 'path': '/servers/{server_id}/topology'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'host:index', check_str=base.SYSTEM_READER, description='Show the NUMA topology data for a server with host NUMA ID and CPU pinning information', operations=[{'method': 'GET', 'path': '/servers/{server_id}/topology'}], scope_types=['system'])]

@p.trace('list_rules')
def list_rules():
    return server_topology_policies