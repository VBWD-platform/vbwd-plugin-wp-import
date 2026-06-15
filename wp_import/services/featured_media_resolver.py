"""FeaturedMediaResolver — WP REST featured-image lookup (S81 D1/D4).

Text-only feeds (proven on the live redrobot.online feed: no media:content,
no enclosure, no ``<img>`` in ``content:encoded``) still have a WP featured
image, reachable only via the public REST API. Two small GETs per post:
``/wp-json/wp/v2/posts?slug=<slug>`` → ``featured_media`` id, then
``/wp-json/wp/v2/media/<id>`` → ``source_url``. The calls go to the link's
own host with no token attached (D1a); ANY failure — timeout, 4xx,
``featured_media`` 0/missing, malformed payload — resolves to ``None`` so
the caller's featured-image precedence falls through. Never fails an item.
"""
import logging
from typing import Any, Callable, Optional
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

_POSTS_LOOKUP_PATH = "/wp-json/wp/v2/posts"
_MEDIA_LOOKUP_PATH = "/wp-json/wp/v2/media"


class FeaturedMediaResolver:
    """Resolves a post link to its REST featured-image source URL, or None."""

    def __init__(
        self, http_get: Callable[..., Any], timeout_seconds: int = 10
    ) -> None:
        self._http_get = http_get
        self._timeout_seconds = timeout_seconds

    def resolve(self, post_link: str) -> Optional[str]:
        link_parts = urlsplit(post_link or "")
        link_slug = link_parts.path.strip("/").rsplit("/", 1)[-1]
        if not (link_parts.scheme and link_parts.netloc and link_slug):
            return None
        site_base = f"{link_parts.scheme}://{link_parts.netloc}"
        featured_media_id = self._fetch_featured_media_id(site_base, link_slug)
        if featured_media_id is None:
            return None
        return self._fetch_media_source_url(site_base, featured_media_id)

    def _fetch_featured_media_id(
        self, site_base: str, link_slug: str
    ) -> Optional[int]:
        posts_payload = self._rest_get_json(
            f"{site_base}{_POSTS_LOOKUP_PATH}"
            f"?slug={quote(link_slug)}&_fields=featured_media"
        )
        if not isinstance(posts_payload, list) or not posts_payload:
            return None
        first_post = posts_payload[0]
        if not isinstance(first_post, dict):
            return None
        featured_media_id = first_post.get("featured_media")
        if isinstance(featured_media_id, int) and featured_media_id > 0:
            return featured_media_id
        return None

    def _fetch_media_source_url(
        self, site_base: str, featured_media_id: int
    ) -> Optional[str]:
        media_payload = self._rest_get_json(
            f"{site_base}{_MEDIA_LOOKUP_PATH}/{featured_media_id}"
            "?_fields=source_url"
        )
        if not isinstance(media_payload, dict):
            return None
        source_url = media_payload.get("source_url")
        if isinstance(source_url, str) and source_url:
            return source_url
        return None

    def _rest_get_json(self, url: str) -> Any:
        try:
            response = self._http_get(url, timeout=self._timeout_seconds)
            if response.status_code >= 400:
                return None
            return response.json()
        except Exception as fetch_error:
            logger.debug(
                "wp_import: REST featured-media lookup failed for %s: %s",
                url,
                fetch_error,
            )
            return None
