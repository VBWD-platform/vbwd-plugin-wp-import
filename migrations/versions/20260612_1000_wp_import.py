"""wp_import: create wp_imported_post (the WP GUID ledger, S81 D2).

Revision ID: 20260612_1000_wp_import
Revises: vbwd_001
Create Date: 2026-06-12

Own branch anchored on the always-present core root (`vbwd_001`) — never
on another plugin's revision (migration-graph fragmentation trap).
`cms_post_id` is a plain UUID on purpose: no cross-plugin FK into
`cms_post` (D2).
"""
from alembic import op
import sqlalchemy as sa


revision = "20260612_1000_wp_import"
down_revision = "vbwd_001"
branch_labels = None
depends_on = None

TABLE_NAME = "wp_imported_post"


def upgrade():
    conn = op.get_bind()
    if _table_exists(conn, TABLE_NAME):
        return

    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wp_guid", sa.String(512), nullable=False),
        sa.Column("wp_link", sa.String(2048), nullable=True),
        sa.Column("feed_url", sa.String(2048), nullable=False),
        sa.Column("title_at_import", sa.String(512), nullable=False),
        sa.Column(
            "cms_post_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wp_guid", name="uq_wp_imported_post_wp_guid"),
    )
    op.create_index("ix_wp_imported_post_wp_guid", TABLE_NAME, ["wp_guid"], unique=True)
    op.create_index("ix_wp_imported_post_cms_post_id", TABLE_NAME, ["cms_post_id"])


def downgrade():
    op.drop_index("ix_wp_imported_post_cms_post_id", table_name=TABLE_NAME)
    op.drop_index("ix_wp_imported_post_wp_guid", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text("SELECT 1 FROM information_schema.tables WHERE table_name = :name"),
        {"name": table_name},
    )
    return result.scalar() is not None
