"""芯片批次、测量、分析版本与不可变准入决定的 SQLite 结构及事务辅助。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 wafer_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, current_decision_id TEXT);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 wavelength_nm REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS analyses(
 analysis_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 seq INTEGER NOT NULL, input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
 result_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(lot_id,seq), UNIQUE(lot_id,input_sha256));
CREATE TABLE IF NOT EXISTS review_requests(
 request_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 seq INTEGER NOT NULL, reason TEXT NOT NULL, requested_by TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','closed')),
 decision_id TEXT, created_at TEXT NOT NULL, UNIQUE(lot_id,seq));
CREATE UNIQUE INDEX IF NOT EXISTS one_open_review_per_lot
ON review_requests(lot_id) WHERE status='open';
CREATE TABLE IF NOT EXISTS approval_decisions(
 decision_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 seq INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('initial','review')),
 analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id),
 decision TEXT NOT NULL CHECK(decision IN ('hold','release','reject')),
 reason TEXT NOT NULL, reviewer TEXT NOT NULL,
 prev_decision_id TEXT REFERENCES approval_decisions(decision_id),
 review_request_id TEXT REFERENCES review_requests(request_id),
 lot_revision INTEGER NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(lot_id,seq), UNIQUE(analysis_id));
CREATE TABLE IF NOT EXISTS idempotency_records(
 scope TEXT NOT NULL, key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
 response_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(scope,key));
CREATE TRIGGER IF NOT EXISTS approval_decisions_no_update
BEFORE UPDATE ON approval_decisions
BEGIN SELECT RAISE(FAIL,'approval decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS approval_decisions_no_delete
BEFORE DELETE ON approval_decisions
BEGIN SELECT RAISE(FAIL,'approval decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS analyses_no_update
BEFORE UPDATE ON analyses
BEGIN SELECT RAISE(FAIL,'analysis versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS analyses_no_delete
BEFORE DELETE ON analyses
BEGIN SELECT RAISE(FAIL,'analysis versions are immutable'); END;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # HTTP 服务以多线程方式复用同一连接；写操作均由 BEGIN IMMEDIATE 串行化，
    # 接口层另以锁保证一个业务用例内的多条语句不被交错执行。
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    # 兼容既有数据库：为旧版 chip_lots 补齐批次版本与当前决定指针。
    columns = {row[1] for row in db.execute("PRAGMA table_info(chip_lots)")}
    if "revision" not in columns:
        db.execute("ALTER TABLE chip_lots ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    if "current_decision_id" not in columns:
        db.execute("ALTER TABLE chip_lots ADD COLUMN current_decision_id TEXT")
    _migrate_legacy_approvals(db)
    db.commit()
    return db


def _migrate_legacy_approvals(db: sqlite3.Connection) -> None:
    """把修复前可覆盖的 approvals 表记录封存进不可变决定链。"""

    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='approvals'"
    ).fetchall()}
    if "approvals" not in tables:
        return
    legacy = db.execute(
        "SELECT lot_id,reviewer,decision,reason,created_at FROM approvals ORDER BY lot_id,created_at,rowid"
    ).fetchall()
    by_lot: dict[str, list] = {}
    for row in legacy:
        by_lot.setdefault(row["lot_id"], []).append(row)
    for lot_id, rows in by_lot.items():
        if db.execute("SELECT 1 FROM approval_decisions WHERE lot_id=? LIMIT 1", (lot_id,)).fetchone():
            continue
        prev_id = None
        for seq, row in enumerate(rows, start=1):
            basis = f"legacy:{lot_id}:{seq}:{row['created_at']}".encode()
            input_digest = hashlib.sha256(basis).hexdigest()
            analysis_id = f"legacy-{input_digest[:24]}"
            decision_id = f"legacy-decision-{input_digest[:20]}"
            legacy_result = json.dumps(
                {"provenance": "legacy-approvals-table", "reason": row["reason"]},
                sort_keys=True,
            )
            db.execute(
                "INSERT OR IGNORE INTO analyses VALUES(?,?,?,?,?,?,?)",
                (analysis_id, lot_id, seq, input_digest, legacy_result, row["reviewer"], row["created_at"]),
            )
            db.execute(
                "INSERT OR IGNORE INTO approval_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id, lot_id, seq, "initial" if seq == 1 else "review",
                    analysis_id, row["decision"], row["reason"], row["reviewer"],
                    prev_id, None, seq, row["created_at"],
                ),
            )
            prev_id = decision_id
        db.execute("UPDATE chip_lots SET current_decision_id=? WHERE lot_id=?", (prev_id, lot_id))


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
