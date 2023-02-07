from bees import profiler as p
import copy
from nova.compute import multi_cell_list
from nova import context
from nova.db import api as db
from nova import exception
from nova import objects
from nova.objects import base

@p.trace_cls('MigrationSortContext')
class MigrationSortContext(multi_cell_list.RecordSortContext):

    def __init__(self, sort_keys, sort_dirs):
        if not sort_keys:
            sort_keys = ['created_at', 'id']
            sort_dirs = ['desc', 'desc']
        if 'uuid' not in sort_keys:
            sort_keys = copy.copy(sort_keys) + ['uuid']
            sort_dirs = copy.copy(sort_dirs) + ['asc']
        super(MigrationSortContext, self).__init__(sort_keys, sort_dirs)

@p.trace_cls('MigrationLister')
class MigrationLister(multi_cell_list.CrossCellLister):

    def __init__(self, sort_keys, sort_dirs):
        super(MigrationLister, self).__init__(MigrationSortContext(sort_keys, sort_dirs))

    @property
    def marker_identifier(self):
        return 'uuid'

    def get_marker_record(self, ctx, marker):
        """Get the marker migration from its cell.

        This returns the marker migration from the cell in which it lives
        """
        results = context.scatter_gather_skip_cell0(ctx, db.migration_get_by_uuid, marker)
        db_migration = None
        for (result_cell_uuid, result) in results.items():
            if not context.is_cell_failure_sentinel(result):
                db_migration = result
                cell_uuid = result_cell_uuid
                break
        if not db_migration:
            raise exception.MarkerNotFound(marker=marker)
        return (cell_uuid, db_migration)

    def get_marker_by_values(self, ctx, values):
        return db.migration_get_by_sort_filters(ctx, self.sort_ctx.sort_keys, self.sort_ctx.sort_dirs, values)

    def get_by_filters(self, ctx, filters, limit, marker, **kwargs):
        return db.migration_get_all_by_filters(ctx, filters, limit=limit, marker=marker, sort_keys=self.sort_ctx.sort_keys, sort_dirs=self.sort_ctx.sort_dirs)

@p.trace('get_migration_objects_sorted')
def get_migration_objects_sorted(ctx, filters, limit, marker, sort_keys, sort_dirs):
    mig_generator = MigrationLister(sort_keys, sort_dirs).get_records_sorted(ctx, filters, limit, marker)
    return base.obj_make_list(ctx, objects.MigrationList(), objects.Migration, mig_generator)