from bees import profiler as p
'Database setup and migration commands.'
from nova.db.sqlalchemy import migration
IMPL = migration

@p.trace('db_sync')
def db_sync(version=None, database='main', context=None):
    """Migrate the database to `version` or the most recent version."""
    return IMPL.db_sync(version=version, database=database, context=context)

@p.trace('db_version')
def db_version(database='main', context=None):
    """Display the current database version."""
    return IMPL.db_version(database=database, context=context)

@p.trace('db_initial_version')
def db_initial_version(database='main'):
    """The starting version for the database."""
    return IMPL.db_initial_version(database=database)