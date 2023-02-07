from bees import profiler as p
from oslo_config import cfg
console_group = cfg.OptGroup('console', title='Console Options', help='\nOptions under this group allow to tune the configuration of the console proxy\nservice.\n\nNote: in configuration of every compute is a ``console_host`` option,\nwhich allows to select the console proxy service to connect to.\n')
console_opts = [cfg.ListOpt('allowed_origins', default=[], deprecated_group='DEFAULT', deprecated_name='console_allowed_origins', help='\nAdds list of allowed origins to the console websocket proxy to allow\nconnections from other origin hostnames.\nWebsocket proxy matches the host header with the origin header to\nprevent cross-site requests. This list specifies if any there are\nvalues other than host are allowed in the origin header.\n\nPossible values:\n\n* A list where each element is an allowed origin hostnames, else an empty list\n'), cfg.StrOpt('ssl_ciphers', help='\nOpenSSL cipher preference string that specifies what ciphers to allow for TLS\nconnections from clients.  For example::\n\n    ssl_ciphers = "kEECDH+aECDSA+AES:kEECDH+AES+aRSA:kEDH+aRSA+AES"\n\nSee the man page for the OpenSSL `ciphers` command for details of the cipher\npreference string format and allowed values::\n\n    https://www.openssl.org/docs/man1.1.0/man1/ciphers.html\n\nRelated options:\n\n* [DEFAULT] cert\n* [DEFAULT] key\n'), cfg.StrOpt('ssl_minimum_version', default='default', choices=[('default', 'Use the underlying system OpenSSL defaults'), ('tlsv1_1', 'Require TLS v1.1 or greater for TLS connections'), ('tlsv1_2', 'Require TLS v1.2 or greater for TLS connections'), ('tlsv1_3', 'Require TLS v1.3 or greater for TLS connections')], help='\nMinimum allowed SSL/TLS protocol version.\n\nRelated options:\n\n* [DEFAULT] cert\n* [DEFAULT] key\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(console_group)
    conf.register_opts(console_opts, group=console_group)

@p.trace('list_opts')
def list_opts():
    return {console_group: console_opts}