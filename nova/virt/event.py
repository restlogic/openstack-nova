from bees import profiler as p
'\nAsynchronous event notifications from virtualization drivers.\n\nThis module defines a set of classes representing data for\nvarious asynchronous events that can occur in a virtualization\ndriver.\n'
import time
from nova.i18n import _
EVENT_LIFECYCLE_STARTED = 0
EVENT_LIFECYCLE_STOPPED = 1
EVENT_LIFECYCLE_PAUSED = 2
EVENT_LIFECYCLE_RESUMED = 3
EVENT_LIFECYCLE_SUSPENDED = 4
EVENT_LIFECYCLE_POSTCOPY_STARTED = 5
EVENT_LIFECYCLE_MIGRATION_COMPLETED = 6
NAMES = {EVENT_LIFECYCLE_STARTED: _('Started'), EVENT_LIFECYCLE_STOPPED: _('Stopped'), EVENT_LIFECYCLE_PAUSED: _('Paused'), EVENT_LIFECYCLE_RESUMED: _('Resumed'), EVENT_LIFECYCLE_SUSPENDED: _('Suspended'), EVENT_LIFECYCLE_POSTCOPY_STARTED: _('Postcopy started'), EVENT_LIFECYCLE_MIGRATION_COMPLETED: _('Migration completed')}

@p.trace_cls('Event')
class Event(object):
    """Base class for all events emitted by a hypervisor.

    All events emitted by a virtualization driver are
    subclasses of this base object. The only generic
    information recorded in the base class is a timestamp
    indicating when the event first occurred. The timestamp
    is recorded as fractional seconds since the UNIX epoch.
    """

    def __init__(self, timestamp=None):
        if timestamp is None:
            self.timestamp = time.time()
        else:
            self.timestamp = timestamp

    def get_timestamp(self):
        return self.timestamp

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self.timestamp)

@p.trace_cls('InstanceEvent')
class InstanceEvent(Event):
    """Base class for all instance events.

    All events emitted by a virtualization driver which
    are associated with a virtual domain instance are
    subclasses of this base object. This object records
    the UUID associated with the instance.
    """

    def __init__(self, uuid, timestamp=None):
        super(InstanceEvent, self).__init__(timestamp)
        self.uuid = uuid

    def get_instance_uuid(self):
        return self.uuid

    def __repr__(self):
        return '<%s: %s, %s>' % (self.__class__.__name__, self.timestamp, self.uuid)

@p.trace_cls('LifecycleEvent')
class LifecycleEvent(InstanceEvent):
    """Class for instance lifecycle state change events.

    When a virtual domain instance lifecycle state changes,
    events of this class are emitted. The EVENT_LIFECYCLE_XX
    constants defined why lifecycle change occurred. This
    event allows detection of an instance starting/stopping
    without need for polling.
    """

    def __init__(self, uuid, transition, timestamp=None):
        super(LifecycleEvent, self).__init__(uuid, timestamp)
        self.transition = transition

    def get_transition(self):
        return self.transition

    def get_name(self):
        return NAMES.get(self.transition, _('Unknown'))

    def __repr__(self):
        return '<%s: %s, %s => %s>' % (self.__class__.__name__, self.timestamp, self.uuid, self.get_name())