"""WpImportService unit tests — mocked cms services + fake image HTTP (S81)."""
from unittest.mock import MagicMock
from uuid import uuid4

from plugins.cms.src.services.post_service import (
    PostNotFoundError,
    PostSlugConflictError,
)
from plugins.wp_import.tests.unit.feed_fixtures import FakeHttpGet, FakeResponse
from plugins.wp_import.wp_import.services.wp_import_service import WpImportService


FEED_URL = "https://redrobot.example/feed/?token=sekret123&show_on_page=10"


def _feed_item(**overrides):
    item = {
        "guid": f"https://redrobot.example/?p={uuid4().hex[:6]}",
        "link": "https://redrobot.example/2024/03/hello-world/",
        "title": "Hello World",
        "content_html": "<p>Body</p>",
        "excerpt": "Short",
        "published_at": "2024-03-04T09:30:00+00:00",
        "term_names": [],
        "media_urls": [],
    }
    item.update(overrides)
    return item


def _image_http(urls):
    http_get = FakeHttpGet()
    for url in urls:
        http_get.route(
            lambda candidate, url=url: candidate == url,
            lambda candidate: FakeResponse(
                content=b"image-bytes", headers={"Content-Type": "image/jpeg"}
            ),
        )
    return http_get


def _build_service(items, http_get=None, repository=None, featured_media_resolver=None):
    feed_reader = MagicMock()
    feed_reader.fetch_all.return_value = items
    term_classifier = MagicMock()
    term_classifier.classify.side_effect = lambda site, names: {
        name: "category" for name in names
    }
    if featured_media_resolver is None:
        featured_media_resolver = MagicMock()
        featured_media_resolver.resolve.return_value = None
    post_service = MagicMock()
    post_service.create_post.side_effect = lambda data: {"id": str(uuid4()), **data}
    term_service = MagicMock()
    term_service.find_or_create.side_effect = lambda term_type, name: {
        "id": str(uuid4()),
        "term_type": term_type,
        "name": name,
    }
    image_service = MagicMock()
    image_service.upload_image.return_value = {"url_path": "/media/images/local.jpg"}
    if repository is None:
        repository = MagicMock()
        repository.find_by_guid.return_value = None
    tags_port = MagicMock()
    tags_port.get_tags.return_value = []
    service = WpImportService(
        feed_reader=feed_reader,
        term_classifier=term_classifier,
        post_service=post_service,
        term_service=term_service,
        image_service=image_service,
        repository=repository,
        featured_media_resolver=featured_media_resolver,
        http_get=http_get or FakeHttpGet(),
        http_timeout_seconds=5,
        max_image_bytes=1024 * 1024,
        tags_port=tags_port,
    )
    return service


def _imported_row(guid, cms_post_id=None):
    row = MagicMock()
    row.wp_guid = guid
    row.cms_post_id = cms_post_id or uuid4()
    return row


class TestStats:
    def test_counts_by_the_d2_importability_rule(self):
        items = [
            _feed_item(guid="g-live"),
            _feed_item(guid="g-stale"),
            _feed_item(guid="g-new"),
        ]
        repository = MagicMock()
        live_row = _imported_row("g-live")
        stale_row = _imported_row("g-stale")
        repository.find_by_guid.side_effect = lambda guid: {
            "g-live": live_row,
            "g-stale": stale_row,
        }.get(guid)
        service = _build_service(items, repository=repository)
        # the stale row's post was deleted externally
        service._post_service.get_post.side_effect = lambda post_id: (
            (_ for _ in ()).throw(PostNotFoundError(post_id))
            if post_id == str(stale_row.cms_post_id)
            else {"id": post_id}
        )

        result = service.stats(FEED_URL)

        assert result == {
            "total_in_feed": 3,
            "never_imported": 2,
            "already_imported": 1,
        }
        # the orphan row is purged lazily (D2)
        repository.delete.assert_called_once_with(stale_row)


