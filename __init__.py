"""WP Import plugin — WordPress RSS → CMS posts (S81).

Depends on the cms plugin (PostService / TermService / CmsImageService);
declared in PluginMetadata.dependencies. All endpoints are gated on the
existing ``cms.manage`` permission (D8 — no new permission).
"""
from typing import Any, Dict, Optional, TYPE_CHECKING

from flask import current_app

from vbwd.plugins.base import BasePlugin, PluginMetadata

if TYPE_CHECKING:
    from flask import Blueprint


DEFAULT_CONFIG: Dict[str, Any] = {
    "debug_mode": False,
    "http_timeout_seconds": 10,
    "max_feed_pages": 50,
    "max_image_bytes": 10485760,
}

_REPOSITORY_PROVIDER_NAME = "wp_import_imported_post_repository"


class WpImportPlugin(BasePlugin):
    """Class MUST be defined in __init__.py (not re-exported) due to
    discovery check obj.__module__ != full_module in manager.py."""

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="wp_import",
            version="26.6",
            author="VBWD Team",
            description=(
                "Import WordPress blog posts from an RSS feed: original "
                "publish dates, re-hosted images, deduplicated categories "
                "and tags"
            ),
            dependencies=["cms"],
        )

    def initialize(self, config: Optional[Dict[str, Any]] = None) -> None:
        merged = {**DEFAULT_CONFIG}
        if config:
            merged.update(config)
        super().initialize(merged)

    def get_blueprint(self) -> Optional["Blueprint"]:
        from plugins.wp_import.wp_import.routes import wp_import_bp

        return wp_import_bp

    def get_url_prefix(self) -> Optional[str]:
        # routes use absolute /api/v1/admin/wp-import/* paths
        return ""

    def on_enable(self) -> None:
        from vbwd.plugins.di_helpers import register_repositories
        from plugins.wp_import.wp_import.repositories.wp_imported_post_repository import (
            WpImportedPostRepository,
        )

        container = getattr(current_app, "container", None)
        if container is not None:
            register_repositories(
                container,
                {_REPOSITORY_PROVIDER_NAME: WpImportedPostRepository},
            )

    def on_disable(self) -> None:
        from vbwd.plugins.di_helpers import unregister_repositories

        container = getattr(current_app, "container", None)
        if container is not None:
            unregister_repositories(container, [_REPOSITORY_PROVIDER_NAME])
