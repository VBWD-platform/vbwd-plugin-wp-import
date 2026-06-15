"""Full fixture-feed import end-to-end against real PostgreSQL (S81).

Real cms services + real WpImportedPost ledger; only HTTP is faked
(fixture feed, fixture images, WP REST taxonomy). Proves: posts + terms +
image rows created with original publish dates, a second run imports
nothing, and remove() reopens the GUIDs (D2).
"""
from urllib.parse import parse_qs, urlsplit

import pytest

from plugins.wp_import.tests.unit.feed_fixtures import (
    FakeHttpGet,
    FakeResponse,
    build_feed_xml,
    EMPTY_FEED_XML,
)
from plugins.wp_import.wp_import.services.factory import build_wp_import_service


FEED_URL = "https://wpsite.example/feed/?token=sekret123&show_on_page=10"
PUB_DATE_ISO = "2024-03-04T09:30:00+00:00"

FEED_ITEMS = [
    {
        "guid": "https://wpsite.example/?p=1",
        "link": "https://wpsite.example/2024/03/first-post/",
        "title": "First Post",
        "content": '<p>Hello</p><img src="https://wpsite.example/img/a.jpg">',
        "excerpt": "First excerpt",
        "pub_date": "Mon, 04 Mar 2024 09:30:00 +0000",
        "categories": ["News", "python"],
        "media_url": "https://wpsite.example/img/featured.jpg",
    },
    {
        # text-only item (the live redrobot.online shape): no media:content,
        # no enclosure, no <img> in content — the featured image is only
        # reachable via the WP REST featured_media lookup; the excerpt
        # carries HTML markup that must import as plain text
        "guid": "https://wpsite.example/?p=2",
        "link": "https://wpsite.example/2024/03/second-post/",
        "title": "Second Post",
        "content": "<p>No images here</p>",
        "excerpt": "<p>Second &amp; excerpt</p>",
        "pub_date": "Tue, 05 Mar 2024 11:00:00 +0000",
        "categories": ["News"],
    },
]

REST_FEATURED_MEDIA_ID = 77
REST_FEATURED_SOURCE_URL = (
    "https://wpsite.example/wp-content/uploads/2024/03/rest-featured.jpg"
)

# minimal valid 1x1 GIF so Pillow can probe dimensions
ONE_PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
    b"\x00\x02\x02D\x01\x00;"
)


def _fixture_http():
    http_get = FakeHttpGet()

    def feed_response(url):
        paged = parse_qs(urlsplit(url).query).get("paged", ["1"])[0]
        if paged == "1":
            return FakeResponse(content=build_feed_xml(FEED_ITEMS))
        return FakeResponse(content=EMPTY_FEED_XML)

    http_get.route(lambda url: "/feed/" in url, feed_response)
    http_get.route(
        lambda url: "/wp-json/wp/v2/categories" in url,
        lambda url: FakeResponse(json_data=[{"name": "News"}]),
    )
    http_get.route(
        lambda url: "/wp-json/wp/v2/posts" in url and "slug=second-post" in url,
        lambda url: FakeResponse(
            json_data=[{"featured_media": REST_FEATURED_MEDIA_ID}]
        ),
    )
    http_get.route(
        lambda url: f"/wp-json/wp/v2/media/{REST_FEATURED_MEDIA_ID}" in url,
        lambda url: FakeResponse(
            json_data={"source_url": REST_FEATURED_SOURCE_URL}
        ),
    )
    http_get.route(
        lambda url: url.endswith(".jpg"),
        lambda url: FakeResponse(
            content=ONE_PIXEL_GIF, headers={"Content-Type": "image/gif"}
        ),
    )
    return http_get


@pytest.fixture
def service(app):
    return build_wp_import_service(http_get=_fixture_http())


@pytest.mark.integration
def test_full_import_run_end_to_end(service, app):
    from plugins.cms.src.routes import _post_service, _term_service
    from vbwd.extensions import db
    from plugins.wp_import.wp_import.models.wp_imported_post import WpImportedPost

    stats_before = service.stats(FEED_URL)
    assert stats_before == {
        "total_in_feed": 2,
        "never_imported": 2,
        "already_imported": 0,
    }

    result = service.run(FEED_URL, chunk=10)

    assert result["imported_count"] == 2
    assert result["feed_exhausted"] is True
    assert [item["status"] for item in result["results"]] == [
        "imported",
        "imported",
    ]
    assert all(item["image_misses"] == [] for item in result["results"])

    # posts exist with original publish dates + rewritten image URLs
    first_post = _post_service().get_post(result["results"][0]["cms_post_id"])
    assert first_post["title"] == "First Post"
    assert first_post["status"] == "published"
    assert first_post["published_at"] == PUB_DATE_ISO
    assert "https://wpsite.example/img/a.jpg" not in first_post["content_html"]
    assert "/img/" not in first_post["content_html"]
    assert first_post["featured_image_url"]
    assert not first_post["featured_image_url"].startswith("https://wpsite.example")

    # the text-only item got its featured image via the REST featured_media
    # lookup, re-hosted through the normal pipeline to a local /uploads/ path,
    # and its HTML excerpt imported as plain text
    second_post = _post_service().get_post(result["results"][1]["cms_post_id"])
    assert second_post["title"] == "Second Post"
    assert second_post["featured_image_url"].startswith("/uploads/")
    assert second_post["excerpt"] == "Second & excerpt"

    # D7: the "News" CATEGORY stays on the cms_term taxonomy (shared by both
    # posts via term_ids); no tag term is created there.
    term_service = _term_service()
    category_names = [term["name"] for term in term_service.list_terms("category")]
    assert category_names.count("News") == 1
    assert first_post["term_ids"] == sorted(
        [
            term["id"]
            for term in term_service.list_terms("category")
            if term["name"] == "News"
        ]
    )

    # the "python" TAG lands in the CORE catalog (vbwd_entity_tag, cms_post),
    # not as a cms_term('tag') row.
    from uuid import UUID

    from vbwd.services.tags_and_custom_fields import resolve_tags_and_custom_fields

    tags_port = resolve_tags_and_custom_fields()
    first_tags = tags_port.get_tags("cms_post", UUID(str(first_post["id"])))
    assert "python" in first_tags

    # ledger rows persisted with the token masked (D1a)
    ledger_rows = db.session.query(WpImportedPost).all()
    assert len(ledger_rows) == 2
    assert all("sekret123" not in row.feed_url for row in ledger_rows)

    # second run imports nothing (D2 dedup)
    second_run = service.run(FEED_URL, chunk=10)
    assert second_run["imported_count"] == 0
    assert second_run["skipped_count"] == 2
    assert second_run["feed_exhausted"] is True
    assert service.stats(FEED_URL)["already_imported"] == 2

    # the imported-posts listing joins live post data
    listing = service.list_imported(
        search=None, sort="title", order="asc", page=1, per_page=20
    )
    assert listing["total"] == 2
    assert [item["title"] for item in listing["items"]] == [
        "First Post",
        "Second Post",
    ]
    assert listing["items"][0]["categories"] == ["News"]
    assert listing["items"][0]["tags"] == ["python"]

    # remove deletes the posts AND ledger rows → GUIDs importable again (D2/D7)
    removal = service.remove([item["id"] for item in listing["items"]])
    assert removal == {"removed": 2}
    assert db.session.query(WpImportedPost).count() == 0
    assert service.stats(FEED_URL)["never_imported"] == 2
