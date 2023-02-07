from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
ROOT_POLICY = 'os_compute_api:os-baremetal-nodes'
BASE_POLICY_NAME = 'os_compute_api:os-baremetal-nodes:%s'
DEPRECATED_BAREMETAL_POLICY = policy.DeprecatedRule(ROOT_POLICY, base.RULE_ADMIN_API)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
baremetal_nodes_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'list', check_str=base.SYSTEM_READER, description='List and show details of bare metal nodes.\n\nThese APIs are proxy calls to the Ironic service and are deprecated.\n', operations=[{'method': 'GET', 'path': '/os-baremetal-nodes'}], scope_types=['system'], deprecated_rule=DEPRECATED_BAREMETAL_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=BASE_POLICY_NAME % 'show', check_str=base.SYSTEM_READER, description='Show action details for a server.', operations=[{'method': 'GET', 'path': '/os-baremetal-nodes/{node_id}'}], scope_types=['system'], deprecated_rule=DEPRECATED_BAREMETAL_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0')]

@p.trace('list_rules')
def list_rules():
    return baremetal_nodes_policies