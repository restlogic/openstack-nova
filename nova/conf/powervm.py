from bees import profiler as p
from oslo_config import cfg
powervm_group = cfg.OptGroup(name='powervm', title='PowerVM Options', help='\nPowerVM options allow cloud administrators to configure how OpenStack will work\nwith the PowerVM hypervisor.\n')
powervm_opts = [cfg.FloatOpt('proc_units_factor', default=0.1, min=0.05, max=1, help='\nFactor used to calculate the amount of physical processor compute power given\nto each vCPU. E.g. A value of 1.0 means a whole physical processor, whereas\n0.05 means 1/20th of a physical processor.\n'), cfg.StrOpt('disk_driver', choices=['localdisk', 'ssp'], ignore_case=True, default='localdisk', help='\nThe disk driver to use for PowerVM disks. PowerVM provides support for\nlocaldisk and PowerVM Shared Storage Pool disk drivers.\n\nRelated options:\n\n* volume_group_name - required when using localdisk\n\n'), cfg.StrOpt('volume_group_name', default='', help='\nVolume Group to use for block device operations. If disk_driver is localdisk,\nthen this attribute must be specified. It is strongly recommended NOT to use\nrootvg since that is used by the management partition and filling it will cause\nfailures.\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(powervm_group)
    conf.register_opts(powervm_opts, group=powervm_group)

@p.trace('list_opts')
def list_opts():
    return {powervm_group: powervm_opts}