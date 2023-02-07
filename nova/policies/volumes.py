from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-volumes'
POLICY_NAME = 'os_compute_api:os-volumes:%s'
DEPRECATED_POLICY = policy.DeprecatedRule(BASE_POLICY_NAME, base.RULE_ADMIN_OR_OWNER)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
volumes_policies = [policy.DocumentedRuleDefault(name=POLICY_NAME % 'list', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='List volumes.\n\nThis API is a proxy call to the Volume service. It is deprecated.', operations=[{'method': 'GET', 'path': '/os-volumes'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'create', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Create volume.\n\nThis API is a proxy call to the Volume service. It is deprecated.', operations=[{'method': 'POST', 'path': '/os-volumes'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'detail', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='List volumes detail.\n\nThis API is a proxy call to the Volume service. It is deprecated.', operations=[{'method': 'GET', 'path': '/os-volumes/detail'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'show', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show volume.\n\nThis API is a proxy call to the Volume service. It is deprecated.', operations=[{'method': 'GET', 'path': '/os-volumes/{volume_id}'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'delete', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Delete volume.\n\nThis API is a proxy call to the Volume service. It is deprecated.', operations=[{'method': 'DELETE', 'path': '/os-volumes/{volume_id}'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'snapshots:list', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='List snapshots.\n\nThis API is a proxy call to the Volume service. It is deprecated.', operations=[{'method': 'GET', 'path': '/os-snapshots'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'snapshots:create', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Create snapshots.\n\nThis API is a proxy call to the Volume service. It is deprecated.', operations=[{'method': 'POST', 'path': '/os-snapshots'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'snapshots:detail', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='List snapshots details.\n\nThis API is a proxy call to the Volume service. It is deprecated.', operations=[{'method': 'GET', 'path': '/os-snapshots/detail'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'snapshots:show', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show snapshot.\n\nThis API is a proxy call to the Volume service. It is deprecated.', operations=[{'method': 'GET', 'path': '/os-snapshots/{snapshot_id}'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'snapshots:delete', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Delete snapshot.\n\nThis API is a proxy call to the Volume service. It is deprecated.', operations=[{'method': 'DELETE', 'path': '/os-snapshots/{snapshot_id}'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0')]

@p.trace('list_rules')
def list_rules():
    return volumes_policies