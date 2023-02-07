from bees import profiler as p
from oslo_config import cfg
guestfs_group = cfg.OptGroup('guestfs', title='Guestfs Options', help='\nlibguestfs is a set of tools for accessing and modifying virtual\nmachine (VM) disk images. You can use this for viewing and editing\nfiles inside guests, scripting changes to VMs, monitoring disk\nused/free statistics, creating guests, P2V, V2V, performing backups,\ncloning VMs, building VMs, formatting disks and resizing disks.\n')
enable_guestfs_debug_opts = [cfg.BoolOpt('debug', default=False, help='\nEnable/disables guestfs logging.\n\nThis configures guestfs to debug messages and push them to OpenStack\nlogging system. When set to True, it traces libguestfs API calls and\nenable verbose debug messages. In order to use the above feature,\n"libguestfs" package must be installed.\n\nRelated options:\n\nSince libguestfs access and modifies VM\'s managed by libvirt, below options\nshould be set to give access to those VM\'s.\n\n* ``libvirt.inject_key``\n* ``libvirt.inject_partition``\n* ``libvirt.inject_password``\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(guestfs_group)
    conf.register_opts(enable_guestfs_debug_opts, group=guestfs_group)

@p.trace('list_opts')
def list_opts():
    return {guestfs_group: enable_guestfs_debug_opts}