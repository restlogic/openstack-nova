from bees import profiler as p
from oslo_config import cfg
consoleauth_group = cfg.OptGroup(name='consoleauth', title='Console auth options')
consoleauth_opts = [cfg.IntOpt('token_ttl', default=600, min=0, deprecated_name='console_token_ttl', deprecated_group='DEFAULT', help='\nThe lifetime of a console auth token (in seconds).\n\nA console auth token is used in authorizing console access for a user.\nOnce the auth token time to live count has elapsed, the token is\nconsidered expired.  Expired tokens are then deleted.\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(consoleauth_group)
    conf.register_opts(consoleauth_opts, group=consoleauth_group)

@p.trace('list_opts')
def list_opts():
    return {consoleauth_group: consoleauth_opts}