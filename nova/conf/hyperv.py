from bees import profiler as p
from oslo_config import cfg
hyperv_opt_group = cfg.OptGroup('hyperv', title='The Hyper-V feature', help='\nThe hyperv feature allows you to configure the Hyper-V hypervisor\ndriver to be used within an OpenStack deployment.\n')
hyperv_opts = [cfg.FloatOpt('dynamic_memory_ratio', default=1.0, help='\nDynamic memory ratio\n\nEnables dynamic memory allocation (ballooning) when set to a value\ngreater than 1. The value expresses the ratio between the total RAM\nassigned to an instance and its startup RAM amount. For example a\nratio of 2.0 for an instance with 1024MB of RAM implies 512MB of\nRAM allocated at startup.\n\nPossible values:\n\n* 1.0: Disables dynamic memory allocation (Default).\n* Float values greater than 1.0: Enables allocation of total implied\n  RAM divided by this value for startup.\n'), cfg.BoolOpt('enable_instance_metrics_collection', default=False, help="\nEnable instance metrics collection\n\nEnables metrics collections for an instance by using Hyper-V's\nmetric APIs. Collected data can be retrieved by other apps and\nservices, e.g.: Ceilometer.\n"), cfg.StrOpt('instances_path_share', default='', help='\nInstances path share\n\nThe name of a Windows share mapped to the "instances_path" dir\nand used by the resize feature to copy files to the target host.\nIf left blank, an administrative share (hidden network share) will\nbe used, looking for the same "instances_path" used locally.\n\nPossible values:\n\n* "": An administrative share will be used (Default).\n* Name of a Windows share.\n\nRelated options:\n\n* "instances_path": The directory which will be used if this option\n  here is left blank.\n'), cfg.BoolOpt('limit_cpu_features', default=False, help='\nLimit CPU features\n\nThis flag is needed to support live migration to hosts with\ndifferent CPU features and checked during instance creation\nin order to limit the CPU features used by the instance.\n'), cfg.IntOpt('mounted_disk_query_retry_count', default=10, min=0, help='\nMounted disk query retry count\n\nThe number of times to retry checking for a mounted disk.\nThe query runs until the device can be found or the retry\ncount is reached.\n\nPossible values:\n\n* Positive integer values. Values greater than 1 is recommended\n  (Default: 10).\n\nRelated options:\n\n* Time interval between disk mount retries is declared with\n  "mounted_disk_query_retry_interval" option.\n'), cfg.IntOpt('mounted_disk_query_retry_interval', default=5, min=0, help='\nMounted disk query retry interval\n\nInterval between checks for a mounted disk, in seconds.\n\nPossible values:\n\n* Time in seconds (Default: 5).\n\nRelated options:\n\n* This option is meaningful when the mounted_disk_query_retry_count\n  is greater than 1.\n* The retry loop runs with mounted_disk_query_retry_count and\n  mounted_disk_query_retry_interval configuration options.\n'), cfg.IntOpt('power_state_check_timeframe', default=60, min=0, help='\nPower state check timeframe\n\nThe timeframe to be checked for instance power state changes.\nThis option is used to fetch the state of the instance from Hyper-V\nthrough the WMI interface, within the specified timeframe.\n\nPossible values:\n\n* Timeframe in seconds (Default: 60).\n'), cfg.IntOpt('power_state_event_polling_interval', default=2, min=0, help='\nPower state event polling interval\n\nInstance power state change event polling frequency. Sets the\nlistener interval for power state events to the given value.\nThis option enhances the internal lifecycle notifications of\ninstances that reboot themselves. It is unlikely that an operator\nhas to change this value.\n\nPossible values:\n\n* Time in seconds (Default: 2).\n'), cfg.StrOpt('qemu_img_cmd', default='qemu-img.exe', help='\nqemu-img command\n\nqemu-img is required for some of the image related operations\nlike converting between different image types. You can get it\nfrom here: (http://qemu.weilnetz.de/) or you can install the\nCloudbase OpenStack Hyper-V Compute Driver\n(https://cloudbase.it/openstack-hyperv-driver/) which automatically\nsets the proper path for this config option. You can either give the\nfull path of qemu-img.exe or set its path in the PATH environment\nvariable and leave this option to the default value.\n\nPossible values:\n\n* Name of the qemu-img executable, in case it is in the same\n  directory as the nova-compute service or its path is in the\n  PATH environment variable (Default).\n* Path of qemu-img command (DRIVELETTER:\\PATH\\TO\\QEMU-IMG\\COMMAND).\n\nRelated options:\n\n* If the config_drive_cdrom option is False, qemu-img will be used to\n  convert the ISO to a VHD, otherwise the config drive will\n  remain an ISO. To use config drive with Hyper-V, you must\n  set the ``mkisofs_cmd`` value to the full path to an ``mkisofs.exe``\n  installation.\n'), cfg.StrOpt('vswitch_name', help='\nExternal virtual switch name\n\nThe Hyper-V Virtual Switch is a software-based layer-2 Ethernet\nnetwork switch that is available with the installation of the\nHyper-V server role. The switch includes programmatically managed\nand extensible capabilities to connect virtual machines to both\nvirtual networks and the physical network. In addition, Hyper-V\nVirtual Switch provides policy enforcement for security, isolation,\nand service levels. The vSwitch represented by this config option\nmust be an external one (not internal or private).\n\nPossible values:\n\n* If not provided, the first of a list of available vswitches\n  is used. This list is queried using WQL.\n* Virtual switch name.\n'), cfg.IntOpt('wait_soft_reboot_seconds', default=60, min=0, help='\nWait soft reboot seconds\n\nNumber of seconds to wait for instance to shut down after soft\nreboot request is made. We fall back to hard reboot if instance\ndoes not shutdown within this window.\n\nPossible values:\n\n* Time in seconds (Default: 60).\n'), cfg.BoolOpt('config_drive_cdrom', default=False, help='\nMount config drive as a CD drive.\n\nOpenStack can be configured to write instance metadata to a config drive, which\nis then attached to the instance before it boots. The config drive can be\nattached as a disk drive (default) or as a CD drive.\n\nRelated options:\n\n* This option is meaningful with ``force_config_drive`` option set to ``True``\n  or when the REST API call to create an instance will have\n  ``--config-drive=True`` flag.\n* ``config_drive_format`` option must be set to ``iso9660`` in order to use\n  CD drive as the config drive image.\n* To use config drive with Hyper-V, you must set the\n  ``mkisofs_cmd`` value to the full path to an ``mkisofs.exe`` installation.\n  Additionally, you must set the ``qemu_img_cmd`` value to the full path\n  to an ``qemu-img`` command installation.\n* You can configure the Compute service to always create a configuration\n  drive by setting the ``force_config_drive`` option to ``True``.\n'), cfg.BoolOpt('config_drive_inject_password', default=False, help='\nInject password to config drive.\n\nWhen enabled, the admin password will be available from the config drive image.\n\nRelated options:\n\n* This option is meaningful when used with other options that enable\n  config drive usage with Hyper-V, such as ``force_config_drive``.\n'), cfg.IntOpt('volume_attach_retry_count', default=10, min=0, help='\nVolume attach retry count\n\nThe number of times to retry attaching a volume. Volume attachment\nis retried until success or the given retry count is reached.\n\nPossible values:\n\n* Positive integer values (Default: 10).\n\nRelated options:\n\n* Time interval between attachment attempts is declared with\n  volume_attach_retry_interval option.\n'), cfg.IntOpt('volume_attach_retry_interval', default=5, min=0, help='\nVolume attach retry interval\n\nInterval between volume attachment attempts, in seconds.\n\nPossible values:\n\n* Time in seconds (Default: 5).\n\nRelated options:\n\n* This options is meaningful when volume_attach_retry_count\n  is greater than 1.\n* The retry loop runs with volume_attach_retry_count and\n  volume_attach_retry_interval configuration options.\n'), cfg.BoolOpt('enable_remotefx', default=False, help='\nEnable RemoteFX feature\n\nThis requires at least one DirectX 11 capable graphics adapter for\nWindows / Hyper-V Server 2012 R2 or newer and RDS-Virtualization\nfeature has to be enabled.\n\nInstances with RemoteFX can be requested with the following flavor\nextra specs:\n\n**os:resolution**. Guest VM screen resolution size. Acceptable values::\n\n    1024x768, 1280x1024, 1600x1200, 1920x1200, 2560x1600, 3840x2160\n\n``3840x2160`` is only available on Windows / Hyper-V Server 2016.\n\n**os:monitors**. Guest VM number of monitors. Acceptable values::\n\n    [1, 4] - Windows / Hyper-V Server 2012 R2\n    [1, 8] - Windows / Hyper-V Server 2016\n\n**os:vram**. Guest VM VRAM amount. Only available on\nWindows / Hyper-V Server 2016. Acceptable values::\n\n    64, 128, 256, 512, 1024\n'), cfg.BoolOpt('use_multipath_io', default=False, help='\nUse multipath connections when attaching iSCSI or FC disks.\n\nThis requires the Multipath IO Windows feature to be enabled. MPIO must be\nconfigured to claim such devices.\n'), cfg.ListOpt('iscsi_initiator_list', default=[], help='\nList of iSCSI initiators that will be used for estabilishing iSCSI sessions.\n\nIf none are specified, the Microsoft iSCSI initiator service will choose the\ninitiator.\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(hyperv_opt_group)
    conf.register_opts(hyperv_opts, group=hyperv_opt_group)

@p.trace('list_opts')
def list_opts():
    return {hyperv_opt_group: hyperv_opts}