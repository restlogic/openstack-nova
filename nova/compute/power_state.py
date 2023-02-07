from bees import profiler as p
"Power state is the state we get by calling virt driver on a particular\ndomain. The hypervisor is always considered the authority on the status\nof a particular VM, and the power_state in the DB should be viewed as a\nsnapshot of the VMs's state in the (recent) past. It can be periodically\nupdated, and should also be updated at the end of a task if the task is\nsupposed to affect power_state.\n"
from nova.objects import fields
NOSTATE = fields.InstancePowerState.index(fields.InstancePowerState.NOSTATE)
RUNNING = fields.InstancePowerState.index(fields.InstancePowerState.RUNNING)
PAUSED = fields.InstancePowerState.index(fields.InstancePowerState.PAUSED)
SHUTDOWN = fields.InstancePowerState.index(fields.InstancePowerState.SHUTDOWN)
CRASHED = fields.InstancePowerState.index(fields.InstancePowerState.CRASHED)
SUSPENDED = fields.InstancePowerState.index(fields.InstancePowerState.SUSPENDED)
STATE_MAP = {fields.InstancePowerState.index(state): state for state in fields.InstancePowerState.ALL if state != fields.InstancePowerState._UNUSED}