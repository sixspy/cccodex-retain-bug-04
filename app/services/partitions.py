import sqlite3


def _augment(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    items = [dict(r) for r in rows]
    snap_rows = conn.execute(
        "SELECT partition_id, judgment_id, archive_uri, domain_code, biz_date, "
        "row_count, content_digest, created_at "
        "FROM archive_snapshots"
    ).fetchall()
    snaps = {r["partition_id"]: dict(r) for r in snap_rows}
    for item in items:
        snap = snaps.get(item["id"])
        item["snapshot"] = snap
        item["archive_uri_missing"] = (
            item["storage_state"] == "已归档"
            and not (item["archive_uri"] or "").strip()
        )
        item["address_complete"] = not item["archive_uri_missing"]
    return items


def list_partitions(
    conn: sqlite3.Connection,
    domain_code: str | None = None,
    storage_state: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if domain_code:
        clauses.append("domain_code = ?")
        params.append(domain_code)
    if storage_state:
        clauses.append("storage_state = ?")
        params.append(storage_state)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT id, domain_code, biz_date, row_count, storage_state, "
        "archive_uri, created_at FROM partitions"
        + where
        + " ORDER BY domain_code, biz_date",
        params,
    ).fetchall()
    return _augment(conn, rows)


def get_partition(conn: sqlite3.Connection, partition_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, domain_code, biz_date, row_count, storage_state, "
        "archive_uri, created_at FROM partitions WHERE id = ?",
        (partition_id,),
    ).fetchone()
    if not row:
        return None
    return _augment(conn, [row])[0]
