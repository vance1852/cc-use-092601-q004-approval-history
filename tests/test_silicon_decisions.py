from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from silicon_qualification.errors import Conflict, InvalidState
from silicon_qualification.service import PhotonService


class DecisionWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.directory.name) / "work.sqlite3")
        self.service = PhotonService(self.db_path)
        self.service.bootstrap_admin()
        self.admin = self.service.auth.login("admin", "photon-admin")
        self.service.auth.create_user("qa-a", "quality-pass-1", "quality")
        self.service.auth.create_user("qa-b", "quality-pass-2", "quality")
        self.service.auth.create_user("op", "operator-pass", "operator")
        self.qa_a = self.service.auth.login("qa-a", "quality-pass-1")
        self.qa_b = self.service.auth.login("qa-b", "quality-pass-2")
        self.op = self.service.auth.login("op", "operator-pass")
        self.service.create_lot(self.admin, "LOT-1", "accelerator", "R1", 10)
        for wavelength, response in ((450, 0.71), (520, 0.93), (650, 0.84)):
            self.service.add_measurement(
                self.admin, "LOT-1", wavelength, response, 0.01, "spectrometer-1"
            )
        self.analysis1 = self.service.analyze(self.admin, "LOT-1")
        self.service.submit_for_review(self.admin, "LOT-1")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _hold_then_supplement(self) -> tuple[dict, dict]:
        hold = self.service.decide(
            self.qa_a, "LOT-1", "hold", "first hold",
            self.analysis1["analysis_id"], "key-hold",
        )
        self.service.add_measurement(self.admin, "LOT-1", 600, 0.91, 0.01, "spectrometer-1")
        analysis2 = self.service.analyze(self.admin, "LOT-1")
        review = self.service.request_review(self.qa_a, "LOT-1", "new data", "key-rev")
        return hold, {"analysis2": analysis2, "review": review}

    def test_decisions_are_immutable_and_chained(self) -> None:
        hold, ctx = self._hold_then_supplement()
        release = self.service.decide(
            self.qa_b, "LOT-1", "release", "now passes",
            ctx["analysis2"]["analysis_id"], "key-release",
            review_request_id=ctx["review"]["request_id"],
        )
        chain = self.service.decision_chain(self.qa_b, "LOT-1")
        self.assertEqual([d["decision"] for d in chain], ["hold", "release"])
        self.assertEqual([d["kind"] for d in chain], ["initial", "review"])
        self.assertEqual(chain[1]["prev_decision_id"], chain[0]["decision_id"])
        self.assertEqual(chain[0]["analysis_id"], self.analysis1["analysis_id"])
        self.assertEqual(chain[1]["analysis_id"], ctx["analysis2"]["analysis_id"])
        current = self.service.current_decision(self.qa_b, "LOT-1")
        self.assertEqual(current["decision_id"], release["decision_id"])
        # 原暂缓决定仍可完整读取，未被覆盖。
        self.assertEqual(chain[0]["reason"], "first hold")
        self.assertEqual(chain[0]["reviewer"], "qa-a")

    def test_report_exposes_current_and_full_chain(self) -> None:
        self.service.decide(
            self.qa_a, "LOT-1", "hold", "hold",
            self.analysis1["analysis_id"], "key-hold",
        )
        report = self.service.report(self.qa_b, "LOT-1")
        self.assertEqual(report["current_decision"]["decision"], "hold")
        self.assertEqual(len(report["decision_chain"]), 1)
        self.assertEqual(len(report["analyses"]), 1)
        self.assertEqual(report["lot"]["status"], "hold")

    def test_replay_returns_original_result(self) -> None:
        first = self.service.decide(
            self.qa_a, "LOT-1", "hold", "hold",
            self.analysis1["analysis_id"], "same-key",
        )
        second = self.service.decide(
            self.qa_a, "LOT-1", "hold", "hold",
            self.analysis1["analysis_id"], "same-key",
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.service.decision_chain(self.qa_a, "LOT-1")), 1)

    def test_same_key_with_different_payload_conflicts(self) -> None:
        self.service.decide(
            self.qa_a, "LOT-1", "hold", "hold",
            self.analysis1["analysis_id"], "dup-key",
        )
        with self.assertRaises(Conflict):
            self.service.decide(
                self.qa_b, "LOT-1", "reject", "changed payload",
                self.analysis1["analysis_id"], "dup-key",
            )

    def test_new_decision_requires_explicit_review_request(self) -> None:
        _, ctx = self._hold_then_supplement()
        with self.assertRaises(InvalidState):
            self.service.decide(
                self.qa_b, "LOT-1", "release", "no review",
                ctx["analysis2"]["analysis_id"], "key-release",
            )

    def test_reviewer_must_differ_from_previous(self) -> None:
        _, ctx = self._hold_then_supplement()
        with self.assertRaises(PermissionError):
            self.service.decide(
                self.qa_a, "LOT-1", "release", "same reviewer",
                ctx["analysis2"]["analysis_id"], "key-release",
                review_request_id=ctx["review"]["request_id"],
            )

    def test_review_must_cite_newer_analysis(self) -> None:
        _, ctx = self._hold_then_supplement()
        with self.assertRaises(InvalidState):
            self.service.decide(
                self.qa_b, "LOT-1", "release", "stale analysis",
                self.analysis1["analysis_id"], "key-release",
                review_request_id=ctx["review"]["request_id"],
            )

    def test_stale_revision_blocks_action(self) -> None:
        with self.assertRaises(Conflict):
            self.service.decide(
                self.qa_a, "LOT-1", "hold", "stale rev",
                self.analysis1["analysis_id"], "key-stale", expected_revision=42,
            )

    def test_released_lot_cannot_be_reopened(self) -> None:
        self.service.decide(
            self.qa_a, "LOT-1", "release", "good",
            self.analysis1["analysis_id"], "key-release",
        )
        with self.assertRaises(InvalidState):
            self.service.request_review(self.qa_a, "LOT-1", "try again", "key-rev")

    def test_operator_cannot_decide_or_request_review(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.decide(
                self.op, "LOT-1", "hold", "no",
                self.analysis1["analysis_id"], "key-op",
            )
        with self.assertRaises(PermissionError):
            self.service.request_review(self.op, "LOT-1", "no", "key-op-rev")

    def test_analysis_versions_are_content_addressed(self) -> None:
        again = self.service.analyze(self.admin, "LOT-1")
        self.assertTrue(again["reused"])
        self.assertEqual(again["analysis_id"], self.analysis1["analysis_id"])

    def test_chain_survives_process_restart(self) -> None:
        _, ctx = self._hold_then_supplement()
        self.service.decide(
            self.qa_b, "LOT-1", "release", "now passes",
            ctx["analysis2"]["analysis_id"], "key-release",
            review_request_id=ctx["review"]["request_id"],
        )
        reopened = PhotonService(self.db_path)
        token = reopened.auth.login("admin", "photon-admin")
        report = reopened.report(token, "LOT-1")
        self.assertEqual([d["decision"] for d in report["decision_chain"]], ["hold", "release"])
        self.assertEqual(report["current_decision"]["decision"], "release")
        self.assertEqual(report["lot"]["status"], "released")
        events = [e["event_type"] for e in reopened.audit(token, "LOT-1")]
        self.assertLess(events.index("approval.initial"), events.index("review.requested"))
        self.assertLess(events.index("review.requested"), events.index("approval.review"))


if __name__ == "__main__":
    unittest.main()
