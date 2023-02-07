from bees import profiler as p
from oslo_policy import policy
RULE_ADMIN_OR_OWNER = 'rule:admin_or_owner'
RULE_ADMIN_API = 'rule:admin_api'
RULE_ANY = '@'
RULE_NOBODY = '!'
DEPRECATED_ADMIN_POLICY = policy.DeprecatedRule(name=RULE_ADMIN_API, check_str='is_admin:True')
DEPRECATED_ADMIN_OR_OWNER_POLICY = policy.DeprecatedRule(name=RULE_ADMIN_OR_OWNER, check_str='is_admin:True or project_id:%(project_id)s')
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
SYSTEM_ADMIN = 'rule:system_admin_api'
SYSTEM_READER = 'rule:system_reader_api'
PROJECT_ADMIN = 'rule:project_admin_api'
PROJECT_MEMBER = 'rule:project_member_api'
PROJECT_READER = 'rule:project_reader_api'
PROJECT_MEMBER_OR_SYSTEM_ADMIN = 'rule:system_admin_or_owner'
PROJECT_READER_OR_SYSTEM_READER = 'rule:system_or_project_reader'
rules = [policy.RuleDefault('context_is_admin', 'role:admin', "Decides what is required for the 'is_admin:True' check to succeed."), policy.RuleDefault('admin_or_owner', 'is_admin:True or project_id:%(project_id)s', 'Default rule for most non-Admin APIs.', deprecated_for_removal=True, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.RuleDefault('admin_api', 'is_admin:True', 'Default rule for most Admin APIs.', deprecated_for_removal=True, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.RuleDefault(name='system_admin_api', check_str='role:admin and system_scope:all', description='Default rule for System Admin APIs.', deprecated_rule=DEPRECATED_ADMIN_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.RuleDefault(name='system_reader_api', check_str='role:reader and system_scope:all', description='Default rule for System level read only APIs.', deprecated_rule=DEPRECATED_ADMIN_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.RuleDefault('project_admin_api', 'role:admin and project_id:%(project_id)s', 'Default rule for Project level admin APIs.', deprecated_rule=DEPRECATED_ADMIN_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.RuleDefault('project_member_api', 'role:member and project_id:%(project_id)s', 'Default rule for Project level non admin APIs.', deprecated_rule=DEPRECATED_ADMIN_OR_OWNER_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.RuleDefault('project_reader_api', 'role:reader and project_id:%(project_id)s', 'Default rule for Project level read only APIs.'), policy.RuleDefault(name='system_admin_or_owner', check_str='rule:system_admin_api or rule:project_member_api', description='Default rule for System admin+owner APIs.', deprecated_rule=DEPRECATED_ADMIN_OR_OWNER_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0'), policy.RuleDefault('system_or_project_reader', 'rule:system_reader_api or rule:project_reader_api', 'Default rule for System+Project read only APIs.', deprecated_rule=DEPRECATED_ADMIN_OR_OWNER_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0')]

@p.trace('list_rules')
def list_rules():
    return rules