class TestRunDeduplication:
    def test_duplicate_guid_skipped_while_its_post_exists(self):
        items = [_feed_item(guid="g-dup"), _feed_item(guid="g-new")]
        repository = MagicMock()
        repository.find_by_guid.side_effect = lambda guid: (
            _imported_row("g-dup") if guid == "g-dup" else None
        )
        service = _build_service(items, repository=repository)
        service._post_service.get_post.return_value = {"id": "x"}

        result = service.run(FEED_URL, chunk=10)

        assert result["imported_count"] == 1
        assert result["skipped_count"] == 1
        assert result["feed_exhausted"] is True
        assert service._post_service.create_post.call_count == 1

    def test_guid_importable_again_after_complete_removal(self):
        items = [_feed_item(guid="g-readded")]
        repository = MagicMock()
        repository.find_by_guid.return_value = None  # row removed → importable
        service = _build_service(items, repository=repository)

        result = service.run(FEED_URL, chunk=10)

        assert result["imported_count"] == 1


class TestRunPostCreation:
    def test_pub_date_lands_in_published_at_exactly(self):
        items = [_feed_item(published_at="2024-03-04T09:30:00+00:00")]
        service = _build_service(items)

        service.run(FEED_URL, chunk=10)

        created = service._post_service.create_post.call_args.args[0]
        assert created["type"] == "post"
        assert created["status"] == "published"
        update_call = service._post_service.update_post.call_args
        assert update_call.args[1] == {"published_at": "2024-03-04T09:30:00+00:00"}

    def test_slug_derived_from_link_path(self):
        items = [_feed_item(link="https://redrobot.example/2024/03/my-article/")]
        service = _build_service(items)

        service.run(FEED_URL, chunk=10)

        created = service._post_service.create_post.call_args.args[0]
        assert created["slug"] == "my-article"

    def test_slug_collision_gets_numeric_suffix(self):
        items = [_feed_item(link="https://redrobot.example/taken/")]
        service = _build_service(items)
        service._post_service.create_post.side_effect = [
            PostSlugConflictError("taken"),
            {"id": str(uuid4())},
        ]

        result = service.run(FEED_URL, chunk=10)

        assert result["imported_count"] == 1
        retried = service._post_service.create_post.call_args.args[0]
        assert retried["slug"] == "taken-2"

    def test_persisted_feed_url_is_token_masked(self):
        items = [_feed_item()]
        service = _build_service(items)

        service.run(FEED_URL, chunk=10)

        saved_row = service._repository.save.call_args.args[0]
        assert "sekret123" not in saved_row.feed_url
        assert saved_row.wp_guid == items[0]["guid"]

    def test_item_error_does_not_abort_the_chunk(self):
        items = [_feed_item(guid="g-bad"), _feed_item(guid="g-good")]
        service = _build_service(items)
        good_post_id = str(uuid4())
        service._post_service.create_post.side_effect = [
            ValueError("title is required"),
            {"id": good_post_id},
        ]

        result = service.run(FEED_URL, chunk=10)

        assert result["imported_count"] == 1
        assert [r["status"] for r in result["results"]] == ["error", "imported"]
        assert result["results"][0]["error"]
        assert result["results"][1]["cms_post_id"] == good_post_id

    def test_chunk_limits_the_run_and_reports_not_exhausted(self):
        items = [_feed_item(guid=f"g-{n}") for n in range(5)]
        service = _build_service(items)

        result = service.run(FEED_URL, chunk=2)

        assert result["imported_count"] == 2
        assert result["feed_exhausted"] is False


