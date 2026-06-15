"""Local fixture-feed builders + fake HTTP collaborators for unit tests.

Tests never touch the network (S81 TDD rule): the feed XML is built here and
served through ``FakeHttpGet`` / ``PagedFeedHttpGet`` stand-ins for
``requests.get``.
"""
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit


DEFAULT_PUB_DATE = "Mon, 04 Mar 2024 09:30:00 +0000"


def build_feed_xml(items: List[Dict[str, Any]]) -> bytes:
    """Render a minimal WordPress-shaped RSS 2.0 feed for the given items."""
    item_blocks = []
    for item in items:
        category_tags = "".join(
            f"<category><![CDATA[{name}]]></category>"
            for name in item.get("categories", [])
        )
        media_tag = ""
        if item.get("media_url"):
            media_tag = f'<media:content url="{item["media_url"]}" medium="image" />'
        enclosure_tag = ""
        if item.get("enclosure_url"):
            enclosure_tag = (
                f'<enclosure url="{item["enclosure_url"]}" '
                'type="image/jpeg" length="1" />'
            )
        item_blocks.append(
            f"""
    <item>
      <title>{item.get("title", "Untitled")}</title>
      <link>{item.get("link", "")}</link>
      <guid isPermaLink="false">{item["guid"]}</guid>
      <pubDate>{item.get("pub_date", DEFAULT_PUB_DATE)}</pubDate>
      <description><![CDATA[{item.get("excerpt", "")}]]></description>
      <content:encoded><![CDATA[{item.get("content", "")}]]></content:encoded>
      {category_tags}{media_tag}{enclosure_tag}
    </item>"""
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:media="http://search.yahoo.com/mrss/">\n'
        "<channel>\n"
        "<title>Fixture feed</title>\n"
        "<link>https://example.com</link>\n"
        "<description>fixture</description>\n"
        f"{''.join(item_blocks)}\n"
        "</channel>\n"
        "</rss>"
    ).encode("utf-8")


EMPTY_FEED_XML = build_feed_xml([])


class FakeResponse:
    """The slice of ``requests.Response`` the plugin reads."""

    def __init__(
        self,
        content: bytes = b"",
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
        json_data: Any = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self._json_data = json_data

    def json(self) -> Any:
        return self._json_data


class FakeHttpGet:
    """Routes URLs to canned responses via a list of (predicate, factory)."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self._routes: List[Any] = []

    def route(
        self, predicate: Callable[[str], bool], factory: Callable[[str], FakeResponse]
    ) -> None:
        self._routes.append((predicate, factory))

    def __call__(self, url: str, timeout: Any = None) -> FakeResponse:
        self.calls.append(url)
        for predicate, factory in self._routes:
            if predicate(url):
                return factory(url)
        raise AssertionError(f"FakeHttpGet has no route for {url}")


class PagedFeedHttpGet:
    """Serves feed pages by the ``paged`` query param; empty beyond the list.

    ``ignore_paged=True`` simulates the real-world feed that repeats page 1
    whatever ``paged`` says.
    """

    def __init__(self, pages: List[bytes], ignore_paged: bool = False) -> None:
        self._pages = pages
        self._ignore_paged = ignore_paged
        self.calls: List[str] = []

    def __call__(self, url: str, timeout: Any = None) -> FakeResponse:
        self.calls.append(url)
        query = parse_qs(urlsplit(url).query)
        page_number = int(query.get("paged", ["1"])[0])
        if self._ignore_paged:
            page_number = 1
        if page_number <= len(self._pages):
            return FakeResponse(content=self._pages[page_number - 1])
        return FakeResponse(content=EMPTY_FEED_XML)
