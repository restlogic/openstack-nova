from bees import profiler as p
'Base class for classes that need database access.'
import nova.db.api

@p.trace_cls('Base')
class Base(object):
    """DB driver is injected in the init method."""

    def __init__(self):
        super(Base, self).__init__()
        self.db = nova.db.api