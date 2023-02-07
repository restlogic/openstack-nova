from bees import profiler as p
from keystoneauth1 import loading as ks_loading
from oslo_config import cfg
SERVICE_USER_GROUP = 'service_user'
service_user = cfg.OptGroup(SERVICE_USER_GROUP, title='Service token authentication type options', help="\nConfiguration options for service to service authentication using a service\ntoken. These options allow sending a service token along with the user's token\nwhen contacting external REST APIs.\n")
service_user_opts = [cfg.BoolOpt('send_service_user_token', default=False, help="\nWhen True, if sending a user token to a REST API, also send a service token.\n\nNova often reuses the user token provided to the nova-api to talk to other REST\nAPIs, such as Cinder, Glance and Neutron. It is possible that while the user\ntoken was valid when the request was made to Nova, the token may expire before\nit reaches the other service. To avoid any failures, and to make it clear it is\nNova calling the service on the user's behalf, we include a service token along\nwith the user token. Should the user's token have expired, a valid service\ntoken ensures the REST API request will still be accepted by the keystone\nmiddleware.\n")]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(service_user)
    conf.register_opts(service_user_opts, group=service_user)
    ks_loading.register_session_conf_options(conf, SERVICE_USER_GROUP)
    ks_loading.register_auth_conf_options(conf, SERVICE_USER_GROUP)

@p.trace('list_opts')
def list_opts():
    return {service_user: service_user_opts + ks_loading.get_session_conf_options() + ks_loading.get_auth_common_conf_options() + ks_loading.get_auth_plugin_conf_options('password') + ks_loading.get_auth_plugin_conf_options('v2password') + ks_loading.get_auth_plugin_conf_options('v3password')}