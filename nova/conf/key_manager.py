from bees import profiler as p
from castellan import options as castellan_opts
from oslo_config import cfg
key_manager_group = cfg.OptGroup('key_manager', title='Key manager options')
key_manager_opts = [cfg.StrOpt('fixed_key', deprecated_group='keymgr', secret=True, help='\nFixed key returned by key manager, specified in hex.\n\nPossible values:\n\n* Empty string or a key in hex value\n')]

@p.trace('register_opts')
def register_opts(conf):
    castellan_opts.set_defaults(conf)
    conf.register_group(key_manager_group)
    conf.register_opts(key_manager_opts, group=key_manager_group)

@p.trace('list_opts')
def list_opts():
    opts = {key_manager_group.name: key_manager_opts}
    for (group, options) in castellan_opts.list_opts():
        if group not in opts.keys():
            opts[group] = options
        else:
            opts[group] = opts[group] + options
    return opts