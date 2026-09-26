"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .service import PhotonService


def run(database: str = ":memory:") -> dict:
    service = PhotonService(database)
    service.bootstrap_admin()
    admin = service.auth.login("admin", "photon-admin")
    service.auth.create_user("quality-a", "quality-pass-1", "quality")
    service.auth.create_user("quality-b", "quality-pass-2", "quality")
    qa = service.auth.login("quality-a", "quality-pass-1")
    qb = service.auth.login("quality-b", "quality-pass-2")

    service.create_lot(admin, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(admin, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    first_analysis = service.analyze(admin, "LOT-DEMO")
    service.submit_for_review(admin, "LOT-DEMO")
    hold = service.decide(
        qa, "LOT-DEMO", "hold", "awaiting supplemental measurements",
        first_analysis["analysis_id"], "decide-1",
    )

    # 补充测量后生成新的分析版本，显式发起复议，由另一名质量人员放行。
    service.add_measurement(admin, "LOT-DEMO", 600, .91, .01, "spectrometer-1")
    second_analysis = service.analyze(admin, "LOT-DEMO")
    review = service.request_review(qa, "LOT-DEMO", "supplemental spectrum captured", "review-1")
    release = service.decide(
        qb, "LOT-DEMO", "release", "supplemental analysis meets spec",
        second_analysis["analysis_id"], "decide-2", review_request_id=review["request_id"],
    )
    report = service.report(qb, "LOT-DEMO")
    return {
        "status": "ok",
        "lot": first_analysis["lot_id"],
        "peak": first_analysis["spectrum"]["peak_wavelength_nm"],
        "events": len(service.audit(qb, "LOT-DEMO")),
        "analyses": len(report["analyses"]),
        "decision_chain": [item["decision"] for item in report["decision_chain"]],
        "current_decision": report["current_decision"]["decision"],
        "final_status": release["status"],
        "database": database,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace")
    args = parser.parse_args()
    if args.workspace:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "photon.sqlite3")
            result = run(path)
            # 重新打开数据库，验证决定链在进程重启后仍可追溯。
            reopened = PhotonService(path)
            admin = reopened.auth.login("admin", "photon-admin")
            report = reopened.report(admin, "LOT-DEMO")
            result["restart_chain"] = [item["decision"] for item in report["decision_chain"]]
            result["restart_status"] = report["lot"]["status"]
    else:
        result = run()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