class TestRunImages:
    CONTENT = (
        '<p>x</p><img src="https://img.example/a.jpg">'
        '<img src="https://img.example/a.jpg">'
    )

    def test_content_img_src_rewritten_to_uploaded_url_path(self):
        items = [_feed_item(content_html=self.CONTENT)]
        http_get = _image_http(["https://img.example/a.jpg"])
        service = _build_service(items, http_get=http_get)

        service.run(FEED_URL, chunk=10)

        created = service._post_service.create_post.call_args.args[0]
        assert "https://img.example/a.jpg" not in created["content_html"]
        assert created["content_html"].count("/media/images/local.jpg") == 2

    def test_same_image_url_downloaded_once_per_run(self):
        items = [
            _feed_item(content_html=self.CONTENT, guid="g-1"),
            _feed_item(content_html=self.CONTENT, guid="g-2"),
        ]
        http_get = _image_http(["https://img.example/a.jpg"])
        service = _build_service(items, http_get=http_get)

        service.run(FEED_URL, chunk=10)

        assert http_get.calls.count("https://img.example/a.jpg") == 1

    def test_failed_image_keeps_remote_url_and_is_reported_as_miss(self):
        items = [_feed_item(content_html='<img src="https://img.example/gone.jpg">')]
        http_get = FakeHttpGet()
        http_get.route(lambda url: True, lambda url: FakeResponse(status_code=404))
        service = _build_service(items, http_get=http_get)

        result = service.run(FEED_URL, chunk=10)

        created = service._post_service.create_post.call_args.args[0]
        assert "https://img.example/gone.jpg" in created["content_html"]
        assert result["results"][0]["image_misses"] == ["https://img.example/gone.jpg"]

    def test_oversized_image_is_a_miss(self):
        items = [_feed_item(content_html='<img src="https://img.example/big.jpg">')]
        http_get = FakeHttpGet()
        http_get.route(
            lambda url: True,
            lambda url: FakeResponse(
                content=b"x" * 2048, headers={"Content-Type": "image/jpeg"}
            ),
        )
        service = _build_service(items, http_get=http_get)
        service._max_image_bytes = 1024

        result = service.run(FEED_URL, chunk=10)

        assert result["results"][0]["image_misses"] == ["https://img.example/big.jpg"]


class TestFeaturedImage:
    def test_media_url_wins_over_content_image(self):
        items = [
            _feed_item(
                media_urls=["https://img.example/featured.jpg"],
                content_html='<img src="https://img.example/inline.jpg">',
            )
        ]
        http_get = _image_http(
            ["https://img.example/featured.jpg", "https://img.example/inline.jpg"]
        )
        service = _build_service(items, http_get=http_get)

        service.run(FEED_URL, chunk=10)

        created = service._post_service.create_post.call_args.args[0]
        assert created["featured_image_url"] == "/media/images/local.jpg"
        assert http_get.calls.count("https://img.example/featured.jpg") == 1

    def test_first_content_image_used_when_no_media(self):
        items = [_feed_item(content_html='<img src="https://img.example/inline.jpg">')]
        http_get = _image_http(["https://img.example/inline.jpg"])
        service = _build_service(items, http_get=http_get)

        service.run(FEED_URL, chunk=10)

        created = service._post_service.create_post.call_args.args[0]
        assert created["featured_image_url"] == "/media/images/local.jpg"

    def test_failed_featured_download_keeps_remote_url(self):
        items = [_feed_item(media_urls=["https://img.example/featured.jpg"])]
        http_get = FakeHttpGet()
        http_get.route(lambda url: True, lambda url: FakeResponse(status_code=500))
        service = _build_service(items, http_get=http_get)

        result = service.run(FEED_URL, chunk=10)

        created = service._post_service.create_post.call_args.args[0]
        assert created["featured_image_url"] == "https://img.example/featured.jpg"
        assert "https://img.example/featured.jpg" in (
            result["results"][0]["image_misses"]
        )


