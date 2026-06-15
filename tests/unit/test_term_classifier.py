"""WpTermClassifier unit tests — paginated category fetch + inverted rule.

Contract (S81 D1, revised after the live walkthrough): only ``/categories``
is fetched (paginated, ``per_page=100&page=N``); a name in the category set
is a category, **every other name is a tag**; ``/tags`` is never fetched.
REST-down fallback unchanged: everything classifies as category.
"""
from urllib.parse import parse_qs, urlsplit

from plugins.wp_import.tests.unit.feed_fixtures import FakeHttpGet, FakeResponse
from plugins.wp_import.wp_import.services.term_classifier import (
    WpTermClassifier,
)


SITE_BASE = "https://redrobot.example"
REST_PAGE_SIZE = 100
MAX_REST_PAGES = 10


def _paged_categories_http(category_names):
    """FakeHttpGet serving ``category_names`` in per_page-sized REST pages."""
    http_get = FakeHttpGet()

    def serve_page(url):
        query = parse_qs(urlsplit(url).query)
        per_page = int(query["per_page"][0])
        page_number = int(query.get("page", ["1"])[0])
        start = (page_number - 1) * per_page
        page_slice = category_names[start : start + per_page]
        return FakeResponse(json_data=[{"name": name} for name in page_slice])

    http_get.route(lambda url: "/wp-json/wp/v2/categories" in url, serve_page)
    return http_get


class TestInvertedClassification:
    def test_category_name_is_category_everything_else_is_tag(self):
        http_get = _paged_categories_http(["News"])
        classifier = WpTermClassifier(http_get=http_get, timeout_seconds=5)

        result = classifier.classify(SITE_BASE, ["News", "python", "Mystery"])

        assert result == {"News": "category", "python": "tag", "Mystery": "tag"}

    def test_category_match_is_case_insensitive(self):
        http_get = _paged_categories_http(["News"])
        classifier = WpTermClassifier(http_get=http_get, timeout_seconds=5)

        result = classifier.classify(SITE_BASE, ["NEWS", "news"])

        assert result == {"NEWS": "category", "news": "category"}

    def test_tags_endpoint_is_never_fetched(self):
        http_get = _paged_categories_http(["News"])
        classifier = WpTermClassifier(http_get=http_get, timeout_seconds=5)

        classifier.classify(SITE_BASE, ["News", "python"])

        assert all("/wp-json/wp/v2/tags" not in url for url in http_get.calls)


class TestPagination:
    def test_full_first_page_triggers_fetch_of_next_page(self):
        category_names = [f"Cat {index}" for index in range(REST_PAGE_SIZE + 50)]
        http_get = _paged_categories_http(category_names)
        classifier = WpTermClassifier(http_get=http_get, timeout_seconds=5)

        result = classifier.classify(SITE_BASE, ["Cat 120", "python"])

        assert result == {"Cat 120": "category", "python": "tag"}
        assert len(http_get.calls) == 2  # page 2 is short → no page 3

    def test_exact_page_multiple_stops_on_empty_followup_page(self):
        category_names = [f"Cat {index}" for index in range(REST_PAGE_SIZE)]
        http_get = _paged_categories_http(category_names)
        classifier = WpTermClassifier(http_get=http_get, timeout_seconds=5)

        result = classifier.classify(SITE_BASE, ["Cat 99"])

        assert result == {"Cat 99": "category"}
        assert len(http_get.calls) == 2  # page 1 full, page 2 empty → stop

    def test_pagination_is_capped(self):
        endless = [f"Cat {index}" for index in range(REST_PAGE_SIZE * 20)]
        http_get = _paged_categories_http(endless)
        classifier = WpTermClassifier(http_get=http_get, timeout_seconds=5)

        classifier.classify(SITE_BASE, ["whatever"])

        assert len(http_get.calls) == MAX_REST_PAGES

    def test_http_error_on_later_page_keeps_names_already_fetched(self):
        # WP answers 400 rest_post_invalid_page_number past the last page.
        first_page = [f"Cat {index}" for index in range(REST_PAGE_SIZE)]
        http_get = FakeHttpGet()

        def serve_page(url):
            query = parse_qs(urlsplit(url).query)
            if int(query.get("page", ["1"])[0]) > 1:
                return FakeResponse(status_code=400)
            return FakeResponse(json_data=[{"name": name} for name in first_page])

        http_get.route(lambda url: "/wp-json/wp/v2/categories" in url, serve_page)
        classifier = WpTermClassifier(http_get=http_get, timeout_seconds=5)

        result = classifier.classify(SITE_BASE, ["Cat 0", "python"])

        assert result == {"Cat 0": "category", "python": "tag"}


class TestRestUnreachableFallback:
    def test_connection_error_classifies_everything_as_category(self):
        def explode(url, timeout=None):
            raise ConnectionError("REST down")

        classifier = WpTermClassifier(http_get=explode, timeout_seconds=5)

        result = classifier.classify(SITE_BASE, ["News", "python"])

        assert result == {"News": "category", "python": "category"}

    def test_http_error_on_first_page_classifies_everything_as_category(self):
        http_get = FakeHttpGet()
        http_get.route(lambda url: True, lambda url: FakeResponse(status_code=401))
        classifier = WpTermClassifier(http_get=http_get, timeout_seconds=5)

        result = classifier.classify(SITE_BASE, ["python"])

        assert result == {"python": "category"}


class TestCaching:
    def test_categories_fetched_once_across_classify_calls(self):
        http_get = _paged_categories_http(["News"])
        classifier = WpTermClassifier(http_get=http_get, timeout_seconds=5)

        classifier.classify(SITE_BASE, ["News"])
        classifier.classify(SITE_BASE, ["python"])

        assert len(http_get.calls) == 1

    def test_rest_down_result_is_cached_too(self):
        call_count = {"value": 0}

        def explode(url, timeout=None):
            call_count["value"] += 1
            raise ConnectionError("REST down")

        classifier = WpTermClassifier(http_get=explode, timeout_seconds=5)

        classifier.classify(SITE_BASE, ["News"])
        classifier.classify(SITE_BASE, ["python"])

        assert call_count["value"] == 1
