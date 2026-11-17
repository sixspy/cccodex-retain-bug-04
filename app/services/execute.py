import hashlib
import sqlite3
import threading
from datetime import datetime

from app.services.partitions import get_partition

# 同一进程内的执行写锁：两人连点同一裁决时，第二个请求排队等待，
# 由事务内的状态复核（CAS）判定为不可执行，杜绝双成功互相覆盖。
_execute_lock = threading.RLock()

# 归档根目录前缀（实验室环境为逻辑地址，下游按此前缀定位入库实体）。
ARCHIVE_ROOT = "archive://retain"


class ArchiveError(ValueError):
    """归档前置条件不满足（如归档地址缺失），本条裁决回滚为失败。"""


def build_archive_uri(part: dict, run_at: datetime | None = None) -> str:
    """按 域/业务日/分区 生成确定性归档地址，保证同分区地址可复现、可核。"""
    run_at = run_at or datetime.now()
    return (
        f"{ARCHIVE_ROOT}/{part['domain_code']}"
        f"/biz_date={part['biz_date']}"
        f"/partition_{part['id']}_{run_at.strftime('%Y%m%d%H%M%S')}.arc"
    )


def build_snapshot_digest(part: dict, archive_uri: str) -> str:
    """实物快照摘要：域 + 业务日 + 行数 + 归档地址，落库后可与实物目录核对。"""
    payload = (
        f"{part['domain_code']}|{part['biz_date']}|"
        f"{part['row_count']}|{archive_uri}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _begin_immediate(conn: sqlite3.Connection) -> None:
    """开启立即写事务，抢先持有 SQLite 写锁，跨连接并发时后到者排队。"""
    if conn.in_transaction:
        return
    conn.execute("BEGIN IMMEDIATE")


def execute_judgment(conn: sqlite3.Connection, judgment_id: int) -> dict:
    """执行单条裁决：归档写地址并固化实物快照；销毁清空在线态。

    并发安全：进程内写锁串行化 + BEGIN IMMEDIATE 库级写锁 + 事务内
    状态/分区条件更新（CAS）。同一裁决连点只有一单成功，其余 409。
    整单失败则回滚本条并记录失败流水。
    """
    with _execute_lock:
        return _execute_locked(conn, judgment_id)


def _execute_locked(conn: sqlite3.Connection, judgment_id: int) -> dict:
    _begin_immediate(conn)
    row = conn.execute(
        "SELECT * FROM judgments WHERE id = ?", (judgment_id,)
    ).fetchone()
    if not row:
        conn.rollback()
        raise ValueError("裁决不存在")
    judgment = dict(row)
    if judgment["status"] not in ("待执行", "已失败"):
        conn.rollback()
        raise ValueError(f"当前状态不可执行：{judgment['status']}")

    part = get_partition(conn, judgment["partition_id"])
    if not part:
        conn.rollback()
        raise ValueError("分区不存在")
    if part["storage_state"] not in ("在线", "裁撤中"):
        conn.rollback()
        raise ValueError(f"分区状态不可裁撤：{part['storage_state']}")

    # CAS 占位：只在裁决仍处于可执行态时翻为执行中。
    # 并发事务若已先行翻态，rowcount=0，本单立即失败回滚，不覆盖前单。
    cur = conn.execute(
        "UPDATE judgments SET status = '执行中', fail_reason = '' "
        "WHERE id = ? AND status IN ('待执行', '已失败')",
        (judgment_id,),
    )
    if cur.rowcount == 0:
        conn.rollback()
        raise ValueError("裁决已被其他执行单处理，请刷新后重试")

    conn.execute(
        "UPDATE partitions SET storage_state = '裁撤中' WHERE id = ?",
        (part["id"],),
    )

    action = judgment["decided_action"]
    try:
        if action == "归档":
            archive_uri = build_archive_uri(part)
            if not archive_uri or not archive_uri.strip():
                # 缺址即拦：不允许无地址的「已归档」落库
                raise ArchiveError("归档地址缺失，已拦截本次归档")
            archive_uri = archive_uri.strip()
            digest = build_snapshot_digest(part, archive_uri)

            # 归档态与地址同一事务落库；条件更新保证只从裁撤中推进
            cur = conn.execute(
                "UPDATE partitions SET storage_state = '已归档', "
                "archive_uri = ? WHERE id = ? AND storage_state = '裁撤中'",
                (archive_uri, part["id"]),
            )
            if cur.rowcount == 0:
                raise ArchiveError("分区状态已变化，归档被拦截")

            # 实物快照固化：行数与摘要随地址同事务写入，下游据此放行
            conn.execute(
                "INSERT OR REPLACE INTO archive_snapshots "
                "(partition_id, judgment_id, archive_uri, domain_code, "
                "biz_date, row_count, content_digest) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    part["id"],
                    judgment_id,
                    archive_uri,
                    part["domain_code"],
                    part["biz_date"],
                    part["row_count"],
                    digest,
                ),
            )
            result_state = "已归档"
            detail = (
                f"分区已归档，地址 {archive_uri}；"
                f"实物快照 {part['row_count']} 行，摘要 {digest}"
            )
        elif action == "销毁":
            conn.execute(
                "UPDATE partitions SET storage_state = '已销毁', archive_uri = NULL, "
                "row_count = 0 WHERE id = ? AND storage_state = '裁撤中'",
                (part["id"],),
            )
            result_state = "已销毁"
            archive_uri = None
            detail = "分区已销毁，在线行数清零"
        else:
            raise ValueError(f"未知动作：{action}")

        conn.execute(
            "UPDATE judgments SET status = '已完成' WHERE id = ?",
            (judgment_id,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO execute_runs "
            "(judgment_id, action, result_state, archive_uri, ok, detail) "
            "VALUES (?,?,?,?,1,?)",
            (judgment_id, action, result_state, archive_uri, detail),
        )
        run = get_run_by_judgment(conn, judgment_id)
        # 归档态、地址、快照同事务提交，调用方（get_db）再提交为空操作
        conn.commit()
        return run
    except Exception as exc:  # noqa: BLE001 — 失败闭：恢复在线并记失败后提交失败态
        conn.execute(
            "UPDATE partitions SET storage_state = '在线', archive_uri = NULL "
            "WHERE id = ?",
            (part["id"],),
        )
        conn.execute(
            "DELETE FROM archive_snapshots WHERE partition_id = ?",
            (part["id"],),
        )
        conn.execute(
            "UPDATE judgments SET status = '已失败', fail_reason = ? WHERE id = ?",
            (str(exc), judgment_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO execute_runs "
            "(judgment_id, action, result_state, archive_uri, ok, detail) "
            "VALUES (?,?,?,?,0,?)",
            (judgment_id, action, "在线", None, str(exc)),
        )
        # 失败记录与分区恢复必须落库，不能随异常一起回滚
        conn.commit()
        raise


def get_run_by_judgment(conn: sqlite3.Connection, judgment_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, judgment_id, action, result_state, archive_uri, ok, detail, created_at "
        "FROM execute_runs WHERE judgment_id = ?",
        (judgment_id,),
    ).fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, judgment_id, action, result_state, archive_uri, ok, detail, created_at "
        "FROM execute_runs ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_snapshot(conn: sqlite3.Connection, partition_id: int) -> dict | None:
    row = conn.execute(
        "SELECT partition_id, judgment_id, archive_uri, domain_code, biz_date, "
        "row_count, content_digest, created_at "
        "FROM archive_snapshots WHERE partition_id = ?",
        (partition_id,),
    ).fetchone()
    return dict(row) if row else None


def list_snapshots(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT partition_id, judgment_id, archive_uri, domain_code, biz_date, "
        "row_count, content_digest, created_at "
        "FROM archive_snapshots ORDER BY partition_id"
    ).fetchall()
    return [dict(r) for r in rows]
