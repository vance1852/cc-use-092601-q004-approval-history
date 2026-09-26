from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from silicon_qualification.api import Handler
from silicon_qualification.errors import Conflict, Forbidden, InvalidState
from silicon_qualification.service import PhotonService


def _seed(service: PhotonService) -> dict[str, str]:
    service.bootstrap_admin()
    service.auth.create_user("eng", "eng-pass-123", "engineer")
    service.auth.create_user("op", "op-pass-1234", "operator")
    service.auth.create_user("qa", "qa-pass-1234", "quality")
    service.auth.create_user("qb", "qb-pass-1234", "quality")
    tokens = {
        "admin": service.auth.login("admin", "photon-admin"),
        "eng": service.auth.login("eng", "eng-pass-123"),
        "op": service.auth.login("op", "op-pass-1234"),
        "qa": service.auth.login("qa", "qa-pass-1234"),
        "qb": service.auth.login("qb", "qb-pass-1234"),
    }
    service.create_lot(tokens["admin"], "LOT-1", "sensor", "P1", 10)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(tokens["admin"], "LOT-1", wavelength, response, .01, "spec-1")
    return tokens


class ApprovalChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService(":memory:")
        self.t = _seed(self.service)

    def _analysis(self) -> dict:
        return self.service.analyze(self.t["eng"], "LOT-1")

    def test_decision_is_immutable_and_chain_is_append_only(self) -> None:
        analysis = self._analysis()
        hold = self.service.approve(self.t["qa"], "LOT-1", "hold", "need more data",
                                    analysis_id=analysis["analysis_id"])
        # 旧缺陷：再次提交会物理覆盖原决定。现在未走复议直接再决定必须被拒绝。
        with self.assertRaises(InvalidState):
            self.service.approve(self.t["qb"], "LOT-1", "release", "not via review",
                                 analysis_id=analysis["analysis_id"])
        chain = self.service.decision_chain(self.t["qa"], "LOT-1")
        self.assertEqual([c["decision"] for c in chain], ["hold"])
        self.assertEqual(chain[0]["decision_id"], hold["decision_id"])
        self.assertEqual(chain[0]["seq"], 1)
        self.assertEqual(chain[0]["effective_status"], "hold")
        lot = self.service.get_lot(self.t["qa"], "LOT-1")
        self.assertEqual(lot["status"], "hold")
        self.assertEqual(lot["current_decision"]["decision_id"], hold["decision_id"])

    def test_review_requires_new_analysis_different_reviewer_and_open_request(self) -> None:
        first = self._analysis()
        self.service.approve(self.t["qa"], "LOT-1", "hold", "need more data",
                             analysis_id=first["analysis_id"])
        # 补充测量但还没有新分析版本时不能放行。
        self.service.add_measurement(self.t["admin"], "LOT-1", 600, .95, .01, "spec-2")
        review = self.service.request_review(self.t["qa"], "LOT-1", "supplement captured")
        self.assertEqual(review["status"], "open")
        with self.assertRaises(InvalidState):
            self.service.approve(self.t["qa"], "LOT-1", "release", "stale analysis",
                                 analysis_id=first["analysis_id"], review_id=review["review_id"])
        second = self.service.analyze(self.t["eng"], "LOT-1")
        self.assertNotEqual(second["analysis_id"], first["analysis_id"])
        # 必须由不同于上一决定人的授权人员作出。
        with self.assertRaises(Forbidden):
            self.service.approve(self.t["qa"], "LOT-1", "release", "same reviewer",
                                 analysis_id=second["analysis_id"], review_id=review["review_id"])
        release = self.service.approve(self.t["qb"], "LOT-1", "release", "cleared",
                                       analysis_id=second["analysis_id"],
                                       review_id=review["review_id"])
        chain = self.service.decision_chain(self.t["qb"], "LOT-1")
        self.assertEqual([c["decision"] for c in chain], ["hold", "release"])
        self.assertEqual([c["decided_by"] for c in chain], ["qa", "qb"])
        self.assertEqual(chain[0]["analysis_id"], first["analysis_id"])
        self.assertEqual(chain[1]["analysis_id"], second["analysis_id"])
        self.assertEqual(chain[1]["review_id"], review["review_id"])
        self.assertEqual(release["revision"], 5)
        report = self.service.decision_report(self.t["qb"], "LOT-1")
        self.assertEqual(report["current_decision"]["decision_id"], release["decision_id"])
        self.assertEqual(len(report["decision_chain"]), 2)
        self.assertEqual(report["review_requests"][0]["status"], "consumed")
        self.assertEqual(report["status"], "released")

    def test_review_must_be_explicitly_requested(self) -> None:
        first = self._analysis()
        self.service.approve(self.t["qa"], "LOT-1", "hold", "need more data",
                             analysis_id=first["analysis_id"])
        self.service.add_measurement(self.t["admin"], "LOT-1", 600, .95, .01, "spec-2")
        second = self.service.analyze(self.t["eng"], "LOT-1")
        with self.assertRaises(InvalidState):
            self.service.approve(self.t["qb"], "LOT-1", "release", "no review request",
                                 analysis_id=second["analysis_id"])
        with self.assertRaises(Exception):
            self.service.request_review(self.t["qa"], "LOT-999", "missing lot")

    def test_review_before_first_decision_or_while_open_is_rejected(self) -> None:
        self._analysis()
        with self.assertRaises(InvalidState):
            self.service.request_review(self.t["qa"], "LOT-1", "too early")
        first = self.service.list_analyses(self.t["qa"], "LOT-1")[0]
        self.service.approve(self.t["qa"], "LOT-1", "hold", "hold now",
                             analysis_id=first["analysis_id"])
        self.service.request_review(self.t["qa"], "LOT-1", "first review")
        with self.assertRaises(Conflict):
            self.service.request_review(self.t["qa"], "LOT-1", "duplicate open review")

    def test_decision_requires_analysis_and_permission(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.approve(self.t["qa"], "LOT-1", "release", "no analysis yet")
        with self.assertRaises(PermissionError):
            self.service.approve(self.t["op"], "LOT-1", "release", "operator cannot approve")
        analysis = self._analysis()
        with self.assertRaises(PermissionError):
            self.service.approve(self.t["eng"], "LOT-1", "hold", "engineer cannot approve")
        from silicon_qualification.errors import NotFound

        with self.assertRaises(NotFound):
            self.service.approve(self.t["qa"], "LOT-1", "release", "missing analysis",
                                 analysis_id="does-not-exist")

    def test_revision_guards_concurrent_decisions(self) -> None:
        analysis = self._analysis()
        lot = self.service.get_lot(self.t["qa"], "LOT-1")
        stale = lot["revision"]
        with self.assertRaises(Conflict):
            self.service.approve(self.t["qa"], "LOT-1", "hold", "stale revision",
                                 analysis_id=analysis["analysis_id"],
                                 expected_revision=stale + 99)
        result = self.service.approve(self.t["qa"], "LOT-1", "hold", "current revision",
                                      analysis_id=analysis["analysis_id"],
                                      expected_revision=stale)
        self.assertEqual(result["revision"], stale + 1)
        with self.assertRaises(Conflict):
            self.service.request_review(self.t["qa"], "LOT-1", "stale",
                                        expected_revision=stale)

    def test_idempotent_replay_returns_original_and_payload_mismatch_conflicts(self) -> None:
        analysis = self._analysis()
        first = self.service.approve(
            self.t["qa"], "LOT-1", "hold", "reason one",
            analysis_id=analysis["analysis_id"], idempotency_key="key-1")
        replay = self.service.approve(
            self.t["qa"], "LOT-1", "hold", "reason one",
            analysis_id=analysis["analysis_id"], idempotency_key="key-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["decision_id"], first["decision_id"])
        self.assertEqual(len(self.service.decision_chain(self.t["qa"], "LOT-1")), 1)
        with self.assertRaises(Conflict):
            self.service.approve(
                self.t["qa"], "LOT-1", "hold", "different reason",
                analysis_id=analysis["analysis_id"], idempotency_key="key-1")
        self.assertEqual(len(self.service.decision_chain(self.t["qa"], "LOT-1")), 1)

    def test_identical_measurements_reuse_analysis_version(self) -> None:
        first = self._analysis()
        again = self.service.analyze(self.t["eng"], "LOT-1")
        self.assertFalse(again["created"])
        self.assertEqual(again["analysis_id"], first["analysis_id"])

    def test_measurement_and_analysis_flows_remain_intact(self) -> None:
        result = self._analysis()
        self.assertEqual(result["spectrum"]["peak_wavelength_nm"], 520.0)
        self.assertEqual(len(self.service.list_analyses(self.t["eng"], "LOT-1")), 1)
        audit = self.service.audit(self.t["admin"], "LOT-1")
        self.assertIn("created", [e["event_type"] for e in audit])


class PersistenceTests(unittest.TestCase):
    def test_chain_survives_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "photon.sqlite3")
            service = PhotonService(path)
            tokens = _seed(service)
            first = service.analyze(tokens["eng"], "LOT-1")
            service.approve(tokens["qa"], "LOT-1", "hold", "hold first",
                            analysis_id=first["analysis_id"])
            service.add_measurement(tokens["admin"], "LOT-1", 600, .95, .01, "spec-2")
            second = service.analyze(tokens["eng"], "LOT-1")
            review = service.request_review(tokens["qa"], "LOT-1", "new data")
            service.approve(tokens["qb"], "LOT-1", "release", "release after review",
                            analysis_id=second["analysis_id"], review_id=review["review_id"])
            del service
            restarted = PhotonService(path)
            report = restarted.decision_report(
                restarted.auth.login("qb", "qb-pass-1234"), "LOT-1")
            self.assertEqual([c["decision"] for c in report["decision_chain"]],
                             ["hold", "release"])
            self.assertEqual([c["seq"] for c in report["decision_chain"]], [1, 2])
            self.assertEqual(report["status"], "released")
            self.assertEqual(report["current_decision"]["decision"], "release")


LEGACY_SCHEMA = """
CREATE TABLE chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 wafer_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL,
 wavelength_nm REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
"""


class MigrationTests(unittest.TestCase):
    def test_legacy_approvals_become_immutable_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "legacy.sqlite3")
            db = sqlite3.connect(path)
            db.executescript(LEGACY_SCHEMA)
            db.execute(
                "INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)",
                ("LOT-OLD", "sensor", "P0", 4, "hold", "admin", "t0", "t1"),
            )
            # 旧缺陷现场：同一人的覆盖只留下最后状态；不同人各留一行。
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,?)",
                       ("LOT-OLD", "qa", "hold", "first hold overwritten", "t1"))
            db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)",
                       ("LOT-OLD", "qa", "reject", "last status only", "t2"))
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,?)",
                       ("LOT-OLD", "qb", "hold", "other reviewer row", "t3"))
            db.commit()
            db.close()

            service = PhotonService(path)
            service.bootstrap_admin()
            service.auth.create_user("eng", "eng-pass-123", "engineer")
            service.auth.create_user("qa2", "qa2-pass-12", "quality")
            admin = service.auth.login("admin", "photon-admin")
            chain = service.decision_chain(admin, "LOT-OLD")
            self.assertEqual([c["decision"] for c in chain], ["reject", "hold"])
            self.assertEqual([c["seq"] for c in chain], [1, 2])
            report = service.decision_report(admin, "LOT-OLD")
            self.assertEqual(report["current_decision"]["decision"], "hold")

            # 迁移后的批次仍可补充测量、复议并由不同授权人员放行。
            for wavelength, response in ((450, .9), (520, .92), (650, .91)):
                service.add_measurement(admin, "LOT-OLD", wavelength, response, .01, "s")
            new_analysis = service.analyze(service.auth.login("eng", "eng-pass-123"),
                                           "LOT-OLD")
            service.request_review(admin, "LOT-OLD", "post-migration review")
            token = service.auth.login("qa2", "qa2-pass-12")
            release = service.approve(token, "LOT-OLD", "release", "migrated and cleared",
                                      analysis_id=new_analysis["analysis_id"], review_id=1)
            decisions = [c["decision"] for c in service.decision_chain(admin, "LOT-OLD")]
            self.assertEqual(decisions, ["reject", "hold", "release"])
            self.assertEqual(service.get_lot(admin, "LOT-OLD")["status"], "released")
            self.assertEqual(release["seq"], 3)


class HttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        Handler.service = PhotonService(":memory:")
        self.service = Handler.service
        self.tokens = _seed(self.service)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        import threading

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(self, method: str, path: str, token: str | None = None,
                 body: dict | None = None, key: str | None = None) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if key:
            headers["Idempotency-Key"] = key
        conn.request(method, path, json.dumps(body or {}).encode(), headers)
        response = conn.getresponse()
        payload = json.loads(response.read().decode())
        conn.close()
        return response.status, payload

    def test_report_replay_and_conflict_over_http(self) -> None:
        first = self.service.analyze(self.tokens["eng"], "LOT-1")
        status, body = self._request(
            "POST", "/lots/LOT-1/approvals", self.tokens["qa"],
            {"decision": "hold", "reason": "http hold", "analysis_id": first["analysis_id"]},
            key="http-1")
        self.assertEqual(status, 201)
        decision_id = body["decision_id"]
        status, replay = self._request(
            "POST", "/lots/LOT-1/approvals", self.tokens["qa"],
            {"decision": "hold", "reason": "http hold", "analysis_id": first["analysis_id"]},
            key="http-1")
        self.assertEqual(status, 200)
        self.assertEqual(replay["decision_id"], decision_id)
        status, conflict = self._request(
            "POST", "/lots/LOT-1/approvals", self.tokens["qa"],
            {"decision": "hold", "reason": "changed", "analysis_id": first["analysis_id"]},
            key="http-1")
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "conflict")

        status, report = self._request("GET", "/lots/LOT-1/approvals", self.tokens["qb"])
        self.assertEqual(status, 200)
        self.assertEqual(report["current_decision"]["decision_id"], decision_id)
        self.assertEqual(len(report["decision_chain"]), 1)

        status, body = self._request(
            "POST", "/lots/LOT-1/reviews", self.tokens["qa"], {"reason": "new cycle"})
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "open")


if __name__ == "__main__":
    unittest.main()
