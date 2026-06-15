"""Data access for the WpImportedPost GUID ledger (S81)."""
from typing import List, Optional, Tuple

from plugins.wp_import.wp_import.models.wp_imported_post import WpImportedPost


class WpImportedPostRepository:
    """Exactly the queries WpImportService needs — nothing speculative."""

    SORT_COLUMNS = {
        "title": WpImportedPost.title_at_import,
        "imported_at": WpImportedPost.created_at,
    }

    def __init__(self, session) -> None:
        self._session = session

    def save(self, row: WpImportedPost) -> WpImportedPost:
        self._session.add(row)
        self._session.commit()
        return row

    def delete(self, row: WpImportedPost) -> None:
        self._session.delete(row)
        self._session.commit()

    def find_by_guid(self, wp_guid: str) -> Optional[WpImportedPost]:
        return (
            self._session.query(WpImportedPost)
            .filter(WpImportedPost.wp_guid == wp_guid)
            .first()
        )

    def find_by_ids(self, import_ids: List[str]) -> List[WpImportedPost]:
        return (
            self._session.query(WpImportedPost)
            .filter(WpImportedPost.id.in_(import_ids))
            .all()
        )

    def search(
        self,
        search: Optional[str],
        sort: str,
        order: str,
        page: int,
        per_page: int,
    ) -> Tuple[List[WpImportedPost], int]:
        query = self._session.query(WpImportedPost)
        if search:
            query = query.filter(
                WpImportedPost.title_at_import.ilike(f"%{search}%")
            )
        total = query.count()
        sort_column = self.SORT_COLUMNS.get(sort, WpImportedPost.created_at)
        ordered = sort_column.asc() if order == "asc" else sort_column.desc()
        rows = (
            query.order_by(ordered)
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        return rows, total
