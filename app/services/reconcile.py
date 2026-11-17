import sqlite3


def run_reconcile(conn: sqlite3.Connection, as_of_date: str) -> dict:
    """策略视角与实物分区对账：统计各存储态，并找出「有完成裁决但状态/实物不符」的错位。

    错位类型：
    - state_mismatch：裁决已完成但分区存储态与动作不符；
    - missing_archive_uri：归档裁决已完成、分区已归档，但归档地址为空/NULL
      （无址归档，下游无法定位入库实体，必须拦截并报错位）。
    """
    counts = {
        r["storage_state"]: r["c"]
        for r in conn.execute(
            "SELECT storage_state, COUNT(*) AS c FROM partitions GROUP BY storage_state"
        ).fetchall()
    }
    online = int(counts.get("在线", 0))
    archived = int(counts.get("已归档", 0))
    destroyed = int(counts.get("已销毁", 0))
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM judgments WHERE status IN ('待执行','执行中')"
    ).fetchone()["c"]

    mismatches = conn.execute(
        """
        SELECT j.id AS judgment_id, j.decided_action, j.status AS judgment_status,
               p.id AS partition_id, p.storage_state,
               COALESCE(p.archive_uri, '') AS archive_uri,
               CASE
                 WHEN j.decided_action = '归档'
                      AND p.storage_state = '已归档'
                      AND (p.archive_uri IS NULL OR TRIM(p.archive_uri) = '')
                 THEN 'missing_archive_uri'
                 ELSE 'state_mismatch'
               END AS mismatch_type
        FROM judgments j
        JOIN partitions p ON p.id = j.partition_id
        WHERE j.status = '已完成'
          AND (
            (j.decided_action = '归档' AND (
                p.storage_state != '已归档'
                OR p.archive_uri IS NULL
                OR TRIM(p.archive_uri) = ''
            ))
            OR (j.decided_action = '销毁' AND p.storage_state != '已销毁')
          )
        """
    ).fetchall()
    mismatch_rows = [dict(m) for m in mismatches]
    missing_uri = sum(
        1 for m in mismatch_rows if m["mismatch_type"] == "missing_archive_uri"
    )
    mismatch_count = len(mismatch_rows)
    summary = (
        f"在线{online} / 已归档{archived} / 已销毁{destroyed} / "
        f"待执行裁决{pending} / 错位{mismatch_count}"
        + (f"（其中缺址归档{missing_uri}）" if missing_uri else "")
    )
    cur = conn.execute(
        "INSERT INTO reconcile_reports "
        "(as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, summary) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            as_of_date,
            online,
            archived,
            destroyed,
            int(pending),
            mismatch_count,
            summary,
        ),
    )
    report_id = int(cur.lastrowid)
    row = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, summary, created_at "
        "FROM reconcile_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    report = dict(row)
    report["mismatches"] = mismatch_rows
    report["missing_archive_uri_count"] = missing_uri
    return report


def list_reports(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, summary, created_at "
        "FROM reconcile_reports ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def archive_overview_by_domain(conn: sqlite3.Connection) -> list[dict]:
    """按主题域汇总归档落地情况：已归档数、缺址数、已固化快照数，供策略详情核地址齐全性。"""
    rows = conn.execute(
        """
        SELECT p.domain_code AS domain_code,
               SUM(CASE WHEN p.storage_state = '已归档' THEN 1 ELSE 0 END) AS archived_count,
               SUM(CASE WHEN p.storage_state = '已归档'
                             AND (p.archive_uri IS NULL OR TRIM(p.archive_uri) = '')
                        THEN 1 ELSE 0 END) AS missing_uri_count,
               (SELECT COUNT(*) FROM archive_snapshots s
                 WHERE s.domain_code = p.domain_code) AS snapshot_count
        FROM partitions p
        GROUP BY p.domain_code
        ORDER BY p.domain_code
        """
    ).fetchall()
    return [dict(r) for r in rows]
