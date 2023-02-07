from bees import profiler as p
from oslo_config import cfg
from oslo_db import options as oslo_db_options
_ENRICHED = False
api_db_group = cfg.OptGroup('api_database', title='API Database Options', help='\nThe *Nova API Database* is a separate database which is used for information\nwhich is used across *cells*. This database is mandatory since the Mitaka\nrelease (13.0.0).\n\nThis group should **not** be configured for the ``nova-compute`` service.\n')
api_db_opts = [cfg.StrOpt('connection', secret=True, help=' Do not set this for the ``nova-compute`` service.'), cfg.StrOpt('connection_parameters', default='', help=''), cfg.BoolOpt('sqlite_synchronous', default=True, help=''), cfg.StrOpt('slave_connection', secret=True, help=''), cfg.StrOpt('mysql_sql_mode', default='TRADITIONAL', help=''), cfg.IntOpt('connection_recycle_time', default=3600, deprecated_name='idle_timeout', help=''), cfg.IntOpt('max_pool_size', help=''), cfg.IntOpt('max_retries', default=10, help=''), cfg.IntOpt('retry_interval', default=10, help=''), cfg.IntOpt('max_overflow', help=''), cfg.IntOpt('connection_debug', default=0, help=''), cfg.BoolOpt('connection_trace', default=False, help=''), cfg.IntOpt('pool_timeout', help='')]

@p.trace('enrich_help_text')
def enrich_help_text(alt_db_opts):

    def get_db_opts():
        for (group_name, db_opts) in oslo_db_options.list_opts():
            if group_name == 'database':
                return db_opts
        return []
    for db_opt in get_db_opts():
        for alt_db_opt in alt_db_opts:
            if alt_db_opt.name == db_opt.name:
                alt_db_opt.help = db_opt.help + alt_db_opt.help

@p.trace('register_opts')
def register_opts(conf):
    conf.register_opts(api_db_opts, group=api_db_group)

@p.trace('list_opts')
def list_opts():
    global _ENRICHED
    if not _ENRICHED:
        enrich_help_text(api_db_opts)
        _ENRICHED = True
    return {api_db_group: api_db_opts}