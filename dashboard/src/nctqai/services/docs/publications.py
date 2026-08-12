"""Publication data services for the NCTQ.ai dashboard.

SQL for /docs/publications pages. Queries silver.nctq_publications.
"""

from nctqai.db import SILVER_SCHEMA, run_sql
from nctqai.models.docs import PublicationSummary


def get_publications(
    search: str | None = None,
    pub_type: str | None = None,
) -> list[PublicationSummary]:
    """Get publication list for the table on /docs/publications."""
    where_clauses: list[str] = []
    params: list = []

    if search:
        where_clauses.append("(p.title ILIKE %s OR p.author ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    if pub_type:
        where_clauses.append("p.publication_type = %s")
        params.append(pub_type)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    sql = f"""
        SELECT
            p.publication_id,
            p.title,
            p.author,
            p.publication_type,
            p.tags,
            p.published_date::text,
            p.url
        FROM {SILVER_SCHEMA}.nctq_publications p
        {where}
        ORDER BY p.published_date DESC NULLS LAST, p.title ASC
        LIMIT 200
    """
    rows = run_sql(sql, tuple(params))

    results = []
    for row in rows:
        results.append(
            PublicationSummary(
                publication_id=str(row["publication_id"]),
                title=row["title"],
                author=row["author"],
                publication_type=row["publication_type"],
                tags=list(row["tags"]) if row["tags"] else None,
                published_date=row["published_date"],
                url=row["url"],
            )
        )
    return results


def get_publication_detail(pub_id: str) -> PublicationSummary | None:
    """Get a single publication by ID."""
    sql = f"""
        SELECT
            p.publication_id,
            p.title,
            p.author,
            p.publication_type,
            p.tags,
            p.published_date::text,
            p.url
        FROM {SILVER_SCHEMA}.nctq_publications p
        WHERE p.publication_id = %s
    """
    rows = run_sql(sql, (pub_id,))
    if not rows:
        return None

    row = rows[0]
    return PublicationSummary(
        publication_id=str(row["publication_id"]),
        title=row["title"],
        author=row["author"],
        publication_type=row["publication_type"],
        tags=list(row["tags"]) if row["tags"] else None,
        published_date=row["published_date"],
        url=row["url"],
    )
