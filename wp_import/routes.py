"""WP Import admin API routes (S81).

All endpoints are admin-only and reuse the existing ``cms.manage``
permission (D8). Upstream feed failures surface as 502 with a
token-masked message (D1a).

    GET  /api/v1/admin/wp-import/stats?feed_url=…
    POST /api/v1/admin/wp-import/run                {"feed_url", "chunk"}
    GET  /api/v1/admin/wp-import/posts              ?search=&sort=&order=&page=&per_page=
    POST /api/v1/admin/wp-import/posts/bulk/remove  {"ids": […]}
"""
from typing import Optional
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request

from vbwd.middleware.auth import require_admin, require_auth, require_permission

from plugins.wp_import.wp_import.services.factory import build_wp_import_service
from plugins.wp_import.wp_import.services.feed_reader import FeedFetchError

wp_import_bp = Blueprint("wp_import", __name__)

DEFAULT_CHUNK = 10
MAX_CHUNK = 100
DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 100
SORTABLE_COLUMNS = ("title", "imported_at")


def validate_feed_url(feed_url: str) -> Optional[str]:
    """Return an error message for an invalid feed URL, else None."""
    split_url = urlsplit(feed_url or "")
    if split_url.scheme not in ("http", "https") or not split_url.netloc:
        return "feed_url must be a valid http(s) URL"
    return None


@wp_import_bp.route("/api/v1/admin/wp-import/stats", methods=["GET"])
@require_auth
@require_admin
@require_permission("cms.manage")
def admin_wp_import_stats():
    feed_url = request.args.get("feed_url", "")
    validation_error = validate_feed_url(feed_url)
    if validation_error:
        return jsonify({"error": validation_error}), 400
    try:
        return jsonify(build_wp_import_service().stats(feed_url)), 200
    except FeedFetchError as fetch_error:
        return jsonify({"error": str(fetch_error)}), 502


@wp_import_bp.route("/api/v1/admin/wp-import/run", methods=["POST"])
@require_auth
@require_admin
@require_permission("cms.manage")
def admin_wp_import_run():
    data = request.get_json(silent=True) or {}
    feed_url = data.get("feed_url", "")
    validation_error = validate_feed_url(feed_url)
    if validation_error:
        return jsonify({"error": validation_error}), 400
    try:
        chunk = int(data.get("chunk", DEFAULT_CHUNK))
    except (TypeError, ValueError):
        return jsonify({"error": "chunk must be an integer"}), 400
    if not 1 <= chunk <= MAX_CHUNK:
        return jsonify({"error": f"chunk must be between 1 and {MAX_CHUNK}"}), 400
    try:
        return jsonify(build_wp_import_service().run(feed_url, chunk)), 200
    except FeedFetchError as fetch_error:
        return jsonify({"error": str(fetch_error)}), 502


@wp_import_bp.route("/api/v1/admin/wp-import/posts", methods=["GET"])
@require_auth
@require_admin
@require_permission("cms.manage")
def admin_wp_import_posts():
    sort = request.args.get("sort", "imported_at")
    if sort not in SORTABLE_COLUMNS:
        sort = "imported_at"
    order = "asc" if request.args.get("order") == "asc" else "desc"
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(
        max(request.args.get("per_page", DEFAULT_PER_PAGE, type=int), 1),
        MAX_PER_PAGE,
    )
    result = build_wp_import_service().list_imported(
        search=request.args.get("search") or None,
        sort=sort,
        order=order,
        page=page,
        per_page=per_page,
    )
    return jsonify(result), 200


@wp_import_bp.route("/api/v1/admin/wp-import/posts/bulk/remove", methods=["POST"])
@require_auth
@require_admin
@require_permission("cms.manage")
def admin_wp_import_bulk_remove():
    data = request.get_json(silent=True) or {}
    import_ids = data.get("ids")
    if not isinstance(import_ids, list) or not import_ids:
        return jsonify({"error": "ids array required"}), 400
    return jsonify(build_wp_import_service().remove(import_ids)), 200
