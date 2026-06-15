"""FeedReader unit tests — fixture feeds only, no network (S81 D1/D1a)."""
import logging
from urllib.parse import parse_qs, urlsplit

import pytest

from plugins.wp_import.tests.unit.feed_fixtures import (
    FakeHttpGet,
    FakeResponse,
    PagedFeedHttpGet,
    build_feed_xml,
)
from plugins.wp_import.wp_import.services.feed_reader import (
    FeedFetchError,
    FeedReader,
    mask_sensitive,
)


TOKENIZED_FEED_URL = (
    "https://redrobot.example/feed/"
    "?token=sekret123&show_on_page=10&show_unread_only=true&mark_as_read=true"
)


def _single_page_http(items):
    return PagedFeedHttpGet([build_feed_xml(items)])


class TestQueryParamMerge:
    def test_existing_params_preserved_and_tracking_flags_forced_false(self):
        http_get = _single_page_http([{"guid": "guid-1"}])
        reader = FeedReader(http_get=http_get, max_feed_pages=5)

        reader.fetch_all(TOKENIZED_FEED_URL)

        first_url = http_get.calls[0]
        query = parse_qs(urlsplit(first_url).query)
        assert query["token"] == ["sekret123"]
        assert query["show_unread_only"] == ["false"]
        assert query["mark_as_read"] == ["false"]
        assert query["paged"] == ["1"]
        assert query["show_on_page"] == [str(FeedReader.DEFAULT_PAGE_SIZE)]


class TestPaging:
    def test_walks_pages_until_empty_page(self):
        pages = [
            build_feed_xml([{"guid": "guid-1"}, {"guid": "guid-2"}]),
            build_feed_xml([{"guid": "guid-3"}]),
        ]
        http_get = PagedFeedHttpGet(pages)
        reader = FeedReader(http_get=http_get, max_feed_pages=10)

        items = reader.fetch_all(TOKENIZED_FEED_URL)

        assert [item["guid"] for item in items] == ["guid-1", "guid-2", "guid-3"]
        # pages 1, 2 then the empty page 3 stop the walk
        assert len(http_get.calls) == 3

    def test_respects_max_feed_pages_cap(self):
        pages = [
            build_feed_xml([{"guid": f"guid-{page}-{i}"} for i in range(2)])
            for page in range(10)
        ]
        http_get = PagedFeedHttpGet(pages)
        reader = FeedReader(http_get=http_get, max_feed_pages=2)

        items = reader.fetch_all(TOKENIZED_FEED_URL)

        assert len(http_get.calls) == 2
        assert len(items) == 4

    def test_feed_ignoring_paged_stops_via_no_new_guids(self):
        page = build_feed_xml([{"guid": "guid-1"}, {"guid": "guid-2"}])
        http_get = PagedFeedHttpGet([page], ignore_paged=True)
        reader = FeedReader(http_get=http_get, max_feed_pages=50)

        items = reader.fetch_all(TOKENIZED_FEED_URL)

        assert [item["guid"] for item in items] == ["guid-1", "guid-2"]
        # page 2 repeats page 1 → no new GUIDs → stop after the second call
        assert len(http_get.calls) == 2

    def test_http_error_on_first_page_raises_feed_fetch_error(self):
        http_get = FakeHttpGet()
        http_get.route(lambda url: True, lambda url: FakeResponse(status_code=503))
        reader = FeedReader(http_get=http_get, max_feed_pages=5)

        with pytest.raises(FeedFetchError):
            reader.fetch_all(TOKENIZED_FEED_URL)

    def test_http_error_on_later_page_stops_the_walk(self):
        def respond(url):
            query = parse_qs(urlsplit(url).query)
            if query["paged"] == ["1"]:
                return FakeResponse(content=build_feed_xml([{"guid": "guid-1"}]))
            return FakeResponse(status_code=500)

        http_get = FakeHttpGet()
        http_get.route(lambda url: True, respond)
        reader = FeedReader(http_get=http_get, max_feed_pages=5)

        items = reader.fetch_all(TOKENIZED_FEED_URL)

        assert [item["guid"] for item in items] == ["guid-1"]