class TestRestFeaturedLookup:
    """REST featured_media enrichment in the precedence (D4 + D1):
    media:content/thumbnail → enclosure → REST lookup → first content image."""

    REST_URL = "https://redrobot.example/wp-content/uploads/rest-featured.jpg"

    @staticmethod
    def _resolver(resolved_url):
        resolver = MagicMock()
        resolver.resolve.return_value = resolved_url
        return resolver

    def test_no_rest_call_when_media_url_present(self):
        items = [_feed_item(media_urls=["https://img.example/featured.jpg"])]
        resolver = self._resolver(self.REST_URL)
        http_get = _image_http(["https://img.example/featured.jpg"])
        service = _build_service(
            items, http_get=http_get, featured_media_resolver=resolver
        )

        service.run(FEED_URL, chunk=10)

        resolver.resolve.assert_not_called()

    def test_rest_resolved_url_rehosted_via_the_normal_pipeline(self):
        items = [_feed_item()]  # no media, no content images
        resolver = self._resolver(self.REST_URL)
        http_get = _image_http([self.REST_URL])
        service = _build_service(
            items, http_get=http_get, featured_media_resolver=resolver
        )

        result = service.run(FEED_URL, chunk=10)

        resolver.resolve.assert_called_once_with(items[0]["link"])
        created = service._post_service.create_post.call_args.args[0]
        assert created["featured_image_url"] == "/media/images/local.jpg"
        assert result["results"][0]["image_misses"] == []

    def test_rest_lookup_wins_over_first_content_image(self):
        items = [_feed_item(content_html='<img src="https://img.example/inline.jpg">')]
        resolver = self._resolver(self.REST_URL)
        http_get = _image_http([self.REST_URL, "https://img.example/inline.jpg"])
        service = _build_service(
            items, http_get=http_get, featured_media_resolver=resolver
        )

        service.run(FEED_URL, chunk=10)

        created = service._post_service.create_post.call_args.args[0]
        assert created["featured_image_url"] == "/media/images/local.jpg"
        assert http_get.calls.count(self.REST_URL) == 1

    def test_rest_miss_falls_through_to_first_content_image(self):
        items = [_feed_item(content_html='<img src="https://img.example/inline.jpg">')]
        resolver = self._resolver(None)  # REST down / featured_media 0 / missing
        http_get = _image_http(["https://img.example/inline.jpg"])
        service = _build_service(
            items, http_get=http_get, featured_media_resolver=resolver
        )

        result = service.run(FEED_URL, chunk=10)

        created = service._post_service.create_post.call_args.args[0]
        assert created["featured_image_url"] == "/media/images/local.jpg"
        assert result["results"][0]["status"] == "imported"

    def test_rest_miss_without_content_image_leaves_featured_none(self):
        items = [_feed_item()]
        resolver = self._resolver(None)
        service = _build_service(items, featured_media_resolver=resolver)

        result = service.run(FEED_URL, chunk=10)

        created = service._post_service.create_post.call_args.args[0]
        assert created["featured_image_url"] is None
        assert result["results"][0]["status"] == "imported"

    def test_failed_download_of_rest_url_keeps_remote_url_as_miss(self):
        items = [_feed_item()]
        resolver = self._resolver(self.REST_URL)
        http_get = FakeHttpGet()
        http_get.route(lambda url: True, lambda url: FakeResponse(status_code=404))
        service = _build_service(
            items, http_get=http_get, featured_media_resolver=resolver
        )

        result = service.run(FEED_URL, chunk=10)

        created = service._post_service.create_post.call_args.args[0]
        assert created["featured_image_url"] == self.REST_URL
        assert result["results"][0]["image_misses"] == [self.REST_URL]

    def test_rest_url_shared_with_content_image_downloaded_once(self):
        items = [_feed_item(content_html=f'<img src="{self.REST_URL}">')]
        resolver = self._resolver(self.REST_URL)
        http_get = _image_http([self.REST_URL])
        service = _build_service(
            items, http_get=http_get, featured_media_resolver=resolver
        )

        service.run(FEED_URL, chunk=10)

        # the content-image pass populated the per-run URL cache, so the
        # REST-resolved featured candidate re-uses it — one download total
        assert http_get.calls.count(self.REST_URL) == 1


