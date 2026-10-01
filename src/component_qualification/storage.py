"""国产电子部件批次和测量记录的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 unit_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 signal_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT PRIMARY KEY REFERENCES chip_lots(lot_id),
 reviewer TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('release','hold','reject')),
 reason TEXT NOT NULL, expected_revision INTEGER, decided_revision INTEGER NOT NULL,
 request_digest TEXT NOT NULL, created_at TEXT NOT NULL);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:", *, cross_thread: bool = False) -> sqlite3.Connection:
    # isolation_level=None：写操作统一走 transaction() 的显式 BEGIN IMMEDIATE，
    # 读取在自动提交模式下进行，语义与同平台其他域保持一致。
    db = sqlite3.connect(path, isolation_level=None, check_same_thread=not cross_thread)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(SCHEMA)
    _migrate(db)
    db.commit()
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """把早期数据库补齐到当前结构。

    旧结构下失败审批可能写入与 chip_lots 无外键关联的审批和事件残片；
    重建带外键的表时这些孤儿行会被自然丢弃，不会被后来同名批次继承。
    """
    lot_columns = {row[1] for row in db.execute("PRAGMA table_info(chip_lots)")}
    if "revision" not in lot_columns:
        db.execute("ALTER TABLE chip_lots ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")

    for table, create_sql in (
        (
            "measurements",
            """
CREATE TABLE {table}_migrate(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 signal_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
INSERT INTO {table}_migrate
SELECT m.* FROM measurements m JOIN chip_lots l ON l.lot_id=m.lot_id;
""",
        ),
        (
            "lot_events",
            """
CREATE TABLE {table}_migrate(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
INSERT INTO {table}_migrate(event_id,lot_id,event_type,actor,payload,created_at)
SELECT e.* FROM lot_events e JOIN chip_lots l ON l.lot_id=e.lot_id;
""",
        ),
    ):
        current_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        if current_sql is not None and "REFERENCES chip_lots" not in current_sql:
            db.executescript(
                create_sql.format(table=table)
                + f"DROP TABLE {table};\nALTER TABLE {table}_migrate RENAME TO {table};"
            )

    approval_columns = {row[1] for row in db.execute("PRAGMA table_info(approvals)")}
    if approval_columns and "request_digest" not in approval_columns:
        db.executescript(
            """
CREATE TABLE approvals_migrate(
 lot_id TEXT PRIMARY KEY REFERENCES chip_lots(lot_id),
 reviewer TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('release','hold','reject')),
 reason TEXT NOT NULL, expected_revision INTEGER, decided_revision INTEGER NOT NULL,
 request_digest TEXT NOT NULL, created_at TEXT NOT NULL);
INSERT INTO approvals_migrate(lot_id,reviewer,decision,reason,expected_revision,
 decided_revision,request_digest,created_at)
SELECT a.lot_id,a.reviewer,a.decision,a.reason,NULL,l.revision,
       'legacy:' || lower(hex(randomblob(16))),a.created_at
FROM approvals a JOIN chip_lots l ON l.lot_id=a.lot_id;
DROP TABLE approvals;
ALTER TABLE approvals_migrate RENAME TO approvals;
"""
        )


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute("INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)", (lot_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()))