class TestTokenMasking:
    def test_mask_sensitive_masks_token_and_key_query_values(self):
        masked = mask_sensitive(
            "https://h/feed/?token=sekret123&api_key=topsecret&paged=2"
        )
        assert "sekret123" not in masked
        assert "topsecret" not in masked
        assert "paged=2" in masked

    def test_token_never_in_raised_error(self):
        def explode(url, timeout=None):
            raise ConnectionError(f"cannot reach {url}")

        reader = FeedReader(http_get=explode, max_feed_pages=5)

        with pytest.raises(FeedFetchError) as raised:
            reader.fetch_all(TOKENIZED_FEED_URL)
        assert "sekret123" not in str(raised.value)

    def test_token_never_in_http_error(self):
        http_get = FakeHttpGet()
        http_get.route(lambda url: True, lambda url: FakeResponse(status_code=502))
        reader = FeedReader(http_get=http_get, max_feed_pages=5)

        with pytest.raises(FeedFetchError) as raised:
            reader.fetch_all(TOKENIZED_FEED_URL)
        assert "sekret123" not in str(raised.value)

    def test_token_never_in_log_output(self, caplog):
        http_get = _single_page_http([{"guid": "guid-1"}])
        reader = FeedReader(http_get=http_get, max_feed_pages=5)

        with caplog.at_level(logging.DEBUG):
            reader.fetch_all(TOKENIZED_FEED_URL)

        assert "sekret123" not in caplog.text


class TestItemNormalization:
    def test_normalizes_all_item_fields(self):
        http_get = _single_page_http(
            [
                {
                    "guid": "https://redrobot.example/?p=42",
                    "link": "https://redrobot.example/2024/03/hello-world/",
                    "title": "Hello World",
                    "content": '<p>Body</p><img src="https://img.example/a.jpg">',
                    "excerpt": "Short summary",
                    "pub_date": "Mon, 04 Mar 2024 09:30:00 +0000",
                    "categories": ["News", "python"],
                    "media_url": "https://img.example/featured.jpg",
                    "enclosure_url": "https://img.example/enclosure.jpg",
                }
            ]
        )
        reader = FeedReader(http_get=http_get, max_feed_pages=5)

        item = reader.fetch_all(TOKENIZED_FEED_URL)[0]

        assert item["guid"] == "https://redrobot.example/?p=42"
        assert item["link"] == "https://redrobot.example/2024/03/hello-world/"
        assert item["title"] == "Hello World"
        assert "<p>Body</p>" in item["content_html"]
        assert item["excerpt"] == "Short summary"
        assert item["published_at"] == "2024-03-04T09:30:00+00:00"
        assert item["term_names"] == ["News", "python"]
        # media:content takes precedence over the enclosure in candidate order
        assert item["media_urls"] == [
            "https://img.example/featured.jpg",
            "https://img.example/enclosure.jpg",
        ]

    def test_excerpt_html_stripped_to_plain_text(self):
        # Proven on the live feed: <description> carries HTML markup which
        # fe-user then renders escaped. Excerpt must be plain text (D1 —
        # content_html stays as-is, only the excerpt is normalised).
        http_get = _single_page_http(
            [
                {
                    "guid": "guid-html-excerpt",
                    "excerpt": (
                        '<p>Hello <a href="https://x.example">world</a>'
                        " &amp; friends</p>"
                    ),
                }
            ]
        )
        reader = FeedReader(http_get=http_get, max_feed_pages=5)

        item = reader.fetch_all(TOKENIZED_FEED_URL)[0]

        assert item["excerpt"] == "Hello world & friends"

    def test_excerpt_whitespace_collapsed(self):
        http_get = _single_page_http(
            [
                {
                    "guid": "guid-spacey-excerpt",
                    "excerpt": "<p>First   line</p>\n\n<p>Second line</p>",
                }
            ]
        )
        reader = FeedReader(http_get=http_get, max_feed_pages=5)

        item = reader.fetch_all(TOKENIZED_FEED_URL)[0]

        assert item["excerpt"] == "First line Second line"

    def test_item_without_optionals_normalizes_to_safe_defaults(self):
        http_get = _single_page_http([{"guid": "guid-min", "title": "Bare"}])
        reader = FeedReader(http_get=http_get, max_feed_pages=5)

        item = reader.fetch_all(TOKENIZED_FEED_URL)[0]

        assert item["guid"] == "guid-min"
        assert item["content_html"] == ""
        assert item["term_names"] == []
        assert item["media_urls"] == []
