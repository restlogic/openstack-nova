from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-assisted-volume-snapshots:%s'
assisted_volume_snapshots_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'create', check_str=base.SYSTEM_ADMIN, description='Create an assisted volume snapshot', operations=[{'path': '/os-assisted-volume-snapshots', 'method': 'POST'}], scope_types=['system']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'delete', check_str=base.SYSTEM_ADMIN, description='Delete an assisted volume snapshot', operations=[{'path': '/os-assisted-volume-snapshots/{snapshot_id}', 'method': 'DELETE'}], scope_types=['system'])]

@p.trace('list_rules')
def list_rules():
    return assisted_volume_snapshots_policies