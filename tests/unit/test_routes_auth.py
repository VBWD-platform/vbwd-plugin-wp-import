"""Route-level auth + validation tests for the wp-import admin endpoints.

Auth specs exercise the Flask test client up to the auth middleware
boundary (all four endpoints are admin-gated, D8). The pure feed-url
validation helper is tested directly — business logic lives in
service-level specs.
"""
import pytest

from plugins.wp_import.wp_import.routes import validate_feed_url


STATS_PATH = "/api/v1/admin/wp-import/stats?feed_url=https://example.com/feed/"
RUN_PATH = "/api/v1/admin/wp-import/run"
POSTS_PATH = "/api/v1/admin/wp-import/posts"
BULK_REMOVE_PATH = "/api/v1/admin/wp-import/posts/bulk/remove"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", STATS_PATH),
        ("POST", RUN_PATH),
        ("GET", POSTS_PATH),
        ("POST", BULK_REMOVE_PATH),
    ],
)
def test_admin_route_without_auth_returns_401(client, method, path):
    response = client.open(path, method=method)
    assert response.status_code == 401


class TestFeedUrlValidation:
    def test_valid_https_url_passes(self):
        assert validate_feed_url("https://example.com/feed/?token=x") is None

    @pytest.mark.parametrize(
        "invalid_url",
        ["", "not-a-url", "ftp://example.com/feed", "https://", None],
    )
    def test_invalid_urls_rejected(self, invalid_url):
        assert validate_feed_url(invalid_url) is not None
