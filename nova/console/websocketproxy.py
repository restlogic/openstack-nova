from bees import profiler as p
'\nWebsocket proxy that is compatible with OpenStack Nova.\nLeverages websockify.py by Joel Martin\n'
import copy
from http import cookies as Cookie
from http import HTTPStatus
import os
import socket
import sys
from urllib import parse as urlparse
from oslo_log import log as logging
from oslo_utils import encodeutils
from oslo_utils import importutils
import websockify
from nova.compute import rpcapi as compute_rpcapi
import nova.conf
from nova import context
from nova import exception
from nova.i18n import _
from nova import objects
websockifyserver = importutils.try_import('websockify.websockifyserver')
LOG = logging.getLogger(__name__)
CONF = nova.conf.CONF

@p.trace_cls('TenantSock')
class TenantSock(object):
    """A socket wrapper for communicating with the tenant.

    This class provides a socket-like interface to the internal
    websockify send/receive queue for the client connection to
    the tenant user. It is used with the security proxy classes.
    """

    def __init__(self, reqhandler):
        self.reqhandler = reqhandler
        self.queue = []

    def recv(self, cnt):
        while len(self.queue) < cnt:
            (new_frames, closed) = self.reqhandler.recv_frames()
            for frame in new_frames:
                self.queue.extend([bytes(chr(c), 'ascii') for c in frame])
            if closed:
                break
        popped = self.queue[0:cnt]
        del self.queue[0:cnt]
        return b''.join(popped)

    def sendall(self, data):
        self.reqhandler.send_frames([encodeutils.safe_encode(data)])

    def finish_up(self):
        self.reqhandler.send_frames([b''.join(self.queue)])

    def close(self):
        self.finish_up()
        self.reqhandler.send_close()

