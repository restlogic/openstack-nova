from bees import profiler as p
from oslo_cache import core

@p.trace('register_opts')
def register_opts(conf):
    core.configure(conf)

@p.trace('list_opts')
def list_opts():
    return dict(core._opts.list_opts())