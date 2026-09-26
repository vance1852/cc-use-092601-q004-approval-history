"""协调认证、批次、测量、分析版本与不可变审批决定链的应用服务。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import connect, event, transaction, utcnow


_DECISION_STATUS = {"release": "released", "hold": "hold", "reject": "rejected"}
_TERMINAL = {"released", "rejected"}


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "photon-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    # ---------- 基础批次与测量（既有流程保持不变） ----------

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, wafer_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if wafer_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)",
                (lot_id, product, process_rev, wafer_count, "engineering", actor.user_id, now, now),
            )
            self.db.execute(
                "INSERT INTO lot_revision(lot_id,revision) VALUES(?,0)", (lot_id,)
            )
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def _lot_row(self, lot_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return row

    def _version_row(self, lot_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM lot_revision WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:  # 兼容迁移前建立连接但未迁移的极端情况
            raise NotFound(lot_id)
        return row

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        lot = dict(self._lot_row(lot_id))
        version = self._version_row(lot_id)
        lot["revision"] = version["revision"]
        lot["current_analysis_id"] = version["current_analysis_id"]
        current = self._decision_record(lot_id, version["current_decision_seq"])
        lot["current_decision"] = None if current is None else self._decision_view(current)
        lot["decision_chain_length"] = self.db.execute(
            "SELECT count(*) FROM approval_records WHERE lot_id=?", (lot_id,)
        ).fetchone()[0]
        return lot

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute(
                "INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)",
                (measurement_id, lot_id, float(wavelength_nm), float(response), float(noise),
                 instrument, actor.user_id, utcnow()),
            )
            event(self.db, lot_id, "measurement", actor.user_id,
                  {"measurement_id": measurement_id, "wavelength_nm": wavelength_nm})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    # ---------- 分析版本：追加、内容寻址、带批次版本 ----------

    def _compute_analysis(self, token: str, lot_id: str) -> tuple[dict, tuple[sqlite3.Row, ...], str]:
        rows = self.db.execute(
            "SELECT wavelength_nm,response FROM measurements WHERE lot_id=? ORDER BY wavelength_nm,measurement_id",
            (lot_id,),
        ).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        lot = self.get_lot(token, lot_id)
        summary = summarize_spectrum([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(lot["wafer_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        result = {"lot_id": lot_id, "spectrum": summary.__dict__, "yield": rates, "response_ci": ci}
        input_digest = content_digest(
            [{"wavelength_nm": r[0], "response": r[1]} for r in rows]
        )
        return result, rows, input_digest

    def analyze(self, token: str, lot_id: str) -> dict:
        actor = self.auth.require(token, "analyze")
        self._lot_row(lot_id)
        result, rows, input_digest = self._compute_analysis(token, lot_id)
        with transaction(self.db):
            version = self._version_row(lot_id)
            existing = self.db.execute(
                "SELECT analysis_id,lot_revision,result_json,created_by,created_at,measurement_count "
                "FROM analysis_versions WHERE lot_id=? AND input_sha256=?",
                (lot_id, input_digest),
            ).fetchone()
            if existing is not None:
                analysis_id = existing["analysis_id"]
                analysis_revision = existing["lot_revision"]
                created = False
            else:
                analysis_id = uuid.uuid4().hex
                analysis_revision = version["revision"] + 1
                self.db.execute(
                    "INSERT INTO analysis_versions(analysis_id,lot_id,lot_revision,measurement_count,"
                    "input_sha256,result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (analysis_id, lot_id, analysis_revision, len(rows), input_digest,
                     canonical_json(result), actor.user_id, utcnow()),
                )
                self.db.execute(
                    "UPDATE lot_revision SET revision=?,current_analysis_id=? WHERE lot_id=?",
                    (analysis_revision, analysis_id, lot_id),
                )
                created = True
            event(self.db, lot_id, "analysis", actor.user_id,
                  {"analysis_id": analysis_id, "input_sha256": input_digest, "created": created})
        return {
            "analysis_id": analysis_id,
            "lot_id": lot_id,
            "lot_revision": analysis_revision,
            "input_sha256": input_digest,
            "created": created,
            **result,
        }

    def get_analysis(self, token: str, lot_id: str, analysis_id: str) -> dict:
        self.auth.require(token, "read")
        self._lot_row(lot_id)
        row = self.db.execute(
            "SELECT * FROM analysis_versions WHERE lot_id=? AND analysis_id=?", (lot_id, analysis_id)
        ).fetchone()
        if row is None:
            raise NotFound("分析版本不存在")
        return {
            "analysis_id": row["analysis_id"],
            "lot_id": row["lot_id"],
            "lot_revision": row["lot_revision"],
            "measurement_count": row["measurement_count"],
            "input_sha256": row["input_sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "result": json.loads(row["result_json"]),
        }

    def list_analyses(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        self._lot_row(lot_id)
        rows = self.db.execute(
            "SELECT analysis_id,lot_revision,measurement_count,input_sha256,created_by,created_at "
            "FROM analysis_versions WHERE lot_id=? ORDER BY lot_revision, analysis_id",
            (lot_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 显式复议 ----------

    def request_review(
        self, token: str, lot_id: str, reason: str, expected_revision: int | None = None
    ) -> dict:
        """对已有有效决定的批次显式发起复议；首次决定前不得发起。"""

        actor = self.auth.require(token, "approve")
        if not reason or not reason.strip():
            raise ValidationFailed("复议原因不能为空")
        with transaction(self.db):
            version = self._version_row(lot_id)
            lot = self._lot_row(lot_id)
            if expected_revision is not None and version["revision"] != expected_revision:
                raise Conflict("批次版本已变化，请基于最新版本发起复议")
            if version["current_decision_seq"] is None:
                raise InvalidState("批次尚未作出首次决定，无需发起复议")
            if self._has_open_review(lot_id):
                raise Conflict("批次已存在开放中的复议，请先基于该复议作出决定")
            cursor = self.db.execute(
                "INSERT INTO review_requests(lot_id,requested_by,reason,status,requested_at) "
                "VALUES(?,?,?, 'open', ?)",
                (lot_id, actor.user_id, reason.strip(), utcnow()),
            )
            review_id = cursor.lastrowid
            new_revision = version["revision"] + 1
            self.db.execute(
                "UPDATE lot_revision SET revision=?,current_review_id=? WHERE lot_id=?",
                (new_revision, review_id, lot_id),
            )
            self.db.execute("UPDATE chip_lots SET status='in_review',updated_at=? WHERE lot_id=?",
                            (utcnow(), lot_id))
            event(self.db, lot_id, "review.requested", actor.user_id,
                  {"review_id": review_id, "reason": reason.strip(),
                   "from_revision": version["revision"]})
        return self.get_review(token, lot_id, review_id)

    def get_review(self, token: str, lot_id: str, review_id: int) -> dict:
        self.auth.require(token, "read")
        self._lot_row(lot_id)
        row = self.db.execute(
            "SELECT * FROM review_requests WHERE lot_id=? AND review_id=?", (lot_id, review_id)
        ).fetchone()
        if row is None:
            raise NotFound("复议不存在")
        return dict(row)

    def list_reviews(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        self._lot_row(lot_id)
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM review_requests WHERE lot_id=? ORDER BY review_id", (lot_id,)
        ).fetchall()]

    def _has_open_review(self, lot_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM review_requests WHERE lot_id=? AND status='open' LIMIT 1", (lot_id,)
        ).fetchone() is not None

    # ---------- 不可变审批决定链 ----------

    def _decision_record(self, lot_id: str, seq: int | None) -> sqlite3.Row | None:
        if seq is None:
            return None
        return self.db.execute(
            "SELECT * FROM approval_records WHERE lot_id=? AND seq=?", (lot_id, seq)
        ).fetchone()

    @staticmethod
    def _decision_view(row: sqlite3.Row) -> dict[str, Any]:
        view = dict(row)
        view["effective_status"] = _DECISION_STATUS[row["decision"]]
        return view

    def _idempotent(self, lot_id: str, key: str, digest: str) -> dict | None:
        row = self.db.execute(
            "SELECT request_sha256,response_json FROM approval_idempotency WHERE scope=? AND key=?",
            (f"approval:{lot_id}", key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def approve(
        self,
        token: str,
        lot_id: str,
        decision: str,
        reason: str,
        analysis_id: str | None = None,
        review_id: int | None = None,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        """在批次决定链上追加一条不可变决定。

        - 首次决定：引用当前分析版本，无需复议；
        - 后续决定：必须显式引用一条开放中的复议、新的分析版本，且由不同于上一决定的
          授权人员作出；
        - 重复请求（同幂等键同载荷）返回原结果，同键不同载荷冲突。
        """

        actor = self.auth.require(token, "approve")
        if decision not in _DECISION_STATUS or not reason or not reason.strip():
            raise ValidationFailed("decision 和 reason 必填，且 decision 必须为 release/hold/reject")
        request_digest = content_digest([{
            "decision": decision,
            "reason": reason.strip(),
            "analysis_id": analysis_id,
            "review_id": review_id,
        }])
        # 事务外的快速命中路径；事务内会再次校验以防并发重复请求。
        if idempotency_key:
            cached = self._idempotent(lot_id, idempotency_key, request_digest)
            if cached is not None:
                return {**cached, "replayed": True}
        with transaction(self.db):
            lot = self._lot_row(lot_id)
            version = self._version_row(lot_id)
            if idempotency_key:
                cached = self._idempotent(lot_id, idempotency_key, request_digest)
                if cached is not None:
                    return {**cached, "replayed": True}
            if expected_revision is not None and version["revision"] != expected_revision:
                raise Conflict("批次版本已变化，请刷新后重试")
            current_analysis = version["current_analysis_id"]
            if current_analysis is None:
                raise InvalidState("批次尚无分析版本，不能作出审批决定")
            cited_analysis = analysis_id or current_analysis
            analysis_row = self.db.execute(
                "SELECT analysis_id FROM analysis_versions WHERE lot_id=? AND analysis_id=?",
                (lot_id, cited_analysis),
            ).fetchone()
            if analysis_row is None:
                raise NotFound("引用的分析版本不存在")
            if cited_analysis != current_analysis:
                raise InvalidState("决定必须引用批次当前分析版本，请先运行新分析并发起复议")
            previous = self._decision_record(lot_id, version["current_decision_seq"])
            bound_review_id = None
            if previous is None:
                if review_id is not None:
                    raise InvalidState("首次决定不得引用复议")
                if lot["status"] != "engineering":
                    raise InvalidState("当前批次状态不允许首次决定")
            else:
                # 后续决定必须显式发起并引用开放中的复议，批次因此处于 in_review。
                if lot["status"] != "in_review":
                    raise InvalidState("批次不在复议中，必须先显式发起复议才能作出新决定")
                review_row = self.db.execute(
                    "SELECT * FROM review_requests WHERE lot_id=? AND status='open' ORDER BY review_id",
                    (lot_id,),
                ).fetchone()
                if review_row is None:
                    raise InvalidState("必须先显式发起复议，才能作出新的决定")
                if review_id is not None and review_id != review_row["review_id"]:
                    raise InvalidState("引用的复议不是批次当前开放的复议")
                bound_review_id = review_row["review_id"]
                # 必须引用新的分析版本。
                if cited_analysis == previous["analysis_id"]:
                    raise InvalidState("复议决定必须引用新的分析版本，不能复用上次决定的分析")
                # 必须由不同授权人员作出。
                if previous["decided_by"] == actor.user_id:
                    raise Forbidden("复议决定必须由不同于上一决定人的授权人员作出")
            seq = (version["current_decision_seq"] or 0) + 1
            decision_id = uuid.uuid4().hex
            now = utcnow()
            self.db.execute(
                "INSERT INTO approval_records(decision_id,lot_id,seq,decision,reason,analysis_id,"
                "review_id,decided_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (decision_id, lot_id, seq, decision, reason.strip(), cited_analysis,
                 bound_review_id, actor.user_id, now),
            )
            new_revision = version["revision"] + 1
            new_status = _DECISION_STATUS[decision]
            self.db.execute(
                "UPDATE lot_revision SET revision=?,current_decision_seq=?,"
                "current_review_id=NULL WHERE lot_id=?",
                (new_revision, seq, lot_id),
            )
            self.db.execute(
                "UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?",
                (new_status, now, lot_id),
            )
            if bound_review_id is not None:
                self.db.execute(
                    "UPDATE review_requests SET status='consumed',consumed_by_seq=? "
                    "WHERE review_id=? AND status='open'",
                    (seq, bound_review_id),
                )
            payload = {
                "decision_id": decision_id,
                "seq": seq,
                "decision": decision,
                "analysis_id": cited_analysis,
                "review_id": bound_review_id,
            }
            event(self.db, lot_id, "approval", actor.user_id, payload)
            response = {
                "decision_id": decision_id,
                "lot_id": lot_id,
                "seq": seq,
                "decision": decision,
                "effective_status": new_status,
                "analysis_id": cited_analysis,
                "review_id": bound_review_id,
                "decided_by": actor.user_id,
                "created_at": now,
                "revision": new_revision,
                "replayed": False,
            }
            if idempotency_key:
                self.db.execute(
                    "INSERT INTO approval_idempotency(scope,key,request_sha256,response_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (f"approval:{lot_id}", idempotency_key, request_digest,
                     canonical_json(response), now),
                )
        return response

    def decision_chain(self, token: str, lot_id: str) -> list[dict]:
        """返回批次完整的不可变决定链，按先后顺序排列。"""

        self.auth.require(token, "read")
        self._lot_row(lot_id)
        rows = self.db.execute(
            "SELECT * FROM approval_records WHERE lot_id=? ORDER BY seq", (lot_id,)
        ).fetchall()
        return [self._decision_view(r) for r in rows]

    def current_decision(self, token: str, lot_id: str) -> dict | None:
        """返回批次当前有效决定；尚未决定时为 None。"""

        self.auth.require(token, "read")
        version = self._version_row(lot_id)
        row = self._decision_record(lot_id, version["current_decision_seq"])
        return None if row is None else self._decision_view(row)

    def decision_report(self, token: str, lot_id: str) -> dict:
        """同时给出当前有效决定与完整决定链，并把每条决定关联到所引用的分析版本。"""

        self.auth.require(token, "read")
        lot = self.get_lot(token, lot_id)
        chain = self.decision_chain(token, lot_id)
        reviews = self.list_reviews(token, lot_id)
        analyses = {a["analysis_id"]: a for a in self.list_analyses(token, lot_id)}
        for item in chain:
            item["analysis"] = analyses.get(item["analysis_id"])
        return {
            "lot_id": lot_id,
            "revision": lot["revision"],
            "status": lot["status"],
            "current_analysis_id": lot["current_analysis_id"],
            "current_decision": self.current_decision(token, lot_id),
            "decision_chain": chain,
            "review_requests": reviews,
        }

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)
        ).fetchall()]