@p.trace_cls('NovaProxyRequestHandler')
class NovaProxyRequestHandler(websockify.ProxyRequestHandler):

    def __init__(self, *args, **kwargs):
        self._compute_rpcapi = None
        websockify.ProxyRequestHandler.__init__(self, *args, **kwargs)

    @property
    def compute_rpcapi(self):
        if not self._compute_rpcapi:
            self._compute_rpcapi = compute_rpcapi.ComputeAPI()
        return self._compute_rpcapi

    def verify_origin_proto(self, connect_info, origin_proto):
        if 'access_url_base' not in connect_info:
            detail = _('No access_url_base in connect_info. Cannot validate protocol')
            raise exception.ValidationError(detail=detail)
        expected_protos = [urlparse.urlparse(connect_info.access_url_base).scheme]
        if 'ws' in expected_protos:
            expected_protos.append('http')
        if 'wss' in expected_protos:
            expected_protos.append('https')
        return origin_proto in expected_protos

    def _check_console_port(self, ctxt, instance_uuid, port, console_type):
        try:
            instance = objects.Instance.get_by_uuid(ctxt, instance_uuid)
        except exception.InstanceNotFound:
            return
        return self.compute_rpcapi.validate_console_port(ctxt, instance, str(port), console_type)

    def _get_connect_info(self, ctxt, token):
        """Validate the token and get the connect info."""
        connect_info = objects.ConsoleAuthToken.validate(ctxt, token)
        valid_port = self._check_console_port(ctxt, connect_info.instance_uuid, connect_info.port, connect_info.console_type)
        if not valid_port:
            raise exception.InvalidToken(token='***')
        return connect_info

    def new_websocket_client(self):
        """Called after a new WebSocket connection has been established."""
        from eventlet import hubs
        hubs.use_hub()
        parse = urlparse.urlparse(self.path)
        if parse.scheme not in ('http', 'https'):
            if sys.version_info < (2, 7, 4):
                raise exception.NovaException(_("We do not support scheme '%s' under Python < 2.7.4, please use http or https") % parse.scheme)
        query = parse.query
        token = urlparse.parse_qs(query).get('token', ['']).pop()
        if not token:
            hcookie = self.headers.get('cookie')
            if hcookie:
                cookie = Cookie.SimpleCookie()
                for hcookie_part in hcookie.split(';'):
                    hcookie_part = hcookie_part.lstrip()
                    try:
                        cookie.load(hcookie_part)
                    except Cookie.CookieError:
                        LOG.warning('Found malformed cookie')
                    else:
                        if 'token' in cookie:
                            token = cookie['token'].value
        ctxt = context.get_admin_context()
        connect_info = self._get_connect_info(ctxt, token)
        expected_origin_hostname = self.headers.get('Host')
        if ':' in expected_origin_hostname:
            e = expected_origin_hostname
            if '[' in e and ']' in e:
                expected_origin_hostname = e.split(']')[0][1:]
            else:
                expected_origin_hostname = e.split(':')[0]
        expected_origin_hostnames = CONF.console.allowed_origins
        expected_origin_hostnames.append(expected_origin_hostname)
        origin_url = self.headers.get('Origin')
        if origin_url is not None:
            origin = urlparse.urlparse(origin_url)
            origin_hostname = origin.hostname
            origin_scheme = origin.scheme
            forwarded_proto = self.headers.get('X-Forwarded-Proto')
            if forwarded_proto is not None:
                origin_scheme = forwarded_proto
            if origin_hostname == '' or origin_scheme == '':
                detail = _('Origin header not valid.')
                raise exception.ValidationError(detail=detail)
            if origin_hostname not in expected_origin_hostnames:
                detail = _('Origin header does not match this host.')
                raise exception.ValidationError(detail=detail)
            if not self.verify_origin_proto(connect_info, origin_scheme):
                detail = _('Origin header protocol does not match this host.')
                raise exception.ValidationError(detail=detail)
        sanitized_info = copy.copy(connect_info)
        sanitized_info.token = '***'
        self.msg(_('connect info: %s'), sanitized_info)
        host = connect_info.host
        port = connect_info.port
        self.msg(_('connecting to: %(host)s:%(port)s') % {'host': host, 'port': port})
        tsock = self.socket(host, port, connect=True)
        if 'internal_access_path' in connect_info:
            path = connect_info.internal_access_path
            if path:
                tsock.send(encodeutils.safe_encode('CONNECT %s HTTP/1.1\r\n\r\n' % path))
                end_token = '\r\n\r\n'
                while True:
                    data = tsock.recv(4096, socket.MSG_PEEK)
                    token_loc = data.find(end_token)
                    if token_loc != -1:
                        if data.split('\r\n')[0].find('200') == -1:
                            raise exception.InvalidConnectionInfo()
                        tsock.recv(token_loc + len(end_token))
                        break
        if self.server.security_proxy is not None:
            tenant_sock = TenantSock(self)
            try:
                tsock = self.server.security_proxy.connect(tenant_sock, tsock)
            except exception.SecurityProxyNegotiationFailed:
                LOG.exception('Unable to perform security proxying, shutting down connection')
                tenant_sock.close()
                tsock.shutdown(socket.SHUT_RDWR)
                tsock.close()
                raise
            tenant_sock.finish_up()
        try:
            self.do_proxy(tsock)
        except Exception:
            if tsock:
                tsock.shutdown(socket.SHUT_RDWR)
                tsock.close()
                self.vmsg(_('%(host)s:%(port)s: Websocket client or target closed') % {'host': host, 'port': port})
            raise

    def socket(self, *args, **kwargs):
        return websockifyserver.WebSockifyServer.socket(*args, **kwargs)

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            parts = urlparse.urlsplit(self.path)
            if not parts.path.endswith('/'):
                new_parts = (parts[0], parts[1], parts[2] + '/', parts[3], parts[4])
                new_url = urlparse.urlunsplit(new_parts)
                if new_url.startswith('//'):
                    self.send_error(HTTPStatus.BAD_REQUEST, 'URI must not start with //')
                    return None
        return super(NovaProxyRequestHandler, self).send_head()

@p.trace_cls('NovaWebSocketProxy')
class NovaWebSocketProxy(websockify.WebSocketProxy):

    def __init__(self, *args, **kwargs):
        """:param security_proxy: instance of
            nova.console.securityproxy.base.SecurityProxy

        Create a new web socket proxy, optionally using the
        @security_proxy instance to negotiate security layer
        with the compute node.
        """
        self.security_proxy = kwargs.pop('security_proxy', None)
        ssl_min_version = kwargs.pop('ssl_minimum_version', None)
        if ssl_min_version and ssl_min_version != 'default':
            kwargs['ssl_options'] = websockify.websocketproxy.select_ssl_version(ssl_min_version)
        super(NovaWebSocketProxy, self).__init__(*args, **kwargs)

    @staticmethod
    def get_logger():
        return LOG