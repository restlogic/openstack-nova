from bees import profiler as p
from oslo_config import cfg
novnc_opts = [cfg.StrOpt('record', help='\nFilename that will be used for storing websocket frames received\nand sent by a proxy service (like VNC, spice, serial) running on this host.\nIf this is not set, no recording will be done.\n'), cfg.BoolOpt('daemon', default=False, help='Run as a background process.'), cfg.BoolOpt('ssl_only', default=False, help='\nDisallow non-encrypted connections.\n\nRelated options:\n\n* cert\n* key\n'), cfg.BoolOpt('source_is_ipv6', default=False, help='Set to True if source host is addressed with IPv6.'), cfg.StrOpt('cert', default='self.pem', help='\nPath to SSL certificate file.\n\nRelated options:\n\n* key\n* ssl_only\n* [console] ssl_ciphers\n* [console] ssl_minimum_version\n'), cfg.StrOpt('key', help='\nSSL key file (if separate from cert).\n\nRelated options:\n\n* cert\n'), cfg.StrOpt('web', default='/usr/share/spice-html5', help='\nPath to directory with content which will be served by a web server.\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_opts(novnc_opts)

@p.trace('register_cli_opts')
def register_cli_opts(conf):
    conf.register_cli_opts(novnc_opts)

@p.trace('list_opts')
def list_opts():
    return {'DEFAULT': novnc_opts}