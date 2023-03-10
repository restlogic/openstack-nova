# Copyright (C) 2011 Midokura KK
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

"""The virtual interfaces extension."""

from webob import exc

from nova.api.openstack import wsgi
from bees import profiler as p

@p.trace_cls("ServerVirtualInterfaceController")
class ServerVirtualInterfaceController(wsgi.Controller):

    @wsgi.expected_errors((410))
    def index(self, req, server_id):
        raise exc.HTTPGone()
