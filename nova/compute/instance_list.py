from bees import profiler as p
import copy
from nova.compute import multi_cell_list
import nova.conf
from nova import context
from nova.db import api as db
from nova import exception
from nova import objects
from nova.objects import instance as instance_obj
CONF = nova.conf.CONF

@p.trace_cls('InstanceSortContext')
class InstanceSortContext(multi_cell_list.RecordSortContext):

    def __init__(self, sort_keys, sort_dirs):
        if not sort_keys:
            sort_keys = ['created_at', 'id']
            sort_dirs = ['desc', 'desc']
        if 'uuid' not in sort_keys:
            sort_keys = copy.copy(sort_keys) + ['uuid']
            sort_dirs = copy.copy(sort_dirs) + ['asc']
        super(InstanceSortContext, self).__init__(sort_keys, sort_dirs)

@p.trace_cls('InstanceLister')
class InstanceLister(multi_cell_list.CrossCellLister):

    def __init__(self, sort_keys, sort_dirs, cells=None, batch_size=None):
        super(InstanceLister, self).__init__(InstanceSortContext(sort_keys, sort_dirs), cells=cells, batch_size=batch_size)

    @property
    def marker_identifier(self):
        return 'uuid'

    def get_marker_record(self, ctx, marker):
        try:
            im = objects.InstanceMapping.get_by_instance_uuid(ctx, marker)
        except exception.InstanceMappingNotFound:
            raise exception.MarkerNotFound(marker=marker)
        elevated = ctx.elevated(read_deleted='yes')
        with context.target_cell(elevated, im.cell_mapping) as cctx:
            try:
                db_inst = db.instance_get_by_uuid(cctx, marker, columns_to_join=[])
            except exception.InstanceNotFound:
                raise exception.MarkerNotFound(marker=marker)
        return (im.cell_mapping.uuid, db_inst)

    def get_marker_by_values(self, ctx, values):
        return db.instance_get_by_sort_filters(ctx, self.sort_ctx.sort_keys, self.sort_ctx.sort_dirs, values)

    def get_by_filters(self, ctx, filters, limit, marker, **kwargs):
        return db.instance_get_all_by_filters_sort(ctx, filters, limit=limit, marker=marker, sort_keys=self.sort_ctx.sort_keys, sort_dirs=self.sort_ctx.sort_dirs, **kwargs)

@p.trace('get_instances_sorted')
def get_instances_sorted(ctx, filters, limit, marker, columns_to_join, sort_keys, sort_dirs, cell_mappings=None, batch_size=None, cell_down_support=False):
    instance_lister = InstanceLister(sort_keys, sort_dirs, cells=cell_mappings, batch_size=batch_size)
    instance_generator = instance_lister.get_records_sorted(ctx, filters, limit, marker, columns_to_join=columns_to_join, cell_down_support=cell_down_support)
    return (instance_lister, instance_generator)

@p.trace('get_instance_list_cells_batch_size')
def get_instance_list_cells_batch_size(limit, cells):
    """Calculate the proper batch size for a list request.

    This will consider config, request limit, and cells being queried and
    return an appropriate batch size to use for querying said cells.

    :param limit: The overall limit specified in the request
    :param cells: The list of CellMapping objects being queried
    :returns: An integer batch size
    """
    strategy = CONF.api.instance_list_cells_batch_strategy
    limit = limit or CONF.api.max_limit
    if len(cells) <= 1:
        return limit
    if strategy == 'fixed':
        batch_size = CONF.api.instance_list_cells_batch_fixed_size
    elif strategy == 'distributed':
        batch_size = int(limit / len(cells) * 1.1)
    return max(min(batch_size, limit), 100)

@p.trace('get_instance_objects_sorted')
def get_instance_objects_sorted(ctx, filters, limit, marker, expected_attrs, sort_keys, sort_dirs, cell_down_support=False):
    """Return a list of instances and information about down cells.

    This returns a tuple of (objects.InstanceList, list(of down cell
    uuids) for the requested operation. The instances returned are
    those that were collected from the cells that responded. The uuids
    of any cells that did not respond (or raised an error) are included
    in the list as the second element of the tuple. That list is empty
    if all cells responded.
    """
    query_cell_subset = CONF.api.instance_list_per_project_cells
    if query_cell_subset and (not ctx.is_admin):
        cell_mappings = objects.CellMappingList.get_by_project_id(ctx, ctx.project_id)
    else:
        context.load_cells()
        cell_mappings = context.CELLS
    batch_size = get_instance_list_cells_batch_size(limit, cell_mappings)
    columns_to_join = instance_obj._expected_cols(expected_attrs)
    (instance_lister, instance_generator) = get_instances_sorted(ctx, filters, limit, marker, columns_to_join, sort_keys, sort_dirs, cell_mappings=cell_mappings, batch_size=batch_size, cell_down_support=cell_down_support)
    if 'fault' in expected_attrs:
        expected_attrs = copy.copy(expected_attrs)
        expected_attrs.remove('fault')
    instance_list = instance_obj._make_instance_list(ctx, objects.InstanceList(), instance_generator, expected_attrs)
    down_cell_uuids = instance_lister.cells_failed + instance_lister.cells_timed_out
    return (instance_list, down_cell_uuids)