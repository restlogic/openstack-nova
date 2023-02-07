from bees import profiler as p
"\nThis is the single point of entry to generate the sample configuration\nfile for Nova. It collects all the necessary info from the other modules\nin this package. It is assumed that:\n\n* every other module in this package has a 'list_opts' function which\n  return a dict where\n  * the keys are strings which are the group names\n  * the value of each key is a list of config options for that group\n* the nova.conf package doesn't have further packages with config options\n* this module is only used in the context of sample file generation\n"
import collections
import importlib
import os
import pkgutil
LIST_OPTS_FUNC_NAME = 'list_opts'

@p.trace('_tupleize')
def _tupleize(dct):
    """Take the dict of options and convert to the 2-tuple format."""
    return [(key, val) for (key, val) in dct.items()]

@p.trace('list_opts')
def list_opts():
    opts = collections.defaultdict(list)
    module_names = _list_module_names()
    imported_modules = _import_modules(module_names)
    _append_config_options(imported_modules, opts)
    return _tupleize(opts)

@p.trace('_list_module_names')
def _list_module_names():
    module_names = []
    package_path = os.path.dirname(os.path.abspath(__file__))
    for (_, modname, ispkg) in pkgutil.iter_modules(path=[package_path]):
        if modname == 'opts' or ispkg:
            continue
        else:
            module_names.append(modname)
    return module_names

@p.trace('_import_modules')
def _import_modules(module_names):
    imported_modules = []
    for modname in module_names:
        mod = importlib.import_module('nova.conf.' + modname)
        if not hasattr(mod, LIST_OPTS_FUNC_NAME):
            msg = "The module 'nova.conf.%s' should have a '%s' function which returns the config options." % (modname, LIST_OPTS_FUNC_NAME)
            raise Exception(msg)
        else:
            imported_modules.append(mod)
    return imported_modules

@p.trace('_append_config_options')
def _append_config_options(imported_modules, config_options):
    for mod in imported_modules:
        configs = mod.list_opts()
        for (key, val) in configs.items():
            config_options[key].extend(val)