# Copyright 2010 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from webob import exc

from nova.api.openstack import wsgi
from bees import profiler as p

@p.trace_cls("ConsolesController")
class ConsolesController(wsgi.Controller):
    """(Removed) The Consoles controller for the OpenStack API.

    This was removed during the Ussuri release along with the nova-console
    service.
    """

    @wsgi.expected_errors(410)
    def index(self, req, server_id):
        raise exc.HTTPGone()

    @wsgi.expected_errors(410)
    def create(self, req, server_id, body):
        raise exc.HTTPGone()

    @wsgi.expected_errors(410)
    def show(self, req, server_id, id):
        raise exc.HTTPGone()

    @wsgi.expected_errors(410)
    def delete(self, req, server_id, id):
        raise exc.HTTPGone()
