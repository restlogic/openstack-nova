from bees import profiler as p
from nova.compute import task_states
from nova.compute import vm_states
import nova.conf
from nova import objects
from nova.virt import block_device as driver_block_device
CONF = nova.conf.CONF

@p.trace_cls('ImageCacheManager')
class ImageCacheManager(object):
    """Base class for the image cache manager.

    This class will provide a generic interface to the image cache manager.
    """

    def __init__(self):
        self.remove_unused_base_images = CONF.image_cache.remove_unused_base_images
        self.resize_states = [task_states.RESIZE_PREP, task_states.RESIZE_MIGRATING, task_states.RESIZE_MIGRATED, task_states.RESIZE_FINISH]

    def _get_base(self):
        """Returns the base directory of the cached images."""
        raise NotImplementedError()

    def _list_running_instances(self, context, all_instances):
        """List running instances (on all compute nodes).

        This method returns a dictionary with the following keys:
            - used_images
            - instance_names
            - used_swap_images
            - used_ephemeral_images
        """
        used_images = {}
        instance_names = set()
        used_swap_images = set()
        used_ephemeral_images = set()
        instance_bdms = objects.BlockDeviceMappingList.bdms_by_instance_uuid(context, [instance.uuid for instance in all_instances])
        for instance in all_instances:
            instance_names.add(instance.name)
            instance_names.add(instance.uuid)
            if instance.task_state in self.resize_states or instance.vm_state == vm_states.RESIZED:
                instance_names.add(instance.name + '_resize')
                instance_names.add(instance.uuid + '_resize')
            for image_key in ['image_ref', 'kernel_id', 'ramdisk_id']:
                image_ref_str = getattr(instance, image_key)
                if image_ref_str is None:
                    continue
                (local, remote, insts) = used_images.get(image_ref_str, (0, 0, []))
                if instance.host == CONF.host:
                    local += 1
                else:
                    remote += 1
                insts.append(instance.name)
                used_images[image_ref_str] = (local, remote, insts)
            bdms = instance_bdms.get(instance.uuid)
            if bdms:
                swap = driver_block_device.convert_swap(bdms)
                if swap:
                    swap_image = 'swap_' + str(swap[0]['swap_size'])
                    used_swap_images.add(swap_image)
                ephemeral = driver_block_device.convert_ephemerals(bdms)
                if ephemeral:
                    os_type = nova.privsep.fs.get_fs_type_for_os_type(instance.os_type)
                    file_name = nova.privsep.fs.get_file_extension_for_os_type(os_type, CONF.default_ephemeral_format)
                    ephemeral_gb = str(ephemeral[0]['size'])
                    ephemeral_image = 'ephemeral_%s_%s' % (ephemeral_gb, file_name)
                    used_ephemeral_images.add(ephemeral_image)
        return {'used_images': used_images, 'instance_names': instance_names, 'used_swap_images': used_swap_images, 'used_ephemeral_images': used_ephemeral_images}

    def _scan_base_images(self, base_dir):
        """Scan base images present in base_dir and populate internal
        state.
        """
        raise NotImplementedError()

    def _age_and_verify_cached_images(self, context, all_instances, base_dir):
        """Ages and verifies cached images."""
        raise NotImplementedError()

    def update(self, context, all_instances):
        """The cache manager.

        This will invoke the cache manager. This will update the cache
        according to the defined cache management scheme. The information
        populated in the cached stats will be used for the cache management.
        """
        raise NotImplementedError()

    def get_disk_usage(self):
        """Return the size of the physical disk space used for the cache.

        :returns: The disk space in bytes that is occupied from
                  CONF.instances_path or zero if the cache directory is mounted
                  to a different disk device.
        """
        raise NotImplementedError()