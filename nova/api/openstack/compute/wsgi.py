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
"""WSGI application entry-point for Nova Compute API, installed by pbr."""

from nova.api.openstack import wsgi_app

from bees import initializer

NAME = "osapi_compute"


def init_application():
    initializer.init_from_conf('nova-osapi-compute-wsgi', eventlet=True, eventlet_scope_manager=True)
    return wsgi_app.init_application(NAME)
