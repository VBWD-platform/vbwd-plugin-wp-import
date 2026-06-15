"""Migration up/down/up for wp_imported_post (real PostgreSQL).

Loads the migration module directly and runs it through alembic's
Operations context, isolated from the conftest ``create_all`` (which
already built the table — so we drop it first to exercise a clean
upgrade). Validates the chain anchors on the always-present core root
and the revision id is ≤ 32 chars.
"""
import importlib.util
import os

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect


# This migration spec opens its OWN connection + transaction and rolls back
# itself, so it must run WITHOUT the autouse rolled-back-session isolation
# (which swaps ``db.engine`` for a Connection). See conftest ``no_db_isolation``.
pytestmark = pytest.mark.no_db_isolation


def _load_migration():
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "migrations",
        "versions",
        "20260612_1000_wp_import.py",
    )
    spec = importlib.util.spec_from_file_location("wp_import_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()
TABLE_NAME = "wp_imported_post"


@pytest.fixture
def migration_connection(app):
    from vbwd.extensions import db

    connection = db.engine.connect()
    transaction = connection.begin()
    operations = Operations(MigrationContext.configure(connection))
    # create_all() already built the table; drop it so upgrade runs clean.
    if inspect(connection).has_table(TABLE_NAME):
        operations.drop_table(TABLE_NAME)
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.integration
def test_revision_anchors_on_core_root_and_id_is_short():
    assert migration.revision == "20260612_1000_wp_import"
    assert migration.down_revision == "vbwd_001"
    assert len(migration.revision) <= 32


@pytest.mark.integration
def test_up_down_up(migration_connection):
    assert not inspect(migration_connection).has_table(TABLE_NAME)
    context = MigrationContext.configure(migration_connection)
    with Operations.context(context):
        migration.upgrade()
    assert inspect(migration_connection).has_table(TABLE_NAME)
    column_names = {
        column["name"]
        for column in inspect(migration_connection).get_columns(TABLE_NAME)
    }
    assert {
        "id",
        "wp_guid",
        "wp_link",
        "feed_url",
        "title_at_import",
        "cms_post_id",
        "created_at",
        "updated_at",
        "version",
    } <= column_names
    with Operations.context(context):
        migration.downgrade()
    assert not inspect(migration_connection).has_table(TABLE_NAME)
    with Operations.context(context):
        migration.upgrade()
    assert inspect(migration_connection).has_table(TABLE_NAME)
