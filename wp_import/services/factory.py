"""Composition root for WpImportService (routes + integration tests).

The cms collaborators come from the cms plugin's own factories (single
home for that wiring — DRY); the HTTP collaborator and config are
injectable so tests run without network.
"""
from typing import Any, Callable, Dict, Optional

from flask import current_app

from vbwd.extensions import db


def build_wp_import_service(
    http_get: Optional[Callable[..., Any]] = None,
    config: Optional[Dict[str, Any]] = None,
):
    import requests

    from plugins.cms.src.routes import _image_service, _post_service, _term_service
    from vbwd.services.tags_and_custom_fields import resolve_tags_and_custom_fields
    from plugins.wp_import import DEFAULT_CONFIG
    from plugins.wp_import.wp_import.repositories.wp_imported_post_repository import (
        WpImportedPostRepository,
    )
    from plugins.wp_import.wp_import.services.featured_media_resolver import (
        FeaturedMediaResolver,
    )
    from plugins.wp_import.wp_import.services.feed_reader import FeedReader
    from plugins.wp_import.wp_import.services.term_classifier import WpTermClassifier
    from plugins.wp_import.wp_import.services.wp_import_service import WpImportService

    if config is None:
        config = _plugin_config(DEFAULT_CONFIG)
    if http_get is None:
        http_get = requests.get
    timeout_seconds = config.get(
        "http_timeout_seconds", DEFAULT_CONFIG["http_timeout_seconds"]
    )
    return WpImportService(
        feed_reader=FeedReader(
            http_get=http_get,
            max_feed_pages=config.get(
                "max_feed_pages", DEFAULT_CONFIG["max_feed_pages"]
            ),
            timeout_seconds=timeout_seconds,
        ),
        term_classifier=WpTermClassifier(
            http_get=http_get, timeout_seconds=timeout_seconds
        ),
        post_service=_post_service(),
        term_service=_term_service(),
        image_service=_image_service(),
        repository=WpImportedPostRepository(db.session),
        featured_media_resolver=FeaturedMediaResolver(
            http_get=http_get, timeout_seconds=timeout_seconds
        ),
        http_get=http_get,
        http_timeout_seconds=timeout_seconds,
        max_image_bytes=config.get(
            "max_image_bytes", DEFAULT_CONFIG["max_image_bytes"]
        ),
        tags_port=resolve_tags_and_custom_fields(),
    )


def _plugin_config(default_config: Dict[str, Any]) -> Dict[str, Any]:
    config_store = getattr(current_app, "config_store", None)
    if config_store is None:
        return dict(default_config)
    return config_store.get_config("wp_import") or dict(default_config)
