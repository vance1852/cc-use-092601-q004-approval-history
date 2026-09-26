"""芯片批次、测量、分析版本与不可变审批记录的 SQLite 结构及事务辅助。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 wafer_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 wavelength_nm REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);

-- 批次版本与当前有效决定指针。revision 随每次分析、复议和决定单调递增，
-- current_* 始终指向当前有效对象，历史记录只追加、从不更新。
CREATE TABLE IF NOT EXISTS lot_revision(
 lot_id TEXT PRIMARY KEY REFERENCES chip_lots(lot_id),
 revision INTEGER NOT NULL DEFAULT 0,
 current_analysis_id TEXT, current_decision_seq INTEGER,
 current_review_id INTEGER);
CREATE TABLE IF NOT EXISTS analysis_versions(
 analysis_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 lot_revision INTEGER NOT NULL,
 measurement_count INTEGER NOT NULL, input_sha256 TEXT NOT NULL,
 result_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(lot_id,input_sha256));
-- 审批决定链：仅追加。seq 为批次内单调序号，决定不可变、不可删除。
CREATE TABLE IF NOT EXISTS approval_records(
 decision_id TEXT NOT NULL, lot_id TEXT NOT NULL, seq INTEGER NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('release','hold','reject')),
 reason TEXT NOT NULL, analysis_id TEXT NOT NULL,
 review_id INTEGER, decided_by TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(lot_id,seq),
 UNIQUE(lot_id,decision_id),
 FOREIGN KEY(analysis_id) REFERENCES analysis_versions(analysis_id));
CREATE INDEX IF NOT EXISTS idx_approval_records_lot ON approval_records(lot_id,seq);
-- 显式复议：首次决定无需复议；其后任何决定都必须引用一条开放中的复议。
CREATE TABLE IF NOT EXISTS review_requests(
 review_id INTEGER PRIMARY KEY AUTOINCREMENT,
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 requested_by TEXT NOT NULL, reason TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','consumed','cancelled')),
 requested_at TEXT NOT NULL, consumed_by_seq INTEGER);
CREATE INDEX IF NOT EXISTS idx_review_requests_lot ON review_requests(lot_id,review_id);
-- 决定幂等：同键同载荷返回原结果；同键不同载荷冲突。
CREATE TABLE IF NOT EXISTS approval_idempotency(
 scope TEXT NOT NULL, key TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(scope,key));
"""

_DECISION_STATUS = {"release": "released", "hold": "hold", "reject": "rejected"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate(db: sqlite3.Connection) -> None:
    """把只保存“最后状态”的旧库升级为追加式审批链，旧审批不丢失。

    调用前新表已由 SCHEMA 建好，且已确认旧 approvals 表存在。
    """

    legacy_rows = db.execute(
        "SELECT lot_id,reviewer,decision,reason,created_at FROM approvals ORDER BY rowid"
    ).fetchall()
    for lot_id, in db.execute("SELECT lot_id FROM chip_lots").fetchall():
        db.execute(
            "INSERT OR IGNORE INTO lot_revision(lot_id,revision) VALUES(?,0)", (lot_id,)
        )
    # 旧库中每个批次至多保留一条审批（旧主键 lot_id+reviewer 下同一人会覆盖，
    # 不同人则各自成行）。按原写入顺序重放为决定链起点，分析版本标记为遗留版本。
    last_status: dict[str, str] = {}
    if legacy_rows:
        per_lot_counter: dict[str, int] = {}
        for lot_id, reviewer, decision, reason, created_at in legacy_rows:
            if decision not in _DECISION_STATUS:
                continue
            seq = per_lot_counter.get(lot_id, 0) + 1
            per_lot_counter[lot_id] = seq
            db.execute(
                "INSERT OR IGNORE INTO analysis_versions(analysis_id,lot_id,lot_revision,"
                "measurement_count,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    f"legacy-{lot_id}", lot_id, 0, 0, f"legacy-{lot_id}",
                    json.dumps({"legacy": True}, sort_keys=True), reviewer, created_at,
                ),
            )
            decision_id = f"legacy-{lot_id}-{seq}"
            db.execute(
                "INSERT OR IGNORE INTO approval_records(decision_id,lot_id,seq,decision,reason,"
                "analysis_id,review_id,decided_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (decision_id, lot_id, seq, decision, reason, f"legacy-{lot_id}", None, reviewer, created_at),
            )
            db.execute(
                "UPDATE lot_revision SET revision=MAX(revision,?),current_analysis_id=?,"
                "current_decision_seq=? WHERE lot_id=?",
                (seq, f"legacy-{lot_id}", seq, lot_id),
            )
            last_status[lot_id] = _DECISION_STATUS[decision]
        for lot_id, status in last_status.items():
            db.execute("UPDATE chip_lots SET status=? WHERE lot_id=?", (status, lot_id))


def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    # 必须先识别旧库：SCHEMA 会以 IF NOT EXISTS 建好新表，识别放在建表之后会永远漏判。
    legacy = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='approvals'"
    ).fetchone()
    db.executescript(SCHEMA)
    if legacy:
        _migrate(db)
    else:
        for lot_id, in db.execute("SELECT lot_id FROM chip_lots").fetchall():
            db.execute(
                "INSERT OR IGNORE INTO lot_revision(lot_id,revision) VALUES(?,0)", (lot_id,)
            )
    db.commit()
    return db


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
    db.execute(
        "INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)",
        (lot_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()),
    )
