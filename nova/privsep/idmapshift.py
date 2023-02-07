from bees import profiler as p
"\n\nIDMapShift is a tool that properly sets the ownership of a filesystem for use\nwith linux user namespaces.\n\nWhen using user namespaces with linux containers, the filesystem of the\ncontainer must be owned by the targeted user and group ids being applied\nto that container. Otherwise, processes inside the container won't be able\nto access the filesystem.\n\nFor example, when using the id map string '0:10000:2000', this means that\nuser ids inside the container between 0 and 1999 will map to user ids on\nthe host between 10000 and 11999. Root (0) becomes 10000, user 1 becomes\n10001, user 50 becomes 10050 and user 1999 becomes 11999. This means that\nfiles that are owned by root need to actually be owned by user 10000, and\nfiles owned by 50 need to be owned by 10050, and so on.\n\nIDMapShift will take the uid and gid strings used for user namespaces and\nproperly set up the filesystem for use by those users. Uids and gids outside\nof provided ranges will be mapped to nobody (max uid/gid) so that they are\ninaccessible inside the container.\n"
import os
from oslo_log import log as logging
import nova.privsep
LOG = logging.getLogger(__name__)
NOBODY_ID = 65534

@p.trace('find_target_id')
def find_target_id(fsid, mappings, nobody, memo):
    if fsid not in memo:
        for (start, target, count) in mappings:
            if start <= fsid < start + count:
                memo[fsid] = fsid - start + target
                break
        else:
            memo[fsid] = nobody
    return memo[fsid]

@p.trace('print_chown')
def print_chown(path, uid, gid, target_uid, target_gid):
    LOG.debug('%s %s:%s -> %s:%s', path, uid, gid, target_uid, target_gid)

@p.trace('shift_path')
def shift_path(path, uid_mappings, gid_mappings, nobody, uid_memo, gid_memo):
    stat = os.lstat(path)
    uid = stat.st_uid
    gid = stat.st_gid
    target_uid = find_target_id(uid, uid_mappings, nobody, uid_memo)
    target_gid = find_target_id(gid, gid_mappings, nobody, gid_memo)
    print_chown(path, uid, gid, target_uid, target_gid)
    os.lchown(path, target_uid, target_gid)

@p.trace('shift_dir')
def shift_dir(fsdir, uid_mappings, gid_mappings, nobody):
    uid_memo = dict()
    gid_memo = dict()

    def shift_path_short(p):
        shift_path(p, uid_mappings, gid_mappings, nobody, uid_memo=uid_memo, gid_memo=gid_memo)
    shift_path_short(fsdir)
    for (root, dirs, files) in os.walk(fsdir):
        for d in dirs:
            path = os.path.join(root, d)
            shift_path_short(path)
        for f in files:
            path = os.path.join(root, f)
            shift_path_short(path)

@p.trace('confirm_path')
def confirm_path(path, uid_ranges, gid_ranges, nobody):
    stat = os.lstat(path)
    uid = stat.st_uid
    gid = stat.st_gid
    uid_in_range = True if uid == nobody else False
    gid_in_range = True if gid == nobody else False
    if not uid_in_range or not gid_in_range:
        for (start, end) in uid_ranges:
            if start <= uid <= end:
                uid_in_range = True
                break
        for (start, end) in gid_ranges:
            if start <= gid <= end:
                gid_in_range = True
                break
    return uid_in_range and gid_in_range

@p.trace('get_ranges')
def get_ranges(maps):
    return [(target, target + count - 1) for (start, target, count) in maps]

@p.trace('confirm_dir')
def confirm_dir(fsdir, uid_mappings, gid_mappings, nobody):
    uid_ranges = get_ranges(uid_mappings)
    gid_ranges = get_ranges(gid_mappings)
    if not confirm_path(fsdir, uid_ranges, gid_ranges, nobody):
        return False
    for (root, dirs, files) in os.walk(fsdir):
        for d in dirs:
            path = os.path.join(root, d)
            if not confirm_path(path, uid_ranges, gid_ranges, nobody):
                return False
        for f in files:
            path = os.path.join(root, f)
            if not confirm_path(path, uid_ranges, gid_ranges, nobody):
                return False
    return True

@p.trace('shift')
@nova.privsep.sys_admin_pctxt.entrypoint
def shift(path, uid_map, gid_map):
    if confirm_dir(uid_map, gid_map, path, NOBODY_ID):
        return
    shift_dir(path, uid_map, gid_map, NOBODY_ID)