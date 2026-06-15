"""FeedReader — paged WordPress RSS fetch via feedparser (S81 D1/D1a).

The pasted feed URL is treated as an opaque base: its query params (incl.
the secret ``token``) are preserved, ``show_on_page`` is set to the fetch
size, the remote read-tracking flags are FORCED off, and ``paged=N`` walks
the feed. Token/key-like query values are masked everywhere they could
surface (logs, raised errors, persisted values).
"""
import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

logger = logging.getLogger(__name__)

_SENSITIVE_QUERY_PATTERN = re.compile(
    r"([?&][^=&\s]*(?:token|key)[^=&\s]*=)[^&\s]+", re.IGNORECASE
)
_MASK = "***"
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_RUN_PATTERN = re.compile(r"\s+")


class FeedFetchError(Exception):
    """Raised when the upstream feed cannot be fetched (message is masked)."""


def mask_sensitive(text: str) -> str:
    """Mask token/key-like query values in any URL-bearing text (D1a)."""
    return _SENSITIVE_QUERY_PATTERN.sub(rf"\g<1>{_MASK}", text)


def strip_html_to_text(markup: str) -> str:
    """Tags removed, entities decoded, whitespace collapsed — plain text.

    The WP RSS ``<description>`` may carry HTML markup (proven on the live
    feed); the excerpt must import as plain text. Only the excerpt is
    normalised — ``content:encoded`` stays as-is (D1).
    """
    without_tags = _HTML_TAG_PATTERN.sub(" ", markup)
    decoded = html.unescape(without_tags)
    return _WHITESPACE_RUN_PATTERN.sub(" ", decoded).strip()


class FeedReader:
    """Walks a tokenized WordPress RSS feed page by page and normalizes items."""

    DEFAULT_PAGE_SIZE = 50
    _FORCED_PARAMS = {"show_unread_only": "false", "mark_as_read": "false"}

    def __init__(
        self,
        http_get: Callable[..., Any],
        max_feed_pages: int,
        timeout_seconds: int = 10,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self._http_get = http_get
        self._max_feed_pages = max_feed_pages
        self._timeout_seconds = timeout_seconds
        self._page_size = page_size

    def fetch_all(self, feed_url: str) -> List[Dict[str, Any]]:
        """Fetch every feed item, walking ``paged`` until a stop condition.

        Stops on: empty page, HTTP error (fatal only on page 1), the
        ``max_feed_pages`` cap, or a page yielding no new GUIDs (feeds that
        ignore ``paged`` and repeat the first page).
        """
        items: List[Dict[str, Any]] = []
        seen_guids: set = set()
        for page_number in range(1, self._max_feed_pages + 1):
            page_url = self._page_url(feed_url, page_number)
            logger.debug("wp_import: fetching feed page %s", mask_sensitive(page_url))
            try:
                response = self._http_get(page_url, timeout=self._timeout_seconds)
            except Exception:
                # `from None` on purpose: the original exception carries the
                # unmasked URL (D1a — the token must never surface).
                raise FeedFetchError(
                    f"feed fetch failed: {mask_sensitive(page_url)}"
                ) from None
            if response.status_code >= 400:
                if page_number == 1:
                    raise FeedFetchError(
                        f"feed returned HTTP {response.status_code}: "
                        f"{mask_sensitive(page_url)}"
                    )
                break
            page_items = self._parse_page(response.content)
            new_items = [
                item for item in page_items if item["guid"] not in seen_guids
            ]
            if not new_items:
                break
            for item in new_items:
                seen_guids.add(item["guid"])
            items.extend(new_items)
        return items

    def _page_url(self, feed_url: str, page_number: int) -> str:
        """Merge query params onto the pasted URL (D1): preserve everything,
        set the page size, force read-tracking off, add ``paged``."""
        split_url = urlsplit(feed_url)
        params = dict(parse_qsl(split_url.query, keep_blank_values=True))
        params["show_on_page"] = str(self._page_size)
        params.update(self._FORCED_PARAMS)
        params["paged"] = str(page_number)
        return urlunsplit(
            (
                split_url.scheme,
                split_url.netloc,
                split_url.path,
                urlencode(params),
                split_url.fragment,
            )
        )

    def _parse_page(self, content: bytes) -> List[Dict[str, Any]]:
        parsed = feedparser.parse(content)
        return [self._normalize_entry(entry) for entry in parsed.entries]

    def _normalize_entry(self, entry: Any) -> Dict[str, Any]:
        return {
            "guid": entry.get("id") or entry.get("link") or "",
            "link": entry.get("link") or "",
            "title": entry.get("title") or "",
            "content_html": self._entry_content(entry),
            "excerpt": strip_html_to_text(entry.get("summary") or ""),
            "published_at": self._entry_published_at(entry),
            "term_names": [
                tag.get("term") for tag in entry.get("tags", []) if tag.get("term")
            ],
            "media_urls": self._entry_media_urls(entry),
        }

    @staticmethod
    def _entry_content(entry: Any) -> str:
        contents = entry.get("content") or []
        if contents:
            return contents[0].get("value") or ""
        return ""

    @staticmethod
    def _entry_published_at(entry: Any) -> Optional[str]:
        published = entry.get("published_parsed")
        if not published:
            return None
        return datetime(*published[:6], tzinfo=timezone.utc).isoformat()

    @staticmethod
    def _entry_media_urls(entry: Any) -> List[str]:
        """Featured-image candidates in precedence order (D4):
        media:content → media:thumbnail → image enclosures."""
        candidate_urls: List[str] = []
        for media in entry.get("media_content", []):
            if media.get("url"):
                candidate_urls.append(media["url"])
        for thumbnail in entry.get("media_thumbnail", []):
            if thumbnail.get("url"):
                candidate_urls.append(thumbnail["url"])
        for enclosure in entry.get("enclosures", []):
            is_image = (enclosure.get("type") or "").startswith("image/")
            if is_image and enclosure.get("href"):
                candidate_urls.append(enclosure["href"])
        return candidate_urls
