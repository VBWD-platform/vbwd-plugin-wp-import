"""WpImportService — imports WordPress RSS items as CMS posts (S81).

Composes the cms plugin's PostService / TermService / CmsImageService
(declared dependency) with the FeedReader, WpTermClassifier and the
WpImportedPost GUID ledger. The D2 rule everywhere: a feed item is
importable iff no ledger row with its GUID references a still-existing
CMS post; stale orphan rows are purged lazily.
"""
import logging
import mimetypes
import os
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit
from uuid import UUID

from plugins.cms.src.services._slug import slugify
from plugins.cms.src.services.post_service import (
    PostNotFoundError,
    PostSlugConflictError,
)
from plugins.cms.src.services.term_service import TermNotFoundError

from plugins.wp_import.wp_import.models.wp_imported_post import WpImportedPost
from plugins.wp_import.wp_import.services.feed_reader import mask_sensitive
from plugins.wp_import.wp_import.services.term_classifier import CATEGORY, TAG

logger = logging.getLogger(__name__)

_IMG_SRC_PATTERN = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_MAX_SLUG_ATTEMPTS = 50
_FALLBACK_MIME_TYPE = "application/octet-stream"
# Imported posts carry their tags on the core ``cms_post`` entity type (D7).
_TAG_ENTITY_TYPE = "cms_post"


def _extract_image_sources(content_html: str) -> List[str]:
    """Unique ``<img src>`` URLs in document order."""
    sources: List[str] = []
    for source in _IMG_SRC_PATTERN.findall(content_html):
        if source not in sources:
            sources.append(source)
    return sources


def _site_base(feed_url: str) -> str:
    split_url = urlsplit(feed_url)
    return f"{split_url.scheme}://{split_url.netloc}"


