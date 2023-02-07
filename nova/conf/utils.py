from bees import profiler as p
'Common utilities for conf providers.\n\nThis module does not provide any actual conf options.\n'
from keystoneauth1 import loading as ks_loading
from oslo_config import cfg
_ADAPTER_VERSION_OPTS = ('version', 'min_version', 'max_version')

@p.trace('get_ksa_adapter_opts')
def get_ksa_adapter_opts(default_service_type, deprecated_opts=None):
    """Get auth, Session, and Adapter conf options from keystonauth1.loading.

    :param default_service_type: Default for the service_type conf option on
                                 the Adapter.
    :param deprecated_opts: dict of deprecated opts to register with the ksa
                            Adapter opts.  Works the same as the
                            deprecated_opts kwarg to:
                    keystoneauth1.loading.session.Session.register_conf_options
    :return: List of cfg.Opts.
    """
    opts = ks_loading.get_adapter_conf_options(include_deprecated=False, deprecated_opts=deprecated_opts)
    for opt in opts[:]:
        if opt.dest in _ADAPTER_VERSION_OPTS:
            opts.remove(opt)
    cfg.set_defaults(opts, valid_interfaces=['internal', 'public'], service_type=default_service_type)
    return opts

@p.trace('_dummy_opt')
def _dummy_opt(name):
    return cfg.Opt(name, type=lambda x: None)

@p.trace('register_ksa_opts')
def register_ksa_opts(conf, group, default_service_type, include_auth=True, deprecated_opts=None):
    """Register keystoneauth auth, Session, and Adapter opts.

    :param conf: oslo_config.cfg.CONF in which to register the options
    :param group: Conf group, or string name thereof, in which to register the
                  options.
    :param default_service_type: Default for the service_type conf option on
                                 the Adapter.
    :param include_auth: For service types where Nova is acting on behalf of
                         the user, auth should come from the user context.
                         In those cases, set this arg to False to avoid
                         registering ksa auth options.
    :param deprecated_opts: dict of deprecated opts to register with the ksa
                            Session or Adapter opts.  See docstring for
                            the deprecated_opts param of:
                    keystoneauth1.loading.session.Session.register_conf_options
    """
    group = getattr(group, 'name', group)
    ks_loading.register_session_conf_options(conf, group, deprecated_opts=deprecated_opts)
    if include_auth:
        ks_loading.register_auth_conf_options(conf, group)
    conf.register_opts(get_ksa_adapter_opts(default_service_type, deprecated_opts=deprecated_opts), group=group)
    for name in _ADAPTER_VERSION_OPTS:
        conf.register_opt(_dummy_opt(name), group=group)

@p.trace('list_opts')
def list_opts():
    return {}