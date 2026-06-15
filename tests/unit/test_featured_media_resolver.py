"""FeaturedMediaResolver unit tests — mocked HTTP, no network (S81 D1/D4).

Text-only feeds (proven on the live redrobot feed) carry the featured image
only in the WP REST API: posts?slug → featured_media id → media/<id> →
source_url. Every failure mode must resolve to None — the featured-image
precedence then falls through; an item import never fails on REST.
"""
from plugins.wp_import.tests.unit.feed_fixtures import FakeHttpGet, FakeResponse
from plugins.wp_import.wp_import.services.featured_media_resolver import (
    FeaturedMediaResolver,
)


POST_LINK = "https://redrobot.example/2024/03/hello-world/"
POSTS_LOOKUP_URL = (
    "https://redrobot.example/wp-json/wp/v2/posts"
    "?slug=hello-world&_fields=featured_media"
)
MEDIA_LOOKUP_URL = "https://redrobot.example/wp-json/wp/v2/media/77?_fields=source_url"
SOURCE_URL = "https://redrobot.example/wp-content/uploads/2024/03/featured.jpg"


def _http_with_featured(featured_media_id=77, source_url=SOURCE_URL):
    http_get = FakeHttpGet()
    http_get.route(
        lambda url: "/wp-json/wp/v2/posts" in url,
        lambda url: FakeResponse(json_data=[{"featured_media": featured_media_id}]),
    )
    http_get.route(
        lambda url: "/wp-json/wp/v2/media/" in url,
        lambda url: FakeResponse(json_data={"source_url": source_url}),
    )
    return http_get


class TestResolveSuccess:
    def test_resolves_source_url_via_two_rest_gets(self):
        http_get = _http_with_featured()
        resolver = FeaturedMediaResolver(http_get=http_get, timeout_seconds=5)

        resolved = resolver.resolve(POST_LINK)

        assert resolved == SOURCE_URL
        assert http_get.calls == [POSTS_LOOKUP_URL, MEDIA_LOOKUP_URL]

    def test_rest_urls_carry_no_token(self):
        # The REST calls go to the public wp-json on the link's own host —
        # the tokenized feed URL is never involved (D1a).
        http_get = _http_with_featured()
        resolver = FeaturedMediaResolver(http_get=http_get, timeout_seconds=5)

        resolver.resolve(POST_LINK)

        assert all("token" not in called_url for called_url in http_get.calls)


class TestResolveFallsThroughToNone:
    def test_link_without_slug_makes_no_http_call(self):
        http_get = FakeHttpGet()
        resolver = FeaturedMediaResolver(http_get=http_get, timeout_seconds=5)

        assert resolver.resolve("") is None
        assert resolver.resolve("https://redrobot.example/") is None
        assert resolver.resolve("/relative/only/") is None
        assert http_get.calls == []

    def test_posts_lookup_http_error_resolves_none(self):
        http_get = FakeHttpGet()
        http_get.route(lambda url: True, lambda url: FakeResponse(status_code=503))
        resolver = FeaturedMediaResolver(http_get=http_get, timeout_seconds=5)

        assert resolver.resolve(POST_LINK) is None
        assert len(http_get.calls) == 1

    def test_posts_lookup_exception_resolves_none(self):
        def explode(url, timeout=None):
            raise ConnectionError(f"cannot reach {url}")

        resolver = FeaturedMediaResolver(http_get=explode, timeout_seconds=5)

        assert resolver.resolve(POST_LINK) is None

    def test_empty_posts_payload_resolves_none_without_media_call(self):
        http_get = FakeHttpGet()
        http_get.route(lambda url: True, lambda url: FakeResponse(json_data=[]))
        resolver = FeaturedMediaResolver(http_get=http_get, timeout_seconds=5)

        assert resolver.resolve(POST_LINK) is None
        assert len(http_get.calls) == 1

    def test_featured_media_zero_resolves_none_without_media_call(self):
        http_get = FakeHttpGet()
        http_get.route(
            lambda url: True,
            lambda url: FakeResponse(json_data=[{"featured_media": 0}]),
        )
        resolver = FeaturedMediaResolver(http_get=http_get, timeout_seconds=5)

        assert resolver.resolve(POST_LINK) is None
        assert len(http_get.calls) == 1

    def test_featured_media_missing_resolves_none(self):
        http_get = FakeHttpGet()
        http_get.route(
            lambda url: True, lambda url: FakeResponse(json_data=[{"id": 1}])
        )
        resolver = FeaturedMediaResolver(http_get=http_get, timeout_seconds=5)

        assert resolver.resolve(POST_LINK) is None

    def test_malformed_posts_payload_resolves_none(self):
        http_get = FakeHttpGet()
        http_get.route(
            lambda url: True,
            lambda url: FakeResponse(json_data={"unexpected": "shape"}),
        )
        resolver = FeaturedMediaResolver(http_get=http_get, timeout_seconds=5)

        assert resolver.resolve(POST_LINK) is None

    def test_media_lookup_failure_resolves_none(self):
        http_get = FakeHttpGet()
        http_get.route(
            lambda url: "/wp-json/wp/v2/posts" in url,
            lambda url: FakeResponse(json_data=[{"featured_media": 77}]),
        )
        http_get.route(
            lambda url: "/wp-json/wp/v2/media/" in url,
            lambda url: FakeResponse(status_code=404),
        )
        resolver = FeaturedMediaResolver(http_get=http_get, timeout_seconds=5)

        assert resolver.resolve(POST_LINK) is None

    def test_media_payload_without_source_url_resolves_none(self):
        http_get = _http_with_featured(source_url="")
        resolver = FeaturedMediaResolver(http_get=http_get, timeout_seconds=5)

        assert resolver.resolve(POST_LINK) is None


class TestTimeout:
    def test_rest_gets_use_the_configured_timeout(self):
        seen_timeouts = []

        class TimeoutRecordingHttpGet(FakeHttpGet):
            def __call__(self, url, timeout=None):
                seen_timeouts.append(timeout)
                return super().__call__(url, timeout=timeout)

        http_get = TimeoutRecordingHttpGet()
        http_get.route(
            lambda url: "/wp-json/wp/v2/posts" in url,
            lambda url: FakeResponse(json_data=[{"featured_media": 77}]),
        )
        http_get.route(
            lambda url: "/wp-json/wp/v2/media/" in url,
            lambda url: FakeResponse(json_data={"source_url": SOURCE_URL}),
        )
        resolver = FeaturedMediaResolver(http_get=http_get, timeout_seconds=7)

        resolver.resolve(POST_LINK)

        assert seen_timeouts == [7, 7]
