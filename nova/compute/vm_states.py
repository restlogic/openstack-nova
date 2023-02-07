from bees import profiler as p
"Possible vm states for instances.\n\nCompute instance vm states represent the state of an instance as it pertains to\na user or administrator.\n\nvm_state describes a VM's current stable (not transition) state. That is, if\nthere is no ongoing compute API calls (running tasks), vm_state should reflect\nwhat the customer expect the VM to be. When combined with task states\n(task_states.py), a better picture can be formed regarding the instance's\nhealth and progress.\n\nSee http://wiki.openstack.org/VMState\n"
from nova.objects import fields
ACTIVE = fields.InstanceState.ACTIVE
BUILDING = fields.InstanceState.BUILDING
PAUSED = fields.InstanceState.PAUSED
SUSPENDED = fields.InstanceState.SUSPENDED
STOPPED = fields.InstanceState.STOPPED
RESCUED = fields.InstanceState.RESCUED
RESIZED = fields.InstanceState.RESIZED
SOFT_DELETED = fields.InstanceState.SOFT_DELETED
DELETED = fields.InstanceState.DELETED
ERROR = fields.InstanceState.ERROR
SHELVED = fields.InstanceState.SHELVED
SHELVED_OFFLOADED = fields.InstanceState.SHELVED_OFFLOADED
ALLOW_SOFT_REBOOT = [ACTIVE]
ALLOW_HARD_REBOOT = ALLOW_SOFT_REBOOT + [STOPPED, PAUSED, SUSPENDED, ERROR]
ALLOW_TRIGGER_CRASH_DUMP = [ACTIVE, PAUSED, RESCUED, RESIZED, ERROR]
ALLOW_RESOURCE_REMOVAL = [DELETED, SHELVED_OFFLOADED]