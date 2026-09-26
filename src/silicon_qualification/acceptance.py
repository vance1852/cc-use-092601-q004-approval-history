"""容器和快照检查使用的冒烟验收命令。

覆盖完整审批生命周期：首次暂缓决定、补充测量后的新分析版本、显式复议、
由另一名授权质量人员引用新分析版本放行，并在“进程重启”后核对决定链。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .errors import Forbidden
from .service import PhotonService


def run(workspace: str | None = None) -> dict:
    database_dir = tempfile.TemporaryDirectory()
    database_path = str(Path(database_dir.name) / "photon.sqlite3")
    service = PhotonService(database_path)
    service.bootstrap_admin()
    service.auth.create_user("engineer-1", "engineer-pass", "engineer")
    service.auth.create_user("quality-a", "quality-pass-1", "quality")
    service.auth.create_user("quality-b", "quality-pass-2", "quality")

    admin = service.auth.login("admin", "photon-admin")
    engineer = service.auth.login("engineer-1", "engineer-pass")
    quality_a = service.auth.login("quality-a", "quality-pass-1")
    quality_b = service.auth.login("quality-b", "quality-pass-2")

    service.create_lot(admin, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(admin, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    first_analysis = service.analyze(engineer, "LOT-DEMO")

    # 第一名质量人员依据第一版分析作出不可变的暂缓决定。
    hold = service.approve(
        quality_a, "LOT-DEMO", "hold", "awaiting supplemental measurements",
        analysis_id=first_analysis["analysis_id"], idempotency_key="hold-1",
    )
    # 重复请求必须返回原结果，而不是再追加一条决定。
    hold_replay = service.approve(
        quality_a, "LOT-DEMO", "hold", "awaiting supplemental measurements",
        analysis_id=first_analysis["analysis_id"], idempotency_key="hold-1",
    )
    assert hold_replay["decision_id"] == hold["decision_id"] and hold_replay["replayed"] is True

    # 补充测量并产生新的分析版本。
    for wavelength, response in ((470, .91), (600, .95)):
        service.add_measurement(admin, "LOT-DEMO", wavelength, response, .01, "spectrometer-2")
    second_analysis = service.analyze(engineer, "LOT-DEMO")
    assert second_analysis["analysis_id"] != first_analysis["analysis_id"]

    # 必须显式发起复议；同一人不能对自己的暂缓决定作出复议决定。
    review = service.request_review(quality_a, "LOT-DEMO", "supplemental spectrum captured")
    try:
        service.approve(
            quality_a, "LOT-DEMO", "release", "same reviewer must be rejected",
            analysis_id=second_analysis["analysis_id"], review_id=review["review_id"],
        )
    except Forbidden:
        pass
    else:  # pragma: no cover - 验收断言
        raise AssertionError("同一人不应能作出复议决定")

    # 另一名授权质量人员引用新分析版本完成放行。
    release = service.approve(
        quality_b, "LOT-DEMO", "release", "supplemental analysis clears the hold",
        analysis_id=second_analysis["analysis_id"], review_id=review["review_id"],
        idempotency_key="release-1",
    )

    before_restart = service.decision_report(quality_b, "LOT-DEMO")
    chain = before_restart["decision_chain"]
    assert [item["decision"] for item in chain] == ["hold", "release"]
    assert before_restart["current_decision"]["decision"] == "release"
    assert chain[0]["analysis_id"] == first_analysis["analysis_id"]
    assert chain[1]["analysis_id"] == second_analysis["analysis_id"]
    assert chain[1]["review_id"] == review["review_id"]

    # 模拟进程重启：重新打开同一个 SQLite 文件，决定链先后关系仍可追溯。
    del service
    restarted = PhotonService(database_path)
    report = restarted.decision_report(
        restarted.auth.login("quality-b", "quality-pass-2"), "LOT-DEMO"
    )
    assert [item["decision"] for item in report["decision_chain"]] == ["hold", "release"]
    assert report["status"] == "released"
    event_count = len(restarted.audit(
        restarted.auth.login("admin", "photon-admin"), "LOT-DEMO"))
    del restarted
    database_dir.cleanup()
    return {
        "status": "ok",
        "lot": first_analysis["lot_id"],
        "peak": first_analysis["spectrum"]["peak_wavelength_nm"],
        "analysis_versions": 2,
        "decisions": [item["decision"] for item in chain],
        "current_decision": report["current_decision"]["decision"],
        "events": event_count,
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
