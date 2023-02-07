from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-server-external-events:%s'
server_external_events_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'create', check_str=base.SYSTEM_ADMIN, description='Create one or more external events', operations=[{'method': 'POST', 'path': '/os-server-external-events'}], scope_types=['system'])]

@p.trace('list_rules')
def list_rules():
    return server_external_events_policies