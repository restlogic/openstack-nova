from bees import profiler as p

@p.trace_cls('Console')
class Console(object):

    def __init__(self, host, port, internal_access_path=None):
        self.host = host
        self.port = port
        self.internal_access_path = internal_access_path

    def get_connection_info(self, token, access_url):
        """Returns an unreferenced dict with connection information."""
        ret = dict(self.__dict__)
        ret['token'] = token
        ret['access_url'] = access_url
        return ret

@p.trace_cls('ConsoleVNC')
class ConsoleVNC(Console):
    pass

@p.trace_cls('ConsoleRDP')
class ConsoleRDP(Console):
    pass

@p.trace_cls('ConsoleSpice')
class ConsoleSpice(Console):

    def __init__(self, host, port, tlsPort, internal_access_path=None):
        super(ConsoleSpice, self).__init__(host, port, internal_access_path)
        self.tlsPort = tlsPort

@p.trace_cls('ConsoleSerial')
class ConsoleSerial(Console):
    pass

@p.trace_cls('ConsoleMKS')
class ConsoleMKS(Console):
    pass