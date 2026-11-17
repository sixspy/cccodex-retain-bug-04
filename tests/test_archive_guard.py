import sqlite3
import threading

import pytest

from app.db import get_db, init_db
from app.services.execute import execute_judgment
from app.services.reconcile import run_reconcile
from app.services.scan import run_expire_scan


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _first_archive_judgment(db_path: str) -> int:
    with get_db(db_path) as conn:
        run_expire_scan(conn, "2026-11-16")
        row = conn.execute(
            "SELECT id FROM judgments WHERE decided_action = '归档' "
            "AND status = '待执行' ORDER BY id LIMIT 1"
        ).fetchone()
        assert row, "种子数据应至少产出一条归档裁决"
        return int(row["id"])


def test_archive_persists_uri_and_snapshot_in_one_commit(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    jid = _first_archive_judgment(db)

    with get_db(db) as conn:
        run = execute_judgment(conn, jid)
        part_id = conn.execute(
            "SELECT partition_id FROM judgments WHERE id = ?", (jid,)
        ).fetchone()["partition_id"]
        part = conn.execute(
            "SELECT storage_state, archive_uri FROM partitions WHERE id = ?",
            (part_id,),
        ).fetchone()
        snap = conn.execute(
            "SELECT * FROM archive_snapshots WHERE partition_id = ?", (part_id,)
        ).fetchone()

    assert run["ok"] == 1
    assert run["result_state"] == "已归档"
    assert run["archive_uri"], "归档执行必须写入非空归档地址"
    # 归档态与地址同事务落库
    assert dict(part)["storage_state"] == "已归档"
    assert dict(part)["archive_uri"] == run["archive_uri"]
    assert dict(part)["archive_uri"].startswith("archive://retain/")
    # 实物快照齐全可核：行数 + 摘要 + 同址
    assert snap is not None
    snap = dict(snap)
    assert snap["archive_uri"] == run["archive_uri"]
    assert snap["row_count"] > 0
    assert len(snap["content_digest"]) == 16


def test_reconcile_flags_archived_partition_with_empty_uri(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    jid = _first_archive_judgment(db)

    with get_db(db) as conn:
        execute_judgment(conn, jid)
        part_id = conn.execute(
            "SELECT partition_id FROM judgments WHERE id = ?", (jid,)
        ).fetchone()["partition_id"]
        # 模拟脏数据：已归档但地址被清空（旧链路留下的无址归档）
        conn.execute(
            "UPDATE partitions SET archive_uri = '' WHERE id = ?", (part_id,)
        )

    with get_db(db) as conn:
        report = run_reconcile(conn, "2026-11-16")

    assert report["mismatch_count"] >= 1
    assert report["missing_archive_uri_count"] >= 1
    mm = report["mismatches"][0]
    assert mm["mismatch_type"] == "missing_archive_uri"
    assert mm["partition_id"] == part_id
    assert "缺址归档" in report["summary"]


def test_concurrent_double_click_only_one_succeeds(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    jid = _first_archive_judgment(db)

    results: list[dict] = []
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def worker():
        conn = _open_conn(db)
        try:
            barrier.wait()
            run = execute_judgment(conn, jid)
            conn.commit()
            results.append(dict(run))
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            errors.append(str(exc))
        finally:
            conn.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    # 两人连点：恰有一单成功，另一单被锁/状态复核拦下
    assert len(results) == 1, f"应只成功一单，实际成功 {len(results)}"
    assert len(errors) == 1
    assert results[0]["ok"] == 1
    assert results[0]["archive_uri"], "成功单必须带归档地址"

    with get_db(db) as conn:
        judgment = dict(
            conn.execute("SELECT status FROM judgments WHERE id = ?", (jid,)).fetchone()
        )
        runs = conn.execute(
            "SELECT COUNT(*) AS c FROM execute_runs WHERE judgment_id = ?", (jid,)
        ).fetchone()["c"]
        part = dict(
            conn.execute(
                "SELECT storage_state, archive_uri FROM partitions p "
                "JOIN judgments j ON j.partition_id = p.id WHERE j.id = ?",
                (jid,),
            ).fetchone()
        )
        snaps = conn.execute(
            "SELECT COUNT(*) AS c FROM archive_snapshots s "
            "JOIN judgments j ON j.partition_id = s.partition_id WHERE j.id = ?",
            (jid,),
        ).fetchone()["c"]

    assert judgment["status"] == "已完成"
    assert runs == 1, "执行流水只能有一条，不得双成功覆盖"
    assert part["storage_state"] == "已归档"
    assert part["archive_uri"], "落库分区必须带非空归档地址，放行可核"
    assert snaps == 1, "实物快照只能固化一份"


def test_re_execute_after_completion_is_rejected(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    jid = _first_archive_judgment(db)
    with get_db(db) as conn:
        first = execute_judgment(conn, jid)
        assert first["ok"] == 1
    with get_db(db) as conn2:
        with pytest.raises(ValueError, match="不可执行"):
            execute_judgment(conn2, jid)
