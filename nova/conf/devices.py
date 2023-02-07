from bees import profiler as p
from oslo_config import cfg
devices_group = cfg.OptGroup(name='devices', title='physical or virtual device options')
vgpu_opts = [cfg.ListOpt('enabled_vgpu_types', default=[], help='\nThe vGPU types enabled in the compute node.\n\nSome pGPUs (e.g. NVIDIA GRID K1) support different vGPU types. User can use\nthis option to specify a list of enabled vGPU types that may be assigned to a\nguest instance.\n\nIf more than one single vGPU type is provided, then for each *vGPU type* an\nadditional section, ``[vgpu_$(VGPU_TYPE)]``, must be added to the configuration\nfile. Each section then **must** be configured with a single configuration\noption, ``device_addresses``, which should be a list of PCI addresses\ncorresponding to the physical GPU(s) to assign to this type.\n\nIf one or more sections are missing (meaning that a specific type is not wanted\nto use for at least one physical GPU) or if no device addresses are provided,\nthen Nova will only use the first type that was provided by\n``[devices]/enabled_vgpu_types``.\n\nIf the same PCI address is provided for two different types, nova-compute will\nreturn an InvalidLibvirtGPUConfig exception at restart.\n\nAn example is as the following::\n\n    [devices]\n    enabled_vgpu_types = nvidia-35, nvidia-36\n\n    [vgpu_nvidia-35]\n    device_addresses = 0000:84:00.0,0000:85:00.0\n\n    [vgpu_nvidia-36]\n    device_addresses = 0000:86:00.0\n\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(devices_group)
    conf.register_opts(vgpu_opts, group=devices_group)

@p.trace('register_dynamic_opts')
def register_dynamic_opts(conf):
    """Register dynamically-generated options and groups.

    This must be called by the service that wishes to use the options **after**
    the initial configuration has been loaded.
    """
    opt = cfg.ListOpt('device_addresses', default=[], item_type=cfg.types.String())
    for vgpu_type in conf.devices.enabled_vgpu_types:
        conf.register_opt(opt, group='vgpu_%s' % vgpu_type)

@p.trace('list_opts')
def list_opts():
    return {devices_group: vgpu_opts}