class TestTerms:
    def test_categories_via_terms_and_tags_via_core_port(self):
        """D7: classified categories stay on the cms_term taxonomy; classified
        tags go to the core tags port (set_tags on cms_post), never
        find_or_create('tag')."""
        items = [_feed_item(term_names=["News", "python", "Web Dev"])]
        service = _build_service(items)
        service._term_classifier.classify.side_effect = None
        service._term_classifier.classify.return_value = {
            "News": "category",
            "python": "tag",
            "Web Dev": "tag",
        }

        service.run(FEED_URL, chunk=10)

        find_or_create_calls = [
            call.args for call in service._term_service.find_or_create.call_args_list
        ]
        assert ("category", "News") in find_or_create_calls
        # No tag term is created on the cms_term taxonomy.
        assert all(args[0] != "tag" for args in find_or_create_calls)
        # Only the single category is assigned as a term.
        service._post_service.assign_terms.assert_called_once()
        assigned_term_ids = service._post_service.assign_terms.call_args.args[1]
        assert len(assigned_term_ids) == 1

        # Tags routed to the core port as cms_post-scoped slugs.
        service._tags_port.set_tags.assert_called_once()
        entity_type, _entity_id, slugs = service._tags_port.set_tags.call_args.args
        assert entity_type == "cms_post"
        assert slugs == ["python", "web-dev"]

    def test_no_tags_does_not_call_set_tags(self):
        items = [_feed_item(term_names=["News"])]
        service = _build_service(items)
        service._term_classifier.classify.side_effect = None
        service._term_classifier.classify.return_value = {"News": "category"}

        service.run(FEED_URL, chunk=10)

        service._tags_port.set_tags.assert_not_called()

    def test_classifier_receives_the_site_base_url(self):
        items = [_feed_item(term_names=["News"])]
        service = _build_service(items)

        service.run(FEED_URL, chunk=10)

        site_base = service._term_classifier.classify.call_args.args[0]
        assert site_base == "https://redrobot.example"


class TestListImported:
    def test_stale_rows_are_purged_and_excluded(self):
        live_row = _imported_row("g-live")
        live_row.id = uuid4()
        live_row.created_at = MagicMock()
        live_row.created_at.isoformat.return_value = "2026-06-11T10:00:00"
        stale_row = _imported_row("g-stale")
        repository = MagicMock()
        repository.search.return_value = ([live_row, stale_row], 2)
        service = _build_service([], repository=repository)
        service._post_service.get_post.side_effect = lambda post_id: (
            (_ for _ in ()).throw(PostNotFoundError(post_id))
            if post_id == str(stale_row.cms_post_id)
            else {
                "id": post_id,
                "title": "Live",
                "status": "published",
                "term_ids": [],
            }
        )

        result = service.list_imported(
            search=None, sort="imported_at", order="desc", page=1, per_page=20
        )

        assert result["total"] == 1
        assert [item["title"] for item in result["items"]] == ["Live"]
        repository.delete.assert_called_once_with(stale_row)


class TestRemove:
    def test_remove_deletes_cms_post_and_ledger_row(self):
        row = _imported_row("g-1")
        repository = MagicMock()
        repository.find_by_ids.return_value = [row]
        service = _build_service([], repository=repository)

        result = service.remove([str(row.cms_post_id)])

        assert result == {"removed": 1}
        service._post_service.delete_post.assert_called_once_with(str(row.cms_post_id))
        repository.delete.assert_called_once_with(row)

    def test_remove_tolerates_already_deleted_post(self):
        row = _imported_row("g-1")
        repository = MagicMock()
        repository.find_by_ids.return_value = [row]
        service = _build_service([], repository=repository)
        service._post_service.delete_post.side_effect = PostNotFoundError("gone")

        result = service.remove(["any-id"])

        assert result == {"removed": 1}
        repository.delete.assert_called_once_with(row)