class WpImportService:
    """stats / run / list_imported / remove for the WP Import admin tab."""

    def __init__(
        self,
        feed_reader: Any,
        term_classifier: Any,
        post_service: Any,
        term_service: Any,
        image_service: Any,
        repository: Any,
        featured_media_resolver: Any,
        http_get: Callable[..., Any],
        http_timeout_seconds: int,
        max_image_bytes: int,
        tags_port: Any,
    ) -> None:
        self._feed_reader = feed_reader
        self._term_classifier = term_classifier
        self._post_service = post_service
        self._term_service = term_service
        self._image_service = image_service
        self._repository = repository
        self._featured_media_resolver = featured_media_resolver
        self._http_get = http_get
        self._http_timeout_seconds = http_timeout_seconds
        self._max_image_bytes = max_image_bytes
        # The core tags port — classified tags go to the single core catalog
        # (categories stay on the cms_term taxonomy — D7).
        self._tags_port = tags_port

    # ── stats ──────────────────────────────────────────────────────────────
    def stats(self, feed_url: str) -> Dict[str, int]:
        items = self._feed_reader.fetch_all(feed_url)
        already_imported = sum(
            1 for item in items if self._is_imported(item["guid"])
        )
        return {
            "total_in_feed": len(items),
            "never_imported": len(items) - already_imported,
            "already_imported": already_imported,
        }

    # ── run ────────────────────────────────────────────────────────────────
    def run(self, feed_url: str, chunk: int) -> Dict[str, Any]:
        items = self._feed_reader.fetch_all(feed_url)
        pending = [item for item in items if not self._is_imported(item["guid"])]
        attempted = pending[:chunk]
        image_cache: Dict[str, Optional[str]] = {}
        results = [
            self._import_item_guarded(item, feed_url, image_cache)
            for item in attempted
        ]
        imported_count = sum(
            1 for result in results if result["status"] == "imported"
        )
        return {
            "imported_count": imported_count,
            "skipped_count": len(items) - len(pending),
            "feed_exhausted": len(pending) <= chunk,
            "results": results,
        }

    def _import_item_guarded(
        self,
        item: Dict[str, Any],
        feed_url: str,
        image_cache: Dict[str, Optional[str]],
    ) -> Dict[str, Any]:
        """An item failure is recorded, never aborts the chunk (D6)."""
        try:
            return self._import_item(item, feed_url, image_cache)
        except Exception as item_error:
            logger.warning(
                "wp_import: item %s failed: %s",
                item.get("guid"),
                mask_sensitive(str(item_error)),
            )
            return {
                "guid": item.get("guid"),
                "title": item.get("title"),
                "status": "error",
                "cms_post_id": None,
                "error": mask_sensitive(str(item_error)),
                "image_misses": [],
            }

    def _import_item(
        self,
        item: Dict[str, Any],
        feed_url: str,
        image_cache: Dict[str, Optional[str]],
    ) -> Dict[str, Any]:
        image_misses: List[str] = []
        content_html = item.get("content_html") or ""
        content_image_sources = _extract_image_sources(content_html)
        for source in content_image_sources:
            local_url = self._rehost_image(source, image_cache)
            if local_url:
                content_html = content_html.replace(source, local_url)
            else:
                image_misses.append(source)

        featured_image_url = self._resolve_featured(
            item, content_image_sources, image_cache, image_misses
        )

        post = self._create_post_with_slug_retry(
            item, content_html, featured_image_url
        )
        if item.get("published_at"):
            self._post_service.update_post(
                post["id"], {"published_at": item["published_at"]}
            )

        self._assign_terms(post["id"], item, feed_url)

        ledger_row = WpImportedPost()
        ledger_row.wp_guid = item["guid"]
        ledger_row.wp_link = item.get("link")
        ledger_row.feed_url = mask_sensitive(feed_url)
        ledger_row.title_at_import = item.get("title") or ""
        ledger_row.cms_post_id = post["id"]
        self._repository.save(ledger_row)

        return {
            "guid": item["guid"],
            "title": item.get("title"),
            "status": "imported",
            "cms_post_id": post["id"],
            "error": None,
            "image_misses": image_misses,
        }

    def _create_post_with_slug_retry(
        self,
        item: Dict[str, Any],
        content_html: str,
        featured_image_url: Optional[str],
    ) -> Dict[str, Any]:
        base_slug = self._derive_slug(item)
        slug = base_slug
        for suffix in range(2, _MAX_SLUG_ATTEMPTS + 2):
            try:
                return self._post_service.create_post(
                    {
                        "type": "post",
                        "title": item.get("title") or "",
                        "slug": slug,
                        "excerpt": item.get("excerpt"),
                        "content_html": content_html,
                        "featured_image_url": featured_image_url,
                        "status": "published",
                    }
                )
            except PostSlugConflictError:
                slug = f"{base_slug}-{suffix}"
        raise PostSlugConflictError(
            f"no free slug found for '{base_slug}' "
            f"after {_MAX_SLUG_ATTEMPTS} attempts"
        )

    @staticmethod
    def _derive_slug(item: Dict[str, Any]) -> str:
        link_path = urlsplit(item.get("link") or "").path.strip("/")
        last_segment = link_path.rsplit("/", 1)[-1] if link_path else ""
        return slugify(last_segment or item.get("title") or "")

    def _resolve_featured(
        self,
        item: Dict[str, Any],
        content_image_sources: List[str],
        image_cache: Dict[str, Optional[str]],
        image_misses: List[str],
    ) -> Optional[str]:
        """media:content/thumbnail → enclosure → REST featured_media lookup
        → first content image; re-hosted (D4). A failed download keeps the
        remote URL and records the miss."""
        featured_candidate = self._featured_candidate(item, content_image_sources)
        if featured_candidate is None:
            return None
        local_url = self._rehost_image(featured_candidate, image_cache)
        if local_url:
            return local_url
        if featured_candidate not in image_misses:
            image_misses.append(featured_candidate)
        return featured_candidate

    def _featured_candidate(
        self, item: Dict[str, Any], content_image_sources: List[str]
    ) -> Optional[str]:
        """The REST lookup runs only when the feed itself carries no
        media/enclosure signal; any REST miss falls through (D1/D4)."""
        media_urls = item.get("media_urls") or []
        if media_urls:
            return media_urls[0]
        rest_featured_url = self._featured_media_resolver.resolve(
            item.get("link") or ""
        )
        if rest_featured_url:
            return rest_featured_url
        if content_image_sources:
            return content_image_sources[0]
        return None

    def _rehost_image(
        self, remote_url: str, image_cache: Dict[str, Optional[str]]
    ) -> Optional[str]:
        """Download + upload through CmsImageService; once per URL per run."""
        if remote_url in image_cache:
            return image_cache[remote_url]
        local_url = None
        try:
            response = self._http_get(
                remote_url, timeout=self._http_timeout_seconds
            )
            if (
                response.status_code == 200
                and response.content
                and len(response.content) <= self._max_image_bytes
            ):
                local_url = self._upload_image(remote_url, response)
        except Exception as download_error:
            logger.warning(
                "wp_import: image download failed for %s: %s",
                remote_url,
                mask_sensitive(str(download_error)),
            )
        image_cache[remote_url] = local_url
        return local_url

    def _upload_image(self, remote_url: str, response: Any) -> Optional[str]:
        filename = os.path.basename(urlsplit(remote_url).path) or "image"
        mime_type = (
            (response.headers.get("Content-Type") or "").split(";")[0].strip()
            or mimetypes.guess_type(filename)[0]
            or _FALLBACK_MIME_TYPE
        )
        uploaded = self._image_service.upload_image(
            file_data=response.content, filename=filename, mime_type=mime_type
        )
        return uploaded.get("url_path")

    def _assign_terms(
        self, post_id: str, item: Dict[str, Any], feed_url: str
    ) -> None:
        """Split classified terms (D7): categories → cms_term taxonomy; tags →
        the core tag catalog (cms_post-scoped slugs)."""
        term_names = item.get("term_names") or []
        if not term_names:
            return
        classification = self._term_classifier.classify(
            _site_base(feed_url), term_names
        )
        category_term_ids: List[str] = []
        tag_slugs: List[str] = []
        for name in term_names:
            if classification.get(name, CATEGORY) == TAG:
                slug = slugify(name)
                if slug:
                    tag_slugs.append(slug)
            else:
                term = self._term_service.find_or_create(CATEGORY, name)
                category_term_ids.append(str(term["id"]))
        if category_term_ids:
            self._post_service.assign_terms(post_id, category_term_ids)
        if tag_slugs:
            self._tags_port.set_tags(_TAG_ENTITY_TYPE, UUID(str(post_id)), tag_slugs)

    # ── list / remove ──────────────────────────────────────────────────────
    def list_imported(
        self,
        search: Optional[str],
        sort: str,
        order: str,
        page: int,
        per_page: int,
    ) -> Dict[str, Any]:
        rows, total = self._repository.search(search, sort, order, page, per_page)
        listed_items = []
        for row in rows:
            post = self._get_live_post(row)
            if post is None:
                total -= 1
                continue
            categories, tags = self._terms_for(post, row.cms_post_id)
            listed_items.append(
                {
                    "id": str(row.id),
                    "cms_post_id": str(row.cms_post_id),
                    "title": post["title"],
                    "categories": categories,
                    "tags": tags,
                    "imported_at": row.created_at.isoformat(),
                    "post_status": post["status"],
                }
            )
        return {
            "items": listed_items,
            "total": total,
            "page": page,
            "per_page": per_page,
        }

    def remove(self, import_ids: List[str]) -> Dict[str, int]:
        """Delete the CMS posts completely AND their ledger rows (D7) —
        the GUIDs become importable again (D2)."""
        rows = self._repository.find_by_ids(import_ids)
        removed = 0
        for row in rows:
            try:
                self._post_service.delete_post(str(row.cms_post_id))
            except PostNotFoundError:
                logger.info(
                    "wp_import: post %s already gone, purging ledger row",
                    row.cms_post_id,
                )
            self._repository.delete(row)
            removed += 1
        return {"removed": removed}

    # ── D2 helpers ─────────────────────────────────────────────────────────
    def _is_imported(self, wp_guid: str) -> bool:
        """D2: imported iff a ledger row exists AND its post still exists.
        Stale orphan rows are purged on sight."""
        row = self._repository.find_by_guid(wp_guid)
        if row is None:
            return False
        if self._post_exists(str(row.cms_post_id)):
            return True
        self._repository.delete(row)
        return False

    def _get_live_post(self, row: Any) -> Optional[Dict[str, Any]]:
        try:
            return self._post_service.get_post(str(row.cms_post_id))
        except PostNotFoundError:
            self._repository.delete(row)
            return None

    def _post_exists(self, post_id: str) -> bool:
        try:
            self._post_service.get_post(post_id)
            return True
        except PostNotFoundError:
            return False

    def _terms_for(self, post: Dict[str, Any], cms_post_id: Any) -> tuple:
        """Category NAMES (from the cms_term taxonomy) + tag SLUGS (from the
        core catalog, ``cms_post``-scoped — D7)."""
        categories: List[str] = []
        for term_id in post.get("term_ids") or []:
            try:
                term = self._term_service.get_term(term_id)
            except TermNotFoundError:
                continue
            if term["term_type"] == CATEGORY:
                categories.append(term["name"])
        tags = self._tags_port.get_tags(_TAG_ENTITY_TYPE, UUID(str(cms_post_id)))
        return categories, tags
