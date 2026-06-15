"""WpTermClassifier — splits WP RSS term names into categories vs tags (S81 D1).

WP RSS carries both taxonomies as plain ``<category>``, so one optional,
cached, **paginated** call to the site's public REST API
(``/wp-json/wp/v2/categories``) fetches the full — small, curated — category
set. A term name in that set is a category; **every other name is a tag**
(the tag set is unbounded, so ``/tags`` is never fetched). When REST is
unreachable (likely on token-gated hosts) every name imports as a CMS
category — RSS's own semantics.
"""
import logging
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

CATEGORY = "category"
TAG = "tag"

_REST_PAGE_SIZE = 100
_MAX_REST_PAGES = 10  # 1000 categories — far beyond any curated set


class WpTermClassifier:
    """Classifies term names via one cached, paginated REST lookup per site."""

    def __init__(self, http_get: Callable[..., Any], timeout_seconds: int = 10):
        self._http_get = http_get
        self._timeout_seconds = timeout_seconds
        # site base → category names lowercased; None = REST down
        self._cache: Dict[str, Optional[Set[str]]] = {}

    def classify(
        self, site_base_url: str, term_names: List[str]
    ) -> Dict[str, str]:
        category_names = self._lookup(site_base_url)
        if category_names is None:
            return {name: CATEGORY for name in term_names}
        return {
            name: CATEGORY if name.lower() in category_names else TAG
            for name in term_names
        }

    def _lookup(self, site_base_url: str) -> Optional[Set[str]]:
        if site_base_url not in self._cache:
            self._cache[site_base_url] = self._fetch_category_names(site_base_url)
        return self._cache[site_base_url]

    def _fetch_category_names(self, site_base_url: str) -> Optional[Set[str]]:
        category_names: Set[str] = set()
        for page_number in range(1, _MAX_REST_PAGES + 1):
            page_entries = self._fetch_page(site_base_url, page_number)
            if page_entries is None:
                if page_number == 1:
                    logger.info(
                        "wp_import: WP REST API unreachable at %s — "
                        "classifying all terms as categories",
                        site_base_url,
                    )
                    return None
                # WP answers 4xx past the last page — keep what we have.
                break
            category_names.update(
                str(entry.get("name", "")).lower()
                for entry in page_entries
                if entry.get("name")
            )
            if len(page_entries) < _REST_PAGE_SIZE:
                break
        return category_names

    def _fetch_page(
        self, site_base_url: str, page_number: int
    ) -> Optional[List[Dict[str, Any]]]:
        url = (
            f"{site_base_url}/wp-json/wp/v2/categories"
            f"?per_page={_REST_PAGE_SIZE}&page={page_number}"
        )
        try:
            response = self._http_get(url, timeout=self._timeout_seconds)
            if response.status_code >= 400:
                return None
            return response.json() or []
        except Exception as fetch_error:
            logger.debug(
                "wp_import: REST categories page %s fetch failed: %s",
                page_number,
                fetch_error,
            )
            return None
