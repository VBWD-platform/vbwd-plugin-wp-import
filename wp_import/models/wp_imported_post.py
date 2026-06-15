"""WpImportedPost — the GUID ledger for WordPress imports (S81 D2).

``cms_post_id`` is a plain UUID column on purpose: NO cross-plugin FK into
``cms_post`` (the known migration-graph anchoring trap). Staleness is
detected lazily by checking post existence through PostService.
``created_at`` (BaseModel) is the import datetime (D3).
"""
from vbwd.extensions import db
from vbwd.models.base import BaseModel


class WpImportedPost(BaseModel):
    """One row per imported WP GUID; row + live post = 'already imported'."""

    __tablename__ = "wp_imported_post"

    wp_guid = db.Column(db.String(512), nullable=False, unique=True, index=True)
    wp_link = db.Column(db.String(2048), nullable=True)
    # stored token-masked (D1a); the full URL exists only in-request
    feed_url = db.Column(db.String(2048), nullable=False)
    title_at_import = db.Column(db.String(512), nullable=False)
    cms_post_id = db.Column(db.UUID(as_uuid=True), nullable=False, index=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "wp_guid": self.wp_guid,
            "wp_link": self.wp_link,
            "feed_url": self.feed_url,
            "title_at_import": self.title_at_import,
            "cms_post_id": str(self.cms_post_id),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
