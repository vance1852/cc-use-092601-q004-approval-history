"""协调认证、批次、测试、分析版本与不可变放行门禁的应用服务。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .analytics import ALGORITHM_VERSION, confidence_interval, summarize_spectrum, yield_rate
from .auth import PERMISSIONS, Auth
from .errors import Conflict, InvalidState, NotFound
from .jsonio import canonical_json, content_digest
from .storage import connect, event, transaction, utcnow


_DECISION_STATUS = {"release": "released", "hold": "hold", "reject": "rejected"}
# 处于哪些批次状态时可以显式发起复议。
_REVIEWABLE_STATUS = {"hold", "rejected"}
# 复议请求开放期间批次处于 in_review，此时允许凭开放请求作出复议决定。
_REVIEW_DECISION_STATUS = _REVIEWABLE_STATUS | {"in_review"}


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "photon-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    # ------------------------------------------------------------------ 批次

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, wafer_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if wafer_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (lot_id, product, process_rev, wafer_count, "engineering", actor.user_id, now, now, 1, None),
            )
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        return self._lot_row(lot_id)

    def _lot_row(self, lot_id: str) -> dict:
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def _locked_lot(self, lot_id: str) -> dict:
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def _check_revision(self, lot: dict, expected_revision: int | None) -> None:
        if expected_revision is not None and int(expected_revision) != lot["revision"]:
            raise Conflict(f"批次版本已变化：期望 {expected_revision}，当前 {lot['revision']}")

    # -------------------------------------------------------------- 测量/分析

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            lot = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if not lot:
                raise KeyError(lot_id)
            if lot["status"] not in {"engineering", "hold", "rejected"}:
                raise InvalidState(f"批次当前状态 {lot['status']} 不能补充测量")
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(wavelength_nm), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "wavelength_nm": wavelength_nm})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def submit_for_review(self, token: str, lot_id: str, expected_revision: int | None = None) -> dict:
        """完成测量与分析后，将批次显式提交质量评审。"""

        actor = self.auth.require(token, "submit")
        with transaction(self.db):
            lot = self._locked_lot(lot_id)
            self._check_revision(lot, expected_revision)
            if lot["status"] != "engineering":
                raise InvalidState(f"批次当前状态 {lot['status']} 不能提交评审")
            now = utcnow()
            self.db.execute(
                "UPDATE chip_lots SET status='pending_review',updated_at=?,revision=? WHERE lot_id=?",
                (now, lot["revision"] + 1, lot_id),
            )
            event(self.db, lot_id, "review.submitted", actor.user_id, {"revision": lot["revision"] + 1})
        return self.get_lot(token, lot_id)

    def _measurement_snapshot(self, lot_id: str) -> list[dict[str, float]]:
        rows = self.db.execute(
            "SELECT wavelength_nm,response,noise FROM measurements WHERE lot_id=? ORDER BY wavelength_nm,measurement_id",
            (lot_id,),
        ).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        return [{"wavelength_nm": r[0], "response": r[1], "noise": r[2]} for r in rows]

    def analyze(self, token: str, lot_id: str) -> dict:
        """对当前全部测量生成一个不可变分析版本；相同测量集合复用既有版本。"""

        actor = self.auth.require(token, "analyze")
        lot = self._lot_row(lot_id)
        snapshot = self._measurement_snapshot(lot_id)
        input_digest = content_digest(snapshot)
        with transaction(self.db):
            existing = self.db.execute(
                "SELECT analysis_id,seq,result_json FROM analyses WHERE lot_id=? AND input_sha256=?",
                (lot_id, input_digest),
            ).fetchone()
            if existing:
                analysis_id = existing["analysis_id"]
                seq = existing["seq"]
                result = json.loads(existing["result_json"])
                reused = True
            else:
                rows = [(point["wavelength_nm"], point["response"]) for point in snapshot]
                summary = summarize_spectrum([r[0] for r in rows], [r[1] for r in rows])
                rates = yield_rate(lot["wafer_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
                ci = confidence_interval([r[1] for r in rows])
                result = {
                    "spectrum": summary.__dict__,
                    "yield": rates,
                    "response_ci": ci,
                    "measurement_count": len(rows),
                }
                seq = self.db.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM analyses WHERE lot_id=?", (lot_id,)
                ).fetchone()[0]
                analysis_id = uuid.uuid4().hex
                self.db.execute(
                    "INSERT INTO analyses VALUES(?,?,?,?,?,?,?)",
                    (analysis_id, lot_id, seq, input_digest, canonical_json(result), actor.user_id, utcnow()),
                )
                reused = False
                event(self.db, lot_id, "analysis", actor.user_id, {
                    "analysis_id": analysis_id, "seq": seq, "input_sha256": input_digest,
                    "algorithm_version": ALGORITHM_VERSION,
                })
        return {
            "lot_id": lot_id,
            "analysis_id": analysis_id,
            "seq": seq,
            "input_sha256": input_digest,
            "algorithm_version": ALGORITHM_VERSION,
            "reused": reused,
            **result,
        }

    # -------------------------------------------------------------- 幂等支持

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT request_sha256,response_json FROM idempotency_records WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一请求编号对应了不同的决定载荷")
        return json.loads(row["response_json"])

    def _store_idempotent(self, scope: str, key: str, request_digest: str, response: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO idempotency_records VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), utcnow()),
        )

    # ------------------------------------------------------------------ 决定

    def decide(
        self,
        token: str,
        lot_id: str,
        decision: str,
        reason: str,
        analysis_id: str,
        idempotency_key: str,
        expected_revision: int | None = None,
        review_request_id: str | None = None,
    ) -> dict[str, Any]:
        """记录一次不可变准入决定（初次决定或复议决定）。

        初次决定只能在批次待审时作出；复议决定必须显式引用一笔开放的复议
        请求、一个新的分析版本，并且由上一决定人之外的授权人员作出。
        """

        actor = self.auth.require(token, "approve")
        if decision not in _DECISION_STATUS or not reason.strip():
            raise ValueError("decision and reason are required")
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        request_digest = content_digest([{
            "decision": decision,
            "reason": reason,
            "analysis_id": analysis_id,
            "expected_revision": expected_revision,
            "review_request_id": review_request_id,
        }])
        scope = f"decision:{lot_id}"
        cached = self._idempotent_response(scope, idempotency_key.strip(), request_digest)
        if cached is not None:
            return cached

        with transaction(self.db):
            lot = self._locked_lot(lot_id)
            self._check_revision(lot, expected_revision)
            analysis = self.db.execute(
                "SELECT analysis_id,seq FROM analyses WHERE analysis_id=? AND lot_id=?",
                (analysis_id, lot_id),
            ).fetchone()
            if analysis is None:
                raise NotFound("分析版本不存在或不属于该批次")
            latest = self.db.execute(
                "SELECT MAX(seq) AS seq FROM analyses WHERE lot_id=?", (lot_id,)
            ).fetchone()["seq"]
            if analysis["seq"] != latest:
                raise InvalidState("只能依据批次当前最新分析版本作出决定")

            prev = self.db.execute(
                "SELECT * FROM approval_decisions WHERE lot_id=? ORDER BY seq DESC LIMIT 1",
                (lot_id,),
            ).fetchone()

            request_row = None
            if prev is None:
                # 初次决定
                if lot["status"] != "pending_review":
                    raise InvalidState(f"批次当前状态 {lot['status']} 不接受初次决定")
                if review_request_id:
                    raise InvalidState("初次决定不能引用复议请求")
                kind = "initial"
            else:
                # 复议决定：必须显式发起复议并引用开放请求
                if lot["status"] not in _REVIEW_DECISION_STATUS:
                    raise InvalidState(f"批次当前状态 {lot['status']} 不接受新的决定")
                if not review_request_id:
                    raise InvalidState("需要重新评审时必须显式发起复议并引用复议请求")
                request_row = self.db.execute(
                    "SELECT * FROM review_requests WHERE request_id=? AND lot_id=?",
                    (review_request_id, lot_id),
                ).fetchone()
                if request_row is None:
                    raise NotFound("复议请求不存在或不属于该批次")
                if request_row["status"] != "open":
                    raise InvalidState("复议请求已经处理")
                if prev["reviewer"] == actor.user_id:
                    raise PermissionError("复议决定必须由上一决定人之外的授权人员作出")
                prev_analysis = self.db.execute(
                    "SELECT seq FROM analyses WHERE analysis_id=?", (prev["analysis_id"],)
                ).fetchone()
                if analysis["seq"] <= prev_analysis["seq"]:
                    raise InvalidState("复议必须引用比上一决定更新的分析版本")
                kind = "review"

            decision_id = uuid.uuid4().hex
            seq = (prev["seq"] + 1) if prev is not None else 1
            now = utcnow()
            new_status = _DECISION_STATUS[decision]
            self.db.execute(
                "INSERT INTO approval_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id, lot_id, seq, kind, analysis_id, decision, reason.strip(),
                    actor.user_id, None if prev is None else prev["decision_id"],
                    review_request_id, lot["revision"] + 1, now,
                ),
            )
            self.db.execute(
                "UPDATE chip_lots SET status=?,updated_at=?,revision=?,current_decision_id=? WHERE lot_id=?",
                (new_status, now, lot["revision"] + 1, decision_id, lot_id),
            )
            if request_row is not None:
                self.db.execute(
                    "UPDATE review_requests SET status='closed',decision_id=? WHERE request_id=? AND status='open'",
                    (decision_id, review_request_id),
                )
            response = {
                "decision_id": decision_id,
                "lot_id": lot_id,
                "seq": seq,
                "kind": kind,
                "decision": decision,
                "status": new_status,
                "reason": reason.strip(),
                "reviewer": actor.user_id,
                "analysis_id": analysis_id,
                "analysis_seq": analysis["seq"],
                "prev_decision_id": None if prev is None else prev["decision_id"],
                "review_request_id": review_request_id,
                "lot_revision": lot["revision"] + 1,
                "created_at": now,
            }
            self._store_idempotent(scope, idempotency_key.strip(), request_digest, response)
            event(self.db, lot_id, f"approval.{kind}", actor.user_id, {
                "decision_id": decision_id,
                "seq": seq,
                "decision": decision,
                "analysis_id": analysis_id,
                "review_request_id": review_request_id,
            })
        return response

    def request_review(
        self,
        token: str,
        lot_id: str,
        reason: str,
        idempotency_key: str,
        expected_revision: int | None = None,
    ) -> dict:
        """对暂缓或拒收的批次显式发起复议；已放行批次不可复议。"""

        user = self.auth.current(token)
        perms = PERMISSIONS[user.role]
        if "approve" not in perms and "submit" not in perms:
            raise PermissionError("permission denied")
        if not reason.strip() or not idempotency_key.strip():
            raise ValueError("reason and idempotency_key are required")
        request_digest = content_digest([{"reason": reason, "expected_revision": expected_revision}])
        scope = f"review_request:{lot_id}"
        cached = self._idempotent_response(scope, idempotency_key.strip(), request_digest)
        if cached is not None:
            return cached

        with transaction(self.db):
            lot = self._locked_lot(lot_id)
            self._check_revision(lot, expected_revision)
            if lot["status"] not in _REVIEWABLE_STATUS:
                raise InvalidState(f"批次当前状态 {lot['status']} 不能发起复议")
            open_request = self.db.execute(
                "SELECT request_id FROM review_requests WHERE lot_id=? AND status='open'", (lot_id,)
            ).fetchone()
            if open_request is not None:
                raise Conflict("该批次已有一笔待处理的复议请求")
            request_id = uuid.uuid4().hex
            seq_row = self.db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM review_requests WHERE lot_id=?", (lot_id,)
            ).fetchone()
            seq = seq_row[0]
            now = utcnow()
            self.db.execute(
                "INSERT INTO review_requests VALUES(?,?,?,?,?,?,?,?)",
                (request_id, lot_id, seq, reason.strip(), user.user_id, "open", None, now),
            )
            self.db.execute(
                "UPDATE chip_lots SET status='in_review',updated_at=?,revision=? WHERE lot_id=?",
                (now, lot["revision"] + 1, lot_id),
            )
            response = {
                "request_id": request_id,
                "lot_id": lot_id,
                "seq": seq,
                "reason": reason.strip(),
                "requested_by": user.user_id,
                "status": "open",
                "lot_revision": lot["revision"] + 1,
                "created_at": now,
            }
            self._store_idempotent(scope, idempotency_key.strip(), request_digest, response)
            event(self.db, lot_id, "review.requested", user.user_id, {
                "request_id": request_id, "seq": seq, "reason": reason.strip(),
            })
        return response

    # -------------------------------------------------------------- 查询/报告

    def decision_chain(self, token: str, lot_id: str) -> list[dict]:
        """返回批次完整的不可变决定链，按决定先后排序。"""

        self.auth.require(token, "read")
        rows = self.db.execute(
            "SELECT * FROM approval_decisions WHERE lot_id=? ORDER BY seq", (lot_id,)
        ).fetchall()
        if not rows and not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
            raise KeyError(lot_id)
        return [dict(r) for r in rows]

    def current_decision(self, token: str, lot_id: str) -> dict | None:
        """返回批次当前有效决定；尚未决定时为 None。"""

        self.auth.require(token, "read")
        lot = self._lot_row(lot_id)
        if not lot["current_decision_id"]:
            return None
        row = self.db.execute(
            "SELECT * FROM approval_decisions WHERE decision_id=?", (lot["current_decision_id"],)
        ).fetchone()
        return dict(row) if row else None

    def report(self, token: str, lot_id: str) -> dict:
        """同时给出当前有效决定与完整决定链，以及分析版本和复议记录。"""

        self.auth.require(token, "read")
        lot = self._lot_row(lot_id)
        analyses = [
            dict(r) for r in self.db.execute(
                "SELECT analysis_id,seq,input_sha256,created_by,created_at FROM analyses "
                "WHERE lot_id=? ORDER BY seq", (lot_id,)
            ).fetchall()
        ]
        review_requests = [
            dict(r) for r in self.db.execute(
                "SELECT request_id,seq,reason,requested_by,status,decision_id,created_at "
                "FROM review_requests WHERE lot_id=? ORDER BY seq", (lot_id,)
            ).fetchall()
        ]
        chain = [dict(r) for r in self.db.execute(
            "SELECT * FROM approval_decisions WHERE lot_id=? ORDER BY seq", (lot_id,)
        ).fetchall()]
        current = None
        if lot["current_decision_id"]:
            current = next((d for d in chain if d["decision_id"] == lot["current_decision_id"]), None)
        return {
            "lot": lot,
            "analyses": analyses,
            "review_requests": review_requests,
            "current_decision": current,
            "decision_chain": chain,
        }

